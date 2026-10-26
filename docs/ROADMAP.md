# Roadmap

## v0.1 (done) — minimal loop, zero-token testable
Graph, store, worktrees, handoffs, scheduler, CLI, mock worker, 32 tests.

## v0.2 (done) — safe on a real repo
- [x] Smoke test with real `claude -p` (3 tasks, 26 s wall, $0.59; cache_read dominates, full-price input ~0).
- [x] `macc plan --from "feature description"`: one read-only planner call, program-side validation, one corrective round.
- [x] Merge agent for textual conflicts (conflict markers handed to a worker restricted to the conflicting files; fallback: retry on fresh base).
- [x] Review REJECT reopens reviewed tasks with the notes; loop bounded by the review task's `max_attempts`.
- [x] Ownership PreToolUse guard injected via `--settings` (MACC_OWNS); exit 2 blocks out-of-scope edits.
- [x] Broken-baseline gates -> BLOCKED without charging an attempt.
- [x] `macc events --follow`.
- [x] Two real runs on the toy repo (plan3: 9 tasks, 97→124 tests, $6.85 over two rounds incl. a change cycle).
- [x] v0.2.2 from the operator's findings: scoped commits (owned paths only, artifacts discarded,
      out-of-scope = failure), transient CLI failures not charged, prompt via argv, `merge_after`
      soft ordering (run in parallel, merge in order), review `REOPEN:` line, hash refresh on first
      completion, cumulative cost budget, `macc change --owns/--done-when`.
- [x] Real-repo trial (AutoLeaseRank "watches": 9 tasks first-try, 2 → 53 tests, $5.08 incl. planning;
      review round 1 via `macc change`: 59 tests, $1.05).
- [x] v0.2.7: `MACC_BASE` / `MACC_INTEGRATION` exported to workers and gates (plans must not hard-code
      hashes; planner warns on them); `usage.est_minutes` recorded per run and `macc status` shows
      actual/est per kind; advisory `rules_conflict` event when rules.md and CLAUDE.md disagree on the
      interpreter path; run summary reports `cost_this_run` and `cost_since_plan`.

### Real-run data (2026-10-26; input for the v0.3 cost model)
Source: `usage` and `events` in `~/macc-smoke/.macc/state.db` and `AutoLeaseRank/.macc/state.db`.
Workers: sonnet for code/tests, opus for contract/integrate/review/planner; `--parallel 4`.

| run | tasks run | wall | worker sum | worker/wall | critical path | cost |
|---|---|---|---|---|---|---|
| plan3 r1 (kv, toy repo) | 9 | 681 s | 607 s | 0.89 | 497 s | $2.96 |
| plan3 r2 (`macc change` round, 2 retries + 1 REJECT) | 8 (11 runs) | 1092 s | 983 s | 0.90 | 751 s | $3.90 |
| watch r1 (AutoLeaseRank) | 9 | 585 s | 715 s | 1.22 | 577 s | $4.05 |
| watch r2 (`macc change` round) | 3 | 144 s | 166 s | 1.15 | 108 s | $1.05 |

Totals: plan3 two rounds **$6.85**, 29 min wall; plan_watch two rounds **$6.13** incl. the $1.02 planner
call, 12 min wall. Planner calls: $0.58 / 110 s (toy repo), $1.02 / 214 s (AutoLeaseRank).

Per worker run, all four runs (transient CLI failure excluded):

| kind | runs | avg seconds | avg cost |
|---|---|---|---|
| code / tests (sonnet) | 21 | 37 | $0.175 |
| contract (opus) | 2 | 145 | $0.80 |
| integrate (opus) | 3 | 116 | $0.65 |
| review (opus) | 5 | 187 | $0.95 |

What this says:
- **Parallel gain is small: 1.22x at best** (worker sum / wall, watch r1), and below 1 on the toy repo.
  Wall time ≈ critical path (contract → impl → integrate → review); the three serial opus nodes on it
  are 69–71% of wall. Fan-out only overlaps the short sonnet tasks. Ceiling = worker sum / critical path = 1.24.
  (The 929 s `worker_time` in `macc status` after watch r1 includes the 214 s planner call; 929/585 = 1.6
  is not a parallel speed-up.)
- **Serial overhead outside workers** (wall − critical path): 183 s and 340 s on plan3, where the
  integration gate runs the full pytest suite after every merge (97–124 tests, ~30 s each), ~8 s on
  AutoLeaseRank (0.5 s suite). Gate cost per merge belongs in the cost model.
- **Opus serial nodes dominate cost**: contract + integrate + review = 67% of watch r1's cost with 3 of 9
  tasks; review alone is the most expensive and slowest node.
- **Planner estimates are 10–50x too high**: actual/est = 0.061 (plan3 r1), 0.047 (watch r1); by kind
  (plan3 r1 / watch r1): code 0.020 / 0.031, contract 0.22 / 0.088, integrate 0.060 / 0.037,
  review 0.28 / 0.115. Estimates are human-minutes; do not use them raw for scheduling or split decisions.
- Reliability: 32 worker runs, 1 transient CLI failure (stdin race, fixed in v0.2.2), 1 gate failure
  from tests merged before their implementation (fixed by `merge_after`), 1 out-of-scope commit
  caught by review (fixed by scoped commits), 1 REJECT → reopen → APPROVE.

## v0.3 — impact analysis and cost model
- [ ] Code dependency graph via ast-grep / import scan per language; map files → tasks via `owns`.
- [ ] Change impact: a changed file invalidates tasks that *read* it, not only those that depend on the task.
- [ ] Cost model for split/no-split, calibrated from `usage` table (per kind: tokens, seconds, success rate,
      and `est_minutes` vs actual: the first real run came in far under the planner's estimates).
- [ ] Dynamic decomposition by a planner call when a task's estimate exceeds a threshold.

## v0.4 — benchmark harness
- [ ] Paired runs: native Claude Code vs macc on (a) SWE-bench Lite subset with `--parallel 1`,
      (b) 10 self-made multi-component tasks with oracle tests.
- [ ] Report: solve rate, wall-clock, Cost/Task, Cost/Submission, human interventions.

## Later
- Multi-model routing (cheap model for low-risk kinds).
- Remote workers (Claude Code on the web / other machines) through the same WorkerFn interface.
- Web UI for the graph.
