import pytest

from macc.graph import GraphError, Kind, State, Task, TaskGraph


def web_graph() -> TaskGraph:
    g = TaskGraph([
        Task("contract", "API contract", spec="define openapi", kind=Kind.CONTRACT.value,
             owns=["api/schema.yaml"], est_minutes=5),
        Task("backend", "Backend", spec="impl api", deps=["contract"], owns=["server/**"], est_minutes=30),
        Task("frontend", "Frontend", spec="impl ui", deps=["contract"], owns=["web/**"], est_minutes=20),
        Task("tests", "E2E tests", spec="write e2e", deps=["contract"], owns=["e2e/**"], est_minutes=10),
        Task("deploy", "Deploy env", spec="docker", deps=[], owns=["infra/**"], est_minutes=15),
        Task("integrate", "Integrate", spec="merge+run", kind=Kind.INTEGRATE.value,
             deps=["backend", "frontend", "tests", "deploy"], est_minutes=5),
    ])
    g.validate()
    g.refresh_hashes()
    return g


def test_topo_and_ready_set():
    g = web_graph()
    order = g.topo_order()
    assert order.index("contract") < order.index("backend")
    assert order.index("backend") < order.index("integrate")
    ready = [t.id for t in g.ready()]
    # contract gates 3 tasks -> longest critical path -> first; deploy is independent
    assert ready == ["contract", "deploy"]


def test_critical_path_priority():
    g = web_graph()
    assert g.critical_path("contract") == 5 + 30 + 5
    assert g.critical_path("deploy") == 15 + 5
    assert g.critical_path("integrate") == 5


def test_fan_out_after_contract():
    g = web_graph()
    g.mark_done("contract", "h1")
    ready = [t.id for t in g.ready()]
    assert ready[0] == "backend"           # longest path
    assert set(ready) == {"backend", "frontend", "tests", "deploy"}


def test_ownership_conflict_blocks_concurrency():
    g = TaskGraph([
        Task("a", "a", owns=["server/**"]),
        Task("b", "b", owns=["server/auth/*.py"]),
        Task("c", "c", owns=["web/**"]),
    ])
    g.validate()
    g.set_state("a", State.RUNNING)
    assert g.conflicts_with_active(g.tasks["b"]) == ["a"]
    assert g.conflicts_with_active(g.tasks["c"]) == []


def test_cycle_detected():
    g = TaskGraph([Task("a", "a", deps=["b"]), Task("b", "b", deps=["a"])])
    with pytest.raises(GraphError):
        g.validate()


def test_contract_must_precede_overlapping_tasks():
    g = TaskGraph([
        Task("schema", "schema", kind=Kind.CONTRACT.value, owns=["api/**"]),
        Task("impl", "impl", owns=["api/handlers.py"]),  # no dep on schema -> invalid
    ])
    with pytest.raises(GraphError):
        g.validate()


def test_stale_propagation_on_output_change():
    g = web_graph()
    g.mark_done("contract", "h1")
    g.mark_done("backend", "b1")
    g.mark_done("frontend", "f1")
    g.set_state("tests", State.RUNNING)
    # contract is redone with a different output (e.g. schema changed)
    changed = g.mark_done("contract", "h2")
    assert g.tasks["backend"].state == State.STALE.value
    assert g.tasks["frontend"].state == State.STALE.value
    assert g.tasks["tests"].preempt is True          # running -> preempt flag
    assert g.tasks["deploy"].state == State.PENDING.value  # not downstream, untouched
    assert set(changed) == {"backend", "frontend", "tests"}


def test_same_output_does_not_invalidate():
    g = web_graph()
    g.mark_done("contract", "h1")
    g.mark_done("backend", "b1")
    changed = g.mark_done("contract", "h1")   # re-run, identical output
    assert changed == []
    assert g.tasks["backend"].state == State.DONE.value


def test_update_spec_invalidates_self_and_downstream():
    g = web_graph()
    g.mark_done("contract", "h1")
    g.mark_done("backend", "b1")
    g.tasks["frontend"].state = State.FAILED.value
    affected = g.update_spec("contract", "define openapi v2 with auth")
    assert g.tasks["contract"].state == State.PENDING.value   # requirement changed -> must re-run
    assert g.tasks["backend"].state == State.STALE.value      # upstream output will change -> revalidate
    assert g.tasks["frontend"].state == State.PENDING.value   # failed -> retry with new inputs
    assert set(affected) == {"contract", "backend", "frontend"}
    assert g.tasks["integrate"].state == State.PENDING.value  # never ran; nothing to invalidate


def test_dynamic_decomposition():
    g = web_graph()
    g.mark_done("contract", "h1")
    g.add_subtasks("backend", [
        Task("backend.auth", "auth", owns=["server/auth/**"], est_minutes=10),
        Task("backend.items", "items", owns=["server/items/**"], est_minutes=10),
    ])
    b = g.tasks["backend"]
    assert b.kind == Kind.INTEGRATE.value
    assert set(b.deps) == {"contract", "backend.auth", "backend.items"}
    assert g.tasks["backend.auth"].deps == ["contract"]
    assert "backend.auth" in [t.id for t in g.ready()]
    assert "backend" not in [t.id for t in g.ready()]


def test_roundtrip_dict():
    g = web_graph()
    dicts = g.to_dicts()
    g2 = TaskGraph(Task.from_dict(d) for d in dicts)
    assert g2.topo_order() == g.topo_order()
    assert "graph TD" in g2.to_mermaid()


def test_first_done_refreshes_downstream_hashes():
    g = web_graph()
    h0 = g.tasks["backend"].input_hash
    g.mark_done("contract", "h1")
    assert g.tasks["backend"].input_hash != h0
    # a no-op refresh must not flip anything to stale afterwards
    g.mark_done("backend", "b1")
    before = {k: t.state for k, t in g.tasks.items()}
    g.refresh_hashes()
    assert {k: t.state for k, t in g.tasks.items()} == before
