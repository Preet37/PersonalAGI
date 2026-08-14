# Night report — night two, 2026-08-13

Night one's report is in git history (`git show 9e9f20b:NIGHT_REPORT.md`).
This replaces it.

Read this, then run the four commands under "Try these first". Budget 30
minutes before you touch anything.

---

## Where it is now

| | night one | now |
|---|---|---|
| Sources | Gmail | Gmail + iMessage (+ calendar, built not authorized) |
| Events | — | **4,346** (3,998 mail, 348 iMessage) |
| Participants indexed | — | **8,773** |
| Person files | 190 | **237** |
| Relevance scored | — | **809** human-sender events, 0 failures |
| Commitments tracked | — | **26** (6 you owe, 20 owed to you) |
| Tests | 224 | **407** |
| Commits | 6 | 6 + 7 |

```bash
source .venv/bin/activate
python -m personalagi owed              # <- the new thing. Start here.
python -m personalagi owed --to-me
python -m personalagi brief --days 2
python -m personalagi relevance --dry-run   # shows the 81% free filter
```

---

## The one number that matters, and it is not accuracy

Stage A now filters **3,537 of 4,346 events structurally, with zero LLM calls.**

```
List-Unsubscribe / List-Id   2827      <- headers we were not storing
robot address pattern         705      <- the old regex
auto-submitted                  5
------------------------------------
                             3537      81% removed for free
```

The header signal is **four times** the address regex. `uber@uber.com`,
`googlecloud@google.com`, and `britishairways@crm.ba.com` all read as *human*
to a local-part heuristic — the brand name IS the local part — and all three
carry `List-Unsubscribe`. The headers were already being fetched at ingest
(`format=full` returns them) and thrown away.

That 81% is what makes the expensive stage affordable: 809 messages get the
120B model instead of 4,346.

---

## What actually got built

**Stage 7A — the eval was broken before the prompt was.** Your 30 labels were
sampled at random from an inbox that is 95% machines, so `needs_response` had
exactly **one** example. Its precision (0.25) and recall (1.00) were computed on
that single row. `evals/labels_v2_template.csv` is 60 rows sampled from human
senders only (318 distinct), balanced across classes. **Unlabelled — that is
still the one thing only you can do.**

**Stage 7B — the classifier never opened the context store.** Verified by grep
before touching anything: 0 references across 305 lines. It read each message
cold and guessed. Now there are two stages, and the second one retrieves the
person's file before deciding.

**Stage 7C — `owed`.** The thing you described in your own words, built. Both
directions, grouped by person, oldest first, each with the sentence that
created it.

**Stage 8 — Event is now the canonical record.** Gmail is an adapter. Nothing
below the adapter layer can see a sender, a subject, or a thread — and that is
enforced by a test that walks the AST of every module and fails on
`gmail_id`, `body_text`, `internal_date_ms`, `headers_json`, `thread_id`.

**Stage 9 — iMessage.** 348 events, and **zero downstream changes** to support
them. That is the whole claim of D1 and it held.

**Stage 10 — calendar.** Adapter and 26 tests, no auth flow run.

**Stage 11 — `draft_email` is real.** Creates an actual Gmail draft through an
API that structurally cannot send. Nothing else was made live.

---

## D1 is no longer a claim. Here is the evidence.

Three people now hold an email address **and** a phone number in one file.
Real identifiers are redacted here because this repo is on GitHub and these
are other people's contact details, not mine:

```yaml
# context/people/<a-friend>.md   (real values redacted for the public repo)
emails: [<personal>@gmail.com, <same-person>@ucdavis.edu]
phones: ['+1<redacted>']
```

A second friend's file has 42 entries across both sources. And `owed` now lists a
commitment that arrived **by phone number**, filed against the person file that
email built:

```
<a friend> <+1<redacted>>
  - [18] resend the link   0d ago
      "wait lemme resend the link twin"
```

The path is `+1XXX... → Contacts → "<their name>" → slug <their-slug> →
the same markdown file`. Without Contacts a phone number can only ever be its
own orphan, and the cross-source premise fails silently. 1,779 contact
identifiers loaded, 312 of 348 handles resolved to a name.

**Say this Monday.** It is the difference between "I have an abstraction" and
"I have an abstraction that survived contact with a second source."

---

## Four bugs, all found by running against real data

### 1. Self-citation — a message was evidence for itself

A Groq decommission notice scored maximum relevance, justified with:

> "matches recent log entry: Groq warns Llama 3.1 8B Instant decommission"

