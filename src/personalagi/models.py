"""SQLModel tables for the derived index.

Markdown is the source of truth for context; these tables are the ingest
landing zone and the retrieval index. Nothing here is authoritative.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


class Message(SQLModel, table=True):
    """A normalized Gmail message.

    Unique on (account_label, gmail_id), not gmail_id alone: the same message
    can legitimately appear in two of your accounts and each copy has its own
    labels and visibility. This constraint is what makes re-ingest a no-op.
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("account_label", "gmail_id", name="uq_message_account_gmail"),
    )

    id: int | None = Field(default=None, primary_key=True)
    gmail_id: str = Field(index=True)
    thread_id: str = Field(index=True)
    account_label: str = Field(index=True)

    sender_name: str = ""
    sender_email: str = Field(default="", index=True)
    subject: str = ""
    body_text: str = ""

    # Whitelisted headers as a JSON object (see normalize.HEADER_WHITELIST).
    # Nullable because rows ingested before this column existed have none —
    # `refresh-headers` backfills them. A missing value means "not fetched",
    # NOT "no bulk headers", and the two must never be conflated: treating
    # unknown as clean would silently reclassify 4,000 messages as human.
    headers_json: str | None = Field(default=None)

    # timestamp is the human-facing value; internal_date_ms is Gmail's raw
    # epoch-ms, kept because the watermark advances on it exactly.
    timestamp: datetime = Field(index=True)
    internal_date_ms: int = Field(index=True)
    ingested_at: datetime


class Event(SQLModel, table=True):
    """The canonical record every source normalizes to (ARCHITECTURE.md D1).

    `{id, source, timestamp, participants[], text, metadata{}}`.

    Message above is now a Gmail-specific landing zone: raw, source-shaped,
    and read by exactly one adapter. Everything downstream — context building,
    classification, relevance, commitments, the brief — reads Events and knows
    nothing about senders, subjects, or threads.

    Event ids are deliberately assigned equal to the Message id they derive
    from. The three derived tables already carried a message_id foreign key
    over thousands of rows; making the ids identical turns a data migration
    with a remapping table into a column rename, and there is no window in
    which a foreign key points at the wrong row.
    """

    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("source", "account_label", "source_id", name="uq_event_source"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # "gmail" | "imessage" | "calendar". The only place a source name appears
    # below the adapter layer, and only ever for display or filtering.
    source: str = Field(index=True)
    # The source's own identifier: gmail_id, iMessage GUID, calendar event id.
    source_id: str = Field(index=True)
    # Which inbox/account/device this arrived through.
    account_label: str = Field(default="", index=True)
    # Groups events into a conversation within one source.
    thread_key: str = Field(default="", index=True)

    # Subject, calendar event title, or "" for sources that have no title
    # (an iMessage has no subject, and inventing one would be a lie).
    title: str = ""
    text: str = ""

    timestamp: datetime = Field(index=True)
    timestamp_ms: int = Field(index=True)

    # Source-specific structure the common shape cannot hold: mail headers,
    # calendar recurrence, iMessage service. D1's stated mitigation for
    # lowest-common-denominator loss.
    metadata_json: str | None = None

    # EXTERNAL (came from the world) or GENERATED (the system wrote it).
    #
    # This is the structural fix for the self-citation bug. Twice in one night
    # the system grounded a conclusion in its own output: a decommission notice
    # cited a log line that notice had produced, and the owner's own sent mail
    # was cited as proof that something mattered to the owner. Both times
    # self-reference read exactly like independent corroboration.
    #
    # Excluding generated records at retrieval time is a patch that has to be
    # remembered at every call site. Marking provenance at write time is a
    # property of the record, and `citable_events()` is the one place that
    # enforces it. Defaults to EXTERNAL because every row that existed before
    # this column did came from a real mailbox or a real phone.
    provenance: str = Field(default="external", index=True)

    ingested_at: datetime


class Participant(SQLModel, table=True):
    """Someone on an event: sender, recipient, attendee, or chat member.

    A separate table rather than a JSON list on Event because "every event
    involving this address" is the central query of a context system, and that
    has to be an index, not a scan.

    The automated/owner flags are resolved once here at adapter time. That is
    what makes the 215-display-names rule and the robot-address filter apply to
    every source for free instead of being reimplemented per pipeline.
    """

    __tablename__ = "participant"
    __table_args__ = (
        UniqueConstraint("event_id", "address", "role", name="uq_participant_event"),
    )

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)

    # Email address, phone number, or handle — whatever identifies this person
    # within the source. Normalized lowercase.
    address: str = Field(default="", index=True)
    display_name: str = ""
    # "from" | "to" | "cc" | "attendee"
    role: str = Field(default="from", index=True)

    is_owner: bool = Field(default=False, index=True)
    is_automated: bool = Field(default=False, index=True)
    # Why is_automated was set, so a filtered-out human is diagnosable.
    automated_reason: str = ""

    # Resolved person-file slug, or "" when the participant is a shared bulk
    # envelope that must never be attached to a person.
    person_slug: str = Field(default="", index=True)


