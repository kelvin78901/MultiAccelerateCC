"""Planner: ONE model call that turns a feature description into a plan YAML.

The model proposes the graph; the program validates it (cycles, unknown deps, contract-before-fanout,
ownership overlap between tasks that could run concurrently) and the human reviews the file before
`macc plan`. The planner never runs tasks and never decides scheduling.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from .graph import Kind, Task, TaskGraph
from .workers import WorkerFn

PLAN_BEGIN = "<<<MACC_PLAN"
PLAN_END = "MACC_PLAN>>>"

PLANNER_PROMPT = """# Role
You are the planner for macc, a deterministic task scheduler that runs Claude Code workers in parallel,
each in its own git worktree. You produce a task graph as YAML. You do NOT implement anything.
Inspect the repository (read-only) as needed before planning.

# Rules for a good plan
1. Contract first. Anything two tasks must agree on (API schema, data model, module interfaces,
   test plan) is its own task with `kind: contract`, and every task that depends on that interface lists it
   in `deps`. Contracts are small and precise.
2. Ownership. Every task lists `owns`: path globs it may write. Two tasks that can run at the same time
   must not overlap. Shared files (schemas, migrations, config) belong to exactly one task.
3. Unknowns become spikes. If something might not work (hardware, a library, performance), add a
   `kind: spike` task whose output is a conclusion document, and make dependent tasks depend on it.
4. Every code task has `done_when`: shell commands (exit 0 = done) that prove it, e.g. a targeted test
   command. Prefer tests that exist or that the task itself creates.
5. Stubs are allowed only where a task says `allow_stubs: true`; the final `kind: integrate` task
   must resolve all debts. End with a `kind: review` task depending on integrate.
6. Keep tasks between 10 and 60 estimated minutes; split bigger ones. 4 to 12 tasks total.
7. effort: contract/integrate/review `high`; ordinary code `medium`; trivial `low`.
8. est_tokens is total tokens for the task (typical: contract 40k, code 60k-150k, review 50k).

# Output
Emit exactly one YAML document between the markers, nothing else after it:
{BEGIN}
config:
  max_parallel: <int>
  token_budget: <int>
  integration_gates: ["<command that runs the whole test suite>"]
defaults:
  effort: medium
  max_attempts: 2
tasks:
  - id: <snake_case>
    title: <short>
    kind: contract|code|spike|integrate|review
    deps: [<ids>]
    owns: ["<glob>", ...]
    effort: low|medium|high
    est_minutes: <int>
    est_tokens: <int>
    allow_stubs: false
    done_when: ["<cmd>"]
    spec: |
      <precise instructions for a worker that has no other context>
{END}

# Feature to plan
{DESCRIPTION}
"""


class PlanError(ValueError):
    pass


def extract_yaml(text: str) -> str:
    m = re.search(re.escape(PLAN_BEGIN) + r"(.*?)" + re.escape(PLAN_END), text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"```ya?ml\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    raise PlanError("no plan block found in planner output")


def validate_plan(data: dict) -> TaskGraph:
    if not isinstance(data, dict) or "tasks" not in data:
        raise PlanError("plan must be a mapping with a `tasks` list")
    defaults = data.get("defaults") or {}
    tasks = []
    for item in data["tasks"]:
        merged = {**defaults, **item}
        unknown = set(merged) - set(Task.__dataclass_fields__)
        if unknown:
            raise PlanError(f"task {item.get('id')}: unknown fields {sorted(unknown)}")
        if merged.get("kind", "code") not in {k.value for k in Kind}:
            raise PlanError(f"task {item.get('id')}: bad kind {merged.get('kind')}")
        tasks.append(Task(**merged))
    g = TaskGraph(tasks)
    g.validate()
    # concurrently-runnable tasks must not share ownership
    for a in g.tasks.values():
        for b in g.tasks.values():
            if a.id < b.id and g.owns_overlap(a, b) and a.id not in g.ancestors(b.id) and b.id not in g.ancestors(a.id):
                raise PlanError(f"{a.id} and {b.id} may run concurrently but their `owns` overlap")
    code_without_gates = [t.id for t in g.tasks.values() if t.kind == Kind.CODE.value and not t.done_when]
    if code_without_gates:
        raise PlanError(f"code tasks without done_when: {code_without_gates}")
    g.refresh_hashes()
    return g


async def generate_plan(description: str, repo_root: Path, worker: WorkerFn, out: Path,
                        effort: str = "high", model: Optional[str] = None, max_rounds: int = 2) -> TaskGraph:
    """Call the planner worker; on validation failure feed the error back once."""
    prompt = PLANNER_PROMPT.replace("{BEGIN}", PLAN_BEGIN).replace("{END}", PLAN_END).replace("{DESCRIPTION}", description)
    task = Task(id="_plan", title="plan", kind="plan", effort=effort, model=model, owns=[])
    last_err = ""
    for _ in range(max_rounds):
        p = prompt + (f"\n\n# Your previous plan was rejected\n{last_err}\nFix it and emit the full plan again.\n" if last_err else "")
        res = await worker(task, repo_root, p)
        if not res.ok:
            raise PlanError(f"planner call failed: {res.error}")
        try:
            text = extract_yaml(res.output_text)
            data = yaml.safe_load(text)
            g = validate_plan(data)
        except (PlanError, yaml.YAMLError) as e:
            last_err = str(e)
            continue
        out.write_text(text + "\n")
        return g
    raise PlanError(f"planner could not produce a valid plan: {last_err}")
