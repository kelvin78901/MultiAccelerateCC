# MultiAccelerateCC

## Project status: Archived

Archived after two REA benchmarks and an Android step 0: on tasks that fit in a single context it was not faster
than one Claude Code session, cost 1.5-2x as much and did not reach acceptable reliability; the boundary for very
large tasks was not tested and will not be pursued. The results below are kept for reference.

In two benchmarks on a real repository (REA) it
was compared with a single Claude Code session (A), background subagents (B) and an agent team (C), all on Opus
with the same task text. Oracle = hidden acceptance cases passed; blind = independent review score out of 40.

- **A single session:** #6404: finished, 749 s, $3.89, oracle 60/60, blind 31. Bench 2: finished, 1879 s, $13.53,
  oracle 81/89, blind 32.
- **B background subagents:** #6404: finished, 1029 s, $7.61, oracle 60/60, blind 33. Bench 2: finished, 2404 s,
  $42.72 (over the $40 cap), oracle 81/89, blind 35.
- **C agent team:** #6404: finished, 721 s, $6.27, oracle 58/60, blind 32. Bench 2: finished, 3751 s, $25.69,
  oracle 82/89 with 2 broken existing tests, blind 33.
- **D macc:** #6404: v0.4.0 finished (1543 s, $7.97, oracle 60/60, 1 regression, blind 25); v0.4.1-v0.4.3 did
  not finish. Bench 2: v0.4.4 and v0.4.5 did not finish (2572 s / $19.84 and 3300 s / $24.13, oracle 4/89 = the
  baseline, blind 14 and 15).

