import subprocess
from pathlib import Path

import pytest

from macc.graph import Task, TaskGraph
from macc.handoff import HANDOFF_BEGIN, HANDOFF_END, build_context_pack, generate_handoff, parse_worker_output
from macc.store import Store
from macc.worktree import INTEGRATION_BRANCH, Repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n")
    (root / "api.py").write_text("def ping():\n    return 'pong'\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return Repo(root)


def test_worktree_lifecycle_and_merge(repo):
    ipath = repo.ensure_integration()
    assert ipath.exists() and repo.branch_exists(INTEGRATION_BRANCH)
    assert ".macc/" in (repo.root / ".gitignore").read_text()

    wa = repo.create_task_worktree("a")
    (wa / "a.py").write_text("def a():\n    return 1\n")
    assert repo.commit_all(wa, "task a") is True
    assert repo.commit_all(wa, "nothing") is False
    assert repo.changed_files(wa) == [("A", "a.py")]
    h1 = repo.output_hash(wa)

    wb = repo.create_task_worktree("b")
    (wb / "b.py").write_text("def b():\n    return 2\n")
    repo.commit_all(wb, "task b")

    hb = repo.output_hash(wb)
    assert hb != h1

    r = repo.merge_task("a")
    assert r.ok and r.merge_commit
    # b forked before a merged; after rebase the base moved but the change is identical -> same hash
    assert repo.rebase_task_on_integration("b").ok
    assert repo.output_hash(wb) == hb
    r2 = repo.merge_task("b")
    assert r2.ok
    assert (ipath / "a.py").exists() and (ipath / "b.py").exists()

    repo.remove_task_worktree("a", delete_branch=True)
    assert not repo.task_path("a").exists()
    assert not repo.branch_exists(repo.task_branch("a"))


def test_merge_conflict_detected_and_aborted(repo):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "api.py").write_text("def ping():\n    return 'A'\n")
    repo.commit_all(wa, "a")
    wb = repo.create_task_worktree("b")
    (wb / "api.py").write_text("def ping():\n    return 'B'\n")
    repo.commit_all(wb, "b")

    assert repo.merge_task("a").ok
    r = repo.merge_task("b")
    assert not r.ok and r.conflicts == ["api.py"]
    # integration tree is clean after abort
    status = repo.git("status", "--porcelain", cwd=repo.integration_path).stdout
    assert status == ""
    assert (repo.integration_path / "api.py").read_text().endswith("'A'\n")


def test_gates(repo):
    repo.ensure_integration()
    res = repo.run_gates(["true", "false", "true"])
    assert [r["ok"] for r in res] == [True, False]   # stops at first failure


def test_parse_worker_output():
    txt = f"blah\n{HANDOFF_BEGIN}\n{{\"open_questions\": \"q\", \"notes_for_downstream\": \"n\", \"stubs\": [\"x.py: pay\"]}}\n{HANDOFF_END}"
    d = parse_worker_output(txt)
    assert d["_parsed"] and d["stubs"] == ["x.py: pay"]
    assert parse_worker_output("no block")["_parsed"] is False


def test_generate_handoff_and_context_pack(repo, tmp_path):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "api.py").write_text("def ping():\n    return 'pong'\n\ndef pay():\n    raise NotImplementedError  # TODO(macc)\n")
    repo.commit_all(wa, "a")
    g = TaskGraph([
        Task("a", "API", spec="add pay", owns=["api.py"], done_when=["python -c 'import api'"]),
        Task("b", "UI", spec="call pay", deps=["a"], owns=["ui/**"]),
    ])
    g.refresh_hashes()
    store = Store(tmp_path / "s.db")
    store.save_graph(g)
    gates = repo.run_gates(["true"], cwd=wa)
    h = generate_handoff(repo, g.tasks["a"], wa, gates, {"open_questions": "", "notes_for_downstream": "pay() is a stub", "stubs": ["api.py: pay"]})
    assert h["files_changed"] == [{"status": "M", "path": "api.py"}]
    assert any("def pay" in x for x in h["api_changes"])
    assert h["stubs_detected"][0]["path"] == "api.py"
    assert h["gates_passed"] is True
    store.save_handoff("a", 1, "in", "out", h)
    store.add_decision("No real payments in v1", "stub provider", affects=["a"])
    g.mark_done("a", "out")

    pack = build_context_pack(g.tasks["b"], g, store, project_rules="Use ruff.")
    assert pack.index("# Project rules") < pack.index("# How you work") < pack.index("# Your task")
    assert "pay() is a stub" in pack and "No real payments" in pack and "STUBS" in pack
    assert "['ui/**']" in pack


def test_parse_verdict_is_strict():
    from macc.handoff import parse_verdict
    assert parse_verdict("VERDICT: APPROVE\nno reason to reject") == "APPROVE"
    assert parse_verdict("**Verdict: REJECT** missing tests") == "REJECT"
    assert parse_verdict("REJECT\nbecause") == "REJECT"
    assert parse_verdict("I see no reason to reject this.") is None
    assert parse_verdict("") is None


def test_stub_detection_ignores_docs(repo, tmp_path):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "docs").mkdir()
    (wa / "docs" / "review.md").write_text("Checked: no TODO(macc) markers, no NotImplementedError.\n")
    (wa / "x.py").write_text("def f():\n    raise NotImplementedError  # TODO(macc)\n")
    repo.commit_all(wa, "a")
    t = Task("a", "A", owns=["docs/**", "x.py"])
    h = generate_handoff(repo, t, wa, [], {})
    assert [s["path"] for s in h["stubs_detected"]] == ["x.py"]


def test_commit_scoped_filters_artifacts_and_out_of_scope(repo):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "srv").mkdir(); (wa / "srv" / "a.py").write_text("x=1\n")
    (wa / "srv" / "__pycache__").mkdir(); (wa / "srv" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00")
    (wa / "web").mkdir(); (wa / "web" / "b.js").write_text("1\n")
    (wa / "README.md").write_text("changed\n")
    sc = repo.commit_scoped(wa, "a", ["srv/**"])
    assert sc.committed and sc.inside == ["srv/a.py"]
    assert set(sc.outside) == {"web/b.js", "README.md"}
    assert sc.ignored == ["srv/__pycache__/a.cpython-313.pyc"]
    assert repo.changed_files(wa) == [("A", "srv/a.py")]
    assert repo.git("status", "--porcelain", cwd=wa).stdout == ""      # tree left clean
    assert (wa / "README.md").read_text() == "hello\n"                  # out-of-scope edit reverted


def test_commit_scoped_no_owns_means_everything(repo):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "z.txt").write_text("z\n")
    sc = repo.commit_scoped(wa, "a", [])
    assert sc.committed and sc.inside == ["z.txt"] and not sc.outside


def test_artifact_matching_by_segment():
    from macc.worktree import _is_artifact
    assert _is_artifact("build/out.js") and _is_artifact("a/__pycache__/x.pyc") and _is_artifact("x.log")
    assert not _is_artifact("tools/rebuild/x.py") and not _is_artifact("distribute.py") and not _is_artifact("src/builder.py")


def test_dirty_paths_handles_unicode_and_spaces(repo):
    repo.ensure_integration()
    wa = repo.create_task_worktree("a")
    (wa / "数据 文件.txt").write_text("x\n")
    (wa / "README.md").write_text("changed\n")
    assert set(repo.dirty_paths(wa)) == {"数据 文件.txt", "README.md"}
    sc = repo.commit_scoped(wa, "a", ["数据 文件.txt"])
    assert sc.inside == ["数据 文件.txt"] and sc.outside == ["README.md"]