class Classification(SQLModel, table=True):
    """One classification per message.

    Unique on event_id so re-classifying updates in place rather than
    accumulating rows. `prompt_version` and `model` are stored on every row
    because an eval number is meaningless without knowing which prompt and
    model produced it.
    """

    __tablename__ = "classification"
    __table_args__ = (UniqueConstraint("event_id", name="uq_classification_event"),)

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)

    # needs_response | fyi | promotional | spam | unclassified
    category: str = Field(index=True)
    urgency: str = Field(default="low", index=True)
    summary: str = ""

    model: str = ""
    prompt_version: str = ""
    # False when the model failed twice and the row is a tombstone, so a
    # parse failure is visible in the data instead of silently absent.
    ok: bool = True
    error: str | None = None
    classified_at: datetime


class Relevance(SQLModel, table=True):
    """Stage B output: does this message matter to the owner?

    Deliberately a separate table from Classification rather than more columns
    on it. Classification answers "what kind of message is this", which is a
    property of the message alone. Relevance answers "does this matter to me",
    which is a property of the message PLUS the context store — different
    inputs, different cost, different refresh cadence. A job alert's category
    never changes; its relevance changes the day you meet someone who works
    there.
    """

    __tablename__ = "relevance"
    __table_args__ = (UniqueConstraint("event_id", name="uq_relevance_event"),)

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)

    # 0 bulk-no-connection, 1 unknown-but-plausible, 2 known-person,
    # 3 touches an open commitment or a tracked person. See evals/TAXONOMY.md.
    score: int = Field(default=0, index=True)
    # The model's justification, which must cite the retrieved context. Stored
    # because a relevance score with no reason is unauditable, and D8 says
    # every proposal carries its evidence.
    why: str = ""
    # Which person file was retrieved, so the reason can be traced back.
    person_slug: str = Field(default="", index=True)
    context_tokens: int = 0

    # Stage A's structural verdict, kept so the cheap path is auditable and
    # a bad filter is diagnosable without re-running it.
    sender_kind: str = Field(default="", index=True)  # human | automated
    sender_reason: str = ""

    model: str = ""
    prompt_version: str = ""
    ok: bool = True
    error: str | None = None
    scored_at: datetime


