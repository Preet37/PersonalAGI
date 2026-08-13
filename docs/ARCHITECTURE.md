# PersonalAGI — Architecture Decision Record

A local-first personal context system. This document records what was chosen, what was rejected, and why. Save it to `docs/ARCHITECTURE.md`.

Read the rationale sections properly. If you disagree with one, change it. A decision you disagree with and can argue against is worth more than one you accepted.

---

## The shape of the system

```
SOURCES          NORMALIZE        STORE            RETRIEVE       ACT
─────────        ─────────        ─────            ────────       ───
Gmail       ┐                  ┌ context/*.md  ┐
Transcripts ├─→  Event      ─→ ┤               ├─→  get_context ─→ Proposal
iMessage    │    (one type)    └ SQLite (FTS5) ┘         │           │
Calendar    │                                            │           ├─ auto
Screen      ┘                                            │           ├─ approve
                                                         │           └─ never
                     ┌───────────────────────────────────┘
                     └─→ compaction (nightly)
```

Everything is an **Event**: a timestamped record with a source, one or more participants, and text. An email is an Event. A transcript segment is an Event. A calendar invite is an Event. This is the single most important decision in the system, and everything below follows from it.

---

## D1. Everything normalizes to one Event type

**Decision.** Every source produces the same record: `{id, source, timestamp, participants[], text, metadata{}}`.

**Rejected.** Per-source pipelines with their own storage and retrieval.

**Why.** The value of this system is cross-source context, not any single source. When Arjun emails you and the name also appears in a meeting transcript, both need to land in the same person file or the whole premise collapses. Per-source pipelines make that a join problem forever. One event type makes it an append.

**Cost.** The lowest common denominator loses source-specific structure. Mitigated by keeping a free-form `metadata` dict per source.

**How to say it.** "Adding a new source means writing one adapter to the Event interface. Everything downstream is untouched."

---

## D2. Markdown vault is the source of truth, SQLite is a derived index

**Decision.** `context/**/*.md` is canonical. `data/personalagi.db` is a rebuildable cache.

**Rejected.** Database as truth with markdown export.

**Why.** Three reasons. It's inspectable, so when the system says something wrong you open a file and see why instead of querying a schema. It's portable, so it opens in Obsidian and survives this codebase. And it makes recovery trivial: delete `data/`, reindex, done. No migration ever corrupts the real data.

**Cost.** Slower than a pure database, and consistency between the two is your problem. Acceptable at personal scale, where the corpus is thousands of files, not millions.

**How to say it.** "The database is a cache. If it's ever wrong, I delete it and rebuild from the files."

---

## D3. Two-layer context files: profile plus log

**Decision.** Each person file has a short LLM-maintained Profile (always loaded, capped at ~150 words) and a chronological Log (retrieved selectively via FTS).

**Rejected.** Loading the full history. Also rejected: profile only.

**Why.** Full history is the naive approach and it doesn't scale — a year of correspondence with one person is far past a usable context window, and most of it is irrelevant to the current message. Profile-only loses the specifics that make the system useful, since "we work on the hackathon" is worth less than the actual thread from March.

Two layers gets you cheap always-on identity plus expensive on-demand detail.

**Cost.** Compaction can drop something that later turns out to matter. Mitigated by never deleting raw log lines — compaction only rewrites the summary.

**How to say it.** "Profile is who they are. Log is what happened. I always load who, I retrieve what."

---

## D4. Retrieval for facts, fine-tuning only for voice

**Decision.** No facts in weights. Facts come from the vault. The only candidate fine-tune is a small model on sent mail so drafts sound like you.

**Rejected.** A per-person fine-tuned model.

**Why.** Fine-tuning teaches behavior, not facts. Facts injected through weights get memorized unreliably and hallucinated confidently. Worse, they can't be cited — for a system that acts on your behalf, being able to point at the specific email a claim came from is not optional. And facts about people change weekly, so a weights-based approach means retraining forever.

**How to say it.** "Facts never go in weights, because I need to audit what the system claims I said."

---

## D5. Compaction, not continuous learning

**Decision.** A nightly job folds new log entries into the Profile. Model weights never change.

**Rejected.** Online weight updates.

