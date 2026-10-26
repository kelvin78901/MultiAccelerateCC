import subprocess

import pytest

from macc.costmodel import (error, predict, reconstruct_serial_flags, split_hints, static_serial_points)
from macc.graph import Kind, State, Task, TaskGraph
from macc.scheduler import Scheduler, SchedulerConfig
from macc.store import Store
from macc.workers import MockWorker
from macc.worktree import Repo


def diamond():
    return TaskGraph([Task("c", "c", kind=Kind.CONTRACT.value, owns=["api/**"]),
                      Task("a", "a", deps=["c"], owns=["a/**"]), Task("b", "b", deps=["c"], owns=["b/**"]),
                      Task("i", "i", kind=Kind.INTEGRATE.value, deps=["a", "b"], owns=["x/**"]),
                      Task("r", "r", kind=Kind.REVIEW.value, deps=["i"], owns=["docs/**"])])


def seed(store, rows):
    for kind, secs, cost, serial in rows:
        store.add_usage(f"h_{kind}_{secs}", 1, "claude", True, model="m", kind=kind, seconds=secs,
                        cost_usd=cost, serial=serial)


def test_static_serial_points():
    g = diamond()
    assert static_serial_points(g, set(g.tasks)) == {"c", "i", "r"}
    assert static_serial_points(g, {"a", "b"}) == set()                     # siblings run side by side
    assert static_serial_points(g, {"a", "i", "r"}) == {"a", "i", "r"}       # a change round on one branch


def test_predict_uses_serial_medians_and_phase_maxima(tmp_path):
    store = Store(tmp_path / "s.db")
    seed(store, [("contract", 100, 0.8, 1), ("contract", 120, 0.9, 1), ("contract", 140, 1.0, 1),
                 ("contract", 30, 0.1, 0),                                     # not serial: ignored for c
                 ("code", 30, 0.2, 0), ("code", 40, 0.2, 0), ("code", 50, 0.2, 0),
                 ("integrate", 80, 0.7, 1), ("integrate", 70, 0.7, 1), ("integrate", 90, 0.7, 1),
                 ("review", 150, 1.0, 1), ("review", 170, 1.2, 1), ("review", 160, 1.1, 1)])
    store.event("full_gate", "x", ok=True, seconds=2.0, cmds=["t"])
    pred = predict(diamond(), store, max_parallel=4)
    per = pred["per_task"]
    assert per["c"]["seconds"] == 120 and "serial point" in per["c"]["source"]
    assert per["a"]["seconds"] == 40 and per["i"]["seconds"] == 80 and per["r"]["seconds"] == 160
    # wall = serial (120 + 80 + 160) + phase max(chain 40, work 80/4) + gates 2 s x (1 integrate + final)
    assert pred["wall_s"] == 360 + 40 + 4
    assert pred["serial_tasks"] == ["c", "i", "r"] and pred["serial_share"] == round(360 / 404, 3)
    # cost: median over all of a kind's runs (contract: 0.8, 0.9, 1.0, 0.1 -> 0.85)
    assert pred["cost_usd"] == round(0.85 + 0.2 + 0.2 + 0.7 + 1.1, 3)
    # a narrow pool turns the phase into work / parallelism
    wide = TaskGraph([Task("c", "c", kind="contract")] + [Task(f"m{i}", "m", deps=["c"]) for i in range(6)])
    p1 = predict(wide, store, max_parallel=2, gates=False)
    assert p1["phases"][0]["seconds"] == 6 * 40 / 2


def test_predict_skips_revalidating_tasks_and_falls_back_without_history(tmp_path):
    store = Store(tmp_path / "s.db")
    g = diamond()
    for t in g.tasks.values():
        t.state = State.DONE.value
    g.tasks["a"].state = State.PENDING.value
    g.tasks["i"].state = State.STALE.value
    g.tasks["i"].done_when = ["true"]                                      # revalidates: no worker
    g.tasks["r"].state = State.STALE.value                                 # reviews always re-run
    g.tasks["a"].est_minutes = 2
    pred = predict(g, store, gates=False)
    assert set(pred["per_task"]) == {"a", "r"}
    assert pred["per_task"]["a"]["source"] == "plan estimate (no history)" and pred["per_task"]["a"]["seconds"] == 120