class Commitment(SQLModel, table=True):
    """Something someone promised, in either direction.

    The actual product. Triage tells you what arrived; this tells you what you
    said you would do and have not done.

    Unique on (event_id, direction, what_hash) so re-extracting the same
    message does not duplicate a commitment, while still allowing one message
    to create several ("I'll send the deck and introduce you to Sam").
    """

    __tablename__ = "commitment"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "direction", "what_hash", name="uq_commitment_dedup"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)

    # "i_owe" — the owner promised someone something.
    # "they_owe" — someone promised the owner something.
    direction: str = Field(index=True)
    # The counterparty: whoever is not the owner.
    person_slug: str = Field(default="", index=True)
    person_name: str = ""
    person_email: str = Field(default="", index=True)

    what: str = ""
    # Stable hash of `what`, because SQLite cannot index a long free-text
    # column usefully and the dedup constraint needs a fixed-width key.
    what_hash: str = Field(default="", index=True)
    # The sentence that created the obligation, verbatim. Never paraphrased:
    # "you promised X" is only credible if you can show the words.
    quote: str = ""
    due_text: str = ""

    # open | done | stale. `stale` is derived from age, not asserted by the
    # model, so the rule is inspectable and cheap to change.
    status: str = Field(default="open", index=True)
    promised_at: datetime = Field(index=True)

    # Staleness is measured from HERE, not from promised_at.
    #
    # The first version measured from the promise, which meant a commitment you
    # fulfilled a week later still went stale on schedule and nagged forever.
    # What matters is "has anything happened on this since", so any later event
    # in the same thread, with the same person, pushes this forward.
    # Defaults to promised_at on rows written before the column existed.
    last_activity_at: datetime | None = Field(default=None, index=True)

    resolved_at: datetime | None = None
    # Which goal this promise serves, when it serves one. Lets a nudge say
    # what is at stake rather than just what is late.
    goal_id: int | None = Field(default=None, foreign_key="goal.id", index=True)
    # Human override. A commitment marked done by hand must survive the next
    # extraction run, exactly like a person file's `corrections`.
    manually_closed: bool = False

    # How much it costs if this is never done: high | medium | low.
    #
    # Age alone ranked "wait lemme resend the link twin" alongside a promise to
    # a recruiter, which is how a useful list becomes noise. Register is real
    # evidence about consequence and the model reads it well.
    stakes: str = Field(default="medium", index=True)
    # Why the resolver reached its verdict, so a wrong close is diagnosable
    # rather than mysterious.
    resolution_why: str = ""
    resolved_by: str = Field(default="", index=True)  # "" | model | human

    model: str = ""
    prompt_version: str = ""
    extracted_at: datetime


class Goal(SQLModel, table=True):
    """Something the owner is trying to achieve. The missing record type.

    Without goals the system can only answer "did something arrive". With them
    it can answer "is anything I care about not moving", which is the question
    that catches the letter of recommendation nobody ever asked for.

    Markdown at context/goals/<slug>.md is the source of truth (D2); this table
    is the queryable index. Deadline arithmetic and staleness checks are plain
    SQL over these columns — no model call — which is what makes the proactive
    sweep cost pennies instead of hundreds of dollars.
    """

    __tablename__ = "goal"
    __table_args__ = (UniqueConstraint("slug", name="uq_goal_slug"),)

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True)
    title: str = ""
    # Why it matters. Carried into proposals so a nudge can say what it serves.
    why: str = ""

    deadline: datetime | None = Field(default=None, index=True)
    # active | done | abandoned | blocked
    status: str = Field(default="active", index=True)

    # Parent goal's slug, or "" for a top-level goal. Goals nest: Cisco is one
    # of five paths to a full-time role, not a goal in its own right, and
    # activity on a child is activity on the parent.
    parent_slug: str = Field(default="", index=True)

    # Last time ANY linked event, commitment, or step moved. Distinct from
    # updated_at, which changes when the goal text is edited — editing a
    # description is not progress and must not reset a staleness clock.
    last_activity: datetime | None = Field(default=None, index=True)
    created_at: datetime
    updated_at: datetime


class GoalStep(SQLModel, table=True):
    """One required step, and whether anything anywhere supports it.

    `done=False` + no evidence + a near deadline is the single highest-value
    signal in the system. It is also the only one that fires on ABSENCE, which
    is why evidence is a linked set rather than a boolean the owner maintains
    by hand: a step nobody has touched looks identical to one nobody recorded.
    """

    __tablename__ = "goal_step"

    id: int | None = Field(default=None, primary_key=True)
    goal_id: int = Field(foreign_key="goal.id", index=True)
    description: str = ""
    position: int = 0

    done: bool = Field(default=False, index=True)
    # True when nothing else can proceed until this is finished.
    blocking: bool = Field(default=False, index=True)

    # Set when evidence is found or the owner marks it done, so "how long has
    # this been stalled" is answerable.
    last_activity: datetime | None = Field(default=None, index=True)
    created_at: datetime


