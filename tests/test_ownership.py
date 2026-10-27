"""v0.4.0: plan-time ownership gaps, gate failures outside owns, scheduler-run heavy checks."""
import json
import subprocess
import time

import pytest

from macc.graph import Kind, State, Task, TaskGraph
from macc.ownership import (failure_files, gap_warnings, outside_owns, ownership_gaps, spec_identifiers)
from macc.scheduler import Scheduler, SchedulerConfig
from macc.store import Store
from macc.workers import MockWorker
from macc.worktree import Repo


def _git_repo(root, files: dict):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()


# ------------------------------------------------------------------ 1. plan-time ownership gaps
def test_spec_identifiers_takes_constants_symbols_and_operation_names_not_paths():
    spec = ("Add set_comment and set_inline_comment to GHIDRA_INVENTORY_OPERATIONS in "
            "src/ghidra/GhidraInventoryValues.ts; export limitationsFor and GhidraCommentRequest. "
            "Keep done_when and $MACC_BASE as they are; see docs/specs/x_y.md. A Note.")
    ids = spec_identifiers(spec)
    assert {"set_comment", "set_inline_comment", "GHIDRA_INVENTORY_OPERATIONS", "limitationsFor",
            "GhidraCommentRequest"} <= ids
    assert not {"GhidraInventoryValues", "done_when", "MACC_BASE", "x_y", "Note"} & ids


def _plan(owns_integrate=("src/**",)):
    return TaskGraph([
        Task("contract", "c", kind=Kind.CONTRACT.value, owns=["src/ghidra/inventory.ts"],
             spec="Add set_comment to GHIDRA_INVENTORY_OPERATIONS and export verifyGhidraComments."),
        Task("impl", "i", deps=["contract"], owns=["src/ghidra/bridge.ts", "scripts/lib/ghidra-comments.mjs"],
             done_when=["true"], spec="Implement set_comment in the bridge."),
        Task("integrate", "g", kind=Kind.INTEGRATE.value, deps=["impl"], owns=list(owns_integrate)),
    ])


def _files(tmp_path):
    files = {
        "src/ghidra/inventory.ts": "export const GHIDRA_INVENTORY_OPERATIONS = []\n",
        "scripts/verify-ghidra-windows.mjs": "for (const op of GHIDRA_INVENTORY_OPERATIONS) check(op)\n",
        "scripts/verify-ghidra-inventory.mjs": "probe('set_comment')\n",
        "scripts/lib/ghidra-comments.mjs": "verifyGhidraComments()\n",              # owned by impl: no gap
        "scripts/hopper-basics.mjs": "hopper.set_comment()\n",
        "docs/ghidra.md": "The inventory has 26 operations; set_comment is Hopper-only.\n",
        "src/ghidra/client.test.ts": "expect(GHIDRA_INVENTORY_OPERATIONS).toHaveLength(26)\n",
        "src/ghidra/other.ts": "GHIDRA_INVENTORY_OPERATIONS\n",                     # not a test: not scanned
        "tests/unrelated.test.ts": "nothing here\n",
    }
    return tmp_path / "r", files


def test_ownership_gaps_lists_unowned_enumerations_most_likely_first(tmp_path):
    root, files = _files(tmp_path)
    tracked = _git_repo(root, files)
    gaps = ownership_gaps(_plan(owns_integrate=["src/ghidra/inventory.ts"]), root, tracked)
    got = [x["file"] for x in gaps]
    assert set(got) == {"scripts/verify-ghidra-windows.mjs", "scripts/verify-ghidra-inventory.mjs",
                        "scripts/hopper-basics.mjs", "docs/ghidra.md", "src/ghidra/client.test.ts"}
    # a constant in a file next to the plan's own ("ghidra") outranks an operation name next door (hopper)
    assert got.index("scripts/verify-ghidra-windows.mjs") < got.index("scripts/hopper-basics.mjs")
    assert got.index("scripts/verify-ghidra-inventory.mjs") < got.index("scripts/hopper-basics.mjs")
    assert all(x["suggest"] == "integrate" for x in gaps)
    w = gap_warnings(gaps, limit=2)[0]
    assert "5 file(s) nobody owns" in w and "integrate's owns" in w and "3 weaker matches" in w


def test_owned_files_and_generic_identifiers_are_not_gaps(tmp_path, monkeypatch):
    import macc.ownership as own
    root, files = _files(tmp_path)
    tracked = _git_repo(root, files)
    assert ownership_gaps(_plan(owns_integrate=["scripts/**", "docs/**", "src/**"]), root, tracked) == []
    monkeypatch.setattr(own, "MAX_HITS_PER_IDENT", 2)      # set_comment (3 files) and the constant (2) ...
    gaps = ownership_gaps(_plan(owns_integrate=["src/ghidra/inventory.ts"]), root, tracked)
    assert all("set_comment" not in x["idents"] for x in gaps)


