# MultiAccelerateCC

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

## What it does today (v0.2)

- **Planner in one call**: `macc plan --from "feature description"` inspects the repo read-only and emits a plan
  YAML; the program validates it (cycles, contract-first, ownership overlap, missing gates) and you review it.
- **Ownership is enforced, not suggested**: a PreToolUse hook blocks edits outside a task's `owns`.
- **Independent review with teeth**: a `review` task's REJECT reopens the reviewed tasks with the notes; the
  loop is bounded by the review task's `max_attempts`.
- **Merge agent**: textual conflicts go to a worker that sees only the conflicting files plus both handoffs.
- **Broken environment ≠ broken task**: a gate that fails on the baseline blocks the task without charging an attempt;
  CLI start-up failures (stdin race, rate limit) are retried without charging either.
- **Scoped commits**: only owned paths are committed; build artifacts are discarded; anything else a worker
  touched (e.g. via Bash) fails the attempt.
- **`merge_after`**: tests run in parallel with the implementation but merge only after it, so the
  integration gate never runs new tests against old code.

- **Parallel fan-out** with a WIP limit; independent tasks never wait for each other.
- **Contract-first**: tasks of kind `contract` must precede anything touching the same paths.
- **Ownership**: tasks declare the paths they may write; overlapping tasks are serialised.
- **Build-system staleness**: every task has an input hash. Change a spec or an upstream output
  and downstream tasks become STALE automatically; running ones are preempted (no attempt charged).
- **Revalidation**: a STALE task whose gates still pass is DONE again without a rerun.
- **Merge in topological order** into `macc/integration`, with gates after every merge and
  automatic revert when they fail. Conflicts are detected and the task is retried on a fresh base.
- **Handoffs generated from facts** (git diff, API-looking lines, gate results, stub markers); the
  model writes only `open_questions`, `notes_for_downstream`, `stubs`.
- **Debt registry**: every stub or minimal fix is recorded and shown to downstream tasks.
- **Decisions** (ADR-style) injected into every later worker's context so changes aren't undone.
- **Crash-safe**: SQLite holds the graph, events, handoffs, usage. `macc run` resumes.
- **Budget**: stop dispatching when a token budget would be exceeded. Usage per kind feeds estimates.
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
macc debts
macc events --follow                               # in a second terminal
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
that fails; nothing is rolled back). `gate_policy: full` runs the full suite after every merge.

Never hard-code a commit hash in a spec or `done_when`: the base moves. Every worker and gate gets
`MACC_BASE` (merge-base of `macc/integration` and the mainline) and `MACC_INTEGRATION` (integration
HEAD), e.g. `git diff --quiet "$MACC_BASE" -- vendor/`.

## Roadmap

See `docs/DESIGN.md` for the full design and `docs/ROADMAP.md` for what is next
(code-dependency graph for impact analysis, cost model for split/no-split, benchmark harness).
