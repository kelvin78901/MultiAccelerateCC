"""macc command line.

  macc init  [--repo .]                       create .macc/, integration branch, state db
  macc plan  PLAN.yaml [--repo .]             load a plan into the state db (replaces pending graph)
  macc run   [--repo .] [--worker claude|mock] [--parallel N] [--budget TOKENS] [--gate CMD ...]
  macc status [--repo .]                      print graph state, debts, usage
  macc change TASK_ID --spec "..." [--why "..."]   change a spec; downstream is invalidated
  macc split  PARENT --plan sub.yaml          replace a task by subtasks (dynamic decomposition)
  macc retry  TASK_ID                         reset a FAILED/BLOCKED task to pending
  macc debts  [--resolve ID]                  list / resolve debts
  macc events [--task ID] [--n 50]            tail the event log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .graph import State, Task
from .plan import load_plan
from .scheduler import Scheduler, SchedulerConfig
from .store import Store
from .workers import ClaudeWorker, MockWorker
from .worktree import Repo


def _open(repo_path: str) -> tuple[Repo, Store]:
    repo = Repo(repo_path)
    store = Store(repo.macc_dir / "state.db")
    return repo, store


def _rules(repo: Repo) -> str:
    p = repo.macc_dir / "rules.md"
    return p.read_text() if p.exists() else ""


def _print_event(type_: str, tid, payload: dict) -> None:
    ts = time.strftime("%H:%M:%S")
    extra = ""
    if payload:
        extra = " " + json.dumps({k: v for k, v in payload.items() if k not in ("worktree",)}, ensure_ascii=False)[:200]
    print(f"[{ts}] {type_:<24} {tid or '-':<20}{extra}", flush=True)


def cmd_init(a) -> int:
    repo, store = _open(a.repo)
    repo.ensure_integration()
    rules = repo.macc_dir / "rules.md"
    if not rules.exists():
        rules.write_text("# Project rules for macc workers\n\n- Keep this file short. Only things a model would guess wrong.\n")
    print(f"initialised {repo.macc_dir} (integration branch + state db)")
    return 0


def cmd_plan(a) -> int:
    repo, store = _open(a.repo)
    if a.from_desc:
        from .planner import generate_plan
        out = Path(a.plan or "plan.yaml")
        if a.worker == "mock":
            worker = MockWorker()
        else:
            worker = ClaudeWorker(default_model=a.model, timeout=a.timeout)
            if not worker.available():
                print("claude binary not found on PATH", file=sys.stderr)
                return 2
        # plan against the integration branch: that is the tree workers will fork from
        ipath = repo.ensure_integration()
        existing = repo.git("ls-files", cwd=ipath).stdout.split()
        cfg_now = json.loads((repo.macc_dir / "config.json").read_text()) if (repo.macc_dir / "config.json").exists() else {}
        gate_now = " && ".join(cfg_now.get("integration_gates", []))
        pr = asyncio.run(generate_plan(a.from_desc, ipath, worker, out, effort=a.effort, model=a.model,
                                       existing_files=existing, current_gate=gate_now))
        g = pr.graph
        for i, u in enumerate(pr.usage, 1):
            store.add_usage("_plan", i, getattr(worker, "name", "worker"), u.ok, model=u.model,
                            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                            cache_read_tokens=u.cache_read_tokens, cost_usd=u.cost_usd, seconds=u.seconds)
        store.event("plan_generated", None, out=str(out), tasks=len(g.tasks), rounds=len(pr.usage),
                    cost_usd=round(sum(u.cost_usd for u in pr.usage), 4))
        print(f"wrote {out} ({len(g.tasks)} tasks, {len(pr.usage)} planner round(s), "
              f"${sum(u.cost_usd for u in pr.usage):.2f}). Review it, then: macc plan {out}")
        for tid in g.topo_order():
            t = g.tasks[tid]
            print(f"  {t.kind:<9} {t.id:<24} deps={t.deps} merge_after={t.merge_after} owns={t.owns} ~{t.est_minutes:.0f}min")
        if pr.config.get("integration_gates"):
            res = repo.run_gates(pr.config["integration_gates"])
            bad = [r for r in res if not r["ok"]]
            if bad:
                pr.warnings.append(f"proposed integration gate FAILS on the current integration tree: {bad[0]['cmd']!r} "
                                   f"-> every task would be BLOCKED (gate_broken_baseline)")
        if pr.warnings:
            print("\nWARNINGS (fix before `macc plan`):")
            for w in pr.warnings:
                print(f"  - {w}")
        print("\n" + g.to_mermaid())
        return 0
    if not a.plan:
        print("usage: macc plan PLAN.yaml | macc plan --from 'description' [PLAN.yaml]", file=sys.stderr)
        return 2
    g, cfg = load_plan(a.plan)
    existing = store.load_graph()
    # keep runtime state for tasks that already exist and whose spec did not change
    for t in g.tasks.values():
        old = existing.tasks.get(t.id)
        if old and old.spec == t.spec and sorted(old.deps) == sorted(t.deps) \
                and old.done_when == t.done_when and sorted(old.owns) == sorted(t.owns):
            for k in ("state", "attempts", "output_hash", "input_hash", "last_error"):
                setattr(t, k, getattr(old, k))
    # tasks from earlier plans stay in the store when finished (history + handoffs);
    # unfinished ones not in the new plan are dropped unless --keep-all
    for old_id, old in existing.tasks.items():
        if old_id in g.tasks:
            continue
        if old.state == State.DONE.value or a.keep_all:
            g.add(old)
        else:
            store.delete_task(old_id)
    g.validate()
    g.refresh_hashes()
    store.save_graph(g)
    cfg["cost_baseline_usd"] = store.usage_totals()["cost_usd"]   # budget counts from here
    (repo.macc_dir / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"loaded {len(g.tasks)} tasks; ready now: {[t.id for t in g.ready()]}")
    return 0


def _config(repo: Repo, a) -> SchedulerConfig:
    cfg = {}
    p = repo.macc_dir / "config.json"
    if p.exists():
        cfg = json.loads(p.read_text())
    if a.parallel is not None:
        cfg["max_parallel"] = a.parallel
    if a.budget is not None:
        cfg["token_budget"] = a.budget
    if a.cost_budget is not None:
        cfg["cost_budget_usd"] = a.cost_budget
    if a.gate:
        cfg["integration_gates"] = a.gate
    if a.keep_worktrees:
        cfg["keep_worktrees"] = True
    cfg["project_rules"] = _rules(repo)
    return SchedulerConfig(**{k: v for k, v in cfg.items() if k in SchedulerConfig.__dataclass_fields__})


def cmd_run(a) -> int:
    repo, store = _open(a.repo)
    g = store.load_graph()
    if not g.tasks:
        print("no tasks; run `macc plan PLAN.yaml` first", file=sys.stderr)
        return 2
    if a.worker == "mock":
        worker = MockWorker()
    else:
        from .hooks import install as install_hooks
        settings = None if a.no_guard else install_hooks(repo.macc_dir)
        worker = ClaudeWorker(default_model=a.model, timeout=a.timeout, settings_file=settings)
        if not worker.available():
            print("claude binary not found on PATH", file=sys.stderr)
            return 2
    s = Scheduler(repo, store, g, worker, _config(repo, a), on_event=_print_event)
    summary = asyncio.run(s.run())
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"state written to {repo.macc_dir / 'STATE.md'}")
    return 0 if summary["finished"] else 1


def cmd_status(a) -> int:
    repo, store = _open(a.repo)
    g = store.load_graph()
    for tid in g.topo_order():
        t = g.tasks[tid]
        flag = " PREEMPT" if t.preempt else ""
        print(f"{t.state:<9} {t.kind:<9} {t.id:<24} attempts={t.attempts} deps={t.deps}{flag}")
    print("\nsummary:", g.summary())
    print("ready:", [t.id for t in g.ready()])
    debts = store.open_debts()
    if debts:
        print(f"\nopen debts ({len(debts)}):")
        for d in debts:
            print(f"  #{d['id']} [{d['kind']}] {d['task_id']}: {d['note']}")
    u = store.usage_totals()
    print(f"\nusage: runs={u['runs']} in={u['input_tokens']} out={u['output_tokens']} "
          f"cache_read={u['cache_read_tokens']} cost=${u['cost_usd']:.2f} worker_time={u['seconds']:.0f}s")
    for row in store.usage_by_kind():
        print(f"  {row['kind']:<10} runs={row['runs']} avg_tokens={row['avg_tokens']:.0f} "
              f"avg_s={row['avg_seconds']:.1f} success={row['success_rate']:.2f}")
    return 0


def cmd_change(a) -> int:
    repo, store = _open(a.repo)
    g = store.load_graph()
    if a.task not in g.tasks:
        print(f"unknown task {a.task}", file=sys.stderr)
        return 2
    fields = {}
    if a.owns:
        fields["owns"] = a.owns
    if a.done_when:
        fields["done_when"] = a.done_when
    affected = g.update_spec(a.task, a.spec if a.spec is not None else g.tasks[a.task].spec, **fields)
    g.validate()
    store.save_graph(g)
    if a.why:
        store.add_decision(f"Spec change: {a.task}", a.why, affects=affected)
    store.event("spec_changed", a.task, affected=affected)
    print(f"changed {a.task}; invalidated: {affected}")
    return 0


def cmd_split(a) -> int:
    repo, store = _open(a.repo)
    g = store.load_graph()
    sub, _ = load_plan(a.plan)
    g.add_subtasks(a.parent, list(sub.tasks.values()))
    store.save_graph(g)
    store.event("split", a.parent, into=[t.id for t in sub.tasks.values()])
    print(f"{a.parent} now integrates {[t.id for t in sub.tasks.values()]}")
    return 0


def cmd_retry(a) -> int:
    repo, store = _open(a.repo)
    g = store.load_graph()
    t = g.tasks[a.task]
    t.state = State.PENDING.value
    t.attempts = 0
    if a.max_attempts:
        t.max_attempts = a.max_attempts
    store.save_task(t)
    store.event("manual_retry", a.task)
    print(f"{a.task} -> pending")
    return 0


def cmd_debts(a) -> int:
    repo, store = _open(a.repo)
    if a.resolve:
        store.resolve_debt(a.resolve)
        print(f"resolved #{a.resolve}")
    for d in store.open_debts():
        print(f"#{d['id']} [{d['kind']}] {d['task_id']} {d['path'] or ''}: {d['note']}")
    return 0


def cmd_events(a) -> int:
    repo, store = _open(a.repo)
    last = 0
    for e in store.events(task_id=a.task, limit=a.n):
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
        print(f"{ts} {e['type']:<24} {e['task_id'] or '-':<20} {e['payload'] or ''}"[:200], flush=True)
        last = e["seq"]
    while a.follow:
        time.sleep(1.0)
        for e in store.events(task_id=a.task, since_seq=last, limit=200):
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
            print(f"{ts} {e['type']:<24} {e['task_id'] or '-':<20} {e['payload'] or ''}"[:200], flush=True)
            last = e["seq"]
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="macc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=".", help="target git repository (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)
    sp = sub.add_parser("plan"); sp.add_argument("plan", nargs="?", default=None)
    sp.add_argument("--from", dest="from_desc", default=None, help="feature description -> generate PLAN.yaml with one planner call")
    sp.add_argument("--worker", choices=["claude", "mock"], default="claude")
    sp.add_argument("--model", default=None); sp.add_argument("--effort", default="high")
    sp.add_argument("--timeout", type=int, default=900)
    sp.add_argument("--keep-all", action="store_true", help="keep unfinished tasks from earlier plans too")
    sp.set_defaults(fn=cmd_plan)
    sp = sub.add_parser("run")
    sp.add_argument("--worker", choices=["claude", "mock"], default="claude")
    sp.add_argument("--model", default=None)
    sp.add_argument("--parallel", type=int, default=None)
    sp.add_argument("--budget", type=int, default=None, help="total in+out token budget for this run")
    sp.add_argument("--cost-budget", type=float, default=None, help="stop dispatching once reported cost (USD) reaches this")
    sp.add_argument("--gate", action="append", help="integration gate command (repeatable)")
    sp.add_argument("--timeout", type=int, default=3600, help="per-worker timeout seconds")
    sp.add_argument("--keep-worktrees", action="store_true")
    sp.add_argument("--no-guard", action="store_true", help="do not inject the ownership PreToolUse hook")
    sp.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sp = sub.add_parser("change"); sp.add_argument("task"); sp.add_argument("--spec", default=None)
    sp.add_argument("--owns", action="append", default=None, help="replace ownership globs (repeatable)")
    sp.add_argument("--done-when", action="append", default=None, help="replace gate commands (repeatable)")
    sp.add_argument("--why", default=None); sp.set_defaults(fn=cmd_change)
    sp = sub.add_parser("split"); sp.add_argument("parent"); sp.add_argument("--plan", required=True)
    sp.set_defaults(fn=cmd_split)
    sp = sub.add_parser("retry"); sp.add_argument("task"); sp.add_argument("--max-attempts", type=int, default=None)
    sp.set_defaults(fn=cmd_retry)
    sp = sub.add_parser("debts"); sp.add_argument("--resolve", type=int, default=None); sp.set_defaults(fn=cmd_debts)
    sp = sub.add_parser("events"); sp.add_argument("--task", default=None); sp.add_argument("--n", type=int, default=50)
    sp.add_argument("--follow", "-f", action="store_true", help="keep printing new events (use in a second terminal)")
    sp.set_defaults(fn=cmd_events)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
