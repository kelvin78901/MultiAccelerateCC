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
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from .costmodel import backfill_fix_rounds, error as cm_error, predict, reconstruct_serial_flags
from .graph import ACTIVE, Kind, State, Task, TaskGraph
from .handoff import (build_context_pack, generate_handoff, interpreter_conflicts, merge_conflict_spec,
                      classify_path, parse_reopen, parse_verdict, public_api_delta, review_facts, review_risk)
from .store import Store
from .workers import WorkerFn, WorkerResult
from .worktree import Repo, clip_output


@dataclass
class SchedulerConfig:
    max_parallel: int = 3
    token_budget: Optional[int] = None          # total input+output tokens across all runs
    cost_budget_usd: Optional[float] = None     # total reported cost (includes cache reads); preferred
    integration_gates: list[str] = field(default_factory=list)   # the full suite (see gate_policy)
    # per_task: after a merge run only that task's done_when on the integration tree; the full
    # integration_gates run after `integrate` merges and once at the end of the run. full: after every merge.
    gate_policy: str = "per_task"
    project_rules: str = ""                      # short, stable text prepended to every context pack
    keep_worktrees: bool = False
    revalidate_stale: bool = True
    merge_agent: bool = True                     # spawn a merge worker on textual conflicts
    skip_noop_integrate: bool = True             # integrate is skipped when gates are green and nothing is open
    # review tiering: a low-risk change (small source diff, existing tests untouched, no open debts, full gate
    # green on the reviewed tree) is reviewed with the cheaper model/effort for that dispatch
    review_tiering: bool = True
    review_low_risk_max_lines: int = 400
    review_low_risk_max_new_api: int = 5         # new public defs/classes/routes; features add 17-31, the refactor 4
    plan_base: Optional[str] = None              # integration HEAD when the plan was loaded: "this plan's change"
    flake_check_runs: int = 3                    # at integrate: run this plan's new test files N times, random seeds
    # cost-model split hints: each names the history level and sample count it rests on (v0.3.7)
    split_hints: bool = True
    worktree_setup: list[str] = field(default_factory=list)   # run in every new worktree (e.g. link node_modules)
    gate_timeout: int = 1800                     # seconds per gate command
    history_dbs: list[str] = field(default_factory=list)   # other repos' state.db: worker history for the cost model
    gate_baseline_s: Optional[float] = None      # this repo's gate time measured by `macc init --gate`
    review_low_model: str = "sonnet"
    review_low_effort: str = "medium"
    strict_ownership: bool = True                # out-of-scope changes fail the attempt (else: dropped + warned)
    cost_baseline_usd: float = 0.0               # store total at plan load; budget applies to cost since then

    def __post_init__(self) -> None:
        if self.gate_policy not in ("per_task", "full"):
            raise ValueError(f"gate_policy must be 'per_task' or 'full', not {self.gate_policy!r}")
    max_transients: int = 4                      # infra failures tolerated per task before BLOCKED


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
        self.parked: dict[str, dict] = {}        # finished workers waiting for merge_after targets
        self._rules_conflicts_seen: set[str] = set()
        self.merged_this_run: list[str] = []
        self._merged_since_full_gate = False      # per_task policy: the final full gate is still owed
        self.gate_seconds = 0.0                   # wall time spent in post-merge gates this run
        self._budget_deferred: set[str] = set()
        self._setup_paths: dict[str, set[str]] = {}   # per task: paths worktree_setup created
        self._serial_points: set[str] = set()     # tasks every other unfinished task waited for (this run)
        self.prediction: Optional[dict] = None
        self._fix_round: dict[str, bool] = {}
        self.run_seq: Optional[int] = None
        self.serial_seconds = 0.0
        self._gated_tree: dict[str, str] = {}     # task -> tree hash its done_when passed on (per_task reuse)
        self.run_seconds: dict[str, float] = {}   # worker seconds per task this run (all attempts, merge agent)
        self.backoff: dict[str, float] = {}      # task -> earliest re-dispatch time (transient failures)
        self.tokens_used = 0
        # budget is cumulative over runs of the same plan: start from what was already spent since plan load
        self.cost_used = max(0.0, store.usage_totals()["cost_usd"] - self.cfg.cost_baseline_usd)
        self._cost_at_start = self.cost_used      # cost_this_run = cost_used - this
        self.runs_done = 0
        if hasattr(worker, "gate_commands"):
            worker.gate_commands = list(self.cfg.integration_gates)
        if hasattr(worker, "env_provider"):
            worker.env_provider = self.repo.macc_env
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
        if self.cfg.cost_budget_usd is not None:
            # reserve for the tasks already running plus this one, each at its kind's historical mean cost
            # (one expensive opus contract must not make every queued sonnet task look as expensive)
            reserved = sum(self._est_cost(self.graph.tasks[r]) for r in self.running) + self._est_cost(t)
            if self.cost_used + reserved > self.cfg.cost_budget_usd:
                return "budget"
        conflicts = self.graph.conflicts_with_active(t)
        if conflicts:
            return f"ownership_conflict:{','.join(conflicts)}"
        return None

    def _est_cost(self, t: Task) -> float:
        by_kind = self.store.avg_cost_by_kind()
        if t.kind in by_kind:
            return by_kind[t.kind]
        return self.cost_used / self.runs_done if self.runs_done else 0.0

    def _gate_covers_current_tree(self, ev: Optional[dict]) -> bool:
        """Did this full_gate event run on the files integration has now? Compared by tree hash (a merge
        commit with identical content, e.g. after merging into main, still counts); by HEAD for old events."""
        if not ev:
            return False
        p = ev["payload"]
        if p.get("tree"):
            return p["tree"] == self.repo.tree(self.repo.integration_path)
        return p.get("head") == self.repo.head(self.repo.integration_path)

    def _new_test_files(self) -> list[str]:
        base = self.cfg.plan_base or self.repo.macc_env().get("MACC_BASE")
        if not base:
            return []
        return sorted(f["path"] for f in self.repo.diff_files(base, "HEAD")
                      if f["status"] == "A" and classify_path(f["path"]) == "test"
                      and f["path"].endswith(".py") and Path(f["path"]).name.startswith("test_"))

    def _flake_check(self, tid: str) -> Optional[dict]:
        """Cheap flakiness check at integrate time: run the test files this plan added N times, each with a
        different random PYTHONHASHSEED (recorded, so a failure reproduces). No model involved. Returns the
        failing run, or None. Only for pytest gates."""
        runs = self.cfg.flake_check_runs
        files = self._new_test_files() if runs > 0 else []
        if not files:
            return None
        gate = next((g for g in self.cfg.integration_gates if "pytest" in g.split()), None)
        if gate is None:
            self.emit("flake_check", tid, skipped="integration gate does not run pytest", files=files)
            return None
        words = gate.split()
        runner = " ".join(words[:words.index("pytest") + 1])
        seeds = [random.randint(1, 2**31 - 1) for _ in range(runs)]
        for seed in seeds:
            cmd = f"PYTHONHASHSEED={seed} {runner} -q -p no:cacheprovider {' '.join(files)}"
            g = self.repo.run_gates([cmd])[0]
            if not g["ok"]:
                self.emit("flaky_tests", tid, files=files, seed=seed, cmd=cmd)
                return g
        self.emit("flake_check", tid, files=files, seeds=seeds, ok=True)
        return None

    def _ensure_full_gate(self, tid: Optional[str]) -> Optional[dict]:
        """Make sure the full integration gate has passed on the current integration HEAD (reusing a green
        result recorded for that HEAD). Returns the failing gate record, or None when green."""
        last = self.store.latest_event("full_gate")
        if last and last["payload"].get("ok") and self._gate_covers_current_tree(last):
            return None
        gates = self.repo.run_gates(self.cfg.integration_gates)
        self._record_gates(tid, gates, full=True)
        bad = next((g for g in gates if not g["ok"]), None)
        if bad is None:
            self._merged_since_full_gate = False
        return bad

    def _integrate_blockers(self, t: Task) -> list[str]:
        """Reasons an integrate task must really run. Empty list = it would have nothing to do."""
        reasons = []
        ancestors = self.graph.ancestors(t.id)
        debts = [d for d in self.store.open_debts() if d["task_id"] in ancestors or d["task_id"] == t.id]
        if debts:
            reasons.append(f"{len(debts)} open debt(s)")
        asked = [a for a in sorted(ancestors)
                 if ((self.store.latest_handoff(a) or {}).get("open_questions") or "").strip()]
        if asked:
            reasons.append(f"open_questions from {asked}")
        if not self.cfg.integration_gates:
            reasons.append("no integration gate to prove the tree is green")
        # green gates do not answer a review's findings (REA #6404: REOPEN: integrate was skipped, and the
        # unchanged tree was rejected again); last_error is cleared when the task next completes
        if (t.last_error or "").startswith("review rejected"):
            reasons.append("reopened by a review rejection")
        return reasons

    def _try_skip_integrate(self, t: Task) -> bool:
        """A ready integrate task with nothing open and a green tree is pure overhead (AutoLeaseRank refactor:
        0 files changed, $0.68, 74 s). Prove green with the full suite plus its own done_when on the
        integration tree; then mark it DONE without a worker."""
        if not self.cfg.skip_noop_integrate or self._integrate_blockers(t):
            return False
        t0 = time.time()
        # its gates block everything downstream with no worker running: serial time like a serial worker's
        serial = not self.running and self._is_global_serial_point(t)
        head = self.repo.head(self.repo.integration_path)
        last = self.store.latest_event("full_gate")
        reuse = bool(last and last["payload"].get("ok") and self._gate_covers_current_tree(last))
        if self._ensure_full_gate(t.id):
            return False                      # something to fix: run the integrate worker as planned
        gates = [] if reuse else [{"cmd": c, "ok": True} for c in self.cfg.integration_gates]
        own = self.repo.run_gates(t.done_when) if t.done_when else []
        if not all(g["ok"] for g in own):
            return False
        flaky = self._flake_check(t.id)
        if flaky:                             # the integrate worker runs, told which test is order/seed dependent
            t.last_error = f"new tests are flaky (passed the gate, failed under another seed): {flaky['cmd']}\n" \
                           f"{clip_output(flaky['stdout'], 700, 1500)}"
            self._persist(t.id)
            return False
        note = "skipped: gates green, nothing to integrate"
        handoff = {"task_id": t.id, "attempt": t.attempts, "files_changed": [], "api_changes": [],
                   "gates": [{"cmd": g["cmd"], "ok": g["ok"], "seconds": g.get("seconds")} for g in gates + own],
                   "gates_passed": True,
                   "stubs_detected": [], "stubs_declared": [], "open_questions": "",
                   "notes_for_downstream": note, "skipped": True, "integration_head": head}
        out_hash = "skipped-" + self.repo.tree(self.repo.integration_path)[:12]
        self.store.save_handoff(t.id, t.attempts, t.input_hash, out_hash, handoff)
        changed = self.graph.mark_done(t.id, out_hash)
        self._persist(t.id, *changed)
        seconds = round(time.time() - t0, 2)
        if serial:
            self._serial_points.add(t.id)
            self.serial_seconds += seconds
        self.emit("integrate_skipped", t.id, head=head, reused_full_gate=reuse,
                  gates=[g["cmd"] for g in gates + own], note=note, seconds=seconds, serial=serial)
        return True

    def _dispatch_ready(self) -> int:
        if self.stopped_reason == "worktree_setup_failed":
            return 0
        # skipping an integrate makes its dependents ready at once (a STALE review must first go back to
        # pending, or the run ends with it never dispatched): repeat until nothing more is skipped
        while any(self._try_skip_integrate(t) for t in self.graph.ready()
                  if t.kind == Kind.INTEGRATE.value and t.id not in self.running):
            self._revalidate_stale()
        n = 0
        now = time.time()
        for t in self.graph.ready():
            if t.id in self.running:
                continue
            if self.backoff.get(t.id, 0) > now:
                continue
            self.backoff.pop(t.id, None)
            if t.kind == Kind.MANUAL.value:
                if t.state != State.BLOCKED.value:
                    t.state = State.BLOCKED.value
                    self._persist(t.id)
                    self.emit("needs_human", t.id, reason="manual task")
                continue
            why = self._can_dispatch(t)
            if why:
                if why == "budget":
                    payload = dict(tokens=self.tokens_used, cost_usd=round(self.cost_used, 2),
                                   token_budget=self.cfg.token_budget, cost_budget_usd=self.cfg.cost_budget_usd)
                    if self.running:
                        # only the reservation for running tasks is in the way: it may fit once they report
                        if t.id not in self._budget_deferred:
                            self._budget_deferred.add(t.id)
                            self.emit("budget_deferred", t.id, running=sorted(self.running), **payload)
                    elif self.stopped_reason is None:
                        self.stopped_reason = "budget_exceeded"
                        self.emit("budget_exceeded", t.id, **payload)
                continue
            self._start(t)
            n += 1
        return n

    def _is_global_serial_point(self, t: Task) -> bool:
        """Every other unfinished task is downstream of t: nothing can run beside it."""
        later = self.graph.descendants(t.id)
        return all(u.id in later for u in self.graph.tasks.values()
                   if u.id != t.id and u.state not in (State.DONE.value, State.CANCELLED.value))

    def _start(self, t: Task) -> None:
        if self._is_global_serial_point(t):
            self._serial_points.add(t.id)
        # completed before (a change round, or reopened by a REJECT); a retry has no handoff yet
        self._fix_round[t.id] = self.store.latest_handoff(t.id) is not None
        t.attempts += 1
        t.state = State.RUNNING.value
        t.preempt = False
        t.worker = self._worker_name()
        t.branch = self.repo.task_branch(t.id)
        wt = self.repo.create_task_worktree(t.id, fresh=True)
        if self.cfg.worktree_setup:
            bad = self.repo.run_setup(self.cfg.worktree_setup, wt)
            if bad:
                t.attempts = max(0, t.attempts - 1)       # the environment failed, not the task
                t.state = State.BLOCKED.value
                t.last_error = (f"worktree_setup failed: {bad['cmd']} (exit {bad['code']})\n"
                                f"{clip_output(bad['stderr'], 400, 800)}{clip_output(bad['stdout'], 400, 800)}")
                self._persist(t.id)
                self.emit("worktree_setup_failed", t.id, cmd=bad["cmd"], code=bad["code"], stderr=bad["stderr"][-300:])
                self._cleanup(t.id)
                return
            self._setup_paths[t.id] = set(self.repo.dirty_paths(wt))   # environment, never the task's output
        head = self.repo.head(self.repo.integration_path) if t.kind == Kind.REVIEW.value else None
        tree = self.repo.tree(self.repo.integration_path) if head else None
        pack = build_context_pack(t, self.graph, self.store, self.cfg.project_rules, integration_head=head,
                                  integration_tree=tree)
        run_as = t
        if t.kind == Kind.REVIEW.value:
            facts, run_as = self._review_setup(t, head)
            pack += "\n" + facts + "\n"
        self._check_rules_conflict(t, wt)
        if t.last_error:
            pack += f"\n# Previous attempt failed\n{t.last_error[-3000:]}\n"
        ctx_dir = self.repo.macc_dir / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / f"{t.id}.md").write_text(pack)
        self._persist(t.id)
        self.emit("dispatched", t.id, attempt=t.attempts, worktree=str(wt))
        fut = asyncio.create_task(self._run_worker(t, wt, pack, run_as), name=f"macc:{t.id}")
        self.running[t.id] = fut

    def _review_setup(self, t: Task, head: Optional[str]) -> tuple[str, Task]:
        """Facts for the reviewer's context pack, and the task as it should run (tiered model/effort)."""
        # this plan's change, not everything unmerged into main (MACC_BASE would count earlier plans too)
        base = self.cfg.plan_base or self.repo.macc_env().get("MACC_BASE")
        files = self.repo.diff_files(base, "HEAD") if base else []
        new_api = 0
        if base:
            diff = self.repo.git("diff", "-U0", base, "HEAD", cwd=self.repo.integration_path).stdout
            new_api = len(public_api_delta(diff)[0])
        last = self.store.latest_event("full_gate")
        gate_green = bool(last["payload"].get("ok")) if self._gate_covers_current_tree(last) else None
        ancestors = self.graph.ancestors(t.id)
        debts = [d for d in self.store.open_debts() if d["task_id"] in ancestors]
        contracts = sorted({o for a in ancestors if self.graph.tasks[a].kind == Kind.CONTRACT.value
                            for o in self.graph.tasks[a].owns
                            if "*" not in o and (self.repo.integration_path / o).is_file()})
        tier, reasons, stats = review_risk(files, len(debts), gate_green, self.cfg.review_low_risk_max_lines,
                                           new_api, self.cfg.review_low_risk_max_new_api)
        if not base:
            tier, reasons = "high", reasons + ["no base to diff against"]
        run_as = t
        if self.cfg.review_tiering and tier == "low":
            run_as = replace(t, model=self.cfg.review_low_model, effort=self.cfg.review_low_effort)
        self.emit("review_tier", t.id, tier=tier, reasons=reasons, model=run_as.model, effort=run_as.effort,
                  source_lines=stats["source_lines"], new_public_api=new_api, files=stats["files"],
                  base=base, applied=run_as is not t)
        return review_facts(stats, tier, reasons, contracts, debts, base), run_as

    def _check_rules_conflict(self, t: Task, wt: Path) -> None:
        """Advisory only: the worker still runs. Emitted once per distinct conflict per run."""
        claude_md = wt / "CLAUDE.md"
        if not self.cfg.project_rules or not claude_md.exists():
            return
        for msg in interpreter_conflicts(self.cfg.project_rules, claude_md.read_text(errors="replace")):
            if msg not in self._rules_conflicts_seen:
                self._rules_conflicts_seen.add(msg)
                self.emit("rules_conflict", t.id, message=msg)

    async def _run_worker(self, t: Task, wt: Path, pack: str,
                          run_as: Optional[Task] = None) -> tuple[str, Path, WorkerResult]:
        try:
            res = await self.worker(run_as or t, wt, pack)
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
        self.cost_used += res.cost_usd
        self.runs_done += 1
        self.run_seconds[tid] = self.run_seconds.get(tid, 0.0) + res.seconds
        if tid in self._serial_points:
            self.serial_seconds += res.seconds
            est_s = t.est_minutes * 60
            if est_s > 0 and res.seconds > 3 * est_s:
                self.emit("serial_bottleneck", tid, actual_s=round(res.seconds, 1), est_min=t.est_minutes,
                          ratio=round(res.seconds / est_s, 1))
        self.store.add_usage(tid, t.attempts, self._worker_name(), res.ok, model=res.model,
                             input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                             cache_read_tokens=res.cache_read_tokens, cost_usd=res.cost_usd, seconds=res.seconds,
                             est_minutes=t.est_minutes, kind=t.kind, serial=tid in self._serial_points,
                             fix_round=self._fix_round.get(tid), run_seq=self.run_seq,
                             **self._predicted_for(tid))

        if res.preempted or t.preempt:
            t.attempts = max(0, t.attempts - 1)       # a preempted run is not a failed attempt
            t.state = State.PENDING.value
            t.preempt = False
            self._persist(tid)
            self.emit("preempted", tid, reason="inputs changed while running")
            self._cleanup(tid)
            return

        if not res.ok and res.transient:
            # infrastructure hiccup (CLI start, stdin race, rate limit): not the task's fault, but bounded
            t.transients += 1
            t.attempts = max(0, t.attempts - 1)
            if t.transients > self.cfg.max_transients:
                t.state = State.BLOCKED.value
                t.last_error = f"{t.transients} infrastructure failures in a row: {(res.error or '')[:500]}"
                self._persist(tid)
                self.emit("transient_limit", tid, count=t.transients)
            else:
                t.state = State.PENDING.value
                self._persist(tid)
                self.emit("transient_failure", tid, count=t.transients, error=(res.error or "")[:300])
                self.backoff[tid] = time.time() + min(30.0, 2.0 ** t.transients)   # non-blocking back-off
            self._cleanup(tid)
            return
        if not res.ok:
            self._fail(t, res.error or "worker reported failure")
            self._cleanup(tid)
            return

        # 1. commit the worktree: only paths the task owns; build artifacts are discarded;
        #    anything else outside the scope is an ownership violation (Bash bypasses the Edit hook)
        sc = self.repo.commit_scoped(wt, f"macc({tid}): attempt {t.attempts}", t.owns,
                                     setup_paths=self._setup_paths.get(tid, ()))
        committed = sc.committed
        if sc.ignored:
            self.emit("artifacts_discarded", tid, paths=sc.ignored[:20])
        if sc.outside:
            self.emit("ownership_violation", tid, paths=sc.outside[:20])
            if self.cfg.strict_ownership:
                self._fail(t, f"changed files outside ownership {t.owns}: {sc.outside[:10]}. "
                              "Only modify owned paths; describe other needs in open_questions.")
                self._cleanup(tid)
                return
        if not committed and t.kind == Kind.REVIEW.value and t.owns and self.store.latest_handoff(tid):
            # a re-run review that commits nothing leaves the previous round's report in place, and a
            # done_when that greps it passes on that stale file
            self._fail(t, "this review round committed nothing: write a fresh report for this round into "
                          f"{t.owns} (the existing file is from an earlier round)")
            self._cleanup(tid)
            return
        if not committed and t.kind in (Kind.CODE.value, Kind.CONTRACT.value):
            self._fail(t, "worker produced no changes")
            self._cleanup(tid)
            return

        # 2. task-level gates (done_when) inside the worktree
        gates = await asyncio.to_thread(self.repo.run_gates, t.done_when, wt) if t.done_when else []
        if gates and not all(g["ok"] for g in gates):
            bad = next(g for g in gates if not g["ok"])
            self._fail(t, f"gate failed: {bad['cmd']}\n{clip_output(bad['stderr'], 700, 1500)}\n{clip_output(bad['stdout'], 700, 1500)}")
            self._cleanup(tid)
            return
        if gates:
            self._gated_tree[tid] = self.repo.tree(wt)    # the exact tree these done_when passed on

        # 3. handoff + debts (generated from facts; LLM wrote two fields)
        base = self.repo.merge_base(wt)
        handoff = generate_handoff(self.repo, t, wt, gates, res.fields, base)
        out_hash = self.repo.output_hash(wt, base)
        stubs = handoff["stubs_declared"] or [s["line"] for s in handoff["stubs_detected"]]
        for s in stubs:
            self.store.add_debt(tid, "stub", str(s))
        if stubs and not t.allow_stubs:
            self.emit("stub_without_permission", tid, stubs=stubs)
        if t.kind == Kind.REVIEW.value and parse_verdict(res.fields.get("notes_for_downstream", "")) == "REJECT":
            self.store.save_handoff(tid, t.attempts, t.input_hash, out_hash, handoff)
            self._review_reject(t, res.fields.get("notes_for_downstream", ""))
            self._cleanup(tid)
            return

        # 3b. soft ordering: `merge_after` tasks must be DONE before this one is merged.
        #     The task ran in parallel; only its merge waits (e.g. tests merge after the implementation).
        if committed and not self._merge_after_satisfied(t):
            t.state = State.REVIEW.value          # keeps ownership reserved and the worktree alive
            self.parked[tid] = dict(wt=wt, res=res, gates=gates, handoff=handoff, out_hash=out_hash)
            self._persist(tid)
            self.emit("awaiting_merge", tid, after=[d for d in t.merge_after
                                                     if self.graph.tasks[d].state != State.DONE.value])
            return
        await self._merge_and_finish(t, wt, res, gates, handoff, out_hash, committed)

    def _merge_after_satisfied(self, t: Task) -> bool:
        return all(self.graph.tasks[d].state == State.DONE.value for d in t.merge_after if d in self.graph.tasks)

    async def _try_parked(self) -> None:
        for tid in list(self.parked):
            t = self.graph.tasks[tid]
            if t.preempt:                           # inputs changed while parked: redo
                p = self.parked.pop(tid)
                t.attempts = max(0, t.attempts - 1)
                t.state = State.PENDING.value
                t.preempt = False
                self._persist(tid)
                self.emit("preempted", tid, reason="inputs changed while awaiting merge")
                self._cleanup(tid)
                continue
            if self._merge_after_satisfied(t):
                p = self.parked.pop(tid)
                # the base moved: refresh facts after rebase inside _merge_and_finish
                await self._merge_and_finish(t, p["wt"], p["res"], p["gates"], p["handoff"], p["out_hash"], True)
            elif not self.running and all(self.graph.tasks[d].state in (State.FAILED.value, State.BLOCKED.value,
                                                                           State.CANCELLED.value)
                                          for d in t.merge_after if self.graph.tasks[d].state != State.DONE.value):
                self.parked.pop(tid)
                t.state = State.BLOCKED.value
                t.last_error = f"merge_after targets failed: {t.merge_after}"
                self._persist(tid)
                self.emit("merge_blocked", tid, after=t.merge_after)
                self._cleanup(tid)

    async def _merge_and_finish(self, t: Task, wt: Path, res: WorkerResult, gates: list[dict],
                                handoff: dict, out_hash: str, committed: bool) -> None:
        tid = t.id
        # 4. rebase + merge (topological order is implied: deps are DONE, i.e. already merged)
        if committed:
            rb = self.repo.rebase_task_on_integration(tid)
            if not rb.ok:
                self.emit("conflict", tid, stage="rebase", files=rb.conflicts)
                resolved = await self._merge_agent(t, wt)
                if not resolved:
                    self._fail(t, f"rebase conflict in {rb.conflicts}; retried on fresh base")
                    self._cleanup(tid)
                    return
                # the merge agent changed the tree: regenerate facts
                base = self.repo.merge_base(wt)
                handoff = generate_handoff(self.repo, t, wt, gates, res.fields, base)
                out_hash = self.repo.output_hash(wt, base)
            if rb.ok:
                base = self.repo.merge_base(wt)
                handoff = generate_handoff(self.repo, t, wt, gates, res.fields, base)
                out_hash = self.repo.output_hash(wt, base)
            before = self.repo.head(self.repo.integration_path)
            mr = self.repo.merge_task(tid)
            if not mr.ok:
                self.emit("conflict", tid, stage="merge", files=mr.conflicts)
                self._fail(t, f"merge conflict in {mr.conflicts}")
                self._cleanup(tid)
                return
            # 5. integration gates; revert the merge if they fail
            full = self.cfg.gate_policy == "full" or t.kind == Kind.INTEGRATE.value
            cmds = list(self.cfg.integration_gates) if full else list(t.done_when)
            if not full and cmds and self._gated_tree.get(tid) == self.repo.tree(self.repo.integration_path):
                # the rebase was a no-op: integration now holds exactly the tree the task's gates passed on
                self.emit("gate_reused", tid, cmds=cmds)
                cmds = []
            igates = await asyncio.to_thread(self.repo.run_gates, cmds) if cmds else []
            self._record_gates(tid, igates, full)
            if igates and not all(g["ok"] for g in igates):
                self.repo.git("reset", "--hard", before, cwd=self.repo.integration_path)
                bad = next(g for g in igates if not g["ok"])
                # Was the baseline already broken? Then this is an environment problem, not the task's.
                # (Only meaningful for the shared suite: a task's own done_when fails on a tree without it.)
                baseline = self.repo.run_gates([bad["cmd"]]) if full else [{"ok": True}]
                if not baseline[0]["ok"]:
                    t.attempts = max(0, t.attempts - 1)       # do not charge the attempt
                    t.state = State.BLOCKED.value
                    t.last_error = (f"integration gate '{bad['cmd']}' fails on the baseline too; fix the "
                                    f"environment and `macc retry {tid}`\n{clip_output(bad['stderr'], 400, 800)}{clip_output(bad['stdout'], 400, 800)}")
                    self._persist(tid)
                    self.emit("gate_broken_baseline", tid, cmd=bad["cmd"])
                    self._cleanup(tid)
                    return
                self.emit("integration_gate_failed", tid, cmd=bad["cmd"])
                self._fail(t, f"integration gate failed after merge: {bad['cmd']}\n{clip_output(bad['stderr'], 700, 1500)}{clip_output(bad['stdout'], 700, 1500)}")
                self._cleanup(tid)
                return
            if t.kind == Kind.INTEGRATE.value:
                flaky = self._flake_check(tid)
                if flaky:
                    self.repo.git("reset", "--hard", before, cwd=self.repo.integration_path)
                    self._fail(t, f"new tests are flaky (failed under another seed): {flaky['cmd']}\n"
                                  f"{clip_output(flaky['stdout'], 700, 1500)}")
                    self._cleanup(tid)
                    return
            handoff["merge_commit"] = mr.merge_commit
            self.merged_this_run.append(tid)
            self._merged_since_full_gate = not (full and cmds)

        elif t.kind == Kind.INTEGRATE.value and self.cfg.integration_gates:
            # an integrate that changed nothing still has to prove the tree is green: otherwise the review
            # after it sees no full-gate result for its tree (and cannot be tiered low)
            bad = self._ensure_full_gate(tid) or self._flake_check(tid)
            if bad:
                self._fail(t, f"integrate changed nothing but a gate fails: {bad['cmd']}\n"
                              f"{clip_output(bad['stderr'], 700, 1500)}{clip_output(bad['stdout'], 700, 1500)}")
                self._cleanup(tid)
                return
        # 6. done + staleness propagation
        self.store.save_handoff(tid, t.attempts, t.input_hash, out_hash, handoff)
        t.last_error = None
        t.transients = 0
        changed = self.graph.mark_done(tid, out_hash)
        self._persist(tid, *changed)
        self.emit("done", tid, output_hash=out_hash, files=len(handoff["files_changed"]), stale=changed)
        for c in changed:
            if c in self.running and self.graph.tasks[c].preempt:
                self.running[c].cancel()
                self.emit("preempt_requested", c, because=tid)
        self._cleanup(tid)

    def _prediction_vs_actual(self, wall: float) -> Optional[dict]:
        if not self.prediction:
            return None
        p = {k: self.prediction[k] for k in ("wall_s", "cost_usd", "serial_share")}
        p["error"] = cm_error(self.prediction, wall, self.cost_used - self._cost_at_start,
                              self.serial_seconds / wall if wall > 0 else 0.0)
        return p

    def _predicted_for(self, tid: str) -> dict:
        p = (self.prediction or {}).get("per_task", {}).get(tid)
        return {"pred_seconds": p["seconds"], "pred_cost": p["cost"]} if p else {}

    def critical_path_this_run(self) -> dict:
        """Longest chain (hard deps) of tasks that ran in this run, by actual worker seconds, with the
        planner's estimate for the same chain. merge_after is not a chain: those tasks run in parallel."""
        ran = self.run_seconds
        if not ran:
            return {}
        memo: dict[str, tuple[float, list[str]]] = {}

        def longest(tid: str) -> tuple[float, list[str]]:
            if tid not in memo:
                best: tuple[float, list[str]] = (0.0, [])
                for d in self.graph.tasks[tid].deps:
                    if d in ran:
                        cand = longest(d)
                        if cand[0] > best[0]:
                            best = cand
                memo[tid] = (best[0] + ran[tid], best[1] + [tid])
            return memo[tid]

        actual, path = max((longest(t) for t in ran), key=lambda x: x[0])
        est = sum(self.graph.tasks[t].est_minutes for t in path)
        return {"tasks": path, "actual_s": round(actual, 1), "est_min": round(est, 1),
                "actual_over_est": round(actual / 60 / est, 3) if est else None}

    def _record_gates(self, tid: Optional[str], gates: list[dict], full: bool) -> None:
        secs = round(sum(g.get("seconds", 0.0) for g in gates), 2)
        self.gate_seconds += secs
        if full and gates:
            bad = next((g for g in gates if not g["ok"]), None)
            self.emit("full_gate", tid, ok=bad is None, seconds=secs, cmds=[g["cmd"] for g in gates],
                      failed_cmd=bad["cmd"] if bad else None, head=self.repo.head(self.repo.integration_path),
                      tree=self.repo.tree(self.repo.integration_path))

    def _final_gate(self) -> str:
        """per_task policy: one full integration gate at the end if anything merged since the last one.
        On failure nothing is rolled back; the merge order is reported for a human or the next integrate."""
        if self.cfg.gate_policy == "full" or not self.cfg.integration_gates:
            return "n/a"
        if not self._merged_since_full_gate:
            return "skipped" if not self.merged_this_run else "covered"
        gates = self.repo.run_gates(self.cfg.integration_gates)
        self._record_gates(None, gates, full=True)
        bad = next((g for g in gates if not g["ok"]), None)
        if bad is None:
            self._merged_since_full_gate = False
            return "passed"
        self.emit("final_gate_failed", None, cmd=bad["cmd"], merged=list(self.merged_this_run),
                  output=(clip_output(bad["stderr"], 400, 800) + clip_output(bad["stdout"], 400, 800)))
        return "failed"

    async def _merge_agent(self, t: Task, wt: Path) -> bool:
        """Textual conflict: merge integration into the task worktree, hand the conflict markers to a
        merge worker restricted to the conflicting files, then verify (no markers, task gates pass)."""
        if not self.cfg.merge_agent:
            return False
        conflicts = self.repo.start_conflict_merge(t.id)
        if not conflicts:
            return True
        others = self.store.handoffs_touching(conflicts, exclude_task=t.id)
        mt = Task(id=f"{t.id}.merge", title=f"resolve conflicts for {t.id}", kind=Kind.MERGE.value,
                  owns=conflicts, effort="high", done_when=t.done_when,
                  spec=merge_conflict_spec(t, conflicts, others), attempts=t.attempts)
        pack = build_context_pack(mt, self.graph, self.store, self.cfg.project_rules)
        (self.repo.macc_dir / "context" / f"{mt.id}.md").write_text(pack)
        self.emit("merge_agent", t.id, files=conflicts, others=[h["_task_id"] for h in others])
        try:
            res = await self.worker(mt, wt, pack)
        except Exception as e:
            res = WorkerResult(ok=False, error=f"merge worker exception: {e!r}")
        self.tokens_used += res.input_tokens + res.output_tokens
        self.cost_used += res.cost_usd
        self.run_seconds[t.id] = self.run_seconds.get(t.id, 0.0) + res.seconds
        self.store.add_usage(t.id, t.attempts, self._worker_name() + ":merge", res.ok, model=res.model,
                             input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                             cache_read_tokens=res.cache_read_tokens, cost_usd=res.cost_usd, seconds=res.seconds,
                             est_minutes=t.est_minutes, kind=t.kind)
        leftover = self.repo.remaining_conflict_markers(t.id, conflicts) if res.ok else conflicts
        gates_ok = True
        if res.ok and not leftover and t.done_when:
            gates_ok = all(g["ok"] for g in self.repo.run_gates(t.done_when, cwd=wt))
        if not res.ok or leftover or not gates_ok:
            self.repo.abort_conflict_merge(t.id)
            self.emit("merge_agent_failed", t.id, leftover=leftover, gates_ok=gates_ok, error=(res.error or "")[:300])
            return False
        self.repo.finish_conflict_merge(t.id)
        self.emit("merge_agent_resolved", t.id, files=conflicts)
        return True

    def _review_reject(self, review: Task, notes: str) -> None:
        """REJECT verdict: reopen the reviewed tasks with the notes as their error context.
        Each rejection consumes one attempt of the review task, so review/fix loops are bounded."""
        redo_kinds = (Kind.CODE.value, Kind.CONTRACT.value, Kind.INTEGRATE.value)
        named = [x for x in parse_reopen(notes) if x in self.graph.ancestors(review.id)
                 and self.graph.tasks[x].kind in redo_kinds]
        reviewed = named or [d for d in review.deps if self.graph.tasks[d].kind in redo_kinds]
        if review.attempts >= review.max_attempts:
            # last round: stop the fix loop, leave reviewed work merged, escalate to a human
            self.emit("review_rejected", review.id, reviewed=reviewed, round=review.attempts, final=True)
            self._fail(review, "review rejected (final round, needs human): " + notes[:1500], final=True)
            return
        for d in reviewed:
            u = self.graph.tasks[d]
            u.state = State.PENDING.value
            u.attempts = 0
            u.last_error = f"review rejected (round {review.attempts}): {notes[:2500]}"
        # A reopened task that is not a direct dep (REOPEN: names an ancestor) leaves the DONE tasks between
        # it and the review untouched, so the review would be ready again at once and re-review the same
        # tree. Make those in-between tasks STALE: they revalidate (or re-run) after the redo, and only then
        # does the review run.
        between = set()
        for d in reviewed:
            between |= self.graph.descendants(d) & self.graph.ancestors(review.id)
        between = sorted(x for x in between - set(reviewed) if self.graph.tasks[x].state == State.DONE.value)
        for x in between:
            self.graph.tasks[x].state = State.STALE.value
        self.store.add_decision(f"Review round {review.attempts} rejected", notes[:2000], affects=reviewed)
        self._persist(*reviewed, *between)
        self.emit("review_rejected", review.id, reviewed=reviewed, round=review.attempts, held=between)
        self._fail(review, "review rejected: " + notes[:1500])   # PENDING until max_attempts, then FAILED

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
            if not self.cfg.revalidate_stale or not t.done_when or t.kind == Kind.REVIEW.value:
                t.state = State.PENDING.value
                t.attempts = 0
                self.emit("stale_rerun", t.id, reason="review must re-run" if t.kind == Kind.REVIEW.value
                          else "no gates to revalidate with")
            else:
                gates = self.repo.run_gates(t.done_when)
                if all(g["ok"] for g in gates) and t.kind == Kind.INTEGRATE.value and self.cfg.integration_gates:
                    bad = self._ensure_full_gate(t.id) or self._flake_check(t.id)   # the review is tiered on this
                    if bad:
                        gates.append(bad)
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
    def _reset_orphans(self) -> None:
        """A previous run died (or exited) while tasks were RUNNING/awaiting merge. Their worktrees and
        results are not durable, so they go back to PENDING without being charged an attempt."""
        for t in self.graph.tasks.values():
            if t.state in {s.value for s in ACTIVE} and t.id not in self.running and t.id not in self.parked:
                t.state = State.PENDING.value
                t.attempts = max(0, t.attempts - 1)
                t.preempt = False
                self._persist(t.id)
                self.emit("orphan_reset", t.id, reason="active in a previous run that did not finish")
                self._cleanup(t.id)

    async def run(self) -> dict:
        self.repo.ensure_integration()
        self.repo.gate_timeout = self.cfg.gate_timeout
        if self.cfg.worktree_setup:                   # the gates run in the integration worktree too
            bad = self.repo.run_setup(self.cfg.worktree_setup, self.repo.integration_path)
            if bad:
                self.stopped_reason = "worktree_setup_failed"
                self.emit("worktree_setup_failed", None, where="integration", cmd=bad["cmd"], code=bad["code"],
                          stderr=bad["stderr"][-300:])
        self._reset_orphans()
        self.graph.refresh_hashes()
        self._persist()
        self.run_seq = self.store.event("run_started", None, tasks=len(self.graph.tasks),
                                        max_parallel=self.cfg.max_parallel)
        if self._on_event:
            self._on_event("run_started", None, {"tasks": len(self.graph.tasks), "max_parallel": self.cfg.max_parallel})
        try:
            reconstruct_serial_flags(self.store)
            backfill_fix_rounds(self.store)
            self.prediction = predict(self.graph, self.store, max_parallel=self.cfg.max_parallel,
                                      review_tiering=self.cfg.review_tiering, low_model=self.cfg.review_low_model,
                                      history_dbs=self.cfg.history_dbs, gate_baseline_s=self.cfg.gate_baseline_s)
            self.emit("prediction", None, **{k: v for k, v in self.prediction.items() if k != "per_task"})
        except Exception as e:                  # the model only reports; it must never stop a run
            self.prediction = None
            self.emit("prediction_failed", None, error=repr(e)[:300])
        t0 = time.time()
        while True:
            self._revalidate_stale()
            self._dispatch_ready()
            await self._try_parked()
            if not self.running:
                if self.parked:
                    await self._try_parked()      # may block parked tasks whose targets failed
                pending_backoff = [v for k, v in self.backoff.items()
                                   if self.graph.tasks[k].state in (State.PENDING.value, State.READY.value)]
                if not self.running and pending_backoff:
                    await asyncio.sleep(max(0.0, min(pending_backoff) - time.time()))
                    continue
                if not self.running:
                    break
            done, _ = await asyncio.wait(set(self.running.values()), return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                tid, wt, res = fut.result()
                await self._on_finished(tid, wt, res)
            await self._try_parked()
        # anything still parked cannot be merged in this run (targets stuck); do not leave it in REVIEW
        for tid in list(self.parked):
            self.parked.pop(tid)
            t = self.graph.tasks[tid]
            t.state = State.PENDING.value
            t.attempts = max(0, t.attempts - 1)
            self._persist(tid)
            self.emit("parked_reset", tid, after=t.merge_after)
            self._cleanup(tid)
        final_gate = self._final_gate()
        wall = time.time() - t0
        summary = {
            "states": self.graph.summary(),
            "finished": self.graph.is_finished(),
            "tokens_used": self.tokens_used,
            # `cost_usd` alone was ambiguous (it became "since plan load" with the cumulative budget)
            "cost_this_run": round(self.cost_used - self._cost_at_start, 4),
            "cost_since_plan": round(self.cost_used, 4),
            "seconds": round(wall, 2),
            # time in which nothing else could run: the number that says where to split next time
            "serial": {"tasks": sorted(self._serial_points), "seconds": round(self.serial_seconds, 1),
                       "share": round(self.serial_seconds / wall, 3) if wall > 0 else 0.0},
            "critical_path": self.critical_path_this_run(),
            "prediction": self._prediction_vs_actual(wall),
            "gate_policy": self.cfg.gate_policy,
            "gate_seconds": round(self.gate_seconds, 2),
            "final_gate": final_gate,
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
