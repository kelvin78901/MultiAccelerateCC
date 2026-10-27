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
    assert summary["seconds"] < 2.5          # slow+fast+after_fast serial would be >= 0.7; generous for CI load


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


async def test_handoff_records_gate_seconds(repo):
    # REA #6404: the operator could not tell how long the Java task's bridge-sources gate took
    tasks = [Task("g", "g", owns=["x/**"], done_when=["sleep 0.2"])]
    s, store, g, worker, events, summary = await run(repo, tasks)
    gate = store.latest_handoff("g")["gates"][0]
    assert gate["cmd"] == "sleep 0.2" and gate["ok"] and gate["seconds"] >= 0.2 and gate["shared"] is False


async def test_gate_failure_keeps_the_head_of_long_output(repo):
    # REA verify:ghidra: "Error: Ghidra set_comment rejection drifted: {...10 KB JSON...}" then a stack;
    # the tail alone hid which operation drifted, so the retry could not tell what to fix
    cmd = ("python3 -c \"import sys; sys.stderr.write('Error: CAUSE-AT-HEAD ' + 'x' * 9000 + "
           "' STACK-AT-TAIL'); sys.exit(1)\"")
    tasks = [Task("g", "g", owns=["x/**"], done_when=[cmd], max_attempts=1)]
    s, store, g, worker, events, summary = await run(repo, tasks)
    err = g.tasks["g"].last_error
    assert "CAUSE-AT-HEAD" in err and "STACK-AT-TAIL" in err and "chars omitted" in err
    assert "CAUSE-AT-HEAD" in store.latest_event("failed")["payload"]["error"]


def test_clip_output_keeps_both_ends():
    from macc.worktree import clip_output
    assert clip_output("short", 3, 3) == "short"
    out = clip_output("A" * 10 + "B" * 100 + "C" * 10, 10, 10)
    assert out.startswith("A" * 10) and out.endswith("C" * 10) and "100 chars omitted" in out


async def test_integration_gate_reverts_merge(repo):
    tasks = [Task("bad", "bad", owns=["x/**"], max_attempts=1)]
    s, store, g, worker, events, summary = await run(
        repo, tasks, integration_gates=["test ! -f x/bad.txt"], gate_policy="full")
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
    assert ("preempted", "down") in events
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
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=["false"], gate_policy="full")
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
    assert "Review round 1 rejected" in ctx and "needs tests" in ctx   # via decision record
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


async def _conflict_setup(repo, merge_spec=""):
    """Two tasks editing the same file concurrently (ownership deliberately overlapping-but-undeclared)."""
    (repo.root / "shared.txt").write_text("line1\nline2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "shared"], cwd=repo.root, check=True)
    # no ownership declared (= may write anything) is the one way two concurrent tasks can collide
    tasks = [
        Task("a", "a", owns=[], spec="mock:sleep=0.1 mock:write=shared.txt " + merge_spec),
        Task("b", "b", owns=[], spec="mock:sleep=0.2 mock:write=shared.txt " + merge_spec),
    ]
    return tasks


async def test_merge_agent_resolves_textual_conflict(repo):
    tasks = await _conflict_setup(repo)
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2)
    assert summary["finished"], summary
    types = [e for e, _ in events]
    assert "conflict" in types and "merge_agent" in types and "merge_agent_resolved" in types
    txt = (repo.integration_path / "shared.txt").read_text()
    assert "<<<<<<<" not in txt and "# a attempt" in txt and "# b attempt" in txt   # both intents kept
    assert any("merge" in u["worker"] for u in [dict(r) for r in store._conn.execute("SELECT worker FROM usage")])


async def test_merge_agent_failure_falls_back_to_retry(repo):
    tasks = await _conflict_setup(repo, merge_spec="mock:merge_fail")
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2)
    types = [e for e, _ in events]
    assert "merge_agent_failed" in types and "retry" in types
    assert summary["finished"], summary           # retry on the fresh base succeeds (b re-forks after a merged)
    txt = (repo.integration_path / "shared.txt").read_text()
    assert "<<<<<<<" not in txt


async def test_merge_agent_can_be_disabled(repo):
    tasks = await _conflict_setup(repo)
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2, merge_agent=False)
    types = [e for e, _ in events]
    assert "merge_agent" not in types and "retry" in types and summary["finished"]


