# Taxonomy proposal — awaiting approval

**Status: PROPOSED, NOT IMPLEMENTED.** The classifier still emits the v1
classes. Nothing in this file has been switched on, because changing the
classes would silently invalidate the baseline in `RESULTS.md` and the 30
labels in `labels.csv`. Approve or amend before Stage 12.

---

## Why v1 needs replacing

Not theory — this comes out of your own 30 labels.

### 1. `fyi` vs `promotional` is not reliably separable, including by you

| Message | You labelled | Near-identical message | You labelled |
|---|---|---|---|
| ICICI nominee update notice | `fyi` | Wells Fargo account notice | `promotional` |
| Planet Fitness one-time passcode | `fyi` | — | — |
| Deepgram devs newsletter | `fyi` | Ollama product announcement | `promotional` |

These are the same *kind* of object — a company sending an account-holder an
unrequested notice. The line between them isn't a property of the message, it's
whether you happened to feel the sender mattered. `fyi` scored precision 0.43 /
recall 0.30, the worst class in the eval. That's not a prompt failure. A class
whose boundary the ground-truth author can't apply consistently cannot be
learned, and shouldn't be measured.

### 2. The classes are topical, but the decision you make is behavioural

Two rows prove this:

**Arjun's LinkedIn connection request → you labelled `fyi`, urgency `med`.**
The classifier said `needs_response`. You were both half right. You genuinely
must *act* — accept it, he's the reason Monday exists — but there is no email
to reply to. v1 has no class for "act, but not by replying", so it forced a
wrong answer either way.

**Deepgram job alert → you labelled `promotional`, urgency `high`.**
A `promotional` message you marked as your joint-highest urgency in the whole
set. That is the taxonomy leaking: you had no way to say "this is bulk mail
that matters enormously to me because Karan works there", so the relevance went
into the urgency column instead.

**The category axis is being asked to carry two independent variables** — what
kind of message is this, and does it matter to me — and it can only express
one. That is why urgency scored macro F1 0.32 and why both of your `high` rows
were missed.

---

## Proposed v2

Two orthogonal outputs instead of one overloaded one.

### Axis 1 — `action`: what must I do?

Every class is defined by a behavioural test with a "what if I do nothing"
answer. The test is the definition; the examples are illustrative.

| Class | Test | If I do nothing |
|---|---|---|
| `reply` | Does a specific person expect **written words back from me**? | A human is left waiting |
| `act` | Is something required of me that is **not** a reply — accept, sign, book, pay, upload, show up? | A deadline passes |
| `note` | No action, but does this **change what I know** about a person, org, or thread I already track? | I'm out of date next time we speak |
| `ignore` | None of the above. | Nothing |
| `spam` | Unsolicited bulk, phishing, fraud, credential or payment extraction. | Nothing, but it is a distinct safety class |

`reply` and `act` are the two that change your day. `note` vs `ignore` replaces
`fyi` vs `promotional`, but with a test you can actually apply: **is this about
someone in my context store?** That is checkable in code, not a vibe.

Worked examples from your own labels:

- Grace / SiBRP form → **`act`** (a form to fill, not an email to answer). v1
  forced `needs_response`.
- Arjun connection request → **`act`**. v1 had to choose wrongly.
- Deepgram job alert → **`ignore`** on the message alone, and then rescued by
  relevance (below), because Karan is in your context store.
- Planet Fitness OTP → **`ignore`**. You already triggered it; there is nothing
  to know and nothing to do.
- ICICI nominee notice → **`act`** (update the nominee) — which is what you
  meant by `fyi`+low, and is more useful than either v1 class.

### Axis 2 — `relevance`: does this matter *to me*?

0–3, and **it is not computable from the message.** It requires the person's
context file and open threads, which is exactly what Stage 7B wires in.

| Score | Meaning |
|---|---|
| 3 | Touches an open commitment or a named person in my context store |
| 2 | Known person or org, no open thread |
| 1 | Unknown sender, plausibly legitimate |
| 0 | Bulk, no connection to anything I track |

Deepgram scores **3** on this axis while sitting at `ignore` on the action
axis, and that pair is the correct description of the message. v1 could not
express it, which is why you had to encode it as urgency and why the eval
couldn't score you.

Urgency stays, but it means only **time pressure**, never importance. That
separation is the fix for the 0.32.

---

## What this costs

Honest list, because switching is not free:

1. **The 30 labels in `labels.csv` do not transfer cleanly.** `needs_response`
   → `reply` is safe. `fyi` splits across `act`/`note`/`ignore` and cannot be
   mapped automatically — the ICICI row alone shows why. Re-labelling the v2
   set under v2 classes is the honest path; `labels.csv` stays frozen as the
   v1 baseline.
2. **`relevance` is not measurable from a CSV of message excerpts.** Scoring it
   requires the context store to be populated at eval time, so the eval harness
   grows a dependency it does not have today.
3. **Five action classes on a 20B model is more than four.** Expect a dip
   before a gain. Mitigated by `note`/`ignore` being structurally decidable.

## Recommendation

Approve the **action** axis and re-label the 60 v2 rows under it. Treat
**relevance** as an output the system computes rather than something you
hand-label, and check it by inspection — if `owed` and the brief surface Karan,
Daniel, and Sisi, relevance is working, and that is a better test than an F1.
