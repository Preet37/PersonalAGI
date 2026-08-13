---
title: Grasp-stability benchmark
type: project
status: active
started: 2025-11-10
tags: [robotics, evaluation, collaboration]
people: [dana-okafor]
summary: >
  Joint benchmark with Example Labs for grasp stability across rigid and
  deformable objects. Two halves: their simulation environment, my evaluation
  harness. v1 shipped internally; v2 spec is in review.
next_action: Draft the eval harness for v2 — due 2026-08-24
---

## Log

### 2026-08-10 — decision
Splitting the metric into stability-under-perturbation and grasp-recovery.
v1 conflated them, which is why the soft-gripper numbers looked broken.

### 2026-03-18 — milestone
v1 harness running end to end on the rigid-object set. 400 trials, ~6 min
per sweep on the laptop.

### 2025-11-10 — note
Scoped the project. Agreed the deliverable is a reproducible protocol, not a
leaderboard — no ranking of other labs' systems.