Five of six macc runs failed, each at a boundary between planner output and the scheduler's checks. See
[Negative results](docs/ROADMAP.md#negative-results-every-macc-d-run-on-rea-bench-1-and-2) and
[Lessons](docs/ROADMAP.md#lessons) in the ROADMAP; benchmark inputs and reports are in [bench/](bench/).

A deterministic task-graph scheduler that runs Claude Code workers in parallel, uses waiting
time instead of blocking on it, and keeps the plan honest when requirements change.

```
plan.yaml ──► TaskGraph ──► Scheduler (asyncio) ──► claude -p (one worktree per task)
                 ▲              │                         │
                 │              ├── ready set (Kahn)       └── handoff (generated) ──► next task
                 │              ├── priority (critical path)
                 │              ├── ownership conflicts
                 │              ├── token budget
                 └── staleness  └── merge in topo order + gates
```

## Why

Claude Code is sequential by default: one conversation, one worker, and ten minutes of waiting
for a test run is ten minutes of nothing. Official parallel features (subagents, agent teams,
dynamic workflows) still make the *model* the scheduler, which costs tokens and loses track of
state. macc moves scheduling into a plain program and calls the model only to do work.

## What it does today (v0.4.4)

Scheduling
- **Parallel fan-out** with a WIP limit; independent tasks never wait for each other; priority by critical path.
- **Contract-first**: tasks of kind `contract` precede anything that depends on their interface.
- **`merge_after`**: tests run in parallel with the implementation but merge only after it.
- **Merge in topological order** into `macc/integration`, gates after every merge, automatic revert on failure.
  Textual conflicts go to a merge agent that sees only the conflicting files plus both handoffs.
- **Build-system staleness**: input hashes per task; a changed spec or upstream output makes downstream STALE
  (running ones are preempted, no attempt charged); a STALE task whose gates still pass is DONE again.
- **Crash-safe**: SQLite holds graph, events, handoffs, usage; `macc run` resumes.

Planning
- **Planner in one call**: `macc plan --from "..."` inspects the repo read-only and emits plan YAML; the program
  validates it (cycles, contract limits, ownership overlap, missing done_when, impossible guards, review asking
  for the full suite) and you review the file before `macc plan PLAN.yaml`. The planner sees `.macc/rules.md` as
  well as AGENTS.md / CONTRIBUTING.md / CLAUDE.md, so its done_when commands follow the same rules as workers (v0.4.2).
  A code/contract task whose done_when repeats an end-to-end command integrate also runs (naming none of the task's
  files, measured >= 60 s or unmeasured) is warned about: it can fail alone until the other tasks merge (v0.4.3).
- **Ownership gaps (v0.4)**: identifiers the contract/code specs name (UPPER_SNAKE constants, camelCase /
  PascalCase symbols, snake_case operation names) are grepped in `scripts/`, `tests/`, `docs/` and `src/**/*.test.*`;
  files that mention them but that no task owns are listed, most likely first, with integrate as the suggested
  owner (`macc plan PLAN.yaml --gaps` lists all). A file covered only by integrate's wildcard owns (`tests/**`)
  and named by no spec counts as a gap too: a broad glob gives permission, not attention. The planner prompt has
  the same rule.

- **done_when is derived, not trusted (v0.4.4)**: with `test_cmd_patterns` in `.macc/config.json`
  (e.g. `{"cmd": "npm run test:focused -- {files}", "paths": ["tests/**"]}`, `"node --check {file}"`), `macc plan`
  rewrites each code task's done_when from the test files it owns plus whitelisted commands (integration gates,
  `--discover-gates` results, the patterns on exact files). Other planner commands are dropped (`dropped: ...`);
  a non-gate command measured >= 60 s is moved to integrate. Integrate also gets the owned test files that mention
  identifiers the specs name. Event `done_when_derived`.
- **Contract error conventions (v0.4.4)**: when one task implements behaviour and another tests or verifies it,
  the contract spec must fix error codes, error message wording and return value vs throw; otherwise a warning.
- **Nearby exports (v0.4.4)**: the context pack lists up to 60 `file: exports` lines from the directories of the
  task's owns, `tests/` beside them and the shared test helper directories, so workers reuse existing harnesses.

Gates
- **Gate discovery**: `macc init --discover-gates` times the repo's own check/lint/format/typecheck/test targets
  on the integration tree; `--pick` adds chosen ones to `integration_gates` (details below).
- **Failures outside owns block (v0.4)**: when a `done_when` or integration gate fails and every file its output
  was raised at (`file:line` headers, compiler errors, FAIL lines, and the first tracked frame of each stack;
  callers further down a stack do not count, v0.4.3) is outside the task's `owns`, the task is BLOCKED at once
  (`fails_outside_owns`, with the files and a suggested owner) instead of retrying something it cannot fix.
- **Integrate runs the tests the change touches (v0.4.1)**: with `focused_test_cmd` set (`macc init
  --focused-test-cmd "npm run test:focused -- {files}"`), integrate also runs up to 40 existing test files that
  mention identifiers changed since the plan's base (changed tests first, then the most specific mentions;
  event `derived_tests`). A failure fails integrate like its own done_when.
- **Heavy checks run once (v0.4)**: the context pack tells each worker which commands the scheduler runs for it
  (done_when, integration gates) and names the measured slow ones (>= 60 s) it must not run itself.
- **Broken environment != broken task**: a gate that fails on the baseline blocks without charging an attempt;
  CLI start-up failures are retried without charging either. Gate output keeps both its head and its tail.
- `worktree_setup` commands (e.g. symlink `node_modules`) run in every new worktree and before every gate.

Workers and review
- **Ownership is enforced**: a PreToolUse hook blocks edits outside `owns`; only owned paths are committed, build
  artifacts are discarded, anything else fails the attempt.
- **Handoffs generated from facts** (diff, API-looking lines, gate results with seconds, stub markers); the
  model writes only `open_questions`, `notes_for_downstream`, `stubs`. Debts and decisions reach later workers.
- **Review with teeth**: REJECT reopens the reviewed tasks with the notes (bounded by `max_attempts`); a reopened
  or changed integrate is never skipped; a reopened task that changes nothing and only asks holds the review for
  a human. Review tiering runs small fix-round reviews on a cheaper model.

