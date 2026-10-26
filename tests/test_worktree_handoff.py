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
    assert ".macc/" in (repo.root / ".git" / "info" / "exclude").read_text()
    assert not (repo.root / ".gitignore").exists()                 # user's tree untouched
    assert ".macc" not in repo.git("status", "--porcelain", "--ignored=no").stdout

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


def test_parse_verdict_accepts_request_changes():
    from macc.handoff import parse_verdict
    assert parse_verdict("**Verdict: REQUEST CHANGES.** needs tests") == "REJECT"
    assert parse_verdict("Verdict: requested changes") == "REJECT"


def test_macc_env_exports_base_and_integration_to_gates(repo):
    main_head = repo.head()
    ipath = repo.ensure_integration()
    (ipath / "new.py").write_text("x = 1\n")
    repo.commit_all(ipath, "work on integration")
    env = repo.macc_env()
    assert env == {"MACC_BASE": main_head, "MACC_INTEGRATION": repo.head(ipath)}
    res = repo.run_gates([f'test "$MACC_BASE" = {main_head} && test "$MACC_INTEGRATION" = {repo.head(ipath)}',
                          'git diff --name-only "$MACC_BASE" | grep -qx new.py'])
    assert all(r["ok"] for r in res), res


def test_default_branch_master_and_no_integration(tmp_path):
    root = tmp_path / "m"
    root.mkdir()
    for cmd in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(cmd, cwd=root, check=True)
    r = Repo(root)
    assert r.default_branch() == "master"
    assert r.macc_env() == {}                      # no integration branch yet: nothing to export
    r.ensure_integration()
    assert r.macc_env()["MACC_BASE"] == r.head()


def test_interpreter_conflicts():
    from macc.handoff import interpreter_conflicts as f
    abs_rules = "Interpreter is `/Users/k/p/.venv/bin/python` (absolute)."
    assert f(abs_rules, "Interpreter: `.venv/bin/python` (never bare)")            # abs vs rel
    assert f(abs_rules, "run ./.venv/bin/python -m pytest")                         # ./ is relative
    assert f(".venv/bin/python -m pytest", "use $HOME/p/.venv/bin/python3")         # rel vs abs
    assert f(abs_rules, "use ~/p/.venv/bin/python") == []                           # both absolute
    assert f(".venv/bin/python", ".venv/bin/python") == []                          # both relative
    assert f(abs_rules, "no interpreter named here") == []


def test_classify_path():
    from macc.handoff import classify_path as c
    assert [c(p) for p in ("tests/test_a.py", "pkg/tests/x.py", "src/foo.test.ts", "tests/fixtures/x.json")] == ["test"] * 4
    assert [c(p) for p in ("docs/r.md", "alr/api/CONTRACT.md", "README.md")] == ["doc"] * 3
    assert [c(p) for p in ("alr/api/errors.py", "web/index.html", "Makefile")] == ["source"] * 3


def test_review_risk_tiers():
    from macc.handoff import review_risk
    f = lambda path, a, d, st="M": {"path": path, "status": st, "added": a, "deleted": d}  # noqa: E731
    small = [f("alr/x.py", 120, 30), f("tests/test_x.py", 200, 0, "A"), f("docs/r.md", 50, 0, "A")]
    assert review_risk(small, 0, True) == ("low", [], {"files": 3, "source_files": 1, "source_lines": 150,
                                                        "test_lines": 200, "doc_lines": 50, "modified_tests": [],
                                                        "new_public_api": 0})
    # appending tests to an existing file is not "changing" them; deleting lines from one is
    assert review_risk([f("tests/test_old.py", 40, 0)], 0, True)[0] == "low"
    tier, reasons, _ = review_risk([f("tests/test_old.py", 3, 5)], 0, True)
    assert tier == "high" and "existing tests changed" in reasons[0]
    assert review_risk([f("alr/x.py", 300, 101)], 0, True)[1] == ["401 source lines changed (> 400)"]
    assert review_risk(small, 2, True)[1] == ["2 open debt(s)"]
    assert review_risk(small, 0, False)[1] == ["full gate not green on this tree"]
    assert review_risk(small, 0, None)[1] == ["no full-gate result for this tree"]
    assert review_risk([f("img.png", None, None)], 0, True)[0] == "low"      # binary: counted as a file, 0 lines


def test_review_facts_text():
    from macc.handoff import review_facts
    stats = {"files": 4, "source_files": 2, "source_lines": 90, "test_lines": 40, "doc_lines": 10,
             "modified_tests": ["tests/test_old.py"]}
    txt = review_facts(stats, "high", ["existing tests changed: ['tests/test_old.py']"], ["alr/api/CONTRACT.md"],
                       [{"kind": "stub", "task_id": "a", "note": "retry not wired"}], "a" * 40)
    assert "source 2 files / 90 lines" in txt and "review tier: high (existing tests changed" in txt
    assert "check they were not weakened" in txt and "alr/api/CONTRACT.md" in txt
    assert "[stub] a: retry not wired" in txt and "Do not re-run the full test suite" in txt
    assert "open debts: none" in review_facts({**stats, "modified_tests": []}, "low", [], [], [], None)


