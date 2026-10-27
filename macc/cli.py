"""macc command line.

  macc init  [--repo .]                       create .macc/, integration branch, state db
             [--discover-gates] [--pick N]    time the repo's own checks; pick integration gates from them
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


# config keys that belong to the repository, not to a plan
REPO_KEYS = ("history_dbs", "gate_baseline_s", "gate_timeout", "worktree_setup", "integration_gates",
             "flake_check_runs")   # flake check is pytest-only: a repo setting


def cmd_init(a) -> int:
    repo, store = _open(a.repo)
    repo.ensure_integration()
    rules = repo.macc_dir / "rules.md"
    if not rules.exists():
        rules.write_text("# Project rules for macc workers\n\n- Keep this file short. Only things a model would guess wrong.\n")
    cfgp = repo.macc_dir / "config.json"
    cfg = json.loads(cfgp.read_text()) if cfgp.exists() else {}
    if a.setup:
        cfg["worktree_setup"] = a.setup
    if a.gate_timeout is not None:
        cfg["gate_timeout"] = a.gate_timeout
    repo.gate_timeout = int(cfg.get("gate_timeout", 1800))
    setup_ok = True
    if cfg.get("worktree_setup"):                 # the gate below runs in the integration worktree: set it up first
        bad = repo.run_setup(cfg["worktree_setup"], repo.integration_path)
        if bad:
            setup_ok = False
            print(f"WARNING: worktree_setup fails in the integration worktree: {bad['cmd']!r} (exit {bad['code']})\n"
                  f"{bad['stderr'][-600:]}")
    if a.gate:
        cfg["integration_gates"] = a.gate
    if a.gate and setup_ok:
        res = repo.run_gates(a.gate)
        bad = [r for r in res if not r["ok"]]
        if bad:
            print(f"WARNING: gate fails on the current integration tree: {bad[0]['cmd']!r}\n{bad[0]['stderr'][-600:]}{bad[0]['stdout'][-600:]}")
        else:
            # this repo's own gate time: the cost model uses it (worker times may come from other repos)
            cfg["gate_baseline_s"] = round(sum(r.get("seconds", 0.0) for r in res), 2)
            print(f"gate verified on integration tree: {a.gate} ({cfg['gate_baseline_s']} s)")
    picked_ok = _discover_gates(a, repo, cfg) if (a.discover_gates or a.pick) and setup_ok else True
    if a.gate_baseline is not None:               # e.g. a build cache made the measured run unrepresentative
        cfg["gate_baseline_s"] = a.gate_baseline
    for h in a.history or []:
        path = str(Path(h).expanduser().resolve())
        if not Path(path).is_file():
            print(f"WARNING: history db not found: {path}")
        dbs = cfg.setdefault("history_dbs", [])
        if path not in dbs:
            dbs.append(path)
    cfgp.write_text(json.dumps(cfg, indent=2))
    print(f"initialised {repo.macc_dir} (integration branch + state db); gates={cfg.get('integration_gates', [])}")
    return 0 if picked_ok else 1


def _discover_gates(a, repo: Repo, cfg: dict) -> bool:
    """--discover-gates times the repo's own check targets; --pick (or the prompt) appends chosen ones to
    integration_gates. Picks without --discover-gates reuse the last discovery, so nothing is timed twice."""
    from . import gates
    import os
    saved = repo.macc_dir / gates.RESULTS_FILE
    if a.discover_gates:
        try:
            rec = gates.discover(repo.integration_path, repo.integration_path, {**os.environ, **repo.macc_env()},
                                 int(cfg.get("gate_timeout", 1800)), exclude=a.discover_exclude or [],
                                 budget_s=a.discover_budget)
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return False
        saved.write_text(json.dumps(rec, indent=2))
    elif saved.exists():
        rec = json.loads(saved.read_text())
        print(f"using the discovery of tree {rec['tree'][:12]} ({saved})")
    else:
        print("ERROR: --pick needs a discovery first: run `macc init --discover-gates`")
        return False
    for line in gates.format_report(rec):
        print(line)
    picks = a.pick or []
    if not picks and a.discover_gates and gates.candidates(rec) and sys.stdin.isatty():
        picks = [input("add which to integration_gates (numbers or names, comma-separated; empty = none)? ")]
    if not picks:
        if gates.candidates(rec):
            print("nothing added; pick with `macc init --pick N[,N...]` (reuses this discovery)")
        return True
    chosen, errors = gates.resolve_picks(rec, picks)
    for e in errors:
        print(f"ERROR: not added: {e}")
    current = cfg.setdefault("integration_gates", [])
    added = [t for t in chosen if t["cmd"] not in current]
    current += [t["cmd"] for t in added]
    if added:                                     # the cost model's gate time grows by what was measured here
        cfg["gate_baseline_s"] = round(cfg.get("gate_baseline_s", 0.0) + sum(t["seconds"] for t in added), 2)
        print(f"added {[t['cmd'] for t in added]}; gate_baseline_s = {cfg['gate_baseline_s']}")
    return not errors


def _prediction_lines(g, store: Store, cfg: dict) -> list[str]:
    """The cost model's numbers for a plan (and split hints only when enabled in config)."""
    from .costmodel import backfill_fix_rounds, describe, predict, reconstruct_serial_flags, split_hints
    try:
        reconstruct_serial_flags(store)
        backfill_fix_rounds(store)
        pred = predict(g, store, max_parallel=int(cfg.get("max_parallel", 4)),
                       review_tiering=cfg.get("review_tiering", True), low_model=cfg.get("review_low_model", "sonnet"),
                       history_dbs=cfg.get("history_dbs", []), gate_baseline_s=cfg.get("gate_baseline_s"))
    except Exception as e:                      # never block planning on the model
        return [f"prediction unavailable: {e!r}"[:200]]
    lines = [describe(pred)]
    if cfg.get("split_hints", True):
        floor = store._conn.execute("SELECT MIN(seconds) m FROM usage WHERE kind='code' AND model IS NOT NULL "
                                    "AND ok=1").fetchone()["m"] or 30.0
        lines += [f"split hint: {h}" for h in split_hints(pred, floor)]
    return lines


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
        repo.gate_timeout = int(cfg_now.get("gate_timeout", 1800))
        pr = asyncio.run(generate_plan(a.from_desc, ipath, worker, out, effort=a.effort, model=a.model,
                                       existing_files=existing, current_gate=gate_now,
                                       history=store.history_by_kind()))
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
        if pr.config.get("scope_note"):
            print(f"\nSCOPE NOTE from planner: {pr.config['scope_note']}")
        proposed = pr.config.get("integration_gates") or []
        trusted = set(cfg_now.get("integration_gates", []))
        if proposed:
            # never execute model-written shell before a human saw it, unless it is the gate already in use
            runnable = [c for c in proposed if c in trusted] if not a.check_gate else proposed
            skipped = [c for c in proposed if c not in runnable]
            if skipped:
                pr.warnings.append("proposed integration gate(s) not executed (model-written, unreviewed): "
                                   f"{skipped}. Inspect them, then re-run with --check-gate to verify on the baseline")
            if runnable:
                res = repo.run_gates(runnable)
                bad = [r for r in res if not r["ok"]]
                if bad:
                    pr.warnings.append(f"proposed integration gate FAILS on the current integration tree: {bad[0]['cmd']!r} "
                                       f"-> every task would be BLOCKED (gate_broken_baseline)")
        # the planner's config holds plan keys (scope_note, max_parallel, ...); repo keys come from config.json
        pred_cfg = {**cfg_now, **(pr.config or {})}
        for k in REPO_KEYS:
            if k in cfg_now and k not in (pr.config or {}):
                pred_cfg[k] = cfg_now[k]
        pr.warnings.extend(_prediction_lines(g, store, pred_cfg))
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
    # repo-level settings written by `macc init` survive loading a plan (unless the plan sets them)
    prev = json.loads((repo.macc_dir / "config.json").read_text()) if (repo.macc_dir / "config.json").exists() else {}
    for k in REPO_KEYS:
        if k in prev and k not in cfg:
            cfg[k] = prev[k]
    cfg["cost_baseline_usd"] = store.usage_totals()["cost_usd"]   # budget counts from here
    cfg["plan_base"] = repo.head(repo.ensure_integration())       # reviews tier on this plan's change
    (repo.macc_dir / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"loaded {len(g.tasks)} tasks; ready now: {[t.id for t in g.ready()]}")
    from .ownership import gap_warnings, ownership_gaps
    ipath = repo.ensure_integration()
    gaps = ownership_gaps(g, ipath, repo.git("ls-files", cwd=ipath).stdout.split())
    if gaps:
        store.event("ownership_gaps", None, files=[x["file"] for x in gaps[:50]], suggest=gaps[0]["suggest"])
        if a.gaps:
            print(f"{len(gaps)} unowned file(s) mention identifiers from the specs (score, file, identifiers):")
            for x in gaps:
                print(f"  {x['score']:>5}  {x['file']}: {', '.join(x['idents'])}")
        else:
            print("WARNING: " + gap_warnings(gaps)[0])
    for line in _prediction_lines(g, store, cfg):
        print(line)
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
    last = store.latest_event("run_finished")
    cp = (last or {}).get("payload", {}).get("critical_path") if last else None
    if cp:
        ratio = f" (actual/est={cp['actual_over_est']:.3f})" if cp.get("actual_over_est") is not None else ""
        print(f"last run critical path: {' -> '.join(cp['tasks'])}: actual {cp['actual_s']:.0f} s, "
              f"est {cp['est_min']:.0f} min{ratio}")
    pv = (last or {}).get("payload", {}).get("prediction") if last else None
    if pv and pv.get("error"):
        e = pv["error"]
        pct = lambda x: "n/a" if x is None else f"{x:+.0%}"   # noqa: E731  (no actual cost on mock runs)
        print(f"last run prediction: wall {pv['wall_s']:.0f} s ({pct(e['wall'])}), cost ${pv['cost_usd']:.2f} "
              f"({pct(e['cost'])}), serial share {pv['serial_share']:.0%} ({e['serial_share'] * 100:+.0f} pt) vs actual")
    ser = (last or {}).get("payload", {}).get("serial") if last else None
    if ser and ser.get("tasks"):
        print(f"last run serial share: {ser['share']:.0%} ({ser['seconds']:.0f} s in {', '.join(ser['tasks'])})")
    for row in store.usage_by_kind():
        est = (f"actual/est={row['est_ratio']:.2f} ({row['est_tasks']} tasks)"
               if row.get("est_ratio") is not None else "actual/est=-")
        print(f"  {row['kind']:<10} runs={row['runs']} avg_tokens={row['avg_tokens']:.0f} "
              f"avg_s={row['avg_seconds']:.1f} success={row['success_rate']:.2f} {est}")
    return 0


