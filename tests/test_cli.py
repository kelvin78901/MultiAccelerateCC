import subprocess
from pathlib import Path

import pytest

from macc.cli import main
from macc.plan import load_plan

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "webapp.yaml"


@pytest.fixture
def repo_dir(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_example_plan_loads():
    g, cfg = load_plan(EXAMPLE)
    assert cfg["max_parallel"] == 4
    assert [t.id for t in g.ready()] == ["contract", "infra"]


def test_cli_end_to_end_with_mock(repo_dir, capsys):
    r = str(repo_dir)
    assert main(["--repo", r, "init"]) == 0
    assert main(["--repo", r, "plan", str(EXAMPLE)]) == 0
    # the example gates need pytest in the target repo; override with trivial gates for the mock run
    # (mock workers write .txt files, so the real done_when commands would fail)
    rc = main(["--repo", r, "run", "--worker", "mock", "--parallel", "3", "--gate", "true"])
    out = capsys.readouterr().out
    # done_when gates in the example fail under mock -> some tasks FAILED, exit 1; the loop must still terminate
    assert rc in (0, 1)
    assert "run_finished" in out
    assert main(["--repo", r, "status"]) == 0
    assert main(["--repo", r, "events", "--n", "5"]) == 0
    assert main(["--repo", r, "debts"]) == 0
    assert main(["--repo", r, "change", "contract", "--spec", "v2", "--why", "scope change"]) == 0
    out = capsys.readouterr().out
    assert "invalidated" in out


def test_cli_split_and_retry(repo_dir, tmp_path, capsys):
    r = str(repo_dir)
    plan = tmp_path / "p.yaml"
    plan.write_text("tasks:\n  - {id: big, title: big, owns: ['x/**']}\n  - {id: after, title: after, deps: [big]}\n")
    sub = tmp_path / "sub.yaml"
    sub.write_text("tasks:\n  - {id: big.a, title: a, owns: ['x/a/**']}\n  - {id: big.b, title: b, owns: ['x/b/**']}\n")
    main(["--repo", r, "init"])
    main(["--repo", r, "plan", str(plan)])
    assert main(["--repo", r, "split", "big", "--plan", str(sub)]) == 0
    assert main(["--repo", r, "run", "--worker", "mock"]) == 0
    main(["--repo", r, "status"])
    out = capsys.readouterr().out
    assert "summary: {'done': 4}" in out
    assert main(["--repo", r, "retry", "after"]) == 0


def test_plan_reload_keeps_finished_tasks(repo_dir, tmp_path, capsys):
    r = str(repo_dir)
    p1 = tmp_path / "p1.yaml"; p1.write_text("tasks:\n  - {id: a, title: a, owns: ['a/**']}\n")
    p2 = tmp_path / "p2.yaml"; p2.write_text("tasks:\n  - {id: b, title: b, owns: ['b/**']}\n")
    main(["--repo", r, "init"]); main(["--repo", r, "plan", str(p1)])
    assert main(["--repo", r, "run", "--worker", "mock"]) == 0
    main(["--repo", r, "plan", str(p2)])
    main(["--repo", r, "status"])
    out = capsys.readouterr().out
    assert "done      code      a" in out and "pending   code      b" in out


def test_change_owns_and_done_when(repo_dir, tmp_path, capsys):
    r = str(repo_dir)
    p = tmp_path / "p.yaml"; p.write_text("tasks:\n  - {id: t, title: t, owns: ['a/**'], done_when: ['true']}\n")
    main(["--repo", r, "init"]); main(["--repo", r, "plan", str(p)])
    assert main(["--repo", r, "run", "--worker", "mock"]) == 0
    assert main(["--repo", r, "change", "t", "--owns", "b/**", "--owns", "c.py", "--why", "renamed"]) == 0
    from macc.store import Store
    g = Store(repo_dir / ".macc" / "state.db").load_graph()
    assert g.tasks["t"].owns == ["b/**", "c.py"] and g.tasks["t"].state == "pending"


def test_init_records_gate(repo_dir, capsys):
    r = str(repo_dir)
    assert main(["--repo", r, "init", "--gate", "true", "--gate", "test -f README.md"]) == 0
    import json
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["integration_gates"] == ["true", "test -f README.md"]
    assert "gate verified" in capsys.readouterr().out


def test_status_shows_estimate_ratio(repo_dir, tmp_path, capsys):
    plan = tmp_path / "p.yaml"
    plan.write_text("tasks:\n  - id: t\n    title: t\n    owns: ['t/**']\n    est_minutes: 10\n")
    r = str(repo_dir)
    assert main(["--repo", r, "plan", str(plan)]) == 0
    assert main(["--repo", r, "run", "--worker", "mock", "--gate", "true"]) == 0
    capsys.readouterr()
    assert main(["--repo", r, "status"]) == 0
    out = capsys.readouterr().out
    assert "actual/est=" in out and "(1 tasks)" in out


def test_status_prints_last_critical_path(repo_dir, tmp_path, capsys):
    plan = tmp_path / "p.yaml"
    plan.write_text("tasks:\n  - id: a\n    title: a\n    owns: ['a/**']\n    est_minutes: 10\n"
                    "  - id: b\n    title: b\n    deps: [a]\n    owns: ['b/**']\n    est_minutes: 20\n")
    r = str(repo_dir)
    assert main(["--repo", r, "plan", str(plan)]) == 0
    assert main(["--repo", r, "run", "--worker", "mock", "--gate", "true"]) == 0
    capsys.readouterr()
    assert main(["--repo", r, "status"]) == 0
    assert "last run critical path: a -> b: actual" in capsys.readouterr().out


def test_plan_reload_ignores_finished_contract_overlap(repo_dir, tmp_path, capsys):
    # plan 1: a contract that owns tests/conftest.py, run to DONE
    p1 = tmp_path / "p1.yaml"
    p1.write_text("tasks:\n  - id: c1\n    title: c1\n    kind: contract\n    owns: ['tests/conftest.py', 'a/**']\n"
                  "  - id: a\n    title: a\n    deps: [c1]\n    owns: ['a/x/**']\n")
    r = str(repo_dir)
    assert main(["--repo", r, "plan", str(p1)]) == 0
    assert main(["--repo", r, "run", "--worker", "mock", "--gate", "true"]) == 0
    # plan 2: a new contract that also extends tests/conftest.py; c1 is finished and kept
    p2 = tmp_path / "p2.yaml"
    p2.write_text("tasks:\n  - id: c2\n    title: c2\n    kind: contract\n    owns: ['tests/conftest.py', 'b/**']\n"
                  "  - id: b\n    title: b\n    deps: [c2]\n    owns: ['b/x/**']\n")
    assert main(["--repo", r, "plan", str(p2)]) == 0
    assert "ready now: ['c2']" in capsys.readouterr().out


def test_unfinished_contract_overlap_still_rejected():
    from macc.graph import GraphError, Task, TaskGraph
    g = TaskGraph([Task("c", "c", kind="contract", owns=["api/**"]), Task("x", "x", owns=["api/x.py"])])
    with pytest.raises(GraphError, match="contracts must be serialised"):
        g.validate()


def test_plan_records_plan_base(repo_dir, tmp_path):
    import json
    plan = tmp_path / "p.yaml"
    plan.write_text("tasks:\n  - id: t\n    title: t\n    owns: ['t/**']\n")
    r = str(repo_dir)
    assert main(["--repo", r, "plan", str(plan)]) == 0
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    head = subprocess.run(["git", "rev-parse", "macc/integration"], cwd=repo_dir, capture_output=True,
                          text=True).stdout.strip()
    assert cfg["plan_base"] == head


def test_plan_load_prints_prediction(repo_dir, tmp_path, capsys):
    plan = tmp_path / "p.yaml"
    plan.write_text("tasks:\n  - id: t\n    title: t\n    owns: ['t/**']\n    est_minutes: 2\n")
    assert main(["--repo", str(repo_dir), "plan", str(plan)]) == 0
    out = capsys.readouterr().out
    assert "prediction: wall 120 s, cost $0.00, serial share 100% (serial points: t)" in out
    # hints are on, but with no history a hint gives no direction
    assert "split hint: t: serial point, insufficient data for a split direction [level 7: plan estimate" in out


def test_init_history_and_gate_baseline_survive_plan_load(repo_dir, tmp_path, capsys):
    import json
    other = tmp_path / "other.db"
    other.write_text("")
    r = str(repo_dir)
    assert main(["--repo", r, "init", "--gate", "true", "--history", str(other)]) == 0
    assert main(["--repo", r, "init", "--history", str(other)]) == 0                 # appending twice: no dup
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["history_dbs"] == [str(other.resolve())] and cfg["gate_baseline_s"] >= 0
    plan = tmp_path / "p.yaml"
    plan.write_text("config:\n  max_parallel: 2\ntasks:\n  - id: t\n    title: t\n    owns: ['t/**']\n")
    assert main(["--repo", r, "plan", str(plan)]) == 0
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["history_dbs"] == [str(other.resolve())] and "gate_baseline_s" in cfg
    assert cfg["integration_gates"] == ["true"] and cfg["max_parallel"] == 2
    assert main(["--repo", r, "backtest"]) == 0
    assert "history: this repo + 1 other db(s)" in capsys.readouterr().out


def test_init_runs_worktree_setup_before_the_gate(repo_dir, capsys):
    import json
    r = str(repo_dir)
    # the gate needs what the setup creates (REA: check:fast needs the symlinked node_modules)
    assert main(["--repo", r, "init", "--setup", "ln -sfn \"$MACC_REPO_ROOT/README.md\" linked.md",
                 "--gate", "test -f linked.md", "--gate-timeout", "60"]) == 0
    out = capsys.readouterr().out
    assert "gate verified" in out and "WARNING" not in out
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["worktree_setup"] == ["ln -sfn \"$MACC_REPO_ROOT/README.md\" linked.md"]
    assert cfg["gate_timeout"] == 60 and cfg["gate_baseline_s"] >= 0
    # an explicit baseline wins over the measured one (a warm build cache makes the measurement meaningless)
    assert main(["--repo", r, "init", "--gate-baseline", "7.8"]) == 0
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["gate_baseline_s"] == 7.8 and cfg["worktree_setup"]


def test_init_failing_setup_skips_the_gate(repo_dir, capsys):
    import json
    r = str(repo_dir)
    assert main(["--repo", r, "init", "--setup", "false", "--gate", "true"]) == 0
    out = capsys.readouterr().out
    assert "worktree_setup fails" in out and "gate verified" not in out
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["integration_gates"] == ["true"] and "gate_baseline_s" not in cfg


def test_plan_from_prediction_keeps_repo_keys(repo_dir, tmp_path, monkeypatch, capsys):
    import macc.cli as cli
    other = tmp_path / "other.db"
    other.write_text("")
    r = str(repo_dir)
    import json
    assert main(["--repo", r, "init", "--history", str(other)]) == 0
    cfgp = repo_dir / ".macc" / "config.json"
    cfgp.write_text(json.dumps({**json.loads(cfgp.read_text()), "gate_baseline_s": 8}))
    seen = {}
    monkeypatch.setattr(cli, "_prediction_lines", lambda g, store, cfg: seen.update(cfg) or [])
    # the mock planner's plan has its own config block (max_parallel, integration_gates, ...)
    assert main(["--repo", r, "plan", "--from", "todo app", "--worker", "mock", str(tmp_path / "p.yaml")]) == 0
    assert seen["history_dbs"] == [str(other.resolve())] and seen["gate_baseline_s"] == 8
    assert seen["max_parallel"] == 3                     # plan keys still come from the planner


def test_flake_check_off_survives_plan_load(repo_dir, tmp_path):
    import json
    r = str(repo_dir)
    assert main(["--repo", r, "init"]) == 0
    cfgp = repo_dir / ".macc" / "config.json"
    cfgp.write_text(json.dumps({**json.loads(cfgp.read_text()), "flake_check_runs": 0}))   # a vitest repo
    plan = tmp_path / "p.yaml"
    plan.write_text("config:\n  max_parallel: 2\ntasks:\n  - id: t\n    title: t\n    owns: ['t/**']\n")
    assert main(["--repo", r, "plan", str(plan)]) == 0
    assert json.loads(cfgp.read_text())["flake_check_runs"] == 0


def _commit(root, files: dict):
    for name, text in files.items():
        (root / name).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "targets"], cwd=root, check=True)


