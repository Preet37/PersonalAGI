# Coordination

Phase 0 is committed. The six record types and the shared vocabulary are frozen.
Read this before starting work; append your track's section when you finish.

## Frozen after Phase 0 — propose changes, do not edit

- `src/personalagi/models.py` — all tables
- `src/personalagi/records.py` — enums, budgets, activation limits
- `src/personalagi/actions/registry.py` — the proposal contract
- `src/personalagi/db.py` — migrations

## The two rules every track honours

1. **Only `external` events may be cited.** Use `records.citable_events()`.
   Never write your own provenance filter; one enforcement point is the point.
2. **Permission and attention are independent axes.** Use
   `records.max_attention_for()` / `clamp_attention()`. Never let a model
   choose an attention level unclamped.

## Track ownership

| Track | Owns | Must not touch |
|---|---|---|
| goals | `goals.py`, `commitments.py` | sweep, activate, judgment |
| loop | `activate.py`, `judgment/`, `prep.py` | goals, sweep |
| triggers | `sweep.py`, `schedule.py` | goals, activate |
| feedback | `feedback.py` | everything else |

## Rules

- Never `git add -A`. Stage explicit paths.
- Never modify another track's tests.
- Rebase on `main` before opening a PR.
- Read model names from `.env`; never hardcode a model string.

## Log

- **Phase 0** — added `Goal`, `GoalStep`, `StepEvidence`, `Fact`, `PersonRole`,
  `Edge`, `ProposalRecord`; `Event.provenance`; `Commitment.last_activity_at`
  and `goal_id`. New `records.py`. Migration backfills provenance on existing
  rows — see the note in `db.py`, it failed silently the first time.

- **Track loop** — added `activate.py` (edges + spreading activation) and
  `prep.py` (meeting prep). **Touched `adapters/base.py`, which is outside this
  track**, to fix shared-envelope identity — reported here per the rules.
  Rationale in the commit; it was blocking `prep` on the Monday meeting.
