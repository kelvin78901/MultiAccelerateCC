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