async def test_spec_change_reruns_node_and_review_but_revalidates_code(repo):
    tasks = [
        Task("impl", "impl", owns=["x/**"], done_when=["test -f x/impl.txt"]),
        Task("other", "other", deps=["impl"], owns=["y/**"], done_when=["test -f y/other.txt"]),
        Task("review", "review", kind=Kind.REVIEW.value, deps=["other"], owns=["docs/**"]),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert summary["finished"]
    g.update_spec("impl", "v2 mock:write=x/impl.txt")
    s2 = Scheduler(repo, store, g, worker, on_event=lambda t, tid, p: events.append((t, tid)))
    await s2.run()
    assert worker.calls.count("impl") >= 2        # re-done (mock may need a 2nd attempt: identical content = "no changes")
    assert worker.calls.count("other") == 1       # revalidated by its gate, not re-run
    assert worker.calls.count("review") == 2      # reviews always re-run when stale
    assert ("revalidated", "other") in events and ("stale_rerun", "review") in events


async def test_out_of_scope_write_fails_attempt(repo):
    tasks = [Task("t", "t", owns=["a/**"], spec="mock:write=b/evil.txt", max_attempts=1)]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["t"].state == State.FAILED.value
    assert ("ownership_violation", "t") in events
    assert not (repo.integration_path / "b" / "evil.txt").exists()


async def test_out_of_scope_write_dropped_when_lenient(repo):
    tasks = [Task("t", "t", owns=["a/**"], spec="mock:write=b/evil.txt", max_attempts=1)]
    s, store, g, worker, events, summary = await run(repo, tasks, strict_ownership=False)
    assert g.tasks["t"].state == State.FAILED.value          # nothing in scope was committed -> "no changes"
    assert ("ownership_violation", "t") in events


async def test_transient_failure_not_charged(repo):
    tasks = [Task("t", "t", owns=["a/**"], spec="mock:transient_first", max_attempts=1)]
    s, store, g, worker, events, summary = await run(repo, tasks)
    assert g.tasks["t"].state == State.DONE.value and g.tasks["t"].attempts == 1
    assert ("transient_failure", "t") in events and worker.calls.count("t") == 2


async def test_merge_after_runs_parallel_but_merges_in_order(repo):
    """tests run concurrently with impl, but merge only after impl is in; the integration gate
    (which needs impl's file) therefore never fails on the old code."""
    tasks = [
        Task("contract", "c", kind=Kind.CONTRACT.value, owns=["api/**"], est_minutes=1),
        Task("impl", "impl", deps=["contract"], owns=["src/**"], spec="mock:sleep=0.4", est_minutes=10),
        Task("tests", "tests", deps=["contract"], merge_after=["impl"], owns=["tests/**"],
             spec="mock:sleep=0.05", est_minutes=5),
    ]
    s, store, g, worker, events, summary = await run(
        repo, tasks, max_parallel=3, integration_gates=["test ! -f tests/tests.txt || test -f src/impl.txt"])
    assert summary["finished"], summary
    seq = [(e, tid) for e, tid in events if e in ("dispatched", "awaiting_merge", "done")]
    assert seq.index(("dispatched", "tests")) < seq.index(("done", "impl"))       # ran in parallel
    assert ("awaiting_merge", "tests") in events
    assert seq.index(("done", "impl")) < seq.index(("done", "tests"))             # merged in order
    assert "integration_gate_failed" not in [e for e, _ in events]
    assert not repo.task_path("tests").exists()


async def test_merge_after_target_failure_blocks_parked(repo):
    tasks = [
        Task("impl", "impl", owns=["src/**"], spec="mock:fail_first", max_attempts=1),
        Task("tests", "tests", merge_after=["impl"], owns=["tests/**"]),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2)
    assert g.tasks["impl"].state == State.FAILED.value
    assert g.tasks["tests"].state == State.BLOCKED.value
    assert ("merge_blocked", "tests") in events


def test_merge_after_validation():
    g = TaskGraph([Task("a", "a", merge_after=["b"]), Task("b", "b", deps=["a"])])
    import pytest as _pt
    from macc.graph import GraphError
    with _pt.raises(GraphError, match="cycle"):
        g.validate()
    g2 = TaskGraph([Task("a", "a"), Task("b", "b", deps=["a"], merge_after=["a"])])
    with _pt.raises(GraphError, match="redundant"):
        g2.validate()


async def test_review_reopen_names_specific_tasks(repo):
    tasks = [
        Task("a", "a", owns=["a/**"]),
        Task("b", "b", owns=["b/**"]),
        Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["a", "b"], owns=["a/**", "b/**"]),
        Task("review", "r", kind=Kind.REVIEW.value, deps=["integrate"], owns=["docs/**"], max_attempts=2),
    ]
    worker = MockWorker()
    orig = worker.__call__
    rounds = {"n": 0}

    async def w(task, wt, pack):
        r = await orig(task, wt, pack)
        if task.kind == "review":
            rounds["n"] += 1
            r.fields["notes_for_downstream"] = ("VERDICT: REJECT\nREOPEN: b\nb lacks tests" if rounds["n"] == 1
                                                else "VERDICT: APPROVE")
        return r
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks); g.validate()
    events = []
    s = Scheduler(repo, store, g, w, on_event=lambda t, tid, p: events.append((t, tid, p)))
    summary = await s.run()
    assert summary["finished"], summary
    rej = next(p for t, tid, p in events if t == "review_rejected")
    assert rej["reviewed"] == ["b"]
    assert rej["held"] == ["integrate"]          # between b and the review: must not stay DONE
    assert worker.calls.count("b") >= 2 and worker.calls.count("a") == 1
    # the second review must see b's redo: it is dispatched only after b is done again and integrate
    # has been revalidated or re-run (regression: it used to start in the same tick as b's redo)
    seq = [(t, tid) for t, tid, p in events]
    second_review = [i for i, e in enumerate(seq) if e == ("dispatched", "review")][1]
    b_redone = [i for i, e in enumerate(seq) if e == ("done", "b")][1]
    integrate_ok = max(i for i, e in enumerate(seq) if e in (("done", "integrate"), ("revalidated", "integrate")))
    assert b_redone < integrate_ok < second_review


async def test_review_reopening_integrate_runs_its_worker_even_when_gates_are_green(repo):
    # REA #6404: REOPEN: integrate was answered by integrate_skipped (gates green, no worker), so the
    # second review saw the same tree and rejected it again
    tasks = [
        Task("a", "a", owns=["a/**"]),
        Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["a"], owns=["a/**"]),
        Task("review", "r", kind=Kind.REVIEW.value, deps=["integrate"], owns=["docs/**"], max_attempts=2),
    ]
    worker = MockWorker()
    orig = worker.__call__
    rounds = {"n": 0}

    async def w(task, wt, pack):
        r = await orig(task, wt, pack)
        if task.kind == "review":
            rounds["n"] += 1
            r.fields["notes_for_downstream"] = ("VERDICT: REJECT\nREOPEN: integrate\nmissing caller"
                                                if rounds["n"] == 1 else "VERDICT: APPROVE")
        return r
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks); g.validate()
    events = []
    s = Scheduler(repo, store, g, w, SchedulerConfig(integration_gates=["true"]),
                  on_event=lambda t, tid, p: events.append((t, tid, p)))
    summary = await s.run()
    assert summary["finished"], summary
    seq = [(t, tid) for t, tid, p in events]
    rejected = seq.index(("review_rejected", "review"))
    after = seq[rejected:]
    assert ("dispatched", "integrate") in after          # a worker answered the findings
    assert ("integrate_skipped", "integrate") not in after
    assert ("integrate_skipped", "integrate") in seq[:rejected]   # first time round it was a no-op


