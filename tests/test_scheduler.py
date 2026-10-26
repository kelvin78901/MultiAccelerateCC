import asyncio
import subprocess

import pytest

from macc.graph import Kind, State, Task, TaskGraph
from macc.scheduler import Scheduler, SchedulerConfig
from macc.store import Store
from macc.workers import MockWorker
from macc.worktree import Repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return Repo(root)


def web_tasks():
    return [
        Task("contract", "contract", kind=Kind.CONTRACT.value, owns=["api/**"], est_minutes=5,
             done_when=["test -f api/contract.txt"]),
        Task("backend", "backend", deps=["contract"], owns=["server/**"], est_minutes=30,
             done_when=["test -f server/backend.txt"]),
        Task("frontend", "frontend", deps=["contract"], owns=["web/**"], est_minutes=20),
        Task("tests", "tests", deps=["contract"], owns=["e2e/**"], est_minutes=10, spec="mock:sleep=0.3"),
        Task("deploy", "deploy", owns=["infra/**"], est_minutes=15),
        Task("integrate", "integrate", kind=Kind.INTEGRATE.value, deps=["backend", "frontend", "tests", "deploy"]),
    ]


async def run(repo, tasks, worker=None, **cfg):
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks)
    g.validate()
    worker = worker or MockWorker()
    events = []
    s = Scheduler(repo, store, g, worker, SchedulerConfig(**cfg),
                  on_event=lambda t, tid, p: events.append((t, tid)))
    summary = await s.run()
    return s, store, g, worker, events, summary


async def test_parallel_fan_out_and_merge(repo):
    s, store, g, worker, events, summary = await run(repo, web_tasks(), max_parallel=4)
    assert summary["finished"], summary
    assert g.summary() == {"done": 6}
    # contract ran first and alone among its subtree; deploy (independent) ran in the first wave too
    first_wave = [tid for typ, tid in events if typ == "dispatched"][:2]
    assert set(first_wave) == {"contract", "deploy"}
    # every task's output landed in the integration tree
    for p in ["api/contract.txt", "server/backend.txt", "web/frontend.txt", "e2e/tests.txt", "infra/deploy.txt"]:
        assert (repo.integration_path / p).exists(), p
    assert store.usage_totals()["runs"] == 6
    assert (repo.macc_dir / "STATE.md").exists()
    assert not repo.task_path("backend").exists()   # worktrees cleaned up


async def test_waiting_time_is_used(repo):
    """A slow task must not block an independent one from being dispatched and finished."""
    tasks = [
        Task("slow", "slow", owns=["a/**"], spec="mock:sleep=0.6"),
        Task("fast", "fast", owns=["b/**"], spec="mock:sleep=0.05"),
        Task("after_fast", "after_fast", deps=["fast"], owns=["c/**"], spec="mock:sleep=0.05"),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=3)
    order = [tid for typ, tid in events if typ == "done"]
    assert order.index("fast") < order.index("slow")
    assert order.index("after_fast") < order.index("slow")   # dispatched while slow still running
    assert summary["seconds"] < 1.2


async def test_retry_then_fail_and_resume(repo):
    tasks = [
        Task("ok", "ok", owns=["a/**"]),
        Task("flaky", "flaky", owns=["b/**"], spec="mock:fail_first", max_attempts=2),
        Task("dead", "dead", owns=["c/**"], spec="mock:fail_first", max_attempts=1),
        Task("child", "child", deps=["dead"], owns=["d/**"]),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["flaky"].state == State.DONE.value and g.tasks["flaky"].attempts == 2
    assert g.tasks["dead"].state == State.FAILED.value
    assert g.tasks["child"].state == State.PENDING.value
    assert summary["failed"] == ["dead"]
    assert ("retry", "flaky") in events and ("failed", "dead") in events

    # resume from disk: bump attempts for dead and run again -> everything completes
    store2 = Store(repo.macc_dir / "state.db")
    g2 = store2.load_graph()
    g2.tasks["dead"].max_attempts = 3
    g2.tasks["dead"].state = State.PENDING.value
    s2 = Scheduler(repo, store2, g2, MockWorker())
    summary2 = await s2.run()
    assert summary2["finished"]
    assert g2.tasks["child"].state == State.DONE.value


async def test_ownership_conflict_serialises(repo):
    tasks = [
        Task("a", "a", owns=["shared/**"], spec="mock:sleep=0.2 mock:write=shared/a.txt"),
        Task("b", "b", owns=["shared/sub/**"], spec="mock:write=shared/sub/b.txt"),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=4)
    assert summary["finished"]
    dispatched = [(typ, tid) for typ, tid in events if typ in ("dispatched", "done")]
    # b must be dispatched only after a is done
    assert dispatched.index(("done", "a")) < dispatched.index(("dispatched", "b"))


async def test_token_budget_stops_dispatch(repo):
    tasks = [Task(f"t{i}", f"t{i}", owns=[f"d{i}/**"], est_tokens=1000) for i in range(5)]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=1, token_budget=2600)
    assert summary["stopped_reason"] == "budget_exceeded"
    assert g.summary()["done"] == 2        # 1250 tokens each actual; est 1000 -> third would exceed
    assert g.summary()["pending"] == 3


async def test_gate_failure_retries_with_error_context(repo):
    tasks = [Task("g", "g", owns=["x/**"], done_when=["test -f x/never.txt"], max_attempts=2)]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["g"].state == State.FAILED.value
    assert "gate failed" in g.tasks["g"].last_error
    ctx = (repo.macc_dir / "context" / "g.md").read_text()
    assert "Previous attempt failed" in ctx     # second attempt saw the error


