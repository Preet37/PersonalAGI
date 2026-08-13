# PersonalAGI — Full build specification

Everything discussed, in one place. Hand this to Claude Code alongside `docs/ARCHITECTURE.md`.

Save to `docs/SPEC.md`.

---

## Part 0 — Where we actually are

**Built and running against real data:**
Gmail ingest (1 of 3 accounts, ~4,000 messages, incremental + backfill cursors) · iMessage ingest · Event abstraction · identity resolution incl. shared-sender detection · two-stage relevance (header filter kills 81% free) · person context store with profile/log split · FTS5 retrieval · compaction with human corrections · commitment tracking (`owed`) · morning brief · action registry with permission tiers · calendar adapter written but unauthorized · draft-only email handler.

**Measured:** classification 60% accuracy, macro F1 0.38, on 30 labels. `needs_response` metrics are meaningless at n=1.

**Not built:** goals, scheduler, gap detection, meeting prep, investigation loop, feedback loop, attention levels, chat-export ingestion, transcripts, desktop app, screen context.

---

## Part 1 — The core model

Six record types. Everything else is derived.

### Event
The atom. Every source normalizes to this.
```
id · source · timestamp · participants[] · text · metadata{} · provenance
```
`provenance` is mandatory: `external` (came from the world) or `generated` (the system wrote it). **Only `external` events may be cited as evidence.** This is the fix for the self-citation bug.

### Person
```
id · display_names[] · identifiers[] (emails, phones, handles) · profile (≤150w)
· log[] · corrections[] · roles{goal_id: role} · confidence · merge_history[]
```
`roles` is new and important: the same person plays different roles relative to different goals. Pratik is `advocate` for the NVIDIA goal and `recommender` for the CMU goal. Prep differs accordingly.

### Goal
```
id · title · why · deadline? · people[] · steps[] · status · last_activity
```
Each step: `{description, done, evidence_event_ids[], blocking}`.
A step with `done=false`, `evidence=[]`, and a near deadline is the highest-value signal in the system.

### Commitment
```
id · direction (i_owe | owed_to_me) · person_id · what · quote · source_event_id
· promised_at · last_activity_at · status · goal_id?
```
Staleness is measured from `last_activity_at`, not `promised_at`. Current code gets this wrong and over-reports.

### Proposal
The unit of work. Never an action directly.
```
id · trigger · action_name · args{} · rationale · evidence_event_ids[]
· confidence · permission_tier · attention_level · outcome · created_at
```

### Fact
For things that aren't events, especially future-dated ones.
```
id · statement · valid_from? · valid_until? · source_event_id · confidence
```
"Builder Club apps open mid-August" is a Fact. When `valid_from` arrives, that's a trigger.

---

## Part 2 — The two triggers

Everything proactive reduces to these. Nothing scans.

### Trigger A — Something arrived

An Event lands. The pipeline:

1. **Cheap filter.** Headers, address patterns, display-name variance. No LLM. Kills ~80%.
2. **Activation.** Extract entities. Walk the graph outward from each. Collect what lights up: people, open commitments, goals, related threads.
3. **Reason.** One model call with the event plus the activated context. Output: does this matter, why, what should happen.
4. **Propose.** Emit zero or more Proposals, each with evidence and both tiers set.

Step 2 is the Deepgram case. The email is worthless; what it activates is the product.

**Activation rules:** energy starts at 1.0, multiplies by the edge weight at each hop, stops below 0.15 or at 3 hops. Without decay everything connects to everything and the output is noise.

### Trigger B — Time passed, nothing arrived

This is the Pratik case: the absence is the signal.

Run these as **plain database queries, no model calls**:
- Goal step: not done, no evidence, deadline within 30 days
- Commitment: `i_owe`, no activity in 7 days
- Goal: no activity in 14 days
- Fact: `valid_from` is now
- Person marked important: no contact in 90 days
- Calendar: meeting within 24 hours (→ meeting prep)
- Contradiction: two events with conflicting statements about the same goal

Only what fires gets a model call to write the proposal. **Five calls a day, not five thousand.**