def test_plan_load_warns_about_gaps_and_records_them(tmp_path, capsys):
    from macc.cli import main
    root, files = _files(tmp_path)
    _git_repo(root, files)
    plan = tmp_path / "p.yaml"
    plan.write_text(json.dumps({"tasks": [
        {"id": t.id, "title": t.title, "kind": t.kind, "deps": t.deps, "owns": t.owns, "spec": t.spec,
         "done_when": t.done_when} for t in _plan(owns_integrate=["src/ghidra/inventory.ts"]).tasks.values()]}))
    assert main(["--repo", str(root), "plan", str(plan)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: 5 file(s) nobody owns" in out and "scripts/verify-ghidra-windows.mjs" in out
    ev = Store(root / ".macc" / "state.db").latest_event("ownership_gaps")
    assert "scripts/verify-ghidra-inventory.mjs" in ev["payload"]["files"] and ev["payload"]["suggest"] == "integrate"
    assert main(["--repo", str(root), "plan", str(plan), "--gaps"]) == 0
    assert "5 unowned file(s)" in capsys.readouterr().out


async def test_planner_prompt_has_the_enumeration_rule_and_returns_gap_warnings(tmp_path):
    from macc.planner import PLANNER_PROMPT, generate_plan
    assert "Enumerations break silently" in PLANNER_PROMPT
    root = tmp_path / "r"
    _git_repo(root, {"scripts/verify.mjs": "listOps(SHARED_SCHEMA_NAME)\n", "README.md": "x\n"})

    class Planner(MockWorker):            # the mock plan names SHARED_SCHEMA_NAME in its contract spec
        async def __call__(self, task, wt, pack):
            res = await super().__call__(task, wt, pack)
            res.output_text = res.output_text.replace("Write api/openapi.yaml.",
                                                      "Write api/openapi.yaml with SHARED_SCHEMA_NAME.")
            return res

    pr = await generate_plan("feature", root, Planner(), tmp_path / "p.yaml")
    assert "SHARED_SCHEMA_NAME" in (tmp_path / "p.yaml").read_text()
    assert any("scripts/verify.mjs: SHARED_SCHEMA_NAME" in w for w in pr.warnings)


# ------------------------------------------------------------------ 2. gate failures outside owns
TRACKED = {"scripts/verify-inventory.mjs", "scripts/run-turbo.mjs", "src/a.ts", "src/a.test.ts", ".github/x.yml"}


def test_failure_files_reads_frames_lines_and_fail_lines_only():
    out = ("\x1b[33mscripts/run-turbo.mjs\x1b[0m built dist/cli/x.js\n"          # build log: no line, not a locus
           "Error: drifted\n    at check (file:///Users/k/wt/integrate/scripts/verify-inventory.mjs:37:5)\n"
           "    at node:internal/process (node:internal/modules/run_main:1:1)\n"
           "    at x (/tmp/other/dist/cli/x.js:9:1)\n")                          # untracked build output
    assert failure_files(out, TRACKED) == ["scripts/verify-inventory.mjs"]
    assert failure_files("src/a.ts(12,3): error TS2322", TRACKED) == ["src/a.ts"]
    assert failure_files(" FAIL  src/a.test.ts > case\n", TRACKED) == ["src/a.test.ts"]
    assert failure_files("./.github/x.yml:4 bad key", TRACKED) == [".github/x.yml"]
    assert failure_files("3 tests failed", TRACKED) == []


def test_outside_owns_only_when_every_failure_file_is_outside():
    frame = "at f (file:///w/scripts/verify-inventory.mjs:37:5)\n"
    assert outside_owns(frame, TRACKED, ["src/**"]) == ["scripts/verify-inventory.mjs"]
    assert outside_owns(frame + "src/a.ts:3 boom", TRACKED, ["src/**"]) == []       # one is ours: retry helps
    assert outside_owns("no locations", TRACKED, ["src/**"]) == []


@pytest.fixture
def repo(tmp_path) -> Repo:
    root = tmp_path / "proj"
    _git_repo(root, {"README.md": "x\n", "scripts/verify.mjs": "throw new Error('x')\n"})
    return Repo(root)


async def _run(repo, tasks, **cfg):
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks)
    g.validate()
    events = []
    s = Scheduler(repo, store, g, MockWorker(), SchedulerConfig(**cfg),
                  on_event=lambda t, tid, p: events.append((t, tid, p)))
    summary = await s.run()
    return store, g, events, summary


FAIL_IN_VERIFIER = "node -e 0; echo '    at main (file://'\"$PWD\"'/scripts/verify.mjs:1:7)' >&2; exit 1"


async def test_done_when_failing_outside_owns_blocks_without_retry(repo):
    tasks = [Task("g", "g", owns=["x/**"], done_when=[FAIL_IN_VERIFIER], max_attempts=2),
             Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["g"], owns=["x/**"])]
    store, g, events, summary = await _run(repo, tasks)
    t = g.tasks["g"]
    assert t.state == State.BLOCKED.value and t.attempts == 1
    assert "outside this task's owns" in t.last_error and "macc change integrate --owns" in t.last_error
    ev = [p for typ, tid, p in events if typ == "fails_outside_owns"]
    assert ev == [{"cmd": FAIL_IN_VERIFIER, "files": ["scripts/verify.mjs"], "suggest": "integrate"}]
    assert not [1 for typ, tid, p in events if typ == "retry"]