async def test_changed_integrate_runs_its_worker_even_when_gates_are_green(repo):
    # REA #6404: `macc change integrate` with review findings, gates already green -> it must not be skipped
    tasks = [Task("a", "a", owns=["a/**"]),
             Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["a"], owns=["a/**"])]
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=["true"])
    assert ("integrate_skipped", "integrate") in events and "integrate" not in worker.calls
    g.update_spec("integrate", "fix the review findings")
    store.save_graph(g)
    store.event("spec_changed", "integrate", affected=["integrate"])
    events2 = []
    worker2 = MockWorker()
    s2 = Scheduler(repo, store, store.load_graph(), worker2, SchedulerConfig(integration_gates=["true"]),
                   on_event=lambda t, tid, p: events2.append((t, tid)))
    assert (await s2.run())["finished"]
    assert "integrate" in worker2.calls and ("integrate_skipped", "integrate") not in events2


async def test_reopened_task_that_only_asks_holds_the_review(repo):
    # REA #6404 round 3: integrate was reopened, could not touch an unowned file, changed nothing and asked
    # for ownership in open_questions; the review then ran again and rejected the same tree
    tasks = [
        Task("a", "a", owns=["a/**"]),
        Task("integrate", "i", kind=Kind.INTEGRATE.value, deps=["a"], owns=["a/**"]),
        Task("review", "r", kind=Kind.REVIEW.value, deps=["integrate"], owns=["docs/**"], max_attempts=3),
    ]
    worker = MockWorker()
    orig = worker.__call__
    calls = {"review": 0, "integrate": 0}

    async def w(task, wt, pack):
        r = await orig(task, wt, pack)
        if task.id in calls:
            calls[task.id] += 1
        if task.kind == "review":
            r.fields["notes_for_downstream"] = "VERDICT: REJECT\nREOPEN: integrate\nfix x/y.mjs"
        if task.id == "integrate":                         # it cannot touch x/y.mjs: no change, only a question
            (wt / "a" / "integrate.txt").unlink(missing_ok=True)
            r.fields["open_questions"] = "grant integrate ownership of x/y.mjs"
        return r
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks); g.validate()
    events = []
    s = Scheduler(repo, store, g, w, SchedulerConfig(integration_gates=["true"]),
                  on_event=lambda t, tid, p: events.append((t, tid, p)))
    await s.run()
    assert calls["review"] == 1                              # not re-run on the unchanged tree
    assert g.tasks["review"].state == State.BLOCKED.value
    assert "grant integrate ownership" in g.tasks["review"].last_error
    assert any(t == "reopen_unanswered" and tid == "integrate" for t, tid, p in events)


async def test_cost_budget_is_cumulative_across_runs(repo):
    store = Store(repo.macc_dir / "state.db")
    store.add_usage("old", 1, "x", True, cost_usd=5.0)       # spent before this plan
    g = TaskGraph([Task("t", "t", owns=["a/**"])]); g.validate()
    cfg = SchedulerConfig(cost_budget_usd=1.0, cost_baseline_usd=5.0)
    s = Scheduler(repo, store, g, MockWorker(), cfg)
    assert s.cost_used == 0.0
    summary = await s.run()
    assert summary["finished"]


async def test_summary_separates_cost_this_run_from_cost_since_plan(repo):
    class PricedWorker(MockWorker):
        async def __call__(self, task, wt_path, context_pack):
            res = await super().__call__(task, wt_path, context_pack)
            res.cost_usd = 0.25
            return res

    store = Store(repo.macc_dir / "state.db")
    store.add_usage("old", 1, "x", True, cost_usd=5.0)       # before this plan was loaded
    store.add_usage("prev", 1, "x", True, cost_usd=1.5)      # an earlier run of this plan
    g = TaskGraph([Task("t", "t", owns=["a/**"]), Task("u", "u", owns=["b/**"])]); g.validate()
    events = []
    s = Scheduler(repo, store, g, PricedWorker(), SchedulerConfig(cost_baseline_usd=5.0),
                  on_event=lambda t, tid, p: events.append((t, p)))
    summary = await s.run()
    assert summary["cost_this_run"] == 0.5 and summary["cost_since_plan"] == 2.0
    assert "cost_usd" not in summary
    fin = next(p for t, p in events if t == "run_finished")
    assert fin["cost_this_run"] == 0.5 and fin["cost_since_plan"] == 2.0


async def test_orphaned_active_tasks_reset_on_restart(repo):
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph([Task("a", "a", owns=["a/**"]), Task("b", "b", owns=["b/**"], merge_after=["a"])]); g.validate()
    g.tasks["a"].state = State.RUNNING.value; g.tasks["a"].attempts = 1
    g.tasks["b"].state = State.REVIEW.value; g.tasks["b"].attempts = 1      # was awaiting merge when we died
    store.save_graph(g)
    events = []
    s = Scheduler(repo, store, store.load_graph(), MockWorker(), on_event=lambda t, tid, p: events.append((t, tid)))
    summary = await s.run()
    assert summary["finished"]
    assert ("orphan_reset", "a") in events and ("orphan_reset", "b") in events
    assert s.graph.tasks["a"].attempts == 1 and s.graph.tasks["b"].attempts == 1   # not double-charged


async def test_transient_failures_are_bounded(repo):
    from macc.workers import WorkerResult

    async def always_transient(task, wt, pack):
        return WorkerResult(ok=False, error="rate limit", transient=True)
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph([Task("t", "t", owns=["a/**"])]); g.validate()
    events = []
    cfg = SchedulerConfig(max_transients=2)
    s = Scheduler(repo, store, g, always_transient, cfg, on_event=lambda t, tid, p: events.append((t, tid)))
    await s.run()
    assert g.tasks["t"].state == State.BLOCKED.value and g.tasks["t"].transients == 3
    assert ("transient_limit", "t") in events
    assert [e for e, _ in events].count("transient_failure") == 2