Hard budget per sweep, enforced in code, configurable.

---

## Part 3 — Features by layer

### Layer 1 — Capture
| Source | State | Notes |
|---|---|---|
| Gmail | built | authorize school + team |
| iMessage | built | `chat.db`, copy before reading, never open live |
| Calendar | written | needs OAuth |
| WhatsApp | todo | export-based |
| LinkedIn | todo | export-based |
| Chat exports (Claude/ChatGPT/Gemini) | todo | **no live API — periodic manual export only** |
| Meeting transcripts | todo | consent-gated, opt-in per meeting |
| Screen context | todo | last, highest risk |
| Files (Downloads, Documents) | todo | read-only |

Each source is an adapter producing Events. No module below the adapter layer may reference source-specific concepts.

### Layer 2 — Identity
- Never merge on one weak signal
- Require independent corroboration (phone in a signature, shared thread, calendar invite)
- Address used by >3 distinct display names → shared bulk sender, never attached to a person
- Every merge is a logged, reversible event
- Merges below confidence threshold surface for confirmation, never auto-applied
- `personalagi identity unmerge <id>` must exist

### Layer 3 — Memory
Person, Goal, Commitment, Fact files. Markdown is truth, SQLite is a rebuildable index.
Two-layer person files. Compaction folds log into profile nightly, never deletes log lines, `corrections[]` are authoritative and never rewritten.

### Layer 4 — Retrieval
- `get_context(person, query)` → frontmatter + profile always, top-k log by FTS
- `activate(entities, depth)` → graph traversal with decay
- `investigate(question)` → **the agentic loop**: search, read, decide if enough, repeat. Bounded by max iterations and a call budget. Same pattern as a coding agent exploring a repo.
- Time decay on relevance: recent beats old

### Layer 5 — Judgment
- Relevance with cited evidence, never uncited
- Commitment extraction, both directions
- Gap detection (goal step ↔ evidence)
- Contradiction detection
- Meeting prep assembly
- **Every claim cites an `external` event or the system says "I don't know"**

### Layer 6 — Action

Two independent axes.

**Permission (reversibility)** — static, registry lookup, `escalate_if` may only raise:
| Tier | Examples |
|---|---|
| auto | read, write context, draft, generate brief |
| approve | send email, calendar write, post, shell command |
| never | delete, spend money, change credentials |

**Attention (interruption cost)** — assigned per proposal type:
| Level | Meaning |
|---|---|
| silent | does it, logs it |
| ambient | appears in the brief |
| nudge | notification at a reasonable hour |
| interrupt | breaks into the day — deadline <48h only |

Getting attention wrong is what makes people turn the system off.

### Layer 7 — Feedback
1. Every Proposal logged with evidence
2. Outcome recorded: `accepted` / `edited` / `dismissed` / `ignored` / `reversed`
3. On new proposals, retrieve the 5 most similar past proposals **with outcomes** and include as examples
4. `--show-suppressed` reveals what fell below threshold, so blind spots stay visible
5. Communication style is learned per-context: "explain simpler" is an explicit correction and stores as one

No training. Outcome-conditioned prompting only.

---

## Part 4 — Parallel build

You already hit a collision last night when `git add -A` raced a subagent. Solve it properly.

### Sequence

**Phase 0 — single session, do not parallelize.**
Define the six record types, the storage interfaces, and the action registry contract. Write the type stubs and their tests. Commit. Everything else builds against these.

**Phase 1 — four parallel tracks, separate git worktrees.**

```bash
cd ~/PersonalAGI
git worktree add ../pa-goals    -b track/goals
git worktree add ../pa-triggers -b track/triggers
git worktree add ../pa-sources  -b track/sources
git worktree add ../pa-loop     -b track/loop
```

One Claude Code session per worktree. Each owns disjoint files.

