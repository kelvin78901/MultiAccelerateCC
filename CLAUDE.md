# MultiAccelerateCC (macc)

Deterministic task-graph scheduler that drives Claude Code workers in parallel.

## Non-negotiables
- The scheduler never asks an LLM *what to run next*. Ready set, priority, staleness, retries,
  budget and conflicts are all computed in `macc/graph.py` and `macc/scheduler.py`.
- LLMs are called as functions: write a spec, write code, review a diff. Each call gets a
  short context pack (`macc/handoff.py`), never conversation history.
- Handoffs are generated from facts (git diff, gates). The model writes only
  `open_questions`, `notes_for_downstream`, `stubs`.
- Any stub or minimal fix must land in the debt registry (`store.add_debt`). Nothing disappears.

## Layout
- `macc/graph.py`     Task, TaskGraph: hashes, Kahn ready set, critical path, stale propagation
- `macc/store.py`     SQLite: tasks, events, handoffs, debts, decisions, usage
- `macc/worktree.py`  git worktrees, rebase/merge into `macc/integration`, gates
- `macc/handoff.py`   handoff generation + context pack
- `macc/workers.py`   ClaudeWorker (`claude -p --output-format json`), MockWorker
- `macc/scheduler.py` asyncio event loop
- `macc/plan.py`, `macc/cli.py`

## Dev
- `python -m pytest -q` must pass before any commit. Tests use MockWorker; no tokens.
- Python >= 3.10 (target machine runs 3.10). No `match`, no `X | Y` at runtime outside annotations.
- Keep this file short. Process goes in `docs/`, not here.