That log line was generated *by that very message*. Context flows one way
(messages → log lines), so retrieving context FOR a message returns the message
back as a prior. **Self-citation is indistinguishable from corroboration.**

### 2. The same bug again, in the mirror

After fixing (1), several of the owner's own outgoing messages scored r3, justified:

> "SENDER block shows sender is Preet Karia, the owner himself"

On sent mail the sender IS you, so "retrieve the sender's context" retrieved
context about you. The person whose history explains an outgoing message is the
person it was sent **to**. 4+ such rows before, 0 after.

**This is the generalisable one, and it is the better version of last night's
gitignore story:** anything that retrieves context about the subject of the
retrieval will find itself, and self-reference reads exactly like
independent confirmation. It has now bitten twice in one system.

### 3. "Preet Karia owes Preet Karia"

The first real `owed` run attributed all three findings to *you*. The extractor
took `sender_email` unconditionally, and on sent mail the sender is the owner.
The counterparty is the other end of the conversation — the recipient on sent
mail. Now resolved from To/Cc, skipping your own addresses.

### 4. Relevance cannot be purely relational

With self-citation fixed, the single most important message in your inbox —
Grace's SiBRP alumni form, the one row you marked highest — still scored **1**.
Correctly, by the rules as written: she had no prior history.

Its importance comes from **your** background, which the system had nowhere to
store. So `context/owner.md` now exists and is injected into every stage B call.
Grace moved **1 → 3** ("OWNER block mentions SiBRP alumni") and the routine
vendor notices correctly fell 3 → 2.

That file is the reason the top of your relevance list is now your HackDev
sponsor threads.

---

## The thing you need to know before Monday

**Your three prospectus commitments are not in your email. At all.**

Verified across the whole corpus:

| term | in Gmail | in iMessage |
|---|---|---|
| "prospectus" | **0** | **0** |
| "Logitech" | **0** | 1 |
| "ASUS" | 150 (all Luma event mail) | 2 |
| sponsor | 161 | 2 |

The word "prospectus" appears **zero times in 4,346 events**. That is your word
for it, not the word used in the actual conversations. Karan, Daniel, and Sisi
are not in your mailbox — those threads are on LinkedIn, or iMessage further
back than the 21 days I ingested, or in person.

So the test you proposed — "if `owed` surfaces those three, it works" — **cannot
pass on this data**, and that is a coverage limit, not a bug. What it *did*
find, in iMessage, is the sponsor conversation itself:

```
[r3] "Hey Jason they said they would be interested..."   hardware sponsorship
[r3] "We should convince them to sponsor at the..."
[r3] "Hello Jason, this is Preet. Curious. It ASUS..."   sponsorship for the hackathon
```

**Two actions follow.** Run `python -m personalagi imessage --days 365` to widen
the window — the older sponsor threads are almost certainly there. And do not
let the system that finds your open loops become the reason you do not close
them: Karan, Daniel, and Sisi are still waiting, and the hackathon is in
October.

---

## Forks I took (no questions asked, per instructions)

- **Wrote `OWNER_EMAILS` into `.env`** (`preetkaria37@gmail.com`,
  `preetkaria37@icloud.com` — both appear as senders in the corpus, 73 and 1).
  Commitment direction is undecidable without it, so `relevance` refuses to run
  rather than filing every promise on the wrong side.
- **Auto-drafted `context/owner.md`.** Every line is tagged `[corpus]`,
  `[stated]`, or `[?]`. **Review it** — it is read into every relevance call, so
  a wrong fact there does not sit harmlessly, it actively misroutes attention.
- **Ingested 21 days of iMessage, not all 212,513 messages.** A full ingest is a
  large token bill and a much larger privacy surface; that is your call, not
  mine.
- **Taxonomy proposed, not implemented.** `evals/TAXONOMY.md` argues from your
  own labels that `fyi`/`promotional` is not separable and that the class axis
  carries two variables at once. Switching it would invalidate your baseline, so
  it waits for your approval.
- **Event ids assigned equal to Message ids**, turning a data migration into a
  column rename. 0 orphaned rows across all three derived tables.
- **Kept the `[g:...]` log anchor format** despite renaming the field to
  `source_id`. Thousands of anchors are already on disk and rewriting them would
  break every file's idempotency key for a cosmetic gain.
- **Did not delete `context/people/preet-karia.md`.** It is stale — built before
  the owner filter existed — and no longer accumulates entries. Your vault,
  your call.

---

## What I could NOT verify

- **Classification accuracy is still unmeasured.** Same as last night. The v2
  eval set is generated but unlabelled.