async def test_done_when_failing_in_owned_file_still_retries(repo):
    cmd = "echo '    at main (file://'\"$PWD\"'/x/a.mjs:1:7)' >&2; mkdir -p x; touch x/a.mjs; exit 1"
    tasks = [Task("g", "g", owns=["x/**", "scripts/**"], done_when=[FAIL_IN_VERIFIER], max_attempts=2),
             Task("h", "h", owns=["y/**"], done_when=["echo 'y/never.mjs:3 boom'; exit 1"], max_attempts=2)]
    store, g, events, summary = await _run(repo, tasks, max_parallel=2)
    assert g.tasks["g"].state == State.FAILED.value and g.tasks["g"].attempts == 2   # its own file: retry helps
    assert g.tasks["h"].state == State.FAILED.value                                  # untracked path: no locus
    assert not [1 for typ, tid, p in events if typ == "fails_outside_owns"]


async def test_integration_gate_failing_outside_owns_blocks_the_task(repo):
    tasks = [Task("g", "g", owns=["x/**"], max_attempts=2)]
    store, g, events, summary = await _run(repo, tasks, integration_gates=[FAIL_IN_VERIFIER.replace(
        "exit 1", "test ! -f x/g.txt")], gate_policy="full")
    assert g.tasks["g"].state == State.BLOCKED.value and g.tasks["g"].attempts == 1
    assert ("fails_outside_owns", "g") in [(typ, tid) for typ, tid, p in events]
    assert not (repo.integration_path / "x" / "g.txt").exists()                  # merge reverted


# ------------------------------------------------------------------ 3. heavy checks: measured, not repeated
def _handoff(store, tid, attempt, gates):
    store.save_handoff(tid, attempt, "in", "out", {"gates": gates})


def test_command_seconds_medians_handoffs_then_discovery(tmp_path):
    store = Store(tmp_path / ".macc" / "state.db")
    _handoff(store, "a", 1, [{"cmd": "verify", "seconds": 230.0}, {"cmd": "lint", "seconds": 5.0}])
    _handoff(store, "a", 2, [{"cmd": "verify", "seconds": 250.0}, {"cmd": "lint", "seconds": 999, "shared": True}])
    _handoff(store, "b", 1, [{"cmd": "verify", "seconds": 240.0}])
    (tmp_path / ".macc" / "gate_discovery.json").write_text(json.dumps({"timed": [
        {"cmd": "fmt", "seconds": 0.6, "ok": True}, {"cmd": "lint", "seconds": 50, "ok": True},
        {"cmd": "slow", "seconds": 300, "ok": False}]}))
    secs = store.command_seconds()
    assert secs == {"verify": 240.0, "lint": 5.0, "fmt": 0.6}     # handoffs win; a failed discovery is no timing
    now = time.time()
    assert "verify" not in store.command_seconds(exclude_window=(now - 60, now + 60))


def test_predict_adds_measured_done_when_time(tmp_path):
    from macc.costmodel import predict
    store = Store(tmp_path / "s.db")
    g = TaskGraph([Task("i", "i", kind=Kind.INTEGRATE.value, done_when=["verify", "unknown-cmd"], est_minutes=1)])
    base = predict(g, store, command_s={})
    with_gate = predict(g, store, command_s={"verify": 240.0})
    assert with_gate["per_task"]["i"]["done_when_s"] == 240.0
    assert with_gate["wall_s"] == pytest.approx(base["wall_s"] + 240.0)


def test_context_pack_names_heavy_checks_the_worker_must_not_run(tmp_path):
    from macc.handoff import build_context_pack
    store = Store(tmp_path / "s.db")
    _handoff(store, "old", 1, [{"cmd": "npm run verify:ghidra", "seconds": 240.0},
                               {"cmd": "npm run check:fast", "seconds": 6.0}])
    t = Task("integrate", "i", kind=Kind.INTEGRATE.value, done_when=["npm run verify:ghidra", "npm run check:fast"])
    pack = build_context_pack(t, TaskGraph([t]), store, gate_cmds=["npm run check:fast", "npm run format:check"])
    assert "# Checks the scheduler runs for you" in pack
    assert "Do NOT run these yourself" in pack and "`npm run verify:ghidra` (~240 s)" in pack
    assert "`npm run check:fast` (~" not in pack                       # light: run it as usual
    assert "['npm run check:fast', 'npm run format:check']" in pack
    light = Task("c", "c", done_when=["npm run check:fast"])
    assert "Do NOT run" not in build_context_pack(light, TaskGraph([light]), store)
