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