async def test_parked_tasks_reset_when_run_ends_early(repo):
    """budget stops dispatch of the merge_after target -> parked task must not be left in REVIEW."""
    tasks = [
        Task("impl", "impl", owns=["src/**"], est_tokens=5000),
        Task("tests", "tests", merge_after=["impl"], owns=["tests/**"], est_tokens=10),
    ]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=1, token_budget=2000)
    assert summary["stopped_reason"] == "budget_exceeded"
    assert g.tasks["tests"].state == State.PENDING.value
    assert ("parked_reset", "tests") in events


def test_merge_after_owns_overlap_rejected():
    import pytest as _pt
    from macc.graph import GraphError
    g = TaskGraph([Task("a", "a", owns=["x/**"]), Task("b", "b", merge_after=["a"], owns=["x/y/**"])])
    with _pt.raises(GraphError, match="overlap"):
        g.validate()


async def test_done_when_sees_macc_refs_and_usage_records_estimate(repo):
    t = Task("a", "a", owns=["a/**"], est_minutes=7,
             done_when=['test -n "$MACC_BASE" && test -n "$MACC_INTEGRATION"',
                        'git merge-base --is-ancestor "$MACC_BASE" HEAD'])
    s, store, g, worker, events, summary = await run(repo, [t])
    assert summary["finished"], (summary, events)
    rows = store._conn.execute("SELECT est_minutes FROM usage WHERE task_id='a'").fetchall()
    assert [r[0] for r in rows] == [7]


async def test_rules_conflict_event_is_advisory_and_emitted_once(repo):
    (repo.root / "CLAUDE.md").write_text("Interpreter: `.venv/bin/python` (never bare python3).\n")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "claude md"], cwd=repo.root, check=True)
    tasks = [Task("a", "a", owns=["a/**"]), Task("b", "b", owns=["b/**"])]
    s, store, g, worker, events, summary = await run(
        repo, tasks, project_rules="- Interpreter is `/opt/p/.venv/bin/python` (absolute).")
    assert summary["finished"]                                   # advisory: blocks nothing
    conflicts = [tid for typ, tid in events if typ == "rules_conflict"]
    assert len(conflicts) == 1 and conflicts[0] in {"a", "b"}   # once per run, not per task
    stored = [e for e in store.events(limit=500) if e["type"] == "rules_conflict"]
    assert len(stored) == 1 and "absolute" in stored[0]["payload"]


async def test_no_rules_conflict_when_consistent(repo):
    (repo.root / "CLAUDE.md").write_text("Interpreter: `/opt/p/.venv/bin/python`.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "claude md"], cwd=repo.root, check=True)
    s, store, g, worker, events, summary = await run(
        repo, [Task("a", "a", owns=["a/**"])], project_rules="- Interpreter is `/opt/p/.venv/bin/python`.")
    assert not [e for e in events if e[0] == "rules_conflict"]


def _counting_gate(tmp_path):
    f = tmp_path / "full_gate_runs.txt"
    return f, [f"echo run >> {f}"]


async def test_gate_policy_per_task_runs_full_suite_after_integrate_only(repo, tmp_path):
    count, gate = _counting_gate(tmp_path)
    tasks = [Task("a", "a", owns=["a/**"], done_when=["test -f a/a.txt"]),
             Task("b", "b", owns=["b/**"]),
             Task("i", "i", kind=Kind.INTEGRATE.value, deps=["a", "b"], owns=["i/**"])]
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=gate)
    assert summary["finished"] and summary["gate_policy"] == "per_task"
    assert count.read_text().count("run") == 1                 # only after the integrate merge
    assert summary["final_gate"] == "covered"                  # nothing merged after that full gate
    full = [e for e in store.events(limit=500) if e["type"] == "full_gate"]
    assert len(full) == 1 and full[0]["task_id"] == "i"


async def test_gate_policy_full_runs_suite_after_every_merge(repo, tmp_path):
    count, gate = _counting_gate(tmp_path)
    tasks = [Task("a", "a", owns=["a/**"]), Task("b", "b", owns=["b/**"]),
             Task("i", "i", kind=Kind.INTEGRATE.value, deps=["a", "b"], owns=["i/**"])]
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=gate, gate_policy="full",
                                                     skip_noop_integrate=False)
    assert summary["finished"] and summary["final_gate"] == "n/a"
    assert count.read_text().count("run") == 3


async def test_per_task_without_integrate_runs_one_final_full_gate(repo, tmp_path):
    count, gate = _counting_gate(tmp_path)
    s, store, g, worker, events, summary = await run(
        repo, [Task("a", "a", owns=["a/**"]), Task("b", "b", owns=["b/**"])], integration_gates=gate)
    assert summary["final_gate"] == "passed" and count.read_text().count("run") == 1
    assert summary["gate_seconds"] >= 0


async def test_final_gate_failure_reports_merge_order_and_keeps_state(repo):
    tasks = [Task("ok", "ok", owns=["ok/**"]), Task("bad", "bad", owns=["x/**"], deps=["ok"])]
    evs = []
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(tasks); g.validate()
    s = Scheduler(repo, store, g, MockWorker(), SchedulerConfig(integration_gates=["test ! -f x/bad.txt"]),
                  on_event=lambda t, tid, p: evs.append((t, p)))
    summary = await s.run()
    assert summary["final_gate"] == "failed"
    fail = next(p for t, p in evs if t == "final_gate_failed")
    assert fail["merged"] == ["ok", "bad"] and fail["cmd"] == "test ! -f x/bad.txt"
    assert g.tasks["bad"].state == State.DONE.value               # not rolled back
    assert (repo.integration_path / "x" / "bad.txt").exists()


async def test_per_task_gate_failure_on_integration_tree_fails_the_task(repo):
    # t forks before w merges (both dispatched at once, t is slower): its done_when passes in its own
    # worktree but fails on the integration tree, where w/w.txt now exists
    tasks = [Task("w", "w", owns=["w/**"]),
             Task("t", "t", owns=["t/**"], max_attempts=1, spec="mock:sleep=0.3", done_when=["test ! -f w/w.txt"])]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2, integration_gates=["true"])
    assert g.tasks["t"].state == State.FAILED.value
    assert ("integration_gate_failed", "t") in events
    assert ("gate_broken_baseline", "t") not in events         # a task's own gate is not a baseline check
    assert not (repo.integration_path / "t" / "t.txt").exists()  # merge reverted