- **Relevance scores are entirely unvalidated.** 111 events scored r3 and
  nobody has checked a single one. Spot-check them before you quote a number.
- **`draft_email` has never actually created a draft.** The token is
  `gmail.readonly`; it correctly returns a failure with instructions. Verified
  no socket is opened.
- **Calendar has never run.** No auth flow was started, per instruction.
- **`school` and `team` accounts** are still unauthorized.
- **Groq key in `.env` is still the one from the transcript.** Rotate it.

---

## The three things most likely to be wrong

**1. `context/owner.md` contains facts I inferred from one conversation.**
It is the highest-leverage file in the system now and the least verified. If it
says something wrong about what you care about, relevance will confidently
misrank your inbox in that direction. Read it first.

**2. Commitment staleness is measured from the promise, not the last
follow-up.** A promise you fulfilled in a later message still goes stale after
7 days. It over-reports on purpose — a false "you still owe this" costs a
glance, a false silence costs a relationship — but it means the STALE flags are
noisier than they look. Thread-level follow-up detection is the fix.

**3. The iMessage `attributedBody` extraction is a heuristic on an undocumented
binary format.** 12% of messages (25,140 of 212,513) store their text only
there. If Apple's encoding differs from what I assumed for some messages, those
come back empty and are silently skipped as "no content" — indistinguishable
from an attachment-only message. The count of skipped rows (52 of 400) looked
plausible, but I could not verify it was *only* attachments.

---

## Monday

What runs today, stated precisely:

> Two live sources — Gmail and iMessage — normalizing to one Event type, with
> participant identity resolved once so a phone number and an email address
> land in the same person file. An 81% structural filter that costs nothing,
> then context-aware relevance scoring on the remainder using a per-person
> markdown store. Commitment extraction in both directions, where every
> commitment carries the verbatim sentence that created it and a quote that
> cannot be found in the source is discarded. Permission tiers enforced in
> code, with one real handler behind them.

Three things to lead with:

**Commitment tracking.** Nobody ships this. Superhuman sorts, Granola
transcribes; nothing tracks what you said you would do to whom, across
channels, and tells you what is rotting. `owed` is the demo.

**The eval story, not the eval number.** *"I measured it, then realised my eval
set had one positive example, so the precision figure was meaningless. Fixing
the sampling mattered more than fixing the prompt."* That is someone who
understands evaluation rather than someone who ran one.

**The self-reference bug.** It bit twice in one system, in mirror-image forms,
and both times self-citation was indistinguishable from corroboration. It is
more interesting than the gitignore bug and it generalises further — it is a
real failure mode of every retrieval-augmented system, including the ones
Arjun's team builds.

And keep D9. Every message body still goes to Groq, including from people who
never agreed to that — and now that includes your text messages, which makes it
sharper, not softer. Name it before someone else does.

---

## Tomorrow, in order

1. Read `context/owner.md` and correct it.
2. Label `evals/labels_v2_template.csv` → save as `evals/labels_v2.csv`.
3. `python -m personalagi imessage --days 365` — widen the window and re-run
   `relevance`; the sponsor threads you care about are older than 21 days.
4. Spot-check 10 of the 111 r3 rows. If they are good, that is your Monday
   artifact and it is better than an F1.
5. Rotate the Groq key.
6. `python -m personalagi auth calendar` when you have a browser.

---

# Night three — the SPEC build

Phase 0 plus three of the four tracks from `docs/SPEC.md`. Repo is now on
GitHub (private), three merged PRs, **538 tests** (was 224 two nights ago).

## What is new and runnable

```bash
python -m personalagi goal list        # goals, soonest deadline first
python -m personalagi goal gaps        # steps with NOTHING behind them
python -m personalagi sweep --dry-run  # the proactive check. costs nothing.
python -m personalagi prep <person>    # meeting brief, every claim cited
python -m personalagi activate <person># what does this connect to
```

| | |
|---|---|
| Events | 4,346 |
| Graph edges | **10,594** |
| People reachable | **464** (was 173) |
| Goals / steps | 2 / 5 |
| Commitments | 26 |
| Tests | **538** |

## The sweep works, and it is free

First run on real data, `model_calls=0`:

```
[surface ] 0.72 deadline_gap: a required step, 17 days out, nothing supports it
[surface ] 0.90 stale_commitment: a promise 62d old, nothing since
[surface ] 0.85 stale_commitment: 35d
[surface ] 0.78 stale_commitment: 28d
[suppress] 0.45 stale_goal: no activity since creation
```

**The deadline gap is the letter-of-rec case.** Nothing arrived, nothing could
have triggered it, and it fired anyway — because absence is now a query rather
than an absence of queries. The same person surfaces twice, independently, from
two different checks.