async def test_integration_gate_reverts_merge(repo):
    tasks = [Task("bad", "bad", owns=["x/**"], max_attempts=1)]
    s, store, g, worker, events, summary = await run(
        repo, tasks, integration_gates=["test ! -f x/bad.txt"])
    assert g.tasks["bad"].state == State.FAILED.value
    assert not (repo.integration_path / "x" / "bad.txt").exists()
    assert ("integration_gate_failed", "bad") in events


async def test_stubs_become_debts(repo):
    tasks = [Task("s", "s", owns=["x/**"], spec="mock:stub", allow_stubs=False)]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["s"].state == State.DONE.value
    assert len(store.open_debts()) == 1
    assert ("stub_without_permission", "s") in events
    assert "open debts" in (repo.macc_dir / "STATE.md").read_text()


async def test_spec_change_mid_run_preempts_and_reruns(repo):
    """Design change while a downstream task is running: it is preempted, upstream redone,
    downstream re-run on the new inputs. No attempt is charged for the preempted run."""
    tasks = [
        Task("up", "up", owns=["u/**"]),
        Task("down", "down", deps=["up"], owns=["d/**"], spec="mock:sleep=0.5"),
    ]
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks)
    g.validate()
    worker = MockWorker()
    events = []
    s = Scheduler(repo, store, g, worker, on_event=lambda t, tid, p: events.append((t, tid)))

    async def change_design():
        # wait until `down` is running, then change `up`'s spec (user changed their mind)
        while g.tasks["down"].state != State.RUNNING.value:
            await asyncio.sleep(0.02)
        affected = g.update_spec("up", "new requirement mock:write=u/v2.txt")
        store.add_decision("Switch u to v2", "user changed design", affects=["up"])
        assert "down" in affected and g.tasks["down"].preempt
        s.running["down"].cancel()

    await asyncio.gather(s.run(), change_design())
    assert g.tasks["up"].state == State.DONE.value and worker.calls.count("up") == 2
    assert g.tasks["down"].state == State.DONE.value and g.tasks["down"].attempts == 1
    assert ("preempted", "down") in events and ("stale_rerun", "up") in events
    assert (repo.integration_path / "u" / "v2.txt").exists()
    assert worker.calls.count("down") == 2          # one cancelled run + one real run
    ctx = (repo.macc_dir / "context" / "down.md").read_text()
    assert "Switch u to v2" in ctx                  # decision visible to the re-run


async def test_stale_revalidation_skips_rerun_when_gates_pass(repo):
    tasks = [
        Task("up", "up", owns=["u/**"]),
        Task("down", "down", deps=["up"], owns=["d/**"], done_when=["test -f d/down.txt"]),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert summary["finished"]
    # upstream changes after everything is done
    g.update_spec("up", "v2 mock:write=u/v2.txt")
    s2 = Scheduler(repo, store, g, worker, on_event=lambda t, tid, p: events.append((t, tid)))
    await s2.run()
    assert worker.calls.count("up") == 2
    assert worker.calls.count("down") == 1         # revalidated, not re-run
    assert ("revalidated", "down") in events


async def test_manual_task_blocks(repo):
    tasks = [Task("m", "approve", kind=Kind.MANUAL.value), Task("c", "c", deps=["m"], owns=["x/**"])]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["m"].state == State.BLOCKED.value and summary["blocked"] == ["m"]
    assert worker.calls == []


async def test_broken_baseline_gate_blocks_without_charging_attempt(repo):
    """If the integration gate fails on the baseline too, it is an environment problem:
    task -> BLOCKED, attempt not charged, merge reverted."""
    tasks = [Task("t", "t", owns=["x/**"], max_attempts=2)]
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=["false"])
    t = g.tasks["t"]
    assert t.state == State.BLOCKED.value and t.attempts == 0
    assert ("gate_broken_baseline", "t") in events and ("retry", "t") not in events
    assert worker.calls == ["t"]                      # exactly one worker run, no retry loop
    assert not (repo.integration_path / "x" / "t.txt").exists()
    assert "baseline" in t.last_error


async def test_review_reject_reopens_reviewed_tasks(repo):
    tasks = [
        Task("impl", "impl", owns=["x/**"]),
        Task("review", "review", kind=Kind.REVIEW.value, deps=["impl"], owns=["docs/review/**"],
             spec="mock:reject_first", max_attempts=2),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert summary["finished"], summary
    assert worker.calls.count("impl") >= 2 and worker.calls.count("review") == 2
    assert ("review_rejected", "review") in events
    # the re-run of impl saw the review notes
    ctx = (repo.macc_dir / "context" / "impl.md").read_text()
    assert "review rejected" in ctx and "needs tests" in ctx
    assert any(d["title"].startswith("Review round") for d in store.decisions())


async def test_review_reject_loop_is_bounded(repo):
    tasks = [
        Task("impl", "impl", owns=["x/**"]),
        Task("review", "review", kind=Kind.REVIEW.value, deps=["impl"], owns=["docs/**"],
             spec="mock:reject_first mock:reject_always", max_attempts=2),
    ]
    # make mock reject on every attempt
    worker = MockWorker()
    orig = worker.__call__

    async def always_reject(task, wt, pack):
        r = await orig(task, wt, pack)
        if task.kind == "review":
            r.fields["notes_for_downstream"] = "REJECT: still bad"
        return r
    worker.__call__ = always_reject  # type: ignore
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks); g.validate()
    s = Scheduler(repo, store, g, always_reject)
    summary = await s.run()
    assert g.tasks["review"].state == State.FAILED.value
    assert summary["failed"] == ["review"]
    assert worker.calls.count("review") == 2       # bounded by max_attempts