def test_diff_files_statuses_and_counts(repo):
    base = repo.head()
    (repo.root / "README.md").write_text("hello\nworld\n")                   # M: +1
    (repo.root / "new.py").write_text("a = 1\nb = 2\n")                       # A: +2
    subprocess.run(["git", "mv", "api.py", "service.py"], cwd=repo.root, check=True)   # R
    (repo.root / "logo.bin").write_bytes(b"\x00\x01\x02")                    # binary
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "changes"], cwd=repo.root, check=True)
    files = {f["path"]: f for f in repo.diff_files(base, "HEAD", cwd=repo.root)}
    assert files["README.md"] == {"path": "README.md", "status": "M", "added": 1, "deleted": 0}
    assert files["new.py"]["status"] == "A" and files["new.py"]["added"] == 2
    assert files["service.py"]["status"] == "R" and "api.py" not in files
    assert files["logo.bin"]["added"] is None


def test_public_api_delta_and_new_interface_tier():
    from macc.handoff import public_api_delta, review_risk
    diff = "\n".join([
        "+++ b/alr/api/errors.py", "+def install_json_errors(app, prefixes):", "+def _helper():",
        "+class ErrorBody(BaseModel):",
        "+++ b/alr/api/watch.py", "-def install_error_handlers(app):", "-def get_con():",
        "+++ b/alr/api/common.py", "+def get_con():",                      # moved, not new
        "+++ b/alr/api/insights.py", '+@router.get("/insights/segments")', "+async def segments():",
        "+++ b/tests/test_x.py", "+def test_new():",                       # tests do not count
        "+++ b/docs/x.md", "+def not_code():"])
    added, removed = public_api_delta(diff)
    assert added == {"install_json_errors", "ErrorBody", "route /insights/segments", "segments"}
    assert removed == {"install_error_handlers"}
    f = [{"path": "alr/x.py", "status": "A", "added": 50, "deleted": 0}]
    assert review_risk(f, 0, True, new_public_api=5)[0] == "low"
    tier, reasons, stats = review_risk(f, 0, True, new_public_api=6)
    assert tier == "high" and "6 new public symbols/routes (> 5): new interface" in reasons
    assert stats["new_public_api"] == 6


def test_worker_protocol_separates_questions_from_notes(tmp_path):
    g = TaskGraph([Task("a", "a", owns=["a/**"])])
    pack = build_context_pack(g.tasks["a"], g, Store(tmp_path / "s.db"))
    assert "`open_questions` is ONLY for a decision a human or the planner must make" in pack
    assert "deviation from the task text you already handled" in pack and "never in open_questions" in pack
    from macc.planner import PLANNER_PROMPT
    assert "resolved deviations and explanations go to\n    notes_for_downstream" in PLANNER_PROMPT


def test_identical_gates_on_a_clean_tree_run_once_even_concurrently(repo, tmp_path):
    import threading
    count = tmp_path / "count.txt"
    cmd = f"sleep 0.3; echo x >> {count}"
    ipath = repo.ensure_integration()
    results = []
    threads = [threading.Thread(target=lambda: results.append(repo.run_gates([cmd])[0])) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert count.read_text().count("x") == 1                       # ran once, two callers waited for it
    assert sum(1 for r in results if r.get("shared")) == 2 and all(r["ok"] for r in results)
    assert repo.run_gates([cmd])[0]["shared"] is True                # later caller: reused
    (ipath / "dirty.txt").write_text("uncommitted\n")
    assert "shared" not in repo.run_gates([cmd])[0]                   # a dirty tree never shares results
    assert count.read_text().count("x") == 2


def test_gate_timeout_is_configurable(repo):
    repo.ensure_integration()
    repo.gate_timeout = 1
    rec = repo.run_gates(["sleep 3"])[0]
    assert rec["ok"] is False and rec["stderr"] == "timeout after 1 s"


def test_setup_created_paths_and_symlinked_artifact_dirs_are_not_committed(repo):
    wa = repo.create_task_worktree("a")
    (wa / "node_modules").symlink_to(repo.root)                      # a symlinked dependency dir
    (wa / "venv_link").symlink_to(repo.root)                         # something a setup command made
    (wa / "a.py").write_text("x = 1\n")
    sc = repo.commit_scoped(wa, "a", ["a.py"], setup_paths={"venv_link"})
    assert sc.inside == ["a.py"] and sc.outside == []
    assert set(sc.ignored) == {"node_modules", "venv_link"}


def test_spike_context_pack_asks_for_sources_and_facts(tmp_path):
    g = TaskGraph([Task("s", "s", kind="spike", owns=["docs/spikes/**"])])
    pack = build_context_pack(g.tasks["s"], g, Store(tmp_path / "s.db"))
    assert "the only kind of task that can" in pack and "source URL" in pack and "docs/spikes/s.md" in pack