Cost model
- `predict` gives wall time, cost and serial share from measured history (this repo plus `history_dbs`), with
  per-task done_when time from this repo's gate records (v0.4) and the gate baseline; every run records its
  prediction and the error. `macc backtest` replays every finished run leave-one-out.
- Budgets: `--budget` (tokens) and `--cost-budget` (USD) stop dispatching.
- **MockWorker**: the whole loop is testable at zero tokens.

## Install

```bash
cd MultiAccelerateCC
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

## Use

```bash
cd /path/to/your/repo
macc init
macc init --discover-gates                         # time the repo's check/lint/format/typecheck/test targets
macc init --pick 1,3                               # append chosen candidates to integration_gates
macc plan --from "Add a todo API with a web UI and e2e tests" plan.yaml   # or hand-write it
macc plan plan.yaml
macc run --worker mock --parallel 4 --gate true   # dry run, no tokens
macc run --parallel 3 --budget 400000             # real claude -p workers
macc status
macc change contract --spec "add auth" --why "customer asked for SSO"   # invalidates downstream
macc split backend --plan sub.yaml                 # dynamic decomposition
macc retry backend                                 # reset a FAILED/BLOCKED task
macc debts
macc events --follow                               # in a second terminal
macc backtest                                      # prediction vs actual for every finished run
```

`--discover-gates` reads package.json scripts, Makefile targets and pyproject.toml task tables (poe, pdm,
hatch, taskipy, plus ruff/black/mypy/pytest when configured), runs each once in the integration worktree and
lists the ones that pass in under half of `gate_timeout`, fastest first. Watch, fix, coverage, CI-aggregate and
in-place format targets are skipped; one that changes the tree is undone and rejected. Results are saved to
`.macc/gate_discovery.json`, so `--pick` does not time anything again. Exported env applies (e.g.
`TURBO_FORCE=true` so a turbo cache replay does not pass for a fast gate).

Project-specific rules for workers go in `.macc/rules.md` (keep it short; it is prepended to
every context pack and should stay byte-identical for cache hits).

## Plan format

```yaml
config: {max_parallel: 4, token_budget: 600000, integration_gates: ["python -m pytest -q"]}
defaults: {effort: medium, max_attempts: 2}
tasks:
  - id: contract
    kind: contract            # code | contract | spike | review | integrate | manual
    owns: ["api/**"]
    done_when: ["test -f api/openapi.yaml"]
    est_minutes: 10
    est_tokens: 40000
    spec: |
      ...
  - id: backend
    deps: [contract]
    owns: ["server/**"]
    spec: ...
  - id: backend_tests
    deps: [contract]
    merge_after: [backend]    # runs now, merges once backend is in
    owns: ["tests/server/**"]
    spec: ...
```

Gates: with `gate_policy: per_task` (default) a merge runs only that task's `done_when` on the
integration tree (skipped when the tree is identical to the one it passed on in the worktree); the full
`integration_gates` run after `integrate` merges and once at the end of the run (`final_gate_failed` if
that fails; nothing is rolled back). Integration gates measured at most `fast_gate_s` seconds (default 15;
from `init --discover-gates` or earlier runs; unmeasured gates are not used) also run after every merge, so no
task of any kind (a spike, a contract) merges a tree that fails them. A task whose integration gate also fails on
the integration tree without its change is BLOCKED, not retried, naming the task that merged the files the
output mentions. `gate_policy: full` runs the full suite after every merge.

Never hard-code a commit hash in a spec or `done_when`: the base moves. Every worker and gate gets
`MACC_BASE` (merge-base of `macc/integration` and the mainline) and `MACC_INTEGRATION` (integration
HEAD), e.g. `git diff --quiet "$MACC_BASE" -- vendor/`.

## Roadmap

See `docs/DESIGN.md` for the full design and `docs/ROADMAP.md` for what is next
(code-dependency graph for impact analysis, cost model for split/no-split, benchmark harness).
