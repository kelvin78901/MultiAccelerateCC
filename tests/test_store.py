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
