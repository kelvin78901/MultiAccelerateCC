"""Event-driven scheduler. A plain asyncio loop; no LLM makes scheduling decisions.

Loop:
  1. dispatch every READY task that passes the deterministic checks
     (WIP limit, ownership conflicts, token budget, kind != manual)
  2. wait for *any* worker to finish (never block on one)
  3. on finish: commit -> task gates -> handoff -> rebase -> merge -> integration gates
     -> mark done -> propagate staleness (may preempt running tasks)
  4. revalidate STALE tasks by running their gates on the integration tree
  5. repeat until nothing is runnable
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .graph import Kind, State, Task, TaskGraph
from .handoff import build_context_pack, generate_handoff
from .store import Store
from .workers import WorkerFn, WorkerResult
from .worktree import Repo


@dataclass
class SchedulerConfig:
    max_parallel: int = 3
    token_budget: Optional[int] = None          # total input+output tokens across all runs
    integration_gates: list[str] = field(default_factory=list)   # run after every merge
    project_rules: str = ""                      # short, stable text prepended to every context pack
    keep_worktrees: bool = False
    revalidate_stale: bool = True


EventFn = Callable[[str, Optional[str], dict], None]


class Scheduler:
    def __init__(self, repo: Repo, store: Store, graph: TaskGraph, worker: WorkerFn,
                 config: Optional[SchedulerConfig] = None, on_event: Optional[EventFn] = None):
        self.repo = repo
        self.store = store
        self.graph = graph
        self.worker = worker
        self.cfg = config or SchedulerConfig()
        self._on_event = on_event
        self.running: dict[str, asyncio.Task] = {}
        self.tokens_used = 0
        self.stopped_reason: Optional[str] = None

    # ------------------------------------------------------------ plumbing
    def emit(self, type_: str, task_id: Optional[str] = None, **payload) -> None:
        self.store.event(type_, task_id, **payload)
        if self._on_event:
            self._on_event(type_, task_id, payload)

    def _persist(self, *task_ids: str) -> None:
        with self.store.tx():
            for tid in task_ids or self.graph.tasks.keys():
                self.store.save_task(self.graph.tasks[tid])

    def _worker_name(self) -> str:
        return getattr(self.worker, "name", "worker")

    # ------------------------------------------------------------- dispatch
    def _can_dispatch(self, t: Task) -> Optional[str]:
        if len(self.running) >= self.cfg.max_parallel:
            return "wip_limit"
        if self.cfg.token_budget is not None and self.tokens_used + t.est_tokens > self.cfg.token_budget:
            return "budget"
        conflicts = self.graph.conflicts_with_active(t)
        if conflicts:
            return f"ownership_conflict:{','.join(conflicts)}"
        return None

    def _dispatch_ready(self) -> int:
        n = 0
        for t in self.graph.ready():
            if t.id in self.running:
                continue
            if t.kind == Kind.MANUAL.value:
                if t.state != State.BLOCKED.value:
                    t.state = State.BLOCKED.value
                    self._persist(t.id)
                    self.emit("needs_human", t.id, reason="manual task")
                continue
            why = self._can_dispatch(t)
            if why:
                if why == "budget" and self.stopped_reason is None:
                    self.stopped_reason = "budget_exceeded"
                    self.emit("budget_exceeded", t.id, used=self.tokens_used, budget=self.cfg.token_budget)
                continue
            self._start(t)
            n += 1
        return n

    def _start(self, t: Task) -> None:
        t.attempts += 1
        t.state = State.RUNNING.value
        t.preempt = False
        t.worker = self._worker_name()
        t.branch = self.repo.task_branch(t.id)
        wt = self.repo.create_task_worktree(t.id, fresh=True)
        pack = build_context_pack(t, self.graph, self.store, self.cfg.project_rules)
        if t.last_error:
            pack += f"\n# Previous attempt failed\n{t.last_error[-3000:]}\n"
        ctx_dir = self.repo.macc_dir / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / f"{t.id}.md").write_text(pack)
        self._persist(t.id)
        self.emit("dispatched", t.id, attempt=t.attempts, worktree=str(wt))
        fut = asyncio.create_task(self._run_worker(t, wt, pack), name=f"macc:{t.id}")
        self.running[t.id] = fut

    async def _run_worker(self, t: Task, wt: Path, pack: str) -> tuple[str, Path, WorkerResult]:
        try:
            res = await self.worker(t, wt, pack)
        except asyncio.CancelledError:
            res = WorkerResult(ok=False, error="preempted", preempted=True)
        except Exception as e:  # worker crashed; never take the loop down
            res = WorkerResult(ok=False, error=f"worker exception: {e!r}")
        return t.id, wt, res

    # ---------------------------------------------------------- completion
    async def _on_finished(self, tid: str, wt: Path, res: WorkerResult) -> None:
        t = self.graph.tasks[tid]
        self.running.pop(tid, None)
        self.tokens_used += res.input_tokens + res.output_tokens
        self.store.add_usage(tid, t.attempts, self._worker_name(), res.ok, model=res.model,
                             input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                             cache_read_tokens=res.cache_read_tokens, cost_usd=res.cost_usd, seconds=res.seconds)

        if res.preempted or t.preempt:
            t.attempts = max(0, t.attempts - 1)       # a preempted run is not a failed attempt
            t.state = State.PENDING.value
            t.preempt = False
            self._persist(tid)
            self.emit("preempted", tid, reason="inputs changed while running")
            self._cleanup(tid)
            return

        if not res.ok:
            self._fail(t, res.error or "worker reported failure")
            self._cleanup(tid)
            return

        # 1. commit the worktree
        committed = self.repo.commit_all(wt, f"macc({tid}): attempt {t.attempts}")
        if not committed and t.kind in (Kind.CODE.value, Kind.CONTRACT.value):
            self._fail(t, "worker produced no changes")
            self._cleanup(tid)
            return

        # 2. task-level gates (done_when) inside the worktree
        gates = self.repo.run_gates(t.done_when, cwd=wt) if t.done_when else []
        if gates and not all(g["ok"] for g in gates):
            bad = next(g for g in gates if not g["ok"])
            self._fail(t, f"gate failed: {bad['cmd']}\n{bad['stderr'][-1500:]}\n{bad['stdout'][-1500:]}")
            self._cleanup(tid)
            return

        # 3. handoff + debts (generated from facts; LLM wrote two fields)
        base = self.repo.merge_base(wt)
        handoff = generate_handoff(self.repo, t, wt, gates, res.fields, base)
        out_hash = self.repo.output_hash(wt, base)
        stubs = handoff["stubs_declared"] or [s["line"] for s in handoff["stubs_detected"]]
        for s in stubs:
            self.store.add_debt(tid, "stub", str(s))
        if stubs and not t.allow_stubs:
            self.emit("stub_without_permission", tid, stubs=stubs)
        if t.kind == Kind.REVIEW.value and "REJECT" in (res.fields.get("notes_for_downstream", "").upper()):
            self.store.save_handoff(tid, t.attempts, t.input_hash, out_hash, handoff)
            self._fail(t, "review rejected: " + res.fields.get("notes_for_downstream", "")[:1500], final=True)
            self.emit("review_rejected", tid, reviewed=t.deps)
            self._cleanup(tid)
            return

        # 4. rebase + merge (topological order is implied: deps are DONE, i.e. already merged)
        if committed:
            rb = self.repo.rebase_task_on_integration(tid)
            if not rb.ok:
                self.emit("conflict", tid, stage="rebase", files=rb.conflicts)
                self._fail(t, f"rebase conflict in {rb.conflicts}; retried on fresh base")
                self._cleanup(tid)
                return
            before = self.repo.head(self.repo.integration_path)
            mr = self.repo.merge_task(tid)
            if not mr.ok:
                self.emit("conflict", tid, stage="merge", files=mr.conflicts)
                self._fail(t, f"merge conflict in {mr.conflicts}")
                self._cleanup(tid)
                return
            # 5. integration gates; revert the merge if they fail
            igates = self.repo.run_gates(self.cfg.integration_gates) if self.cfg.integration_gates else []
            if igates and not all(g["ok"] for g in igates):
                self.repo.git("reset", "--hard", before, cwd=self.repo.integration_path)
                bad = next(g for g in igates if not g["ok"])
                self.emit("integration_gate_failed", tid, cmd=bad["cmd"])
                self._fail(t, f"integration gate failed after merge: {bad['cmd']}\n{bad['stderr'][-1500:]}{bad['stdout'][-1500:]}")
                self._cleanup(tid)
                return
            handoff["merge_commit"] = mr.merge_commit

        # 6. done + staleness propagation
        self.store.save_handoff(tid, t.attempts, t.input_hash, out_hash, handoff)
        t.last_error = None
        changed = self.graph.mark_done(tid, out_hash)
        self._persist(tid, *changed)
        self.emit("done", tid, output_hash=out_hash, files=len(handoff["files_changed"]), stale=changed)
        for c in changed:
            if c in self.running and self.graph.tasks[c].preempt:
                self.running[c].cancel()
                self.emit("preempt_requested", c, because=tid)
        self._cleanup(tid)

    def _fail(self, t: Task, error: str, final: bool = False) -> None:
        t.last_error = error
        if final or t.attempts >= t.max_attempts:
            t.state = State.FAILED.value
            self.emit("failed", t.id, attempt=t.attempts, error=error[:500])
        else:
            t.state = State.PENDING.value
            self.emit("retry", t.id, attempt=t.attempts, error=error[:500])
        self._persist(t.id)

    def _cleanup(self, tid: str) -> None:
        if not self.cfg.keep_worktrees:
            self.repo.remove_task_worktree(tid, delete_branch=False)

    # --------------------------------------------------------- revalidation
    def _revalidate_stale(self) -> None:
        """Build-system rule: a STALE node whose gates still pass on the integration tree is DONE again
        (same output). Otherwise it goes back to PENDING and is re-run as a repair task."""
        for t in list(self.graph.tasks.values()):
            if t.state != State.STALE.value or not self.graph.deps_done(t.id):
                continue
            if not self.cfg.revalidate_stale or not t.done_when:
                t.state = State.PENDING.value
                t.attempts = 0
                self.emit("stale_rerun", t.id, reason="no gates to revalidate with")
            else:
                gates = self.repo.run_gates(t.done_when)
                if all(g["ok"] for g in gates):
                    t.state = State.DONE.value
                    self.emit("revalidated", t.id)
                else:
                    t.state = State.PENDING.value
                    t.attempts = 0
                    t.last_error = "stale revalidation failed: " + next(g["cmd"] for g in gates if not g["ok"])
                    self.emit("stale_rerun", t.id, reason=t.last_error)
            self._persist(t.id)

    # ---------------------------------------------------------------- main
    async def run(self) -> dict:
        self.repo.ensure_integration()
        self.graph.refresh_hashes()
        self._persist()
        self.emit("run_started", None, tasks=len(self.graph.tasks), max_parallel=self.cfg.max_parallel)
        t0 = time.time()
        while True:
            self._revalidate_stale()
            self._dispatch_ready()
            if not self.running:
                break
            done, _ = await asyncio.wait(set(self.running.values()), return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                tid, wt, res = fut.result()
                await self._on_finished(tid, wt, res)
        summary = {
            "states": self.graph.summary(),
            "finished": self.graph.is_finished(),
            "tokens_used": self.tokens_used,
            "seconds": round(time.time() - t0, 2),
            "stopped_reason": self.stopped_reason,
            "failed": [t.id for t in self.graph.tasks.values() if t.state == State.FAILED.value],
            "blocked": [t.id for t in self.graph.tasks.values() if t.state == State.BLOCKED.value],
            "open_debts": len(self.store.open_debts()),
        }
        self.emit("run_finished", None, **summary)
        (self.repo.macc_dir / "STATE.md").write_text(self.state_markdown(summary))
        return summary

    def state_markdown(self, summary: dict) -> str:
        lines = ["# macc state", "", f"updated: {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
                 "## summary", "```json", json.dumps(summary, indent=2), "```", "",
                 "## graph", "```mermaid", self.graph.to_mermaid(), "```", ""]
        debts = self.store.open_debts()
        if debts:
            lines += ["## open debts"] + [f"- [{d['kind']}] {d['task_id']}: {d['note']}" for d in debts] + [""]
        failed = [t for t in self.graph.tasks.values() if t.state in (State.FAILED.value, State.BLOCKED.value)]
        if failed:
            lines += ["## needs attention"] + [f"- {t.id} ({t.state}): {(t.last_error or '')[:300]}" for t in failed]
        return "\n".join(lines) + "\n"
