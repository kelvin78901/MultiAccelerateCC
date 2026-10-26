# Design

## 1. Principle: the scheduler is a program, the model is a function

| Decision | Who makes it | Mechanism |
|---|---|---|
| Which tasks can start | graph | Kahn ready set: PENDING and all deps DONE |
| Which starts first | graph | critical path length (sum of `est_minutes` on the longest downstream path), then cheapest |
| How many at once | scheduler | `max_parallel` + token budget |
| Can two run together | graph | ownership globs must not overlap |
| What happens on failure | scheduler | retry up to `max_attempts` with the error injected into the next context pack; then FAILED |
| What is affected by a change | graph | input hash = hash(spec, owns, done_when, deps' output hashes); mismatch ⇒ STALE / preempt |
| Merge order | scheduler | deps are DONE ⇒ already merged ⇒ topological by construction |
| Whether to split a task | *v0.2* cost model | split if contract + max(children) + merge + P(conflict)·rework < single run |
| What the code should be | model | worker with a context pack |
| Is the diff good | model | independent `review` task with read-only tools |

## 2. Lifecycle of a task

```
PENDING ─deps done─► READY ─dispatch─► RUNNING ─ok─► (commit, gates, handoff, rebase, merge, igates) ─► DONE
                                         │                                                           │
                                         ├─fail/conflict/gate─► PENDING (retry) ─► FAILED             │ upstream changed
                                         └─preempt (inputs changed)─► PENDING (no attempt charged)     ▼
                                                                                                    STALE ─gates pass─► DONE
                                                                                                          └─fail────► PENDING
```

## 3. Changes: requirements, test surprises, hardware limits

All three are the same event: *some node's spec or output changed*.

1. `macc change TASK --spec ... --why ...` rewrites the spec, records a decision, recomputes the hash.
2. `propagate_stale` walks descendants: DONE→STALE, RUNNING→preempt, FAILED→PENDING, PENDING untouched.
3. The next loop iteration revalidates STALE nodes by running their `done_when` on the integration
   tree. Passing nodes are DONE again at zero cost; failing nodes rerun as repair tasks (the worker
   sees the merged code and the decision record).
4. A `spike` task is how unknowns enter the graph: it produces a conclusion in `docs/spikes/`, and
   the planner (or you) then `change`s or `split`s downstream nodes based on it.

## 4. Conflicts

- **Prevent**: contracts first; ownership globs; shared files (schema, migrations) owned by one task.
- **Detect**: rebase onto integration, then `merge --no-ff`. Both abort cleanly on conflict.
- **Resolve** (v0.1): retry the task on the fresh base (the worker now sees the other change).
  v0.2 adds a merge agent for textual conflicts and escalation to planner for contract conflicts.
- **Semantic conflicts** (merges but gates fail): merge is reverted with `git reset --hard`, task retried.

## 5. Handoff and context

A worker gets a context pack, never a transcript:

```
# Project rules            (stable, from .macc/rules.md)        ┐ identical prefix every call
# How you work             (protocol: ownership, stubs, output)  ┘ ⇒ prompt-cache friendly
# Decisions in force
# Upstream handoffs        (generated: files, api lines, stubs, 2 LLM-written fields)
# Open debts
# Your task                (spec, owns, done_when)
# Previous attempt failed  (only on retries)
```

Truth order when documents disagree: code + tests > decisions > handoffs > rules > logs.

## 6. Stubs and debt

Any task may leave a stub if `allow_stubs: true` (and gets a warning event if not). The scheduler
records it in `debts`, shows it to downstream tasks, and the `integrate` task is told to resolve
every open debt. The project is not done while debts are open.

## 7. Effort policy

Effort is per task, not per session: `contract`/`review`/`integrate` high, ordinary `code`
medium, `max` only for a single reproduce-the-failure task. Done conditions are explicit so
workers stop re-verifying.

## 8. What is deliberately missing in v0.1

- No LLM planner: plans are YAML you (or a Claude session) write. `macc split` is manual.
- No code-dependency graph: impact analysis is by task graph only.
- No cost model for split/no-split.
- Conflict resolution is retry-on-fresh-base only.
- Review tasks produce a verdict but don't automatically reopen the reviewed tasks.
