from macc.graph import State, Task, TaskGraph
from macc.store import Store


def test_graph_roundtrip_and_resume(tmp_path):
    db = tmp_path / "state.db"
    s = Store(db)
    g = TaskGraph([Task("a", "A", owns=["x/**"]), Task("b", "B", deps=["a"])])
    g.refresh_hashes()
    g.mark_done("a", "out1")
    s.save_graph(g)
    s.close()

    s2 = Store(db)                      # "crash" and reopen
    g2 = s2.load_graph()
    assert g2.tasks["a"].state == State.DONE.value
    assert g2.tasks["a"].output_hash == "out1"
    assert [t.id for t in g2.ready()] == ["b"]


def test_events_handoffs_debts_usage(tmp_path):
    s = Store(tmp_path / "s.db")
    s.event("dispatched", "a", worker="mock")
    s.event("finished", "a", ok=True)
    evs = s.events("a")
    assert [e["type"] for e in evs] == ["dispatched", "finished"]

    s.save_handoff("a", 1, "in1", "out1", {"files_changed": ["x.py"], "open_questions": "none"})
    h = s.latest_handoff("a")
    assert h["files_changed"] == ["x.py"] and h["_attempt"] == 1

    d = s.add_debt("a", "stub", "payment provider stubbed", path="x.py")
    assert len(s.open_debts()) == 1
    s.resolve_debt(d)
    assert s.open_debts() == []

    s.add_decision("Switch to SQLite", "Postgres not available on target hw", affects=["a"])
    assert s.decisions()[0]["affects"] == ["a"]

    s.save_graph(TaskGraph([Task("a", "A")]))
    s.add_usage("a", 1, "mock", True, input_tokens=100, output_tokens=50, seconds=2.0)
    tot = s.usage_totals()
    assert tot["input_tokens"] == 100 and tot["runs"] == 1
    assert s.usage_by_kind()[0]["kind"] == "code"


def test_usage_est_minutes_migration_and_ratio(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)        # a pre-v0.2.7 usage table without est_minutes
    old.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER "
                "NOT NULL, worker TEXT NOT NULL, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER "
                "DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, seconds REAL DEFAULT 0, "
                "ok INTEGER NOT NULL)")
    old.execute("INSERT INTO usage(task_id, attempt, worker, ok, seconds) VALUES ('legacy', 1, 'claude', 1, 60)")
    old.commit()
    old.close()
    s = Store(db)
    s.save_graph(TaskGraph([Task("a", "A", est_minutes=10), Task("b", "B", est_minutes=30),
                            Task("legacy", "L"), Task("r", "R", kind="review", est_minutes=20)]))
    s.add_usage("a", 1, "claude", False, seconds=120, est_minutes=10)    # failed attempt still counts
    s.add_usage("a", 2, "claude", True, seconds=60, est_minutes=10)      # estimate counts once per task
    s.add_usage("b", 1, "claude", True, seconds=180, est_minutes=30)
    s.add_usage("r", 1, "claude", True, seconds=600, est_minutes=20)
    ratios = {r["kind"]: r for r in s.est_ratio_by_kind()}
    assert ratios["code"]["tasks"] == 2                                   # legacy row (no estimate) skipped
    assert abs(ratios["code"]["ratio"] - (3 + 3) / (10 + 30)) < 1e-9
    assert abs(ratios["review"]["ratio"] - 0.5) < 1e-9
    by_kind = {r["kind"]: r for r in s.usage_by_kind()}
    assert by_kind["code"]["est_ratio"] == ratios["code"]["ratio"] and by_kind["code"]["runs"] == 4