def test_gate_policy_is_validated():
    with pytest.raises(ValueError):
        SchedulerConfig(gate_policy="sometimes")


def test_review_context_pack_reports_latest_full_gate(tmp_path):
    from macc.handoff import build_context_pack
    store = Store(tmp_path / "s.db")
    g = TaskGraph([Task("i", "i", kind="integrate"), Task("r", "r", kind="review", deps=["i"])])
    assert "Latest full integration gate" not in build_context_pack(g.tasks["r"], g, store)
    store.event("full_gate", "i", ok=True, seconds=12.5, cmds=["pytest -q"], failed_cmd=None, head="a" * 40)
    same = build_context_pack(g.tasks["r"], g, store, integration_head="a" * 40)
    assert "PASSED" in same and "12.5 s" in same and "Do not re-run it in full" in same
    moved = build_context_pack(g.tasks["r"], g, store, integration_head="b" * 40)
    assert "older tree" in moved and "Do not re-run" not in moved
    code = build_context_pack(g.tasks["i"], g, store)
    assert "Latest full integration gate" not in code             # only reviewers get it


async def test_summary_reports_critical_path_with_estimate(repo):
    tasks = [Task("c", "c", kind=Kind.CONTRACT.value, owns=["api/**"], est_minutes=10, spec="mock:sleep=0.2"),
             Task("x", "x", deps=["c"], owns=["x/**"], est_minutes=30, spec="mock:sleep=0.2"),
             Task("y", "y", deps=["c"], owns=["y/**"], est_minutes=5)]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=3)
    cp = summary["critical_path"]
    assert cp["tasks"] == ["c", "x"] and cp["est_min"] == 40
    assert 0.3 <= cp["actual_s"] < 5 and cp["actual_over_est"] == round(cp["actual_s"] / 60 / 40, 3)


async def test_per_task_reuses_worktree_gate_when_tree_is_unchanged(repo, tmp_path):
    runs = tmp_path / "done_when_runs.txt"
    gate = f"echo run >> {runs}"
    # a alone: nothing merges between its gate and its merge, so the integration tree equals its own
    s, store, g, worker, events, summary = await run(repo, [Task("a", "a", owns=["a/**"], done_when=[gate])],
                                                     integration_gates=["true"])
    assert summary["finished"] and ("gate_reused", "a") in events
    assert runs.read_text().count("run") == 1            # worktree only; not repeated after the merge


async def test_per_task_reruns_gate_when_base_moved(repo, tmp_path):
    runs = tmp_path / "done_when_runs.txt"
    gate = f"echo run >> {runs}"
    tasks = [Task("w", "w", owns=["w/**"]),
             Task("t", "t", owns=["t/**"], spec="mock:sleep=0.3", done_when=[gate])]   # w merges first
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2, integration_gates=["true"])
    assert summary["finished"] and ("gate_reused", "t") not in events
    assert runs.read_text().count("run") == 2            # worktree, then again on the moved integration tree


async def test_budget_reservation_uses_cost_by_kind_and_defers_without_stopping(repo):
    class PricedWorker(MockWorker):
        async def __call__(self, task, wt_path, context_pack):
            res = await super().__call__(task, wt_path, context_pack)
            res.cost_usd = 1.8 if task.kind == "contract" else 0.2
            return res

    store = Store(repo.macc_dir / "state.db")
    tasks = [Task("c", "c", kind=Kind.CONTRACT.value, owns=["api/**"])] + \
            [Task(f"m{i}", f"m{i}", deps=["c"], owns=[f"m{i}/**"], spec="mock:sleep=0.1") for i in range(6)]
    g = TaskGraph(tasks); g.validate()
    for i in range(3):                                      # history: code runs cost ~$0.2
        store.add_usage(f"old{i}", 1, "claude", True, cost_usd=0.2, model="m", kind="code")
    store.add_usage("oldc", 1, "claude", True, cost_usd=1.8, model="m", kind="contract")
    events = []
    s = Scheduler(repo, store, g, PricedWorker(),
                  SchedulerConfig(max_parallel=6, cost_budget_usd=4.0, cost_baseline_usd=2.4),
                  on_event=lambda t, tid, p: events.append((t, tid)))
    summary = await s.run()
    # 1.8 spent after the contract; 6 code tasks at ~0.2 each fit in 4.0 with the per-kind estimate
    # (the old run-average estimate reserved 1.8 per task and held most of them back)
    first_wave = [tid for t, tid in events if t == "dispatched"][1:7]
    assert len(first_wave) == 6
    assert summary["finished"] and summary["stopped_reason"] is None
    assert not [e for e in events if e[0] == "budget_exceeded"]


async def test_budget_deferral_is_not_a_stop_until_nothing_runs(repo):
    class PricedWorker(MockWorker):
        async def __call__(self, task, wt_path, context_pack):
            res = await super().__call__(task, wt_path, context_pack)
            res.cost_usd = 1.0
            return res

    store = Store(repo.macc_dir / "state.db")
    tasks = [Task(f"t{i}", f"t{i}", owns=[f"t{i}/**"], spec="mock:sleep=0.1") for i in range(4)]
    g = TaskGraph(tasks); g.validate()
    store.add_usage("old", 1, "claude", True, cost_usd=1.0, model="m", kind="code")
    store.add_usage("old2", 1, "claude", True, cost_usd=1.0, model="m", kind="code")
    store.add_usage("old3", 1, "claude", True, cost_usd=1.0, model="m", kind="code")
    events = []
    s = Scheduler(repo, store, g, PricedWorker(),
                  SchedulerConfig(max_parallel=4, cost_budget_usd=2.5, cost_baseline_usd=3.0),
                  on_event=lambda t, tid, p: events.append((t, tid)))
    summary = await s.run()
    types = [t for t, _ in events]
    assert "budget_deferred" in types                      # 3rd task waited while two were running
    assert summary["stopped_reason"] == "budget_exceeded"  # then the money really ran out
    assert summary["states"].get("done") == 2


