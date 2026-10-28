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
- [x] v0.2.9: `gate_policy: per_task | full` (per_task default: a task's own `done_when` after its merge,
      reused when the merged tree equals the tree it passed on; full suite after `integrate` and once at
      the end, `final_gate_failed` keeps state); reviewers get the latest full-gate result (only claimed
      for the same integration HEAD); planner prompt carries per-kind median seconds and actual/est from
      `usage` (>= 3 samples; `usage.kind` added and backfilled); run summary and `status` show this run's
      critical path, actual vs estimate. Fix: a `REOPEN:` naming a non-direct ancestor now holds the tasks
      in between as STALE, so the review waits for the redo (it used to re-review the unchanged tree).
- [x] v0.2.10/11: plan reload ignores finished tasks in the contract-ordering rule (a second feature
      plan could not load); cost reservations use the per-kind historical mean instead of this run's
      average (one $1.81 opus contract made each queued $0.2 sonnet task look like $1.81), and a
      reservation that only waits for running tasks is `budget_deferred`, not a sticky `budget_exceeded`.
- [x] v0.2.12: slim contracts. Planner rules: a contract is interfaces, models, error codes and a test plan
      of WHAT to test, never expected values; shared fixtures are their own parallel `code` task that test
      tasks `merge_after`. `validate_plan` rejects a contract with est_minutes > 8 or a spec asking for
      expected values (one corrective round). Scheduler: tasks that every other unfinished task waits for
      are global serial points; `serial_bottleneck` when one runs > 3x its estimate; run summary and
      `status` report the serial share of wall time.

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
- **Gate policy, measured** (macc-smoke, plan3 review round 2 via `macc change`, full suite 124-127 tests
  ≈ 41 s): run A (kv_server + kv_tests_server, then a test redo; 3 merges) spent 41 s in post-merge gates
  under per_task (the final full gate only) against ≈ 123 s under full: **82 s saved**. Run B (a lone
  review whose `done_when` is the full suite) spent 82 s against 41 s: the post-merge re-run duplicated
  the worktree gate. Fixed in the same release by reusing the gate when the tree is unchanged; with it,
  run B costs the same as full. Rule of thumb: per_task saves (merges − 1) × full-suite time when task
  gates are cheap; it buys nothing for a task whose own gate is the full suite.