class StepEvidence(SQLModel, table=True):
    """Link table: which events support which step.

    A separate table rather than a JSON list because the central query is
    "steps with no evidence", and an anti-join needs an index. Also lets one
    event support several steps, which is common — a single email can be
    evidence for both "ask for the letter" and "confirm the deadline".
    """

    __tablename__ = "step_evidence"
    __table_args__ = (
        UniqueConstraint("step_id", "event_id", name="uq_step_evidence"),
    )

    id: int | None = Field(default=None, primary_key=True)
    step_id: int = Field(foreign_key="goal_step.id", index=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    # How the link was made: "search" | "manual" | "llm". Kept because an
    # automatically-guessed link and one the owner confirmed are not equally
    # trustworthy, and a wrong auto-link silently marks a step as handled.
    method: str = Field(default="search", index=True)
    confidence: float = 0.0
    linked_at: datetime


class Fact(SQLModel, table=True):
    """A statement that is not an event, especially a future-dated one.

    "Builder Club applications open mid-August" is not something that happened;
    it is something that will be true. Events cannot express it, and without it
    a date arriving can never itself be a trigger.

    `valid_from` is the trigger: when it passes and the fact is still open,
    that is a proposal with zero model calls to detect.
    """

    __tablename__ = "fact"

    id: int | None = Field(default=None, primary_key=True)
    statement: str = ""
    # When this becomes true / stops being true. Either may be null.
    valid_from: datetime | None = Field(default=None, index=True)
    valid_until: datetime | None = Field(default=None, index=True)

    # The EXTERNAL event this was learned from. A fact with no source event is
    # not citable, by the same rule that governs everything else.
    source_event_id: int | None = Field(default=None, foreign_key="event.id", index=True)
    goal_id: int | None = Field(default=None, foreign_key="goal.id", index=True)
    person_slug: str = Field(default="", index=True)

    confidence: float = 0.0
    # open | fired | expired | dismissed. `fired` means a proposal was already
    # emitted for it, so the same date does not nag every single sweep.
    status: str = Field(default="open", index=True)
    created_at: datetime


class PersonRole(SQLModel, table=True):
    """What role a person plays relative to a specific goal.

    The same person is not the same thing in two contexts. One contact can be
    an `advocate` for a job goal and a `recommender` for an application goal,
    and the right thing to ask them differs completely. Prep that ignores this
    produces a generic dossier instead of "here is what to ask THIS person".
    """

    __tablename__ = "person_role"
    __table_args__ = (
        UniqueConstraint("person_slug", "goal_id", name="uq_person_goal_role"),
    )

    id: int | None = Field(default=None, primary_key=True)
    person_slug: str = Field(index=True)
    goal_id: int = Field(foreign_key="goal.id", index=True)
    # advocate | recommender | decision_maker | gatekeeper | collaborator |
    # sponsor | mentor | contact
    role: str = Field(default="contact", index=True)
    note: str = ""
    created_at: datetime


class Edge(SQLModel, table=True):
    """A typed, weighted link between two records. The graph.

    Spreading activation needs edges. Without them Person, Goal, Commitment and
    Event are four islands and a Deepgram job alert can never light up the
    person who works there.

    Weight is the decay multiplier applied when activation crosses this edge.
    Storing it per-edge rather than per-type is what lets a strong link
    (a commitment naming a person) carry further than a weak one (two people
    who appeared in the same thread once).
    """

    __tablename__ = "edge"
    __table_args__ = (
        UniqueConstraint(
            "src_type", "src_id", "dst_type", "dst_id", "relation", name="uq_edge"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    # "person" | "goal" | "commitment" | "event" | "fact"
    src_type: str = Field(index=True)
    src_id: str = Field(index=True)
    dst_type: str = Field(index=True)
    dst_id: str = Field(index=True)

    # mentions | works_at | owes | about | attended | recommends | blocks ...
    relation: str = Field(default="mentions", index=True)
    weight: float = 0.5

    # Which EXTERNAL event justified drawing this edge. An edge with no
    # source cannot be cited, and an uncitable edge should not move a score.
    source_event_id: int | None = Field(default=None, foreign_key="event.id", index=True)
    created_at: datetime


class ProposalRecord(SQLModel, table=True):
    """A logged proposal and what the owner did about it.

    Named ProposalRecord because `Proposal` is already the in-memory dataclass
    the dispatcher consumes; this is its durable ledger row. Keeping them
    separate stops the dispatcher's untrusted-input contract from acquiring a
    database dependency.

    Two axes, not one:
      permission_tier  - how reversible is it        (auto/approve/never)
      attention_level  - what does telling me cost   (silent/ambient/nudge/interrupt)

    They are independent. A letter-of-rec nudge is `approve` + `interrupt`; a
    LinkedIn request is `approve` + `ambient`. Collapsing them into one number
    is what makes assistants that people switch off.
    """

    __tablename__ = "proposal"

    id: int | None = Field(default=None, primary_key=True)
    proposal_id: str = Field(index=True)

    # What woke the system up: "event:123" | "sweep:deadline" | "fact:9".
    trigger: str = Field(default="", index=True)
    action_name: str = Field(default="", index=True)
    args_json: str = "{}"

    rationale: str = ""
    # Comma-separated EXTERNAL event ids. Every claim must trace to one.
    evidence_event_ids: str = ""
    confidence: float = 0.0

    permission_tier: str = Field(default="approve", index=True)
    attention_level: str = Field(default="ambient", index=True)

    # Suppressed proposals are STORED, not discarded. Once the system starts
    # staying quiet its blind spots become invisible, so `--show-suppressed`
    # needs something to show.
    suppressed: bool = Field(default=False, index=True)

    # pending | accepted | edited | dismissed | ignored | reversed
    outcome: str = Field(default="pending", index=True)
    outcome_at: datetime | None = None
    # For `edited`: what the owner actually sent. The diff is the lesson.
    outcome_note: str = ""

    goal_id: int | None = Field(default=None, foreign_key="goal.id", index=True)
    created_at: datetime = Field(index=True)


class Judgement(SQLModel, table=True):
    """A cached semantic verdict.

    A (task, event) pair has a stable answer — neither the message nor the step
    text changes on its own — so re-paying for the judgement on every sweep
    would make the nightly run cost real money for an answer already known.

    Keyed on a hash of the normalised question, so editing the step text
    correctly invalidates the cache instead of silently reusing a verdict about
    a different question.
    """

    __tablename__ = "judgement"
    __table_args__ = (
        UniqueConstraint("question_hash", name="uq_judgement_question"),
    )

    id: int | None = Field(default=None, primary_key=True)
    question_hash: str = Field(index=True)
    # evidence | contradiction | relevance
    kind: str = Field(default="evidence", index=True)
    # The task or claim being judged, kept for debugging a bad verdict.
    subject: str = ""
    event_id: int | None = Field(default=None, foreign_key="event.id", index=True)

    verdict: bool = Field(default=False, index=True)
    why: str = ""

    model: str = ""
    prompt_version: str = ""
    decided_at: datetime


class ExtractedFact(SQLModel, table=True):
    """Link from a Fact back to the events that produced it.

    Separate from Fact.source_event_id because one statement about the future
    can be corroborated by several messages, and a fact backed by three
    independent mentions is worth more than one backed by a single aside.
    """

    __tablename__ = "extracted_fact"
    __table_args__ = (
        UniqueConstraint("fact_id", "event_id", name="uq_extracted_fact"),
    )

    id: int | None = Field(default=None, primary_key=True)
    fact_id: int = Field(foreign_key="fact.id", index=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    quote: str = ""
    linked_at: datetime


class IngestState(SQLModel, table=True):
    """Per-account sync cursors.

    Two cursors, not one. Gmail's messages.list returns newest-first, so a
    capped bootstrap keeps the NEWEST n and leaves a hole in the past. A
    single forward watermark cannot represent that: it correctly says "caught
    up with new mail" while older mail sits permanently unreachable.

    - forward cursor  (last_history_id / last_internal_date_ms) -> new mail
    - backfill cursor (oldest_internal_date_ms)                 -> old mail

    Both advance only after the message batch commits, so a crash re-fetches
    (cheap, deduped) rather than skips (silent, permanent).
    """

    __tablename__ = "ingest_state"

    account_label: str = Field(primary_key=True)

    # --- forward cursor: everything NEWER than this is fetched ---
    # Primary watermark. Gmail expires history after ~1 week; when that
    # happens this is cleared and the date watermark takes over.
    last_history_id: str | None = None
    # Fallback watermark. Never expires.
    last_internal_date_ms: int | None = None

    # --- backfill cursor: everything NEWER than this is fetched, back to
    # here. Walking further back is `personalagi backfill`. ---
    oldest_internal_date_ms: int | None = None
    # True only once a backfill pass returns nothing older. Until then there
    # is, or may be, unreachable history.
    backfill_complete: bool = False
    # Set when a message cap truncated a listing. Purely informational, but
    # it is the flag that would have made the original bug visible.
    last_run_truncated: bool = False

    last_synced_at: datetime | None = None
    message_count: int = 0