async def test_serial_points_share_and_bottleneck(repo):
    # c is a global serial point (everything waits for it) and overruns its estimate 3x+;
    # a and b run side by side, so neither is serial; r is the last task and therefore serial
    tasks = [Task("c", "c", kind=Kind.CONTRACT.value, owns=["api/**"], est_minutes=0.001, spec="mock:sleep=0.3"),
             Task("a", "a", deps=["c"], owns=["a/**"], est_minutes=1),
             Task("b", "b", deps=["c"], owns=["b/**"], est_minutes=1),
             Task("r", "r", kind=Kind.REVIEW.value, deps=["a", "b"], owns=["docs/**"], est_minutes=1)]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=3)
    ser = summary["serial"]
    assert ser["tasks"] == ["c", "r"]
    assert 0.3 <= ser["seconds"] and 0 < ser["share"] <= 1
    assert ("serial_bottleneck", "c") in events
    assert ("serial_bottleneck", "r") not in events            # r finished within 3x its estimate


async def test_independent_tasks_are_not_serial_points(repo):
    tasks = [Task("x", "x", owns=["x/**"]), Task("y", "y", owns=["y/**"])]
    s, store, g, worker, events, summary = await run(repo, tasks, max_parallel=2)
    assert summary["serial"]["tasks"] == [] and summary["serial"]["share"] == 0.0


def _integrate_plan(**a_kw):
    return [Task("a", "a", owns=["a/**"], **a_kw), Task("b", "b", owns=["b/**"]),
            Task("i", "i", kind=Kind.INTEGRATE.value, deps=["a", "b"], owns=["a/**", "b/**"],
                 done_when=["test -f a/a.txt"]),
            Task("r", "r", kind=Kind.REVIEW.value, deps=["i"], owns=["docs/**"])]


async def test_noop_integrate_is_skipped_when_gates_green_and_nothing_open(repo):
    s, store, g, worker, events, summary = await run(repo, _integrate_plan(), integration_gates=["true"])
    assert summary["finished"]
    assert ("integrate_skipped", "i") in events and "i" not in worker.calls
    assert "r" in worker.calls                                  # the review still ran, in the same run
    h = store.latest_handoff("i")
    assert h["notes_for_downstream"] == "skipped: gates green, nothing to integrate" and h["skipped"]
    assert g.tasks["i"].state == State.DONE.value


async def test_skipped_integrate_gate_time_counts_as_serial(repo):
    # REA #7296: a skipped integrate ran 77 s of gates with every task waiting, but counted 0 s serial
    tasks = _integrate_plan()
    tasks[2].done_when = ["sleep 0.3", "test -f a/a.txt"]
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=["true"])
    ev = store.latest_event("integrate_skipped")["payload"]
    assert ev["serial"] is True and ev["seconds"] >= 0.3
    assert "i" in summary["serial"]["tasks"] and summary["serial"]["seconds"] >= 0.3
    from macc.costmodel import backtest
    row = backtest(store)["runs"][0]
    worker_serial = sum(u["seconds"] for u in store._conn.execute(
        "SELECT seconds FROM usage WHERE serial = 1").fetchall())
    assert row["actual"]["serial_share"] * row["actual"]["wall_s"] >= worker_serial + 0.29


async def test_integrate_runs_when_a_debt_is_open(repo):
    s, store, g, worker, events, summary = await run(
        repo, _integrate_plan(spec="mock:stub", allow_stubs=True), integration_gates=["true"])
    assert ("integrate_skipped", "i") not in events and "i" in worker.calls


async def test_integrate_runs_when_upstream_left_open_questions(repo):
    worker = MockWorker()
    orig = worker.__call__

    async def asking(task, wt, pack):
        r = await orig(task, wt, pack)
        if task.id == "b":
            r.fields["open_questions"] = "should b also cover the legacy path?"
        return r
    asking.name = "mock"
    s, store, g, _, events, summary = await run(repo, _integrate_plan(), worker=asking, integration_gates=["true"])
    assert ("integrate_skipped", "i") not in events and "i" in worker.calls


async def test_integrate_runs_when_the_full_gate_or_its_own_gate_fails(repo):
    s, store, g, worker, events, summary = await run(repo, _integrate_plan(), integration_gates=["false"])
    assert ("integrate_skipped", "i") not in events and "i" in worker.calls
    tasks = _integrate_plan()
    tasks[2].done_when = ["test -f nothing/here.txt"]             # its own acceptance check fails
    s, store, g, worker, events, summary = await run(repo, tasks, integration_gates=["true"])
    assert ("integrate_skipped", "i") not in events


async def test_skip_can_be_disabled(repo):
    s, store, g, worker, events, summary = await run(repo, _integrate_plan(), integration_gates=["true"],
                                                     skip_noop_integrate=False)
    assert "i" in worker.calls and ("integrate_skipped", "i") not in events


def _review_plan():
    return [Task("c", "c", kind=Kind.CONTRACT.value, owns=["api/CONTRACT.md"]),
            Task("a", "a", deps=["c"], owns=["src/a.py"]),
            Task("i", "i", kind=Kind.INTEGRATE.value, deps=["a"], owns=["src/**"]),
            Task("r", "r", kind=Kind.REVIEW.value, deps=["i"], owns=["docs/**"], model="opus", effort="high")]


def _recording_worker():
    worker, seen = MockWorker(), {}
    orig = worker.__call__

    async def w(task, wt, pack):
        seen[task.id] = (task.model, task.effort, pack)
        return await orig(task, wt, pack)
    w.name = "mock"
    return worker, w, seen


async def test_low_risk_review_runs_on_the_cheaper_tier_with_facts(repo):
    worker, w, seen = _recording_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["true"])
    assert summary["finished"]
    assert seen["r"][:2] == ("sonnet", "medium")                  # this dispatch only...
    assert g.tasks["r"].model == "opus" and g.tasks["r"].effort == "high"   # ...the plan is unchanged
    tier = next(e for e in store.events(limit=500) if e["type"] == "review_tier")
    assert '"tier": "low"' in tier["payload"] and '"applied": true' in tier["payload"]
    pack = seen["r"][2]
    assert "# Facts for the review" in pack and "api/CONTRACT.md" in pack and "review tier: low" in pack
    assert (repo.macc_dir / "context" / "r.md").read_text() == pack


