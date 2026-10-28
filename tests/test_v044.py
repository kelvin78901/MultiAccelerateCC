"""v0.4.4: done_when derived by the program, contract error conventions, nearby exports in the context pack."""
import json
import subprocess

from macc.donewhen import derive_done_when, parse_patterns
from macc.graph import Kind, Task, TaskGraph
from macc.planner import PLANNER_PROMPT, contract_error_conventions

PATTERNS = [{"cmd": "npm run test:local -- {files}", "paths": ["src/**"]},
            {"cmd": "npm run test:focused -- {files}", "paths": ["tests/**"]},
            {"cmd": "node --check {file}", "paths": ["scripts/**"]}]
TRACKED = ["src/ghidra/inv.ts", "src/ghidra/inv.test.ts", "tests/boundary/mcp/routing.test.ts",
           "tests/boundary/mcp/other.test.ts", "tests/boundary/providers/ghidra/client.test.ts",
           "scripts/verify-real.mjs", "scripts/lib/real.mjs", "README.md"]
CFG = {"test_cmd_patterns": PATTERNS, "integration_gates": ["npm run check:fast && npm run format:check"]}
SECS = {"npm run verify:ghidra": 260.0, "npm run verify:bridge-sources": 1.7}
DISCOVERED = ["npm run verify:ghidra", "npm run verify:bridge-sources", "npm run knip"]


def _plan():
    return TaskGraph([
        Task("contract", "c", kind=Kind.CONTRACT.value, owns=["src/ghidra/inv.ts"], done_when=["test -s x"],
             spec="Add set_comment."),
        Task("bridge", "b", deps=["contract"], owns=["bridge/**", "scripts/verify-real.mjs"],
             done_when=["npm run verify:bridge-sources", "npm run verify:ghidra", "node --check scripts/verify-real.mjs"]),
        Task("tests", "t", deps=["contract"], owns=["src/ghidra/inv.test.ts", "tests/boundary/mcp/new.test.ts",
                                                   "tests/boundary/providers/ghidra/**"],    # bench D v0.4.1
             done_when=["npm run test:focused -- tests/boundary/providers/ghidra",          # a directory
                        "npm run test:local -- src/ghidra/inv.test.ts", "grep -q foo src/ghidra/inv.ts"]),
        Task("docs", "d", deps=["contract"], owns=["docs/**"], done_when=["npx prettier --check docs/x.md"]),
        Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["bridge", "tests", "docs"],
             owns=["src/**", "tests/**", "scripts/**"],
             done_when=["npm run verify:bridge-sources", "npm run check:fast && npm run format:check && rm -rf /"]),
    ])


def test_done_when_is_derived_from_owned_tests_and_the_whitelist():
    g = _plan()
    rewrites, notes = derive_done_when(g, CFG, SECS, DISCOVERED, TRACKED)
    t = g.tasks
    # a directory, a grep and an unlisted formatter are dropped; the owned files' tests are derived instead
    assert t["tests"].done_when == ["npm run test:local -- src/ghidra/inv.test.ts",
                                    "npm run test:focused -- tests/boundary/mcp/new.test.ts "
                                    "tests/boundary/providers/ghidra/client.test.ts"]
    assert any(n.startswith("dropped: tests: 'npm run test:focused -- tests/boundary/providers/ghidra'")
               for n in notes)
    assert any(n.startswith("dropped: tests: 'grep -q foo") for n in notes)
    # heavy and not a gate: removed from the worker, moved to integrate; quick own check and node --check stay
    assert t["bridge"].done_when == ["npm run verify:bridge-sources", "node --check scripts/verify-real.mjs"]
    assert "npm run verify:ghidra" in t["integrate"].done_when
    assert any(n.startswith("moved to integrate: bridge: 'npm run verify:ghidra'") for n in notes)
    # nothing targeted left: the quickest integration gate
    assert t["docs"].done_when == ["npm run check:fast"]
    # integrate: chained gates judged part by part; the unlisted part is dropped
    assert t["integrate"].done_when[:3] == ["npm run verify:bridge-sources", "npm run check:fast",
                                            "npm run format:check"]
    assert any("rm -rf /" in n and n.startswith("dropped: integrate") for n in notes)
    assert t["contract"].done_when == ["test -s x"]                       # contracts are left alone


