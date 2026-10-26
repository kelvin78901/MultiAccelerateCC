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
      interpreter path.

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