async def test_high_risk_review_keeps_the_planned_model(repo):
    worker, w, seen = _recording_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["true"],
                                                review_low_risk_max_lines=0)
    assert seen["r"][:2] == ("opus", "high")
    assert "review tier: high (1 source lines changed (> 0))" in seen["r"][2]


async def test_review_tiering_can_be_disabled_but_facts_remain(repo):
    worker, w, seen = _recording_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["true"],
                                                review_tiering=False)
    assert seen["r"][:2] == ("opus", "high") and "# Facts for the review" in seen["r"][2]


async def test_review_tier_measures_this_plans_change_from_plan_base(repo):
    # an earlier plan left a large, unmerged change on integration; this plan's own change is tiny
    ipath = repo.ensure_integration()
    (ipath / "big").mkdir()
    for i in range(8):
        (ipath / "big" / f"m{i}.py").write_text("".join(f"def f{i}_{j}():\n    return {j}\n" for j in range(40)))
    repo.commit_all(ipath, "earlier plan, not merged into main")
    plan_base = repo.head(ipath)
    worker, w, seen = _recording_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["true"],
                                                plan_base=plan_base)
    assert seen["r"][:2] == ("sonnet", "medium")                    # only this plan's 1-line change counts
    s2, store2, g2, _, _, _ = await run(repo, _review_plan(), worker=w, integration_gates=["true"])
    assert seen["r"][:2] == ("opus", "high")                        # MACC_BASE would include the earlier plan
    assert "new public symbols" in seen["r"][2] or "source lines changed" in seen["r"][2]


def _noop_integrate_worker():
    """MockWorker, except the integrate task returns without changing anything."""
    from macc.workers import WorkerResult
    worker, seen = MockWorker(), {}
    orig = worker.__call__

    async def w(task, wt, pack):
        seen[task.id] = (task.model, task.effort, pack)
        if task.kind == "integrate":
            return WorkerResult(ok=True, fields={"open_questions": "", "notes_for_downstream": "nothing to do",
                                                 "stubs": []}, model="mock")
        return await orig(task, wt, pack)
    w.name = "mock"
    return w, seen


async def test_noop_integrate_still_proves_the_tree_green_for_the_review(repo):
    w, seen = _noop_integrate_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["true"],
                                                skip_noop_integrate=False)
    assert summary["finished"] and "i" in seen                      # integrate really ran (skip disabled)
    full = [e for e in store.events(limit=500) if e["type"] == "full_gate"]
    assert full and full[0]["task_id"] == "i"                        # proved green although nothing merged
    assert seen["r"][:2] == ("sonnet", "medium")                     # so the review can be tiered low


async def test_noop_integrate_fails_when_the_full_gate_fails(repo):
    w, seen = _noop_integrate_worker()
    s, store, g, _, events, summary = await run(repo, _review_plan(), worker=w, integration_gates=["false"],
                                                skip_noop_integrate=False)
    assert g.tasks["i"].state in (State.FAILED.value, State.PENDING.value) and "r" not in seen


async def test_revalidated_integrate_records_full_gate_so_review_can_be_tiered(repo):
    worker, w, seen = _recording_worker()
    store = Store(repo.macc_dir / "state.db")
    tasks = _review_plan()
    tasks[2].done_when = ["test -f src/a.py"]                          # integrate can revalidate on its gate
    g = TaskGraph(tasks); g.validate()
    cfg = SchedulerConfig(integration_gates=["true"], skip_noop_integrate=False)
    await Scheduler(repo, store, g, w, cfg).run()
    g.update_spec("a", g.tasks["a"].spec + " (changed)")                # a `macc change` on the code task
    seen.clear()
    events = []
    await Scheduler(repo, store, g, w, cfg, on_event=lambda t, tid, p: events.append((t, tid))).run()
    assert ("revalidated", "i") in events and "i" not in seen          # integrate not re-run...
    assert ("full_gate", "i") in events                                 # ...but proved the new tree green
    assert seen["r"][:2] == ("sonnet", "medium")


async def test_rerun_review_must_rewrite_its_report(repo):
    from macc.workers import WorkerResult
    worker = MockWorker()
    orig = worker.__call__
    calls = {"r": 0}

    async def w(task, wt, pack):
        if task.kind == "review":
            calls["r"] += 1
            if calls["r"] == 2:                                          # round 2 writes nothing
                return WorkerResult(ok=True, fields={"open_questions": "", "stubs": [],
                                                     "notes_for_downstream": "VERDICT: APPROVE"}, model="mock")
        return await orig(task, wt, pack)
    w.name = "mock"
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(_review_plan()); g.validate()
    cfg = SchedulerConfig(integration_gates=["true"])
    await Scheduler(repo, store, g, w, cfg).run()                         # round 1 writes docs/r.txt
    g.update_spec("a", g.tasks["a"].spec + " (changed)")
    events = []
    await Scheduler(repo, store, g, w, cfg, on_event=lambda t, tid, p: events.append((t, tid, p))).run()
    retry = next(p for t, tid, p in events if t == "retry" and tid == "r")
    assert "write a fresh report" in retry["error"]
    assert calls["r"] == 3 and g.tasks["r"].state == State.DONE.value     # round 3 wrote it


async def test_skipped_integrate_in_a_change_round_still_runs_the_review(repo):
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(_review_plan()); g.validate()
    cfg = SchedulerConfig(integration_gates=["true"])
    worker = MockWorker()
    await Scheduler(repo, store, g, worker, cfg).run()
    g.update_spec("a", g.tasks["a"].spec + " (changed)")                # integrate and review go stale
    events = []
    summary = await Scheduler(repo, store, g, worker, cfg,
                              on_event=lambda t, tid, p: events.append((t, tid))).run()
    assert ("integrate_skipped", "i") in events
    assert ("dispatched", "r") in events and summary["finished"], (summary, events)