| Track | Owns | Builds |
|---|---|---|
| **goals** | `goals/`, `commitments/` | Goal records, step/evidence linking, commitment staleness fix, `personalagi goals` |
| **triggers** | `sweep/`, `schedule/` | Trigger B queries, budget enforcement, attention levels, `personalagi sweep`, `--show-suppressed` |
| **sources** | `ingest/` | Calendar adapter wiring, chat-export ingestion, WhatsApp/LinkedIn export, file reader |
| **loop** | `judgment/`, `activate/` | Graph edges + activation with decay, `investigate()`, meeting prep, feedback ledger |

**Rules for every track:**
- Never edit a file outside your track without saying so in your report
- Never touch `models.py` or `registry.py` after Phase 0 — propose changes instead
- Never run `git add -A`. Stage explicit paths only
- Rebase on `main` before opening a PR, never rewrite shared history
- Write your own tests; do not modify another track's tests

**Phase 2 — merge, single session.** Rebase each track in order goals → sources → triggers → loop. Run the full suite after each. Fix conflicts by hand.

### Coordination file
`COORDINATION.md` at repo root. Each session appends: track name, files touched, interfaces needed from another track, blockers. Read it before starting work.

---

## Part 5 — Build order within tracks

Numbered so you can point a session at a range.

**15. Goal records** — schema, CRUD, `personalagi goal add`, link to people and commitments
**16. Step/evidence linking** — for each step, search all sources for supporting events
**17. Commitment staleness fix** — measure from `last_activity_at`
**18. Graph edges** — typed links between Person, Goal, Commitment, Event
**19. Activation** — traversal with decay, threshold, max depth
**20. Trigger A rewrite** — activation feeds the relevance call
**21. Trigger B sweep** — the seven queries, model calls only on hits
**22. Attention levels** — second axis, assigned per proposal type
**23. Call budget** — hard ceiling, enforced in code
**24. Fact records** — future-dated facts as triggers
**25. Meeting prep** — `personalagi prep <person|meeting>`, every claim cited
**26. Feedback ledger** — proposal log + outcome recording
**27. Outcome-conditioned prompting** — 5 similar proposals with outcomes as examples
**28. Investigation loop** — bounded agentic retrieval
**29. Calendar authorized** — you run OAuth, then wire the 24h prep trigger
**30. Chat export ingestion** — Claude/ChatGPT/Gemini exports as Events
**31. Contradiction detection** — conflicting statements about one goal
**32. Real send handler** — approve tier, with a confirmation surface
**33. Desktop app shell** — Electron or Tauri, tray icon, open = active
**34. Meeting transcripts** — consent record mandatory, opt-in per meeting
**35. Screen context** — last

**Stop at 32 for Monday.** 33-35 are post-Monday.

---

## Part 6 — Standing rules for every session

```
- Read docs/ARCHITECTURE.md and docs/SPEC.md before starting. They are binding.
- Read model names from .env. Never hardcode a model string.
- Commit per numbered stage. Stage explicit paths, never `git add -A`.
- At a genuine fork: pick the more conservative and more reversible option,
  record it in NIGHT_REPORT.md, keep going. Do not stop to ask.
- Never run `personalagi auth` — it needs a browser.
- Nothing sends email, writes calendar, or executes shell commands.
- Every stored record carries provenance. Only `external` provenance may be
  cited as evidence. Generated content is never corroboration.
- Every claim in user-facing output cites a source event id.
- Write your track's section of NIGHT_REPORT.md: completed, tested against
  real data vs fakes, forks taken, unverified, three most likely wrong.
```

---

## Part 7 — Deferred, deliberately

Named so they don't get forgotten:

- **Security hardening.** Prompt injection through ingested content is the top risk once real handlers exist. The tier system is the structural defense; it has not been penetration-tested.
- **Recording consent.** Transcripts need per-meeting opt-in and a stored consent record. Jurisdiction rules vary; get advice before shipping capture.
- **Multi-user.** `gmail.readonly` is a restricted scope; a multi-user product needs a Google security assessment.
- **Local inference.** Everything currently goes to Groq, including third parties' mail. Classification is the highest-volume, lowest-difficulty path to move local.
- **Chunked commits on backfill.** Large runs hold everything in memory.

---

## The one-line pitch

Every assistant processes what arrives. This one notices what didn't.