def test_find_candidates_reads_package_makefile_and_pyproject(tmp_path):
    import json
    from macc.gates import find_candidates
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "build": "tsc -b", "format": "oxfmt .", "format:check": "oxfmt --check .", "lint": "oxlint",
        "precheck": "node deps.mjs", "check": "turbo run typecheck lint", "check:pr": "turbo run typecheck lint",
        "test:watch": "vitest", "test": "vitest run", "metadata:check": "node gen.mjs --check", "knip": "knip",
        "verify:cross-format": "node v.mjs", "test:prepare": "tsc -b"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "Makefile").write_text("FOO := 1\n.PHONY: lint\nlint typecheck: deps\n\tcheck\n%.o: %.c\nbuild:\n")
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nline-length = 100\n[tool.poe.tasks]\ntest = "pytest"\n')
    keep, skipped = find_candidates(tmp_path)
    cmds = [c["cmd"] for c in keep]
    # format first, tests last; pnpm detected from its lockfile; lifecycle hooks and non-check targets left out
    assert cmds[0] == "pnpm run format:check" and cmds[-1] == "poe test"
    assert {"pnpm run lint", "pnpm run knip", "pnpm run metadata:check", "make lint", "make typecheck",
            "ruff check .", "ruff format --check ."} <= set(cmds)
    assert not {"pnpm run build", "pnpm run precheck", "make build", "make FOO"} & set(cmds)
    # identical bodies are timed once and listed as aliases
    check = next(c for c in keep if c["cmd"] == "pnpm run check")
    assert check["aliases"] == ["pnpm run check:pr"] and "pnpm run check:pr" not in cmds
    reasons = {s["cmd"]: s["reason"] for s in skipped}
    assert "watch" in reasons["pnpm run test:watch"] and "in place" in reasons["pnpm run format"]
    assert "pnpm run verify:cross-format" not in reasons and "prepare" in reasons["pnpm run test:prepare"]
    keep, skipped = find_candidates(tmp_path, exclude=["^test$"])
    assert "pnpm run test" not in [c["cmd"] for c in keep]


