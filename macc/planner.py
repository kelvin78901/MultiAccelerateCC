"""Planner: ONE model call that turns a feature description into a plan YAML.

The model proposes the graph; the program validates it (cycles, unknown deps, contract-before-fanout,
ownership overlap between tasks that could run concurrently) and the human reviews the file before
`macc plan`. The planner never runs tasks and never decides scheduling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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
   in `deps`. Contracts are small and precise: interface signatures, data models, error codes and rules,
   and a test plan that says WHAT to test (cases, boundaries), never the expected values. Do not ask the
   contract to compute expected outputs or to hand-design fixture data: it is a serial node that every
   other task waits for. Test tasks compute expected values themselves; they run in parallel anyway.
   A contract's est_minutes is at most 8 and its output stays around 10k tokens or less.
2. Ownership. Every task lists `owns`: path globs it may write. Two tasks that can run at the same time
   must not overlap. Shared files (schemas, migrations, config) belong to exactly one task.
3. Unknowns become spikes. If something might not work (hardware, a library, performance), add a
   `kind: spike` task whose output is a conclusion document, and make dependent tasks depend on it.
4. Every code task has `done_when`: shell commands (exit 0 = done) that prove it, e.g. a targeted test
   command. Prefer tests that exist or that the task itself creates.
5. Stubs are allowed only where a task says `allow_stubs: true`; the final `kind: integrate` task
   must resolve all debts. End with a `kind: review` task depending on integrate.
6. est_minutes is expected worker minutes (use the measured history below, not human time). Split a
   task only when the parts can really run in parallel. 4 to 12 tasks total.
7. effort: contract/integrate/review `high`; ordinary code `medium`; trivial `low`.
8. est_tokens is total tokens for the task (typical: contract 40k, code 60k-150k, review 50k).
9. Tests in parallel, merged in order: a test task may run at the same time as the implementation it
   tests, but must list that implementation task under `merge_after` (NOT `deps`) so its tests are
   merged only once the implementation is in. Otherwise the integration gate fails on the old code.
9b. Fixture data shared by several test tasks is its own `kind: code` task (`deps: [<contract>]`,
   owning the fixture files / conftest additions), running in parallel with the implementations. Test
   tasks list it under `merge_after` together with the implementation they test. The contract only names
   the fixtures (file paths, fixture names, what they must contain), it does not write them.
10. Model per task: `model: sonnet` for ordinary code and tests; `model: opus` only for contract,
   integrate and review. Put `model: sonnet` in defaults.

11. Review verdict vocabulary is fixed: the review task's spec must ask for `VERDICT: APPROVE` or
    `VERDICT: REJECT` (nothing else), and its done_when must not grep for other words.
12. A contract task's done_when must check structure (parse the schema file, import the interface
    module), not grep for keywords you yourself chose.
13. If the repository already contains some or all of what the description asks for, plan the delta
    and say so in `config.scope_note` (one sentence). Never silently change the scope.

# Output
Emit exactly one YAML document between the markers, nothing else after it:
{BEGIN}
config:
  max_parallel: <int>
  cost_budget_usd: <float>
  scope_note: "<only if the plan deviates from the description; else omit>"
  integration_gates: ["<command that runs the whole test suite>"]
defaults:
  effort: medium
  model: sonnet
  max_attempts: 2
tasks:
  - id: <snake_case>
    title: <short>
    kind: contract|code|spike|integrate|review
    deps: [<ids>]
    merge_after: [<ids>]      # optional; see rule 9
    owns: ["<glob>", ...]
    effort: low|medium|high
    est_minutes: <int>
    est_tokens: <int>
    allow_stubs: false
    done_when: ["<cmd>"]
    spec: |
      <precise instructions for a worker that has no other context>
{END}

# Repository state (the integration branch, where workers run; NOT necessarily what `main` shows)
Files currently present:
{FILES}

Existing integration gate (reuse it unless there is a reason not to): {GATE}
Rules for commands in `done_when` / `integration_gates`:
- They run via the shell with no templating: write them literally (no `%%`, no `$VAR` you did not set,
  except the two below).
- Never write a commit hash (in a spec or a command): the base moves. Use `$MACC_BASE` (merge-base of
  the integration branch and mainline: "everything this work changed" is `git diff $MACC_BASE`) and
  `$MACC_INTEGRATION` (integration HEAD). macc exports both to every worker and gate.
- Never use a bare `python`/`python3`: it is whatever is first on the operator's PATH. Use the same
  interpreter the existing gate uses, or `python3 -c` only for pure-stdlib checks that need no packages.
- A task that replaces or extends an existing file must say so explicitly in its spec, name the file,
  and own it. Do not assume an empty repository.
- If a check must verify "stdlib only", test against `sys.stdlib_module_names`, never a hand-written list.
- Do not assert "nothing changed under X since $MACC_BASE" for paths an upstream task (a dep) writes: once
  that task merges, the check can never pass. A task's own scope is already enforced by its `owns` (only
  owned paths are committed); guard with `git diff --quiet $MACC_BASE -- <paths>` only for paths that no
  task in the plan owns.

# Measured history in this repo (worker time per run, from macc's usage table)
{HISTORY}

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


BARE_PY = re.compile(r"(?:^|[\s;&|(])python3?(?:\s|$)")
PURE_STDLIB_USE = re.compile(r"python3?\s+(?:-c\s|-m\s+(?:py_compile|compileall|ast|json\.tool)\b)")
MODIFY_WORDS = re.compile(r"\b(modify|modifies|extend|extends|replace|replaces|overwrite|overwrites|update|updates|"
                          r"rewrite|rewrites|existing|already exists|refactor|edit|edits|append)\b", re.I)
# 7-40 hex chars with at least one digit and one a-f letter: a commit-ish, not `1048576` or `deadbeef`
COMMIT_HASH = re.compile(r"(?<![0-9A-Za-z_])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z_])")
FOREIGN_VERDICTS = re.compile(r"request(?:ed)?\s+changes|needs?\s+work|LGTM|PASS\b|FAIL\b|ACCEPT\b|DECLINE", re.I)


def plan_warnings(g: TaskGraph, existing_files: set[str] | None = None, gates: list[str] | None = None) -> list[str]:
    """Non-fatal problems a human should see before running."""
    warns: list[str] = []
    def bare(cmd: str) -> bool:
        return bool(BARE_PY.search(cmd)) and not PURE_STDLIB_USE.search(cmd)

    for cmd in list(gates or []):
        if bare(cmd):
            warns.append(f"integration gate uses a bare python interpreter: {cmd!r}")
    for t in g.tasks.values():
        for cmd in t.done_when:
            if bare(cmd):
                warns.append(f"{t.id}: done_when uses a bare python interpreter (not a pure-stdlib -c/-m py_compile use): {cmd!r}")
            if "%%" in cmd:
                warns.append(f"{t.id}: done_when contains '%%' (no formatting is applied; probably a bug): {cmd!r}")
        hashes = sorted({h for text in [t.spec, *t.done_when] for h in COMMIT_HASH.findall(text)})
        if hashes:
            warns.append(f"{t.id}: spec/done_when hard-codes commit hash(es) {hashes[:3]}; they go stale when the "
                         "base moves. Use $MACC_BASE / $MACC_INTEGRATION")
        if existing_files and t.kind != Kind.INTEGRATE.value:   # integrate owning everything is by design
            for g_ in t.owns:
                hits = sorted(f for f in existing_files if _glob_hit(f, g_))
                if hits and not MODIFY_WORDS.search(t.spec):
                    warns.append(f"{t.id}: owns {g_!r} which already contains {hits[:3]}, but the spec never says it "
                                 "modifies/extends/replaces existing files (it would silently overwrite them)")
        if t.kind == Kind.REVIEW.value:
            blob = t.spec + " ".join(t.done_when)
            if FOREIGN_VERDICTS.search(blob):
                warns.append(f"{t.id}: review spec/done_when uses a verdict vocabulary other than APPROVE/REJECT; "
                             "macc only acts on `VERDICT: APPROVE|REJECT` (REQUEST CHANGES is read as REJECT, "
                             "but a done_when grepping for other words will fail)")
        if t.kind == Kind.CONTRACT.value and t.done_when and all(("grep" in c or "test -f" in c) for c in t.done_when):
            warns.append(f"{t.id}: contract done_when only greps/stat-checks its own output; prefer a structural check "
                         "(parse the schema, import the stub module)")
    return warns


def _glob_hit(path: str, glob: str) -> bool:
    import fnmatch
    if fnmatch.fnmatch(path, glob):
        return True
    d = glob.rstrip("/*")
    return bool(d) and (path == d or path.startswith(d + "/"))


# `git diff --quiet $MACC_BASE -- p1 p2 ...` (quotes/braces around the variable allowed)
_UNCHANGED_GUARD = re.compile(r"git\s+diff\s+(?:--\S+\s+)*[\"']?\$\{?MACC_BASE\}?[\"']?\s+(?:--\S+\s+)*--\s+([^&|;]+)")


def impossible_guards(g: TaskGraph) -> list[str]:
    """done_when checks that some path is unchanged since $MACC_BASE while an ancestor task owns it."""
    from .graph import _paths_overlap
    out = []
    for t in g.tasks.values():
        for cmd in t.done_when:
            for m in _UNCHANGED_GUARD.finditer(cmd):
                paths = [x.strip("'\"") for x in m.group(1).split() if not x.startswith(":(")]
                for a in sorted(g.ancestors(t.id)):
                    # a bare path in `git diff -- p` covers the file p and everything under directory p
                    hit = [p for p in paths for o in g.tasks[a].owns
                           if _paths_overlap(p.rstrip("/"), o) or _paths_overlap(p.rstrip("/") + "/**", o)]
                    if hit:
                        out.append(f"{t.id}: done_when asserts {sorted(set(hit))} unchanged since $MACC_BASE, but "
                                   f"upstream task {a} owns them; the check fails once {a} merges")
    return out


CONTRACT_MAX_MINUTES = 8
# a contract that pre-computes answers becomes a long serial node (AutoLeaseRank /insights: 22 min of 28)
_EXPECTED = re.compile(r"\bexpected\s+(?:outputs?|values?|results?|answers?|numbers?)\b"
                       r"(?!\s+(?:format|shape|schema|type|model)s?\b)", re.I)
_EXPECTED_OK = re.compile(r"\b(?:not|no|never|don'?t|without|instead of)\b|\btests?\b[^.]{0,30}\b(?:compute|derive|calculate)",
                          re.I)


def asks_for_expected_values(spec: str) -> Optional[str]:
    """The sentence of a contract spec that asks for expected values, if any (negations and "the tests
    compute ..." are fine)."""
    for sentence in re.split(r"(?<=[.;:!?])\s+|\n\s*\n", spec):
        if _EXPECTED.search(sentence) and not _EXPECTED_OK.search(sentence):
            return sentence.strip()[:160]
    return None


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
    for t in g.tasks.values():
        if t.kind != Kind.CONTRACT.value:
            continue
        if t.est_minutes > CONTRACT_MAX_MINUTES:
            raise PlanError(f"contract {t.id}: est_minutes {t.est_minutes} > {CONTRACT_MAX_MINUTES}; a contract is "
                            "interfaces and rules only (move fixtures and expected values to parallel tasks)")
        bad = asks_for_expected_values(t.spec)
        if bad:
            raise PlanError(f"contract {t.id}: spec asks for expected values ({bad!r}); the contract's test "
                            "plan says what to test, the test tasks compute expected values")
    bad_guards = impossible_guards(g)
    if bad_guards:
        raise PlanError("; ".join(bad_guards))
    code_without_gates = [t.id for t in g.tasks.values() if t.kind == Kind.CODE.value and not t.done_when]
    if code_without_gates:
        raise PlanError(f"code tasks without done_when: {code_without_gates}")
    g.refresh_hashes()
    return g


def format_history(history: Optional[dict]) -> str:
    """Calibration lines for the planner; kinds with too few samples are already left out by the store."""
    if not history:
        return "- (no history yet: estimate as usual)"
    lines = []
    for kind, h in sorted(history.items()):
        bits = []
        if "median_seconds" in h:
            bits.append(f"median {h['median_seconds']:.0f} s of worker time per run ({h['runs']} runs)")
        if "median_actual_over_est" in h:
            bits.append(f"median actual/est_minutes = {h['median_actual_over_est']:.3f} ({h['tasks']} tasks)")
        lines.append(f"- {kind}: " + "; ".join(bits))
    lines.append("Set est_minutes from these medians (actual worker minutes), not from how long a human would take. "
                 "A ratio far below 1 means past estimates were too high.")
    return "\n".join(lines)


@dataclass
class PlanResult:
    graph: TaskGraph
    warnings: list[str]
    config: dict
    usage: list                      # WorkerResult per round


async def generate_plan(description: str, workdir: Path, worker: WorkerFn, out: Path,
                        effort: str = "high", model: Optional[str] = None, max_rounds: int = 2,
                        existing_files: Optional[list[str]] = None, current_gate: str = "",
                        history: Optional[dict] = None) -> PlanResult:
    """Call the planner worker inside `workdir` (the integration worktree, where workers will run);
    on validation failure feed the error back once. Returns the graph plus non-fatal warnings."""
    files = existing_files if existing_files is not None else sorted(
        str(p.relative_to(workdir)) for p in workdir.rglob("*")
        if p.is_file() and ".git" not in p.parts and ".macc" not in p.parts)
    listing = "\n".join(f"- {f}" for f in files[:300]) or "- (empty)"
    if len(files) > 300:
        listing += f"\n- ... {len(files) - 300} more"
    prompt = (PLANNER_PROMPT.replace("{BEGIN}", PLAN_BEGIN).replace("{END}", PLAN_END)
              .replace("{DESCRIPTION}", description).replace("{FILES}", listing)
              .replace("{GATE}", current_gate or "(none configured)")
              .replace("{HISTORY}", format_history(history)))
    task = Task(id="_plan", title="plan", kind="plan", effort=effort, model=model, owns=[])
    last_err = ""
    usage = []
    for _ in range(max_rounds):
        p = prompt + (f"\n\n# Your previous plan was rejected\n{last_err}\nFix it and emit the full plan again.\n" if last_err else "")
        res = await worker(task, workdir, p)
        usage.append(res)
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
        cfg = (data.get("config") or {}) if isinstance(data, dict) else {}
        warns = plan_warnings(g, set(files), cfg.get("integration_gates"))
        return PlanResult(graph=g, warnings=warns, config=cfg, usage=usage)
    raise PlanError(f"planner could not produce a valid plan: {last_err}")