Suppressed findings are stored, not dropped. `--show-suppressed` exists because
once a system goes quiet its blind spots become invisible to the person
relying on it.

## Three bugs the real data caught

**1. `"letter"` matches inside `"newsletter"`.** The first evidence scorer used
substring matching and produced six links. All six false — `"Ask Pratik for a
letter of recommendation"` was "supported" by `"Welcome to Balenciaga"`. This
is the dangerous direction: a false link marks a step **handled** when nothing
happened. Word boundaries, threshold 0.34 → 0.6. Six false links → two.

**2. A finding that always asked for `NUDGE` could never interrupt** — even the
day before a deadline — because `clamp_attention` only ever caps. Deadline
findings now request exactly what their deadline permits.

**3. The shared-envelope rule made 215 people invisible.** `prep` on the Monday
meeting returned "no person matching". The row was
`('invitations@linkedin.com', 'Arjun Sambamoorthy', '', 1, ...)`. Refusing the
ADDRESS is right — attaching it would fuse 215 LinkedIn requesters into one
file. But refusing the *person* too is a different decision, and conflating
them hid everyone who only ever reached you through a bulk envelope. On a
shared envelope the display name **is** the identity. Reachable people 173 → 464.

## The migration that failed silently

Adding `provenance` left every existing row NULL, so `citable_events()` matched
**0 of 4,346**. Nothing raised. Every claim would simply have lost its evidence,
quietly, and the symptom weeks later would have been "it got vaguer".

Caught by printing the count after migrating — not by a test. Same lesson as
the gitignore bug: verify the artefact, not the intent.

## What is NOT built

- **Track feedback (26/27/28).** No proposal-outcome recording, no
  outcome-conditioned prompting, no investigation loop. `ProposalRecord`
  carries the `outcome` field, so the schema is ready and the logic is not.
- **Track sources.** Chat exports, WhatsApp, LinkedIn, file reader.
- Stages 29-35: calendar auth, contradiction detection, real send handler,
  desktop app, transcripts, screen context.
- Trigger A was **not** rewritten to feed activation into the relevance call.
  Activation exists and is tested; relevance does not consume it yet.

## Three things most likely wrong

1. **The evidence linker still has a semantic ceiling.** `"Submit the CMU
   application"` matches a *credit-card* application at 0.67. Lexical match,
   semantic miss. Links carry `method` and `confidence`, so check them before
   trusting a closed gap.
2. **Edge weights are invented, not measured.** `DEFAULT_EDGE_WEIGHT` was
   chosen so an obligation carries further than a coincidence, then left alone.
   Activation output has never been evaluated against what you would consider
   relevant.
3. **Sweep confidence numbers are arithmetic, not calibration.** `0.72` means
   "17 days out on a 30-day horizon", not "72% likely to matter to you".

## Before Monday

1. Read `context/owner.md` and correct it — still the highest-risk file.
2. **Ask Pratik.** The system now surfaces it; that does not send it.
3. `goal add` your real goals. Two exist, both written by me as examples.
4. Spot-check `prep` on three people you know well.
5. Rotate the Groq key.

---

# Track feedback (26/27/28) + the FIX FIRST items

**592 tests.** PR #4 merged.

## The evidence linker had 0% precision, and only an audit showed it

Word boundaries cut six false links to two. **Both survivors were still false.**
`submit` and `application` are everywhere in a mailbox, so two common words
looked like a two-thirds match while `cmu` — the only identifying word — was
absent from both.

| version | links | true | precision |
|---|---|---|---|
| substring | 6 | 0 | 0% |
| word boundaries | 2 | 0 | 0% |
| **+ IDF** | **1** | **1** | **100%** |

The survivor is genuine: *"thank you for starting an application for the college
of engineering at Carnegie Mellon University"*. Your CMU goal is now accurate —
**application started and evidenced, letter of rec never asked and still a gap.**

**The lesson is sharper than the bug.** Fixing the *mechanism* was not the same
as fixing the *result*, and the gap between them was invisible except by reading
output. That is now the fourth instance of the same family: broken gitignore
looked like a clean tree, false evidence looked like a completed step, NULL
provenance looked like a passing suite, and a fixed matcher looked like a fixed
feature.

## Migration guard

`_assert_no_nulls` runs on every `init_db` and raises. Tests verify logic, not
data state — this makes the count check a mechanism rather than a test someone
remembers to write.

## The feedback loop closes