def test_discover_gates_times_lists_and_picks(repo_dir, capsys):
    import json
    _commit(repo_dir, {
        "package.json": json.dumps({"scripts": {
            "format:check": "true", "lint": "exit 3", "typecheck": "touch generated.txt",
            "test": "sleep 5", "check": "test -f package.json"}}),
        ".gitignore": "node_modules\n"})
    r = str(repo_dir)
    # gate_timeout 4: the limit is 2 s, so `sleep 5` is killed rather than waited for
    assert main(["--repo", r, "init", "--gate-timeout", "4", "--discover-gates"]) == 0
    out = capsys.readouterr().out
    assert "npm run format:check" in out and "fails (exit 3)" in out and "modifies the tree" in out
    assert "over 2 s (killed)" in out and "nothing added" in out
    integ = repo_dir / ".macc" / "worktrees" / "_integration"
    assert not (integ / "generated.txt").exists()          # what a candidate wrote is undone
    rec = json.loads((repo_dir / ".macc" / "gate_discovery.json").read_text())
    by_cmd = {t["cmd"]: t for t in rec["timed"]}
    assert by_cmd["npm run test"]["seconds"] < 4.5 and not by_cmd["npm run test"]["ok"]
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert "integration_gates" not in cfg
    # picking reuses the saved discovery (nothing is timed again); failing ones are refused with their status
    import re
    num = re.search(r"(\d+)\.\s+[\d.]+ s  npm run format:check", out).group(1)   # numbers follow the printed list
    assert main(["--repo", r, "init", "--pick", num, "--pick", "npm run check,npm run lint"]) == 1
    out = capsys.readouterr().out
    assert "using the discovery" in out and "timing" not in out and "'npm run lint': fails (exit 3)" in out
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert sorted(cfg["integration_gates"]) == ["npm run check", "npm run format:check"]
    assert cfg["gate_baseline_s"] == round(by_cmd["npm run check"]["seconds"]
                                           + by_cmd["npm run format:check"]["seconds"], 2)
    assert main(["--repo", r, "init", "--pick", "npm run check"]) == 0     # already a gate: not added twice
    cfg = json.loads((repo_dir / ".macc" / "config.json").read_text())
    assert cfg["integration_gates"].count("npm run check") == 1


def test_pick_without_discovery_and_dirty_tree_are_errors(repo_dir, capsys):
    r = str(repo_dir)
    assert main(["--repo", r, "init", "--pick", "1"]) == 1
    assert "needs a discovery first" in capsys.readouterr().out
    (repo_dir / ".macc" / "worktrees" / "_integration" / "stray.txt").write_text("x")
    assert main(["--repo", r, "init", "--discover-gates"]) == 1
    assert "uncommitted changes" in capsys.readouterr().out