def test_error_and_split_hints():
    pred = {"wall_s": 400.0, "cost_usd": 4.0, "serial_share": 0.9, "serial_tasks": ["c", "r"],
            "per_task": {"c": {"seconds": 300.0}, "r": {"seconds": 60.0}}}
    assert error(pred, 500, 5.0, 0.8) == {"wall": -0.2, "cost": -0.2, "serial_share": 0.1}
    hints = split_hints(pred, floor_s=21)
    assert len(hints) == 1 and hints[0].startswith("c: serial point, predicted 300 s (75% of wall)")
    assert "into 4 parallel parts would save ~141 s" in hints[0]               # 300·3/4 − 4·21


@pytest.fixture
def repo(tmp_path) -> Repo:
    root = tmp_path / "proj"
    root.mkdir()
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return Repo(root)


async def test_run_records_prediction_flags_and_reconstruction_agrees(repo):
    store = Store(repo.macc_dir / "state.db")
    g = diamond()
    g.validate()
    summary = await Scheduler(repo, store, g, MockWorker(), SchedulerConfig(integration_gates=["true"],
                                                                            skip_noop_integrate=False)).run()
    pv = summary["prediction"]
    assert set(pv) == {"wall_s", "cost_usd", "serial_share", "error"}
    assert set(pv["error"]) == {"wall", "cost", "serial_share"}
    rows = {r["task_id"]: dict(r) for r in store._conn.execute("SELECT * FROM usage")}
    assert all(r["pred_seconds"] is not None for r in rows.values())
    live = {t: r["serial"] for t, r in rows.items()}
    assert live == {"c": 1, "a": 0, "b": 0, "i": 1, "r": 1}
    store._conn.execute("UPDATE usage SET serial = NULL")
    assert reconstruct_serial_flags(store) == 5
    assert {r["task_id"]: r["serial"] for r in store._conn.execute("SELECT * FROM usage")} == live


def test_backfill_fix_rounds(tmp_path):
    from macc.costmodel import backfill_fix_rounds
    store = Store(tmp_path / "s.db")
    for tid, attempt in [("a", 1), ("a", 2), ("b", 1), ("a", 1), ("b", 1), ("a", 2)]:
        store.add_usage(tid, attempt, "claude", True, model="m", kind="code")
    assert backfill_fix_rounds(store) == 6
    flags = [r["fix_round"] for r in store._conn.execute("SELECT fix_round FROM usage ORDER BY id")]
    #        a1 first  a2 retry  b1 first  a1 later round  b1 later round  a2 retry in that round
    assert flags == [0, 0, 0, 1, 1, 1]


def test_fix_round_review_is_predicted_on_the_low_tier(tmp_path):
    store = Store(tmp_path / "s.db")
    for _ in range(3):
        store.add_usage("old_r", 1, "claude", True, model="claude-opus-5-5", kind="review", seconds=150,
                        cost_usd=1.0, serial=True, fix_round=False)
        store.add_usage("old_r", 1, "claude", True, model="claude-sonnet-5-5", kind="review", seconds=40,
                        cost_usd=0.2, serial=True, fix_round=True)
    g = TaskGraph([Task("r", "r", kind=Kind.REVIEW.value, model="opus")])
    first = predict(g, store, gates=False)["per_task"]["r"]
    assert (first["seconds"], first["cost"], first["model"], first["fix_round"]) == (150, 1.0, "opus", False)
    store.save_handoff("r", 1, "h", "o", {"notes_for_downstream": "VERDICT: APPROVE"})        # it ran before
    fix = predict(g, store, gates=False)["per_task"]["r"]
    assert (fix["seconds"], fix["cost"], fix["model"], fix["fix_round"]) == (40, 0.2, "sonnet", True)
    assert "review/sonnet/fix round as serial point (n=3)" == fix["source"]
    off = predict(g, store, gates=False, review_tiering=False)["per_task"]["r"]
    assert off["model"] == "opus" and off["seconds"] == 150                    # no opus fix rows: opus level


async def test_run_records_fix_round_live(repo):
    store = Store(repo.macc_dir / "state.db")
    g = diamond()
    g.validate()
    cfg = SchedulerConfig(integration_gates=["true"], skip_noop_integrate=False)
    await Scheduler(repo, store, g, MockWorker(), cfg).run()
    n_first = store._conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    g.update_spec("a", g.tasks["a"].spec + " (changed)")
    await Scheduler(repo, store, g, MockWorker(), cfg).run()
    rows = store._conn.execute("SELECT id, task_id, fix_round FROM usage ORDER BY id").fetchall()
    assert all(r["fix_round"] == 0 for r in rows[:n_first])
    assert {r["task_id"]: r["fix_round"] for r in rows[n_first:]} == {"a": 1, "i": 1, "r": 1}
