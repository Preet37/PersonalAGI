# Eval results

One row per prompt version. Never edit a row after it is written — the point
of this file is that "it got better" is checkable rather than asserted.

**Read the caveats before quoting any number.**

---

## v1 baseline — `labels.csv`, 30 rows

| | |
|---|---|
| Label set | `evals/labels.csv` (30 rows, hand-labelled by Preet) |
| Sampling | random across distinct senders, whole inbox |
| Model | `openai/gpt-oss-20b` |
| Prompt | `ad7bd0ec952b` |
| Date | 2026-08-13 |

### Category

| class | prec | recall | f1 | support | predicted |
|---|---|---|---|---|---|
| needs_response | 0.25 | 1.00 | 0.40 | **1** | 4 |
| fyi | 0.43 | 0.30 | 0.35 | 10 | 7 |
| promotional | 0.78 | 0.74 | 0.76 | 19 | 18 |
| spam | 0.00 | 0.00 | 0.00 | 0 | 1 |

**accuracy 0.60 (18/30) · macro F1 0.38**

### Urgency

| class | prec | recall | f1 | support | predicted |
|---|---|---|---|---|---|
| low | 0.69 | 0.90 | 0.78 | 20 | 26 |
| med | 0.33 | 0.12 | 0.18 | 8 | 3 |
| high | 0.00 | 0.00 | 0.00 | **2** | 1 |

**accuracy 0.63 (19/30) · macro F1 0.32**

---

## What this baseline does and does not say

### Do not quote `needs_response` precision or recall

Support is **1**. Recall 1.00 means "the single positive example was caught";
precision 0.25 means "3 of 4 flags were wrong, out of 4 flags". Flip that one
row and recall reads 0.00. These are not measurements, they are anecdotes with
decimal points. This is what Stage 7A's `labels_v2_template.csv` exists to fix.

### Do not quote urgency at all

The urgency column was hand-labelled on a **0–9 scale** and mapped to
low/med/high by a third party using invented cut-points (0–3 low, 4–6 med, 7+
high). Macro F1 0.32 is partly measuring that mapping. The mapping was never
confirmed with the labeller.

### The one number worth saying out loud

**Both `high`-urgency rows were missed** — predicted `low` and `med`. Support 2,
recall 0.00.

Those two rows are the Stanford SiBRP alumni update form (a real person, a real
institution, a form to fill) and a LinkedIn job alert for Deepgram. They are the only two messages in 30 that the
owner marked as mattering to him, and the classifier ranked both as ignorable
while getting 18 of 20 `low` rows right.

That is a precise statement of the actual defect: **the system is good at
recognising bulk and blind to personal relevance.** It also explains why —
relevance is not a property of the message. The Deepgram alert is objectively
a bulk job blast; it matters only because Karan works there, the owner spoke
to him days ago, and Deepgram may sponsor the hackathon. None of that is in the
email, and `classify.py` never opened the context store that holds it
(0 references, verified by grep before Stage 7B).

### The `fyi` boundary is unlearnable as specified

`fyi` is the worst class (prec 0.43 / recall 0.30) and the labels themselves are
inconsistent across near-identical messages — an ICICI account notice labelled
`fyi`, a Wells Fargo account notice labelled `promotional`. See `TAXONOMY.md`.
No prompt change fixes a class whose boundary the ground truth does not hold.

---

## v2 label set — pending

`evals/labels_v2_template.csv`, 60 rows, generated 2026-08-13:

```
labels-template --out evals/labels_v2_template.csv --n 60 --human-only --stratify
```

- Sampled from **human senders only** (318 distinct, after dropping robot
  addresses and shared bulk envelopes) rather than the whole 3,998-message
  inbox.
- Balanced across predicted class. Human-sender pool by prediction:
  `promotional=258, fyi=164, needs_response=62, spam=19`.
- **Not** base-rate faithful. Per-class precision/recall remain meaningful;
  overall accuracy is not comparable to the v1 row above and must not be
  reported next to it.

Awaiting hand-labels. `labels.csv` stays frozen as the v1 baseline.