async def test_full_gate_result_survives_a_content_identical_merge_commit(repo):
    # the gate ran on integration; then integration gets a new commit with identical files (as when it is
    # fast-forwarded to a main that merged it with --no-ff): the review must still see a green gate
    worker, w, seen = _recording_worker()
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph(_review_plan()); g.validate()
    cfg = SchedulerConfig(integration_gates=["true"])
    s = Scheduler(repo, store, g, w, cfg)
    await s.run()
    ipath = repo.integration_path
    repo.git("commit", "-q", "--allow-empty", "-m", "merge commit, same tree", cwd=ipath)
    s2 = Scheduler(repo, store, g, w, cfg)
    assert s2._gate_covers_current_tree(store.latest_event("full_gate"))
    assert s2._review_setup(g.tasks["r"], repo.head(ipath))[1].model == "sonnet"
    (ipath / "src" / "new.py").write_text("x = 1\n")
    repo.commit_all(ipath, "real change")
    assert not s2._gate_covers_current_tree(store.latest_event("full_gate"))


def _plan_with_new_tests(repo, test_body):
    import sys
    ipath = repo.ensure_integration()
    base = repo.head(ipath)
    (ipath / "tests").mkdir(exist_ok=True)
    (ipath / "tests" / "test_new.py").write_text(test_body)
    (ipath / "tests" / "test_old_untouched.txt").write_text("not a test\n")
    repo.commit_all(ipath, "this plan added a test")
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph([Task("i", "i", kind=Kind.INTEGRATE.value)]); g.validate()
    cfg = SchedulerConfig(integration_gates=[f"{sys.executable} -m pytest -q"], plan_base=base)
    return Scheduler(repo, store, g, MockWorker(), cfg), store


def test_flake_check_catches_seed_dependent_new_tests(repo):
    s, store = _plan_with_new_tests(repo, "import os\n\ndef test_seed():\n"
                                          "    assert os.environ.get('PYTHONHASHSEED') in (None, '0')\n")
    assert s._new_test_files() == ["tests/test_new.py"]
    bad = s._flake_check("i")
    assert bad is not None and "PYTHONHASHSEED=" in bad["cmd"] and "tests/test_new.py" in bad["cmd"]
    ev = store.latest_event("flaky_tests")
    assert ev["payload"]["files"] == ["tests/test_new.py"] and ev["payload"]["seed"] > 0


def test_flake_check_passes_stable_tests_and_can_be_disabled(repo):
    s, store = _plan_with_new_tests(repo, "def test_ok():\n    assert sorted({3, 1, 2}) == [1, 2, 3]\n")
    assert s._flake_check("i") is None
    ev = store.latest_event("flake_check")
    assert ev["payload"]["ok"] and len(ev["payload"]["seeds"]) == 3
    s.cfg.flake_check_runs = 0
    assert s._flake_check("i") is None


async def test_flaky_new_tests_block_the_integrate_skip(repo):
    s, store = _plan_with_new_tests(repo, "import os\n\ndef test_seed():\n"
                                          "    assert os.environ.get('PYTHONHASHSEED') in (None, '0')\n")
    t = s.graph.tasks["i"]
    assert s._try_skip_integrate(t) is False                       # full gate green, but new tests are flaky
    assert "new tests are flaky" in t.last_error


async def test_worktree_setup_runs_in_task_and_integration_worktrees(repo):
    shared = repo.root.parent / "shared_deps"
    shared.mkdir()
    (shared / "dep.txt").write_text("dep\n")
    seen = {}
    worker = MockWorker()
    orig = worker.__call__

    async def w(task, wt, pack):
        seen[task.id] = (wt / "node_modules" / "dep.txt").is_file()
        return await orig(task, wt, pack)
    w.name = "mock"
    setup = [f"ln -sfn {shared} node_modules"]
    s, store, g, _, events, summary = await run(
        repo, [Task("a", "a", owns=["a/**"], done_when=["test -f node_modules/dep.txt"])], worker=w,
        integration_gates=["test -f node_modules/dep.txt"], worktree_setup=setup)
    assert summary["finished"] and seen == {"a": True}               # the worker and its gate saw the link
    assert (repo.integration_path / "node_modules" / "dep.txt").is_file()
    assert not [p for p in subprocess.run(["git", "ls-files"], cwd=repo.integration_path, capture_output=True,
                                          text=True).stdout.split() if p.startswith("node_modules")]


async def test_worktree_setup_failure_blocks_the_task_without_charging(repo):
    calls = {"n": 0}

    async def w(task, wt, pack):
        calls["n"] += 1
        return await MockWorker()(task, wt, pack)
    w.name = "mock"
    store = Store(repo.macc_dir / "state.db")
    g = TaskGraph([Task("a", "a", owns=["a/**"])]); g.validate()
    events = []
    s = Scheduler(repo, store, g, w, SchedulerConfig(worktree_setup=["test -f /nonexistent/setup_marker"]),
                  on_event=lambda t, tid, p: events.append((t, tid, p)))
    summary = await s.run()                                          # integration setup fails first
    assert summary["stopped_reason"] == "worktree_setup_failed" and calls["n"] == 0
    assert any(t == "worktree_setup_failed" and p.get("where") == "integration" for t, tid, p in events)
    # integration ok, task worktree fails: BLOCKED, attempt not charged
    marker = repo.integration_path / "only_here"
    marker.write_text("x\n")
    s2 = Scheduler(repo, store, g, w, SchedulerConfig(worktree_setup=[f"test -f {marker} && test -d .macc_never"]),
                   on_event=lambda t, tid, p: events.append((t, tid, p)))
    s2.cfg.worktree_setup = ["test \"$MACC_WORKTREE\" != \"$MACC_REPO_ROOT/.macc/worktrees/a\""]
    summary = await s2.run()
    assert g.tasks["a"].state == State.BLOCKED.value and g.tasks["a"].attempts == 0 and calls["n"] == 0
    assert "worktree_setup failed" in g.tasks["a"].last_error
    assert any(t == "worktree_setup_failed" and tid == "a" for t, tid, p in events)