```
sweep    -> 5 proposals in the ledger
feedback -> 2 judged, acceptance 50%
bias     -> +0.346 accepted-shaped   -0.320 dismissed-shaped
```

No training. Behaviour changes by showing the model its own track record.

`ignored` is weighted lowest, because silence is ambiguous — you may not have
looked. A suppressed proposal never ages out at all. `acceptance_rate` returns
`None`, not 0%, when nothing is judged.

`proposals --explore` shows suppressed items deliberately, so blind spots stay
falsifiable.

## Three bugs the real run found

1. **`keywords()` dropped 2-char tokens, so "DJ" was filtered out** — your own
   worked example returned nothing. Also killed "AI" and "ML".
2. **Phone-number slugs became search terms**, matching unrelated events that
   contained the digits.
3. **You appeared as a discovered "person" in your own investigation.**

## Still not built

Tracks **sources** (30: chat/WhatsApp/LinkedIn exports) and **action** (32: send
handler behind a fake transport; 33: Tauri shell). Trigger A rewrite (20),
Fact records wired to the sweep (24 — the table and query exist, nothing writes
Facts yet), contradiction detection (31).

## Three things most likely wrong

1. **IDF is tuned on one corpus and one true positive.** 100% precision on n=1
   is the same statistical joke as the original `needs_response` recall.
2. **`outcome_bias` is computed but not yet wired into anything.** It is
   available; no caller adjusts confidence with it.
3. **Investigation depth is untested against a real multi-hop question.** The
   DJ chain works in fixtures; on your corpus it stopped at one hop because the
   participants were unnamed phone handles.

---

# Final build — everything remaining

**656 tests.** Stages 20, 24, 30, 31, 32, 33 plus the semantic layer. Nothing
is stubbed out and waiting except the one thing that must be: the live send.

## The hardcoding is gone

You were right. The system had stopword lists, regex ask-markers and IDF
keyword scoring standing in for understanding. They failed exactly where you
would expect — `letter` inside `newsletter`, a credit-card application
"supporting" a Carnegie Mellon one.

The keyword pass now only **nominates**; the model **decides**. `MIN_SCORE`
went 0.6 → 0.34 on purpose: it no longer has to be right, only to avoid
missing things.

On real data the judge rejected all four keyword candidates, **including the
one I told you was a true positive**:

```
rejects  Submit the CMU application   "only confirms start, not submission"
rejects  Send Daniel the prospectus   "mentions Logitech but no prospectus sent"
```

That first one is a correction to me. The step says SUBMIT; the email confirms
STARTING. The gap stays open, correctly.

## What now runs

| | |
|---|---|
| Events | 4,348 across **gmail, imessage, claude** |
| Graph edges | 10,594 |
| Facts (dated triggers) | 4 |
| Cached judgements | 4 |
| Commitments | 26 |

```bash
personalagi sweep              # proactive, model_calls=0
personalagi facts extract      # future dates become triggers
personalagi contradictions     # statements that cannot both be true
personalagi import <file>      # Claude/ChatGPT/Gemini/WhatsApp/LinkedIn
personalagi investigate "..."  # search, read, repeat
personalagi prep <person>      # every claim cited
cd desktop && npm run tauri dev
```

Fact extraction on 25 real events found 3, all grounded, all dated — including
the Llama decommission on **August 16**.

## Provenance now covers assistant transcripts

An export is the first file containing **both** kinds of record. Your turns are
`external` and citable; the assistant's replies are `generated` and never are.
Verified: `{'external': 1, 'generated': 1}`. Without that the system could cite
an answer a model invented as evidence for a claim it then makes.

## Sending

`SEND_ENABLED=false`, and the whole path runs against a transport that records
instead of delivering — so enabling it changes where the bytes go and nothing
else. The real Gmail transport is **deliberately unimplemented**; it raises
with the three steps to enable it. The AST test forbidding a live send path
still passes and was not weakened.

Confirmation is bound to a hash of the exact content: approve a draft, edit the
body, and the approval is void by construction.

## Three things most likely wrong

1. **The judge may be too strict.** It rejected 4 of 4 on real data. That is
   the safe direction by design, but I have not seen it accept anything yet, so
   its precision is unmeasured in the positive direction.
2. **The desktop shell has never been run.** It compiles as written but needs
   `rustup` and `npm install`; I did not install a Rust toolchain to prove it.
3. **Edge weights and sweep confidences are still invented numbers.** `0.72`
   means "17 days out on a 30-day horizon", not "72% likely to matter".

## Still not built, honestly

Transcription, screen context, calendar authorisation. Those need you at a
keyboard or a consent design, not more code.
