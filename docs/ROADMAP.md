# Roadmap

## v0.1 (done) — minimal loop, zero-token testable
Graph, store, worktrees, handoffs, scheduler, CLI, mock worker, 32 tests.

## v0.2 — make it safe on a real repo
- [ ] Smoke test with real `claude -p` on a toy repo; record tokens/time per task kind.
- [ ] `macc plan --from "feature description"`: a planner call that emits plan YAML (one LLM call, reviewed by you).
- [ ] Merge agent for textual conflicts (gets both handoffs + conflict hunks; read/write limited to conflict files).
- [ ] Review verdict REJECT reopens the reviewed tasks with the review notes as `last_error`.
- [ ] Hooks template for target repos (format on PostToolUse, block writes outside `owns` on PreToolUse).
- [ ] `macc watch`: tail events + STATE.md in a terminal.

## v0.3 — impact analysis and cost model
- [ ] Code dependency graph via ast-grep / import scan per language; map files → tasks via `owns`.
- [ ] Change impact: a changed file invalidates tasks that *read* it, not only those that depend on the task.
- [ ] Cost model for split/no-split, calibrated from `usage` table (per kind: tokens, seconds, success rate).
- [ ] Dynamic decomposition by a planner call when a task's estimate exceeds a threshold.

## v0.4 — benchmark harness
- [ ] Paired runs: native Claude Code vs macc on (a) SWE-bench Lite subset with `--parallel 1`,
      (b) 10 self-made multi-component tasks with oracle tests.
- [ ] Report: solve rate, wall-clock, Cost/Task, Cost/Submission, human interventions.

## Later
- Multi-model routing (cheap model for low-risk kinds).
- Remote workers (Claude Code on the web / other machines) through the same WorkerFn interface.
- Web UI for the graph.