**Why.** Continuous learning of weights has a hard known failure — catastrophic forgetting — and no reliable solution at this scale. Compaction gets the same practical benefit (the system's understanding improves over time) with none of the risk, and it's fully observable: you can diff a profile across days and see exactly what changed.

**How to say it.** Never say "continuous learning." Say "persistent memory with retrieval and compaction."

---

## D6. Three permission tiers, keyed to reversibility

**Decision.** Every action the system can take is classified at design time:

| Tier | Rule | Examples |
|---|---|---|
| **Auto** | Reversible, private, no external effect | Read mail, write context files, generate a brief, draft (not send) |
| **Approve** | Externally visible or hard to undo | Send an email, create a calendar event, post anything, run a shell command |
| **Never** | Irreversible or high-blast-radius | Delete data, spend money, change credentials or permissions |

The tier is a property of the action type, not a runtime judgment by the model. It's checked in code before dispatch, not decided in a prompt.

**Rejected.** Letting the model judge risk per-call.

**Why.** A model asked "is this safe?" will sometimes say yes when it shouldn't, and prompt injection makes that worse — a malicious email could instruct the agent to send something. If the tier is enforced in code, no text the model reads can escalate its own permissions.

**Why it matters beyond this project.** This is the same problem Cisco's Cloud Control solves with governance, audit logs, and guardrails. Arriving at the same structure independently is worth saying out loud.

**How to say it.** "Permission is a property of the action, checked in code. The model can propose anything and escalate nothing."

---

## D7. Consent is a stored field, not an assumption

**Decision.** Any capture involving another person carries an explicit consent record: who, when, what form. Meeting capture is opt-in per meeting and defaults off.

**Why.** Two reasons and both are real. Legally, recording rules vary by jurisdiction and several states require every party to agree — this is worth getting proper advice on before shipping capture, not after. Practically, third-party data is why `context/` is gitignored: those people never agreed to appear in a public repo.

**How to say it.** "Every capture has a consent record attached. If I can't show consent, it doesn't get stored."

---

## D8. Propose, don't execute

**Decision.** The system's output is a **Proposal**: what it thinks should happen, why, its confidence, and the evidence. Auto-tier proposals execute immediately. Everything else waits.

**Rejected.** Direct execution with a rollback.

**Why.** Rollback is a lie for most real actions. You cannot un-send an email. Making the proposal the unit of work means every action has a reason attached and an audit trail by construction, rather than as a feature bolted on later.

**How to say it.** "The unit of work is a proposal, not an action. Every proposal carries its evidence."

---

## D9. Local-first, cloud where it earns its place

**Decision.** Storage, indexing, and retrieval are local. Inference is cloud (Groq) today, with a local classifier planned as a gate in front of it.

**Honest current state.** Today every ingested message body goes to Groq. That includes mail from people who never consented. This is the largest open issue in the system.

**The fix, in order of value:** run classification locally on a small model, since it's the highest-volume path and the easiest to move; keep cloud for summarization and drafting where quality matters more; add a redaction pass before any cloud call.

**How to say it.** Don't claim the local gate exists. Say: "Right now everything goes to Groq, which is the thing I'd fix first. The classification path is the obvious one to move local because it's high-volume and low-difficulty."

That answer is stronger than pretending it's solved.

---

## Build order

Each stage is usable on its own. Don't skip ahead.

| Stage | What | Status |
|---|---|---|
| 1 | Gmail ingest, 3 accounts | built, untested against real API |
| 2 | Classification + measured eval | next |
| 3 | Context store + FTS retrieval | next |
| 4 | Compaction | |
| 5 | Morning brief | |
| 6 | Proposal engine + permission tiers | |
| 7 | Calendar and iMessage as Event sources | |
| 8 | Meeting transcripts (consent-gated) | |
| 9 | Desktop overlay | |
| 10 | Screen context | |

Stages 1 through 5 make it genuinely useful to you daily. That's the bar that matters, because a system you don't use is a system you won't improve.

---

## Open questions you should answer yourself

These aren't settled and you should have a view:

1. When two sources disagree about a fact, which wins?
2. How does a person file handle someone with three email addresses and a phone number? Identity resolution is harder than it looks.
3. What's the retention policy? Does a log entry from four years ago ever get dropped?
4. If the profile is wrong, how do you correct it so compaction doesn't reintroduce the error?
5. What happens when someone asks you to delete their data?

Question 4 is the one most likely to come up Monday. Have an answer.