- **Fan-out run** (AutoLeaseRank `/insights`, 12 tasks: contract → 4 independent modules + 4 test tasks
  (merge_after) → api → integrate → review; `--parallel 6`, per_task gates): $5.65 run + $1.35 planner,
  1683 s wall, worker sum 1934 s → **1.15x overall**. The contract alone took 1350 s / 41.8k output
  tokens / $1.81 (it pre-computed every module's expected output on the fixtures). After the contract:
  584 s of work in 333 s wall = **1.75x** (ceiling 1.92 against that phase's critical path). Fan-out
  works; one oversized serial node erases it. Contract scope is the cost model's biggest lever.
  Gates: 4 s total (suite 1.4 s; three `gate_reused`).
- **Refactor run** (AutoLeaseRank error handlers → `alr/api/errors.py`, planner v0.2.12): 6 tasks (one impl
  task for the whole refactor, two parallel test tasks), $3.51 run + $0.79 planner, 425 s wall, worker sum
  494 s → 1.16x. **Serial share 88%** (contract 111 s, integrate 74 s, review 188 s); no
  `serial_bottleneck` (all within 3x). The slim contract took 111 s / $0.81 (vs 1350 s before). Integrate
  changed nothing (0 files, $0.68): for a single-impl change whose tests already pass at merge, the
  integrate node is pure overhead; candidate rule: skip it when the full gate passes after the last merge.
- Reliability: 32 worker runs, 1 transient CLI failure (stdin race, fixed in v0.2.2), 1 gate failure
  from tests merged before their implementation (fixed by `merge_after`), 1 out-of-scope commit
  caught by review (fixed by scoped commits), 1 REJECT → reopen → APPROVE.

### Four real runs on AutoLeaseRank (the v0.3 baseline)
Source: `AutoLeaseRank/.macc/state.db`. Serial share = worker time in tasks that every other unfinished
task of the run was waiting for, divided by wall time (recorded by macc from v0.2.12; the first three
runs reconstructed from the event log with the same definition; the refactor's reconstruction matches
the recorded 0.877). Wall excludes the planner call.

| run | tasks (with a worker) | wall | worker sum | worker/wall | serial share (serial nodes) | cost | review | tests |
|---|---|---|---|---|---|---|---|---|
| watch r1 (new feature) | 9 (9) | 585 s | 715 s | 1.22x | 71% (contract, integrate, review) | $4.05 + $1.02 plan | 172 s / $1.03 | +51 (2→53) |
| watch r2 (review fixes via `change`) | 4 (3) | 144 s | 166 s | 1.15x | 75% (review) | $1.05 | 108 s / $0.73 | +6 (53→59) |
| insights (new feature, 4-way fan-out) | 12 (12) | 1683 s | 1934 s | 1.15x | 94% (contract, integrate, review) | $5.65 + $1.35 plan | 152 s / $1.19 | +88 (59→147) |
| error-handler refactor | 6 (6) | 425 s | 494 s | 1.16x | 88% (contract, integrate, review) | $3.51 + $0.79 plan | 188 s / $1.35 | +66 (147→213) |
| **total** | 31 (30) | 2837 s | 3309 s | 1.17x | 87% | $14.26 + $3.16 plan | 620 s / $4.30 | +211 |

Read-outs: the speed-up is ~1.2x in every shape of run, because 71-94% of wall time is spent in serial
nodes; the reviews alone are 22% of wall and 30% of run cost; the cheapest possible worker run (a
small sonnet code task) is ~21 s and ~$0.16, so that is the floor every extra task pays.

## v0.3 — proposal (data-driven; not implemented)

**1. Cost model: wall = serial chain + widest parallel phase + gates.** Model the plan as phases
separated by serial nodes (a node every remaining task waits for). Predicted wall =
Σ serial-node time + Σ over phases of (max branch time in the phase) + gate time per merge; predicted
cost = Σ per-run cost. Per-task time and cost come from `usage` by kind and model (median, with the
plan's est_minutes only as a weak prior: actual/est has ranged 0.02-1.7). Weighting: a serial node
counts at 1.0 for wall; a task inside a parallel phase counts only through the phase maximum, so its
wall weight is ~0 unless it *is* the maximum. Every task, serial or not, counts 1.0 for cost plus the
~$0.16 / 21 s floor. Decisions this enables, all deterministic:
- Split a task into k parallel parts only if it is a serial node or the phase maximum, and
  t · (1 − 1/k) > k · 21 s. Otherwise splitting only adds cost (the insights 4-way fan-out saved ~4 min
  of a 28 min run; the contract alone was 22 min).
- Refuse or flag plans whose predicted serial share is above ~80% when the description promises
  independent modules: the planner put the work in the wrong place.
- Budget reservations and `serial_bottleneck` use the same per-kind numbers (already partly true).

**2. Cut the serial overhead, biggest first.** (2a and 2b done in v0.3.0, see below.)
- *Review (largest single node, 108-188 s, $0.73-1.35, 30% of cost).*
  (a) Tier it by risk: diff size, files touched and whether public interfaces changed decide sonnet vs
  opus and effort; the refactor's review (behaviour-preserving, fully gated) did not need opus/high.
  (b) Hand it facts instead of letting it rediscover them: the full-gate result is already passed on;
  add the diff stat, the contract checklist and the debt list, and forbid re-running the suite
  (the watch r1 and refactor reviews re-ran it; the insights review, told by the v0.2.9 full-gate note,
  did not, and spent its time on probes instead).
  (c) Make round 2+ incremental: review only the files changed since the rejected round (watch r2 was
  108 s against 172 s; it can go lower).
  (d) Overlap it: once every code task has merged and integrate is predicted to be a no-op, start the
  review then, not after integrate (already a no-op with v0.2.13's skip, so this mostly applies when
  integrate must run).
- *Contract.* Keep the v0.2.12 limits; additionally merge contract and implementation into one task
  when only one task consumes the contract (the refactor: one impl, 111 s of contract for it).
- *Integrate.* v0.2.13 skips the no-op case; next, let integrate start from the failing gate output
  only (it currently re-reads the whole change).
- *Planner.* $0.79-1.35 and 150-320 s per feature (18% of cost): reuse its repository reading across
  `plan --from` calls in the same repo (file list, contracts, gate) and skip re-reading what has not
  changed since the last plan.

**Done (v0.3.0): review tiering and fact hand-off.** At review dispatch macc diffs this plan's change
(`plan_base` = integration HEAD at `macc plan`; `MACC_BASE` would also count earlier unmerged plans) and
rates it high when any of: > 5 new public defs/classes/routes (a new interface), > 400 changed source lines
(tests and docs excluded), an existing test file lost lines, an open debt, or no green full gate on the
reviewed tree. Low → that dispatch runs on `sonnet`/`medium` (`review_tier` event; the plan keeps opus).
Every review's context pack gets a facts section: diff stat by source/tests/docs, new public symbols,
the contract files to check against, all open debts on its ancestors, and "do not re-run the suite".
Applied retroactively to the four runs:

| review | source lines | new public API | tier | what the opus review actually did |
|---|---|---|---|---|
| watch r1 (new feature) | 283 | 17 | high | 172 s / $1.03, found M1, M2, L1 |
| watch r2 (fix round) | 15 | 0 | low | 108 s / $0.73 |
| insights (new feature) | 484 | 31 | high | 152 s / $1.19, found F1-F4 |
| refactor | 222 | 4 | low | 188 s / $1.35, 128-request differential probe, no findings |

Opus stays where it found things; the two low-risk reviews ($2.08 of the $4.30 spent on reviews) move to
sonnet.

**First live A/B (AutoLeaseRank `add_flat_routes` fix, 31 source lines, 0 new public API).** The live run
rated the review *high* only because a no-op integrate left no full-gate result (fixed in v0.3.1), so
the live review was opus/high; the same context pack and commit were then reviewed by sonnet/medium:

| reviewer | time | cost | output tokens | report | verdict |
|---|---|---|---|---|---|
| opus / high (live) | 144 s | $1.06 | 15.9k | 106 lines | APPROVE |
| sonnet / medium (A/B) | 117 s | $0.36 | 8.3k | 35 lines | REJECT: `test_openapi_equals_include_router` is flaky |

The sonnet verdict was right: unseeded, the full suite fails 2 of 25 runs (the multi-method test
route's default operationId follows `methods` set order, which depends on the hash seed). Opus ran the
suite once, saw it pass, and noted the `Duplicate Operation ID` warning as benign; every macc gate also
passed by luck. One sample, so no general claim, but it is evidence that the cheaper tier is not
weaker at finding defects when it actually runs the code, and that single-run gates do not catch
flakiness (candidate: run newly added test files several times with random seeds at integrate).

Follow-up (same day): the flaky test was fixed through `macc change flat_routes_tests` (one line: an
explicit `operation_id` on the multi-method test route; done_when now runs the file 10 times with a random
hash seed). Unseeded full suite: 2/25 failures before, **0/25** after. That round's review was tiered low
and ran on sonnet/medium: 39 s, $0.21.

v0.3.1 fixes found by this run: a `done_when` asserting paths unchanged since `$MACC_BASE` while an
upstream task owns them is rejected by `validate_plan` (it could never pass; it cost 2 attempts); an
integrate that changes nothing now runs the full gate itself, so the review after it can be tiered.
v0.3.2/0.3.3: a revalidated integrate also proves the full gate (else the review is tiered high again);
after an integrate is skipped in a change round, stale dependents are re-examined (the review used to be
left STALE and the run ended unfinished); reviewer tool allowlists skip leading `VAR=value` words and
allow the project interpreter itself (a `PYTHONPATH=. …` gate produced `Bash(PYTHONPATH=.:*)`, so probes
worked only by accident and one review could not run Python); a re-run review that commits nothing is
retried with "write a fresh report" (the earlier round's file satisfied its done_when).

**Done (v0.3.5): phase cost model, predict only** (`macc/costmodel.py`). wall = Σ serial-point time +
Σ phases max(longest chain, work / parallelism) + full-gate time; cost = Σ per-task cost. A serial point
is weighted with the median actual seconds of its kind *when it ran as a serial point* (`usage.serial`,
recorded live and reconstructed from the event log for older runs); other tasks with their kind's
median; no tuned constants. Shown with the plan warnings (`plan --from`) and on `macc plan`; at the end
of a run compared with the actual values (`run_finished.prediction.error`, `macc status`), with the
per-task prediction stored next to the actual in `usage.pred_seconds` / `pred_cost`.

Backtest, leave-one-out (prediction / actual):

| run | wall | cost | serial share |
|---|---|---|---|
| watch r1 | 453 / 585 s (−23%) | $3.88 / $4.05 (−4%) | 82% / 71% |
| watch r2 (fix round) | 193 / 144 s (+34%) | $1.50 / $1.05 (+42%) | 79% / 75% |
| insights | 463 / 1683 s (−72%) | $4.54 / $5.65 (−20%) | 80% / 94% |
| refactor | 420 / 425 s (−1%) | $3.31 / $3.51 (−6%) | 90% / 88% |

2 of 4 within 30% on wall and cost; mean error wall 32%, cost 18%, serial share 8 points. The misses
have identifiable causes, not noise: insights' contract ran 1350 s against a 155 s median (the
pre-computed-answers contract v0.2.12 now rejects; `serial_bottleneck` is the live signal for it), and fix
rounds are cheaper than first rounds (watch r2's review 108 s vs 152 s). A later low-tier round (routes C)
was +267% on cost because the model does not yet know the review tier (sonnet $0.21 vs the opus-dominated
review median). **Split hints stay off** (`split_hints: false`) until the error is within 30%.

v0.3.6 adds those two inputs: history is looked up by kind / model family / first-vs-fix round / serial
point (falling back level by level until >= 3 samples), a fix round is a task that completed before
(`usage.fix_round`, recorded at dispatch, reconstructed from done events for older rows), and with
tiering on a fix-round review is predicted on the low-tier model. Backtest again (leave-one-out):

| history | runs within 30% (wall and cost) | mean wall error | mean cost error | mean serial error |
|---|---|---|---|---|
| v0.3.5 model, AutoLeaseRank only | 2 / 4 | 32% | 18% | 8 pt |
| v0.3.6 model, AutoLeaseRank only | 2 / 4 | 33% | 18% | 8 pt |
| v0.3.6 model, AutoLeaseRank + macc-smoke | **3 / 4** | **31%** | **14%** | 7 pt |

The new dimensions are right but starved: AutoLeaseRank has 5 fix-round rows and 2 sonnet reviews, so
under leave-one-out most lookups fall back past them. Pooling the macc-smoke history (5 fix-round opus
reviews, 9 fix-round code runs) brings watch r2 from +41% / +55% to +29% / −18%. The remaining wall error
is almost all insights (−72%, the 1350 s contract that v0.2.12 now rejects); without it the three other
runs average 18%. Hints stay off until that is decided; low-tier (sonnet) review samples are still
missing everywhere (routes C, +91% wall).

**v0.3.7: the model in production.** `history_dbs` (config; `macc init --history <db>` appends) pools other
repositories' worker rows; the gate term uses only this repository (its recorded full gates, else
`gate_baseline_s` measured by `macc init --gate`). `macc backtest` is built in: leave-one-out over every
finished run, runs with a serial bottleneck excluded as a fixed rule (a `serial_bottleneck` event or,
before that event existed, a serial worker > 3x its estimate). Split hints are on; each names the history
level and sample count behind it, and at level 5 or coarser (kind only) it says "insufficient data".
`macc plan` now keeps repository settings (`history_dbs`, gate baseline, gate timeout, worktree setup).

AutoLeaseRank, `history_dbs` = macc-smoke, `macc backtest`:

| run | wall pred / actual | cost pred / actual | serial share |
|---|---|---|---|
| watch r1 | 458 / 585 s (−22%) | $3.84 / $4.05 (−5%) | 85% / 72% |
| watch r2 | 186 / 144 s (+29%) | $0.87 / $1.05 (−18%) | 76% / 75% |
| insights | excluded (serial bottleneck: 1350 s contract) | | |
| refactor | 436 / 425 s (+2%) | $3.19 / $3.51 (−9%) | 92% / 88% |
| routes A (failed run) | 179 / 265 s (−32%) | $1.27 / $1.61 (−21%) | 81% / 75% |
| routes B | 314 / 251 s (+25%) | $2.08 / $1.86 (+12%) | 99% / 97% |
| routes C (sonnet review) | 188 / 99 s (+91%) | $0.80 / $0.35 (+133%) | 99% / 88% |

The four original runs: all three that count are within 30%. Over all six: 4/6, mean error wall 34%, cost
33%, serial share 6 points. The two misses are a run that burnt two failed attempts (routes A) and the only
low-tier review, which has no sonnet-review history to draw on yet (routes C).

**v0.3.8: large-repository gaps.** `worktree_setup` (config commands run in each new task worktree and in
the integration worktree; failure blocks the task uncharged; whatever they create is never committed or
cleaned); gate results shared per (tree hash, command) within a run, concurrent identical gates wait for
the first, async-path gates run off the event loop, `gate_timeout` configurable; planner sees a directory
digest instead of the file list above 300 files plus AGENTS.md / CONTRIBUTING.md / CLAUDE.md, and may
WebSearch; network access by kind: spike = WebSearch + WebFetch (and documents sources for downstream),
plan = WebSearch, every other kind none (explicitly disallowed).

**3. Code dependency graph / change impact: defer.** Its value is in change cycles (invalidate only
what reads a changed file), but the measured change cycle (watch r2) already re-ran only 2 tasks plus
the review, and integrate was revalidated by its gates; wall time there was 75% review. Nothing in the
four runs was slowed by over-invalidation. Build the graph only when a run shows stale_rerun or
revalidation work that an import graph would have avoided; until then a cheap per-language import scan
is enough to warn when two parallel tasks' owns import each other.

**4. Dynamic decomposition: only on the serial chain.** Split a running plan only when
`serial_bottleneck` fires on a node the cost model says is splittable (rule in 1); never decompose
parallel-phase tasks.

## v0.4.0 — mechanisms from REA #6404 (done)
Five rounds, $14.20, of which three rounds came from one gap (three unowned verifiers that enumerate Ghidra
operations) and one from a missing 0.6 s gate. Built: plan-time ownership gaps (spec identifiers grepped in
scripts/tests/docs, unowned hits ranked and attributed to integrate; on #6404's plan all three verifiers rank in
the top 8 of 33), `fails_outside_owns` (a gate failing only in unowned files BLOCKS instead of retrying; on the
r1 log it names scripts/verify-real-ghidra-inventory.mjs), scheduler-run heavy checks (context pack names
measured >= 60 s commands; the cost model adds measured done_when time), and `init --discover-gates` (3e487bf).

## v0.4 — benchmark harness
- [ ] Paired runs: native Claude Code vs macc on (a) SWE-bench Lite subset with `--parallel 1`,
      (b) 10 self-made multi-component tasks with oracle tests.
- [ ] Report: solve rate, wall-clock, Cost/Task, Cost/Submission, human interventions.

## v0.4.4 — done_when owned by the program (done)
From the #6404 bench lesson that every failed macc run came from planner-written done_when. `test_cmd_patterns`
+ the gate whitelist rewrite done_when at `macc plan`; contract error-convention check (fires on all four bench D
plans, each missing error message wording, the v0.4.3 failure); nearby exports in the context pack. Replay
(`~/bench/replay_v044.py`): verify:ghidra moved off the worker task in D v0.4.2 and v0.4.3; ghidraRoutingMcp.test.ts
covered by integrate's derived done_when in D v0.4.0. No live run.

## v0.4.5 — no merge leaves integration failing a fast gate (done)
Bench 2 (REA #4302+#4304+#4309) D v0.4.4 failed in parallel code: a spike (done_when `test -s spike.md`, per_task)
merged an unformatted file; four code tasks spent both attempts on format:check of it and apple_docs' fix was
rejected as outside its owns. Now: under per_task, integration gates measured <= `fast_gate_s` (15 s; unmeasured
gates never) also run after every merge of any kind, and a failure reverts that merge. An integration gate that
fails in a task's worktree and on the integration tree without its change BLOCKs the task (attempt not charged,
event gate_broken_on_integration) naming the done tasks whose merged files the output mentions.
**Did not engage in the live run (bench 2 D v0.4.5).** Both checks match `integration_gates` by exact string, and the
plan that ran had one entry: `macc plan --from` hands the planner the gates joined with ` && `, the planner writes
that chain back into plan.yaml, and plan load replaces config.json's 11 entries with it. The chain was never
measured, so fast_gates was empty (0 s added per merge) and `npm run format:check` was never "an integration
gate". The pre-release replay read config.json's 11 entries, not the post-load config, and wrongly reported
9 of 11 fast. Not fixed (bench rule: no fix after a failed D).

## Lessons

### REA #6404 bench (2026-10-27): single session vs subagents vs agent team vs macc
One run per arm, Opus, $25 cap, same `task.md` (issue verbatim + acceptance criteria), baseline-only clones, scored
by `~/bench/score.sh` (11 gates, real verify:ghidra, a 60-case oracle written by a separate session, 90 related
test files, out-of-bounds check) and a blind five-way review (8 items, max 40). Data: `~/bench/results/final/`.

| arm | finished | wall s | cost $ | oracle | regressions | blind |
|---|---|---|---|---|---|---|
| A single session | yes | 749 | 3.89 | 60/60 | 0 | 31 |
| B background subagents | yes | 1029 | 7.61 | 60/60 | 0 | 33 |
| C agent team | yes | 721 | 6.27 | 58/60 | 0 | 32 |
| D macc v0.4.0 | yes | 1543 | 7.97 | 60/60 | 1 | 25 |
| D macc v0.4.1 | no | 947 | 5.44 | 59/60 | 8 | - |
| D macc v0.4.2 | no | 1461 | 6.52 | 24/60 | 1 | - |
| D macc v0.4.3 | no | 2680 | 13.49 | 56/60 | 0 | 28 |

1. **This task had no parallel room; macc's fixed cost was pure loss.** One short dependency chain plus a 4-minute
   real-Ghidra check that only runs serially. D v0.4.0: planner 378 s, contract 64 s, six code workers 387 s of work
   in 185 s of wall, integrate 747 s (worker 404 s + 343 s of done_when/gates), review 163 s. Parallelism saved
   ~200 s; the planner and the serial integrate/review cost ~1290 s.
2. **A clear acceptance spec did what the ownership mechanisms were built for.** All four arms updated all three
   verifiers that enumerate Ghidra operations without being told their names; the original #6404 macc run needed
   three extra rounds to find them.
3. **Root cause of every D failure: the planner writes done_when freely.** v0.4.1: a directory passed to
   `test:focused` (REA rejects it; rules.md said so, the planner did not see rules.md). v0.4.2: the bridge task got
   `verify:ghidra`, an end-to-end check that cannot pass before other tasks merge. v0.4.3: the v0.4.3 plan-time
   warning fired on exactly that pattern (`real_verifier: npm run verify:ghidra`, measured 260 s) and the run still
   failed there: after a review REJECT the task's own new assertion disagreed with another task's error text inside
   verify:ghidra, and the retry changed nothing. Warnings without a human to act on them change nothing; each fix
   closed one hole and the next run found another. Next step is not another warning: macc should own done_when
   (derive targeted checks from owns and the repo's test command; reserve end-to-end checks for integrate) and
   treat planner-written commands as suggestions.
4. **Diff size was plan-driven, not duplicated work.** D v0.4.0 +926 vs A +421: same production code size; the
   excess is a 35-line wire-rules JSDoc the contract task wrote into GhidraInventoryValues.ts, a fake Ghidra that
   re-implements the bridge's validation because the plan told it to follow that JSDoc, and a new 363-line MCP
   test with a hand-rolled 107-line harness where the existing `connectGhidraMcp` would do (A used it: +60 lines).
5. **Derived tests (v0.4.1) work but caught nothing in a real run.** v0.4.3: 40 files from 143 changed
   identifiers, 58 s, all green (the plan already owned the routing test). On v0.4.0's final tree the same rule
   catches the one regression (ghidraRoutingMcp, 75 s).
6. **The oracle saturates; the blind review and the oracle agree where they overlap** (D v0.4.3: unmapped address
   throws instead of returning false, found by both).

### REA #4302+#4304+#4309 bench 2 (2026-10-28): same four arms, a task with fan-out
$40 cap, A/B/C may continue (max 6 sessions) while any of 11 gates fails or `TASK_COMPLETE` is missing. Scored by
`~/bench2/score.sh` (11 gates, verify:macos-bundle, verify:interface-builder, 89-case oracle, 75 related test files,
out-of-bounds) and a blind five-way review by a new reviewer. Data: `~/bench2/REPORT.md`, `~/bench2/results/`.

| arm | continues | finished | wall s | cost $ | oracle | regressions | blind (5-way) |
|---|---|---|---|---|---|---|---|
| A single session | 0 | yes | 1879 | 13.53 | 81/89 | 0 | 32 |
| B background subagents | 2 | yes | 2404 | 42.72 (over cap, $5.20 est.) | 81/89 | 0 | 35 |
| C agent team | 0 | yes | 3751 | 25.69 | 82/89 | 2 | 33 |
| D macc v0.4.4 | - | no | 2572 | 19.84 | 4/89 | 0 | 14 |
| D macc v0.4.5 | - | no | 3300 | 24.13 | 4/89 | 0 | 15 |

1. **Fan-out did not help macc: both D runs died before integrate, on the same file.** The planner wrote a spike
   whose output is a markdown file with done_when `test -s`; under per_task nothing else checked it, and the
   unformatted file then failed every later task's `format:check`. The ownership rule turned the one worker that
   fixed it into a failure.
2. **The contract phase is a planner-made serial chain.** Both plans put spike -> contract_bundle_tools: 405 + 636 s
   (v0.4.4) and 348 + 819 s (v0.4.5) before any code task of #4304/#4309 started. In v0.4.4 the two contracts
   added 737 and 1595 lines (spec + schema + tests); A finished the whole task in 1879 s.
3. **Planner-kept test commands for tests-only tasks cannot pass in a worktree.** v0.4.5 split impl_X / tests_X
   with `merge_after: [impl_X]`; the tests tasks' done_when ran their own new tests before impl_X existed, so all
   three failed both attempts by construction. done_when derivation kept them (exact owned files are whitelisted).
4. **Everything macc adds is keyed to strings the planner can rewrite.** v0.4.5's two fixes compared exact gate
   strings; the planner turned 11 gates into one chain and both fixes became no-ops.
5. **The contract error-convention check fired in v0.4.5 (not v0.4.4) and nothing acted on it.** The exports list
   was used (workers imported listed helpers), but no code task merged.
6. **Blind reviewers disagree on regressions:** the 4-way reviewer named C's broken existing `inspect_plist` MCP
   tests; the 5-way reviewer did not. Score.sh's related tests caught them in both cases.

### Negative results: every macc D run on REA (bench 1 and 2)
Six D runs, five did not finish. Class = where the defect that ended the run sits.

| run | finished | where it ended | class | contributing |
|---|---|---|---|---|
| #6404 v0.4.0 | yes (1 regression) | ghidraRoutingMcp broke; no task owned or ran it | other (ownership coverage) | - |
| #6404 v0.4.1 | no | planner done_when passed a directory to `test:focused` | planner | gate policy (done_when not checked) |
| #6404 v0.4.2 | no | bridge task's done_when = verify:ghidra, needs other tasks merged | gate policy | planner |
| #6404 v0.4.3 | no | after review REJECT, test assertion vs another task's error text in verify:ghidra; retry changed nothing | contract | attribution (retry could not fix it) |
| bench 2 v0.4.4 | no | spike merged unformatted spike.md unchecked; every code task's format:check failed on it | gate policy | attribution (charged to the wrong tasks) |
| bench 2 v0.4.5 | no | same spike.md; v0.4.5 checks matched no gate (planner chained the 11 gates); tests_X ran tests before impl_X merged | gate policy | attribution; other (macc fix verified on the wrong config) |

Counts by class: gate policy 3, planner 1, contract 1, attribution 0 as the ending cause (3 contributing), other 1
(the finished run). Pattern: each release closed the hole the last run died in, and the next run died in a new one
at the boundary between planner output and scheduler checks. macc has not finished a fan-out REA task.

## Later
- Multi-model routing (cheap model for low-risk kinds).
- Remote workers (Claude Code on the web / other machines) through the same WorkerFn interface.
- Web UI for the graph.