def cmd_backtest(a) -> int:
    from .costmodel import backtest
    repo, store = _open(a.repo)
    cfgp = repo.macc_dir / "config.json"
    cfg = json.loads(cfgp.read_text()) if cfgp.exists() else {}
    res = backtest(store, cfg.get("history_dbs", []))
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    pct = lambda x: "n/a" if x is None else f"{x:+.0%}"   # noqa: E731
    print("| run | tasks | wall pred / actual | cost pred / actual | serial share pred / actual |")
    print("|---|---|---|---|---|")
    for r in res["runs"]:
        p_, a_, e = r["pred"], r["actual"], r["error"]
        print(f"| {r['run_seq']} | {r['tasks']} | {p_['wall_s']:.0f} / {a_['wall_s']:.0f} s ({pct(e['wall'])}) | "
              f"${p_['cost_usd']:.2f} / ${a_['cost_usd']:.2f} ({pct(e['cost'])}) | "
              f"{p_['serial_share']:.0%} / {a_['serial_share']:.0%} ({e['serial_share'] * 100:+.0f} pt) |")
    for x in res["excluded"]:
        print(f"excluded run {x['run_seq']}: {x['reason']}")
    m = res["mean_abs_error"]
    print(f"\n{res['within_30pct']}/{len(res['runs'])} runs within 30% (wall and cost); mean |error|: "
          f"wall {pct(m['wall'])[1:] if m['wall'] is not None else 'n/a'}, cost "
          f"{pct(m['cost'])[1:] if m['cost'] is not None else 'n/a'}, serial share "
          f"{m['serial_share'] * 100:.0f} pt" if m["serial_share"] is not None else "")
    print(f"history: this repo + {len(cfg.get('history_dbs', []))} other db(s)")
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

    sp = sub.add_parser("init"); sp.add_argument("--gate", action="append", default=None,
                                                 help="integration gate command (repeatable); recorded in config.json and seen by the planner")
    sp.add_argument("--history", action="append", default=None,
                    help="another macc state.db whose worker history calibrates the cost model (repeatable, appends)")
    sp.add_argument("--setup", action="append", default=None,
                    help="worktree_setup command (repeatable, replaces the list); run in the integration worktree before the gate")
    sp.add_argument("--gate-timeout", type=int, default=None, help="seconds per gate command (config gate_timeout)")
    sp.add_argument("--gate-baseline", type=float, default=None,
                    help="record this gate time (s) instead of the measured one (e.g. the measured run hit a build cache)")
    sp.add_argument("--discover-gates", action="store_true",
                    help="time the repo's check/lint/format/typecheck/test targets on the integration tree and list "
                         "those passing in under half of gate_timeout (saved to .macc/gate_discovery.json)")
    sp.add_argument("--discover-exclude", action="append", default=None, metavar="REGEX",
                    help="with --discover-gates: also skip targets whose name matches (repeatable)")
    sp.add_argument("--discover-budget", type=float, default=None, metavar="SECONDS",
                    help="with --discover-gates: stop timing new targets after this much total time")
    sp.add_argument("--pick", action="append", default=None,
                    help="discovered candidates to append to integration_gates: list numbers, names or commands")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("plan"); sp.add_argument("plan", nargs="?", default=None)
    sp.add_argument("--from", dest="from_desc", default=None, help="feature description -> generate PLAN.yaml with one planner call")
    sp.add_argument("--worker", choices=["claude", "mock"], default="claude")
    sp.add_argument("--model", default=None); sp.add_argument("--effort", default="high")
    sp.add_argument("--timeout", type=int, default=900)
    sp.add_argument("--keep-all", action="store_true", help="keep unfinished tasks from earlier plans too")
    sp.add_argument("--gaps", action="store_true",
                    help="list every unowned file that mentions an identifier from the specs (default: the top 12)")
    sp.add_argument("--check-gate", action="store_true",
                    help="with --from: execute the planner's proposed integration gate on the baseline (model-written shell!)")
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
    sp = sub.add_parser("backtest", help="predict every past run leave-one-out and compare with what happened")
    sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_backtest)
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