def test_no_patterns_means_no_rewrite():
    g = _plan()
    before = {t.id: list(t.done_when) for t in g.tasks.values()}
    assert derive_done_when(g, {"integration_gates": ["x"]}, SECS, DISCOVERED, TRACKED) == ([], [])
    assert {t.id: t.done_when for t in g.tasks.values()} == before


def test_pattern_shapes():
    ps = parse_patterns(["npm test -- {files}", {"cmd": "node --check {file}", "paths": ["scripts/**"]},
                         {"cmd": "no placeholder"}, 3])
    assert [p.cmd for p in ps] == ["npm test -- {files}", "node --check {file}"]
    assert ps[1].render(["a b.mjs", "c.mjs"]) == ["node --check 'a b.mjs'", "node --check c.mjs"]
    assert ps[0].render(["x", "y"]) == ["npm test -- x y"]


def test_integrate_gets_owned_tests_that_mention_spec_identifiers(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    files = {"src/inv.ts": "x", "tests/routing.test.ts": "expect(set_comment).unavailable",
             "tests/other.test.ts": "nothing"}
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    g = TaskGraph([Task("impl", "i", owns=["src/inv.ts"], done_when=["npm run check:fast"],
                        spec="Admit set_comment in the inventory."),
                   Task("integrate", "g", kind=Kind.INTEGRATE.value, deps=["impl"], owns=["tests/**"])])
    derive_done_when(g, CFG, {}, [], list(files), root)
    assert g.tasks["integrate"].done_when == ["npm run test:focused -- tests/routing.test.ts"]


def test_plan_load_rewrites_done_when_and_prints_dropped(tmp_path, capsys):
    from macc.cli import main
    from macc.store import Store
    root = tmp_path / "r"
    root.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    for f in TRACKED:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    assert main(["--repo", str(root), "init"]) == 0
    cfgp = root / ".macc" / "config.json"
    cfgp.write_text(json.dumps({**json.loads(cfgp.read_text()), **CFG}))
    (root / ".macc" / "gate_discovery.json").write_text(json.dumps({"timed": [
        {"cmd": c, "seconds": SECS.get(c, 2.0), "ok": True} for c in DISCOVERED]}))
    plan = tmp_path / "p.yaml"
    plan.write_text(json.dumps({"tasks": [
        {"id": t.id, "title": t.title, "kind": t.kind, "deps": t.deps, "owns": t.owns, "done_when": t.done_when,
         "spec": t.spec} for t in _plan().tasks.values()]}))
    assert main(["--repo", str(root), "plan", str(plan)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: dropped: tests: 'grep -q foo src/ghidra/inv.ts'" in out
    assert "note: moved to integrate: bridge: 'npm run verify:ghidra'" in out
    g = Store(root / ".macc" / "state.db").load_graph()
    assert "npm run verify:ghidra" not in g.tasks["bridge"].done_when
    assert "npm run verify:ghidra" in g.tasks["integrate"].done_when
    assert "test_cmd_patterns" in json.loads(cfgp.read_text())           # a repo key: survives loading a plan


def test_planner_prompt_calls_done_when_a_suggestion_and_wants_error_conventions():
    assert "Your done_when is a suggestion" in PLANNER_PROMPT
    assert "Cross-task observable behaviour belongs in the contract" in PLANNER_PROMPT


# ------------------------------------------------------------------ 2. contract error conventions
def _split_plan(contract_spec):
    tasks = [Task("impl", "i", owns=["bridge/Bridge.java"], done_when=["true"]),
             Task("verifier", "v", owns=["scripts/verify-real.mjs", "tests/x.test.ts"], done_when=["true"])]
    if contract_spec is not None:
        tasks.insert(0, Task("contract", "c", kind=Kind.CONTRACT.value, owns=["src/api.ts"], spec=contract_spec))
        for t in tasks[1:]:
            t.deps = ["contract"]
    return TaskGraph(tasks)


def test_contract_must_fix_error_conventions_when_impl_and_verifier_are_split():
    w = contract_error_conventions(_split_plan("Add set_comment. Store PRE comments."))
    assert len(w) == 1 and "error codes, return value vs throw, error message wording" in w[0]
    assert "impl implements, verifier tests/verifies" in w[0]
    full = ("Errors: invalid_request with kind `invalid_text` for NUL. An unmapped address returns false; nothing "
            "throws. The bridge owns the message text: 'Annotation comment contains an unpaired surrogate at UTF-16 "
            "index N'; verifiers match it exactly.")
    assert contract_error_conventions(_split_plan(full)) == []
    assert "no contract task" in contract_error_conventions(_split_plan(None))[0]
    alone = TaskGraph([Task("impl", "i", owns=["src/a.ts", "src/a.test.ts"], done_when=["true"])])
    assert contract_error_conventions(alone) == []                       # one task does both: nothing to agree on


# ------------------------------------------------------------------ 3. nearby exports
def test_nearby_exports_lists_helpers_next_to_owned_files(tmp_path):
    from macc.exports import nearby_exports
    files = {
        "tests/boundary/mcp/ghidraMcpHarness.ts": "export const connectGhidraMcp = async () => 1;\n"
                                                  "export function sessionEvidence() {}\nconst hidden = 1;\n",
        "tests/boundary/mcp/routing.test.ts": "export const notListed = 1;\n",
        "tests/fixtures/fakeGhidra.mjs": "export { start, stop as halt };\n",
        "src/ghidra/inv.ts": "export type Op = string;\nexport interface Inv {}\nexport class Bridge {}\n",
        "pkg/helpers.py": "def visible():\n    pass\ndef _private():\n    pass\nclass Thing:\n    def method(self): pass\n",
        "unrelated/far.ts": "export const far = 1;\n",
    }
    for f, text in files.items():
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(text)
    lines = nearby_exports(["tests/boundary/mcp/new.test.ts", "src/ghidra/**", "pkg/mod.py"], tmp_path)
    assert lines[0] == "tests/boundary/mcp/ghidraMcpHarness.ts: connectGhidraMcp, sessionEvidence"
    assert "tests/fixtures/fakeGhidra.mjs: start, halt" in lines
    assert "src/ghidra/inv.ts: Op, Inv, Bridge" in lines
    assert "pkg/helpers.py: visible, Thing" in lines
    assert not any("routing.test" in x or "far.ts" in x for x in lines)
    assert len(nearby_exports(["tests/boundary/mcp/new.test.ts", "src/ghidra/**"], tmp_path, limit=2)) == 2


def test_context_pack_has_exports_before_the_task_section(tmp_path):
    from macc.handoff import build_context_pack
    from macc.store import Store
    (tmp_path / "tests/boundary/mcp").mkdir(parents=True)
    (tmp_path / "tests/boundary/mcp/ghidraMcpHarness.ts").write_text("export const connectGhidraMcp = 1;\n")
    t = Task("mcp_tests", "m", owns=["tests/boundary/mcp/new.test.ts"], spec="Write the MCP tests.")
    pack = build_context_pack(t, TaskGraph([t]), Store(tmp_path / "s.db"), project_rules="rule", workdir=tmp_path)
    i = pack.index("# Existing exports next to your files")
    assert pack.index("# How you work") < i < pack.index("# Your task: mcp_tests")
    assert "- tests/boundary/mcp/ghidraMcpHarness.ts: connectGhidraMcp" in pack
    assert "# Existing exports" not in build_context_pack(t, TaskGraph([t]), Store(tmp_path / "s.db"))
