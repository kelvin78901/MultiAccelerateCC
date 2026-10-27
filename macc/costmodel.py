"""Phase cost model (v0.3): predict a run's wall time, cost and serial share from measured history.

A run is a sequence of global serial points (tasks every other task of the run waits for, or is waited
for by) and parallel phases between them:

    wall = sum(serial-point durations) + sum over phases of max(longest chain, phase work / parallelism)
           + full-gate time
    cost = sum(per-task cost)

Durations and costs come from the `usage` table, never from tuned constants: a serial point is weighted
with the median actual seconds of its kind *when that kind ran as a serial point*, other tasks with the
median of their kind. Only predicts; it never changes a plan.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Iterable, Optional

from .graph import Kind, State, TaskGraph
from .store import Store

MIN_SAMPLES = 3          # below this a statistic falls back to the next broader one


# --------------------------------------------------------------------------- structure
def static_serial_points(graph: TaskGraph, ids: set[str]) -> set[str]:
    """Tasks of `ids` that every other task of `ids` is upstream or downstream of (hard deps only:
    merge_after does not order running, only merging)."""
    out = set()
    for t in ids:
        related = (graph.ancestors(t) | graph.descendants(t)) & ids
        if ids - {t} <= related:
            out.add(t)
    return out


def _phases(graph: TaskGraph, ids: set[str], serial: set[str]) -> dict[int, set[str]]:
    """Non-serial tasks grouped by how many serial points precede them: one group per parallel phase."""
    phases: dict[int, set[str]] = {}
    for t in ids - serial:
        phases.setdefault(len(graph.ancestors(t) & serial), set()).add(t)
    return phases


def _longest_chain(graph: TaskGraph, group: set[str], dur: dict[str, float]) -> float:
    memo: dict[str, float] = {}

    def run(t: str) -> float:
        if t not in memo:
            memo[t] = dur[t] + max((run(d) for d in graph.tasks[t].deps if d in group), default=0.0)
        return memo[t]
    return max((run(t) for t in group), default=0.0)


# ----------------------------------------------------------------------------- history
def reconstruct_serial_flags(store: Store) -> int:
    """Fill usage.serial for rows recorded before macc wrote it, from the event log: a dispatch was a
    serial point if every other task the run touched and had not finished yet was downstream of it.
    Rows whose task (and so its deps) is no longer in the store stay NULL. Returns rows updated."""
    conn = store._conn
    graph = store.load_graph()
    events = [dict(r) for r in conn.execute("SELECT seq, type, task_id, payload FROM events ORDER BY seq")]
    flags: dict[tuple[str, int], list[int]] = {}
    fixes: dict[tuple[str, int], list[int]] = {}   # same keys: had the task completed before this dispatch?
    runs_of: dict[tuple[str, int], list[int]] = {}  # same keys: seq of the run_started the dispatch belongs to
    completed: set[str] = set()                     # across runs
    run: list = []
    for ev in events + [{"seq": None, "type": "run_started", "task_id": None, "payload": None}]:
        if ev["type"] == "run_started" and run:
            touched = {e["task_id"] for e in run if e["type"] in ("dispatched", "revalidated", "stale_rerun",
                                                                  "integrate_skipped") and e["task_id"] in graph.tasks}
            finished: set[str] = set()
            for e in run:
                tid = e["task_id"]
                if e["type"] == "dispatched" and tid:
                    attempt = json.loads(e["payload"] or "{}").get("attempt", 1)
                    fixes.setdefault((tid, attempt), []).append(int(tid in completed))
                    runs_of.setdefault((tid, attempt), []).append(run[0]["seq"] if run[0]["type"] == "run_started" else None)
                    if tid in graph.tasks:
                        later = graph.descendants(tid)
                        flags.setdefault((tid, attempt), []).append(int(touched - finished - {tid} <= later))
                elif e["type"] in ("done", "revalidated", "integrate_skipped") and tid:
                    finished.add(tid)
                    completed.add(tid)
            run = []
        if ev["type"] != "run_started" or ev["seq"] is not None:
            run.append(ev)
    updated = 0
    seen: dict[tuple[str, int], int] = {}
    rows = conn.execute("SELECT id, task_id, attempt, worker FROM usage ORDER BY id").fetchall()
    last_run = None
    for r in rows:
        if r["worker"].endswith(":merge"):      # a merge agent runs inside its task's run
            if last_run is not None:
                conn.execute("UPDATE usage SET run_seq=? WHERE id=? AND run_seq IS NULL", (last_run, r["id"]))
            continue
        key = (r["task_id"], r["attempt"])
        i = seen.get(key, 0)
        seen[key] = i + 1
        vals, fx, rs = flags.get(key, []), fixes.get(key, []), runs_of.get(key, [])
        if i < len(rs) and rs[i] is not None:
            last_run = rs[i]
            conn.execute("UPDATE usage SET run_seq=? WHERE id=? AND run_seq IS NULL", (rs[i], r["id"]))
        if i < len(vals):
            updated += conn.execute("UPDATE usage SET serial=? WHERE id=? AND serial IS NULL",
                                    (vals[i], r["id"])).rowcount
        if i < len(fx):
            conn.execute("UPDATE usage SET fix_round=? WHERE id=? AND fix_round IS NULL", (fx[i], r["id"]))
    return updated


def model_family(model: Optional[str]) -> Optional[str]:
    """'opus' / 'sonnet' / 'haiku' from a CLI model id or alias (claude-opus-5-5, opus, ...)."""
    m = (model or "").lower()
    return next((f for f in ("opus", "sonnet", "haiku") if f in m), m or None)


def backfill_fix_rounds(store: Store) -> int:
    """Fallback for rows reconstruct_serial_flags could not match to a dispatch event: a row is a fix round
    if an earlier row of the same task has an attempt number >= its own (attempts restart when a task is
    re-run in a later round; a retry inside a round has a higher attempt number than the rows before it)."""
    conn = store._conn
    rows = conn.execute("SELECT id, task_id, attempt, fix_round FROM usage WHERE worker NOT LIKE '%:merge' "
                        "ORDER BY id").fetchall()
    seen: dict[str, list[int]] = {}
    updated = 0
    for r in rows:
        prev = seen.setdefault(r["task_id"], [])
        if r["fix_round"] is None:
            fix = int(any(a >= r["attempt"] for a in prev))
            updated += conn.execute("UPDATE usage SET fix_round=? WHERE id=?", (fix, r["id"])).rowcount
        prev.append(r["attempt"])
    return updated


def history(store: Store, exclude_ids: Iterable[int] = (), extra_dbs: Iterable[str] = ()) -> list[dict]:
    """Every worker run that can calibrate the model: kind, model family, fix round, serial, seconds, cost.
    `extra_dbs` are other repositories' state.db files (config `history_dbs`): worker times transfer across
    repositories, gate times do not (those come from this repository only)."""
    out = _history_rows(store, set(exclude_ids))
    for path in extra_dbs:
        if Path(path).is_file():
            other = Store(path)
            try:
                out += _history_rows(other, set())
            finally:
                other.close()
    return out


def _history_rows(store: Store, excl: set) -> list[dict]:
    out = []
    for r in store._conn.execute("SELECT id, kind, model, seconds, cost_usd, serial, fix_round FROM usage "
                                 "WHERE model IS NOT NULL AND kind IS NOT NULL AND kind != 'plan' "
                                 "AND worker NOT LIKE '%:merge'"):
        if r["id"] in excl:
            continue
        out.append({"kind": r["kind"], "family": model_family(r["model"]), "fix": r["fix_round"] == 1,
                    "serial": r["serial"] == 1, "seconds": r["seconds"], "cost": r["cost_usd"]})
    return out


def full_gate_seconds(store: Store, exclude_seq: Iterable[int] = ()) -> Optional[float]:
    excl = set(exclude_seq)
    xs = [json.loads(r["payload"])["seconds"] for r in store._conn.execute(
        "SELECT seq, payload FROM events WHERE type='full_gate'") if r["seq"] not in excl]
    return statistics.median(xs) if xs else None


# Lookup levels, finest first. 1-4 use the task's own kind and at least one more dimension; 5 is the kind
# alone, 6 every kind, 7 the plan's estimate. A split hint resting on level >= COARSE_LEVEL has no direction.
COARSE_LEVEL = 5


def _pick(rows: list[dict], levels: list[tuple[str, dict]], field: str) -> Optional[tuple[float, str, int, int]]:
    """(median, name, level number, n) of `field` for the first level with at least MIN_SAMPLES rows."""
    for i, (name, flt) in enumerate(levels, 1):
        xs = [r[field] for r in rows if all(r[k] == v for k, v in flt.items())]
        if len(xs) >= MIN_SAMPLES:
            return statistics.median(xs), name, i, len(xs)
    return None


def _estimate(kind: str, family: Optional[str], fix: bool, serial: bool, rows: list[dict],
              est_minutes: float) -> tuple[float, float, str, int, int]:
    rnd = "fix round" if fix else "first round"
    ser = {"serial": True} if serial else {}
    sname = " as serial point" if serial else ""
    levels = [(f"{kind}/{family}/{rnd}{sname}", {"kind": kind, "family": family, "fix": fix, **ser}),
              (f"{kind}/{family}{sname}", {"kind": kind, "family": family, **ser}),
              (f"{kind}/{rnd}{sname}", {"kind": kind, "fix": fix, **ser}),
              (f"{kind}{sname}", {"kind": kind, **ser}), (kind, {"kind": kind}), ("all kinds", {})]
    got = _pick(rows, levels, "seconds")
    secs, name, level, n = got if got else (est_minutes * 60, "plan estimate (no history)", len(levels) + 1, 0)
    cost_levels = [(nm, {k: v for k, v in f.items() if k != "serial"}) for nm, f in levels]
    c = _pick(rows, cost_levels, "cost")
    src = f"{name} (n={n})" if n else name
    return secs, (c[0] if c else 0.0), src, level, n


# ---------------------------------------------------------------------------- predict
def predict(graph: TaskGraph, store: Store, ids: Optional[set[str]] = None, max_parallel: int = 4,
            exclude_usage: Iterable[int] = (), exclude_gate_seq: Iterable[int] = (),
            gates: bool = True, review_tiering: bool = True, low_model: str = "sonnet",
            fix_rounds: Optional[set[str]] = None, history_dbs: Iterable[str] = (),
            gate_baseline_s: Optional[float] = None) -> dict:
    """Prediction for running `ids` (default: every task not DONE/CANCELLED). A STALE task with gates that
    is not a review is expected to revalidate (no worker, no time). A task is a fix round if it completed
    before (`fix_rounds`, default: it has a handoff). With review tiering on, a fix-round review is
    predicted on the low-tier model (fix rounds are small changes), a first-round review on its planned one."""
    if ids is None:
        ids = {t.id for t in graph.tasks.values() if t.state not in (State.DONE.value, State.CANCELLED.value)}
    workers = {t for t in ids if not (graph.tasks[t].state == State.STALE.value and graph.tasks[t].done_when
                                      and graph.tasks[t].kind != Kind.REVIEW.value)}
    rows = history(store, exclude_usage, history_dbs)
    serial = static_serial_points(graph, workers)
    if fix_rounds is None:
        fix_rounds = {t for t in workers if store.latest_handoff(t) is not None}
    per: dict[str, dict] = {}
    for t in workers:
        task = graph.tasks[t]
        fix = t in fix_rounds
        family = model_family(task.model)
        if task.kind == Kind.REVIEW.value and review_tiering and fix:
            family = model_family(low_model)
        secs, cost, src, level, n = _estimate(task.kind, family, fix, t in serial, rows, task.est_minutes)
        per[t] = {"seconds": round(secs, 1), "cost": round(cost, 4), "serial": t in serial, "fix_round": fix,
                  "model": family, "source": src, "level": level, "n": n}
    dur = {t: per[t]["seconds"] for t in workers}
    phase_times = []
    for _, group in sorted(_phases(graph, workers, serial).items()):
        chain = _longest_chain(graph, group, dur)
        work = sum(dur[t] for t in group)
        phase_times.append({"tasks": sorted(group), "seconds": round(max(chain, work / max(1, max_parallel)), 1)})
    serial_s = sum(dur[t] for t in serial)
    gate_s = 0.0
    if gates:
        # this repository's own gate time: its recorded full gates, else the `macc init --gate` measurement
        fg = full_gate_seconds(store, exclude_gate_seq) or gate_baseline_s or 0.0
        n_full = sum(1 for t in workers if graph.tasks[t].kind == Kind.INTEGRATE.value) + 1   # + final gate
        gate_s = fg * n_full
    wall = serial_s + sum(p["seconds"] for p in phase_times) + gate_s
    return {"wall_s": round(wall, 1), "cost_usd": round(sum(per[t]["cost"] for t in workers), 3),
            "serial_share": round(serial_s / wall, 3) if wall else 0.0, "serial_tasks": sorted(serial),
            "phases": phase_times, "gate_s": round(gate_s, 1), "per_task": per}


def error(pred: dict, actual_wall: float, actual_cost: float, actual_serial_share: float) -> dict:
    """Relative error for wall and cost ((pred - actual) / actual), absolute error for the serial share."""
    rel = lambda p, a: round((p - a) / a, 3) if a else None   # noqa: E731
    return {"wall": rel(pred["wall_s"], actual_wall), "cost": rel(pred["cost_usd"], actual_cost),
            "serial_share": round(pred["serial_share"] - actual_serial_share, 3)}


def split_hints(pred: dict, floor_s: float) -> list[str]:
    """For each serial point: the k (2-4) that saves most wall time if it were split into k parallel parts,
    when t·(1 − 1/k) > k·floor (floor = the cheapest worker run's fixed time). Every hint names the history
    level and sample count its time rests on; at a coarse level (the kind alone or less) it says so and
    gives no direction."""
    hints = []
    for t in pred["serial_tasks"]:
        p = pred["per_task"][t]
        d = p["seconds"]
        basis = f"[level {p['level']}: {p['source']}]"
        if p["level"] >= COARSE_LEVEL:
            hints.append(f"{t}: serial point, insufficient data for a split direction {basis}")
            continue
        best = max(((k, d * (1 - 1 / k) - k * floor_s) for k in (2, 3, 4)), key=lambda x: x[1])
        if best[1] > 0:
            hints.append(f"{t}: serial point, predicted {d:.0f} s ({d / pred['wall_s']:.0%} of wall); splitting it "
                         f"into {best[0]} parallel parts would save ~{best[1]:.0f} s {basis}")
    return hints


def describe(pred: dict) -> str:
    return (f"prediction: wall {pred['wall_s']:.0f} s, cost ${pred['cost_usd']:.2f}, serial share "
            f"{pred['serial_share']:.0%} (serial points: {', '.join(pred['serial_tasks']) or 'none'})")


# --------------------------------------------------------------------------- backtest
def bottleneck_runs(store: Store) -> set[int]:
    """Runs excluded from the backtest, a fixed rule: a `serial_bottleneck` event, or (runs recorded before
    that event existed) a serial-point worker that ran > 3x its estimate, the same test the scheduler applies."""
    conn = store._conn
    out = {r["run_seq"] for r in conn.execute(
        "SELECT u.run_seq FROM usage u WHERE u.serial = 1 AND u.est_minutes > 0 "
        "AND u.seconds > 3 * u.est_minutes * 60 AND u.run_seq IS NOT NULL")}
    starts = [r["seq"] for r in conn.execute("SELECT seq FROM events WHERE type='run_started' ORDER BY seq")]
    for r in conn.execute("SELECT seq FROM events WHERE type='serial_bottleneck'"):
        prior = [x for x in starts if x < r["seq"]]
        if prior:
            out.add(prior[-1])
    return out


def backtest(store: Store, history_dbs: Iterable[str] = ()) -> dict:
    """Predict every finished run of this repository leave-one-out (its own usage rows and gate events are
    left out of the history) and compare with what happened. Runs with a serial bottleneck are excluded."""
    reconstruct_serial_flags(store)
    backfill_fix_rounds(store)
    conn = store._conn
    graph = store.load_graph()
    excluded = bottleneck_runs(store)
    evs = [dict(e) for e in conn.execute("SELECT seq, type, task_id, payload FROM events ORDER BY seq")]
    starts = [e for e in evs if e["type"] == "run_started"]
    rows, skipped = [], []
    for i, st in enumerate(starts):
        end = next((e for e in evs if e["seq"] > st["seq"] and e["type"] == "run_finished"
                    and (i + 1 == len(starts) or e["seq"] < starts[i + 1]["seq"])), None)
        if end is None:
            continue
        run_evs = [e for e in evs if st["seq"] <= e["seq"] <= end["seq"]]
        usage = conn.execute("SELECT id, seconds, cost_usd, serial FROM usage WHERE run_seq=?", (st["seq"],)).fetchall()
        if not usage:
            continue
        if st["seq"] in excluded:
            skipped.append({"run_seq": st["seq"], "reason": "serial bottleneck"})
            continue
        dispatched = {e["task_id"] for e in run_evs if e["type"] == "dispatched"}
        no_worker = {e["task_id"] for e in run_evs if e["type"] in ("revalidated", "integrate_skipped")} - dispatched
        ids = dispatched | no_worker
        if not ids <= set(graph.tasks):
            skipped.append({"run_seq": st["seq"], "reason": "tasks no longer in the store"})
            continue
        saved = {t: graph.tasks[t].state for t in graph.tasks}
        for t in graph.tasks.values():
            t.state = State.DONE.value
        for t in dispatched:
            graph.tasks[t].state = State.PENDING.value
        for t in no_worker:
            graph.tasks[t].state = State.STALE.value
        completed = {e["task_id"] for e in evs if e["seq"] < st["seq"]
                     and e["type"] in ("done", "revalidated", "integrate_skipped")}
        pred = predict(graph, store, ids=ids, max_parallel=json.loads(st["payload"] or "{}").get("max_parallel", 4),
                       exclude_usage=[u["id"] for u in usage],
                       exclude_gate_seq=[e["seq"] for e in run_evs if e["type"] == "full_gate"],
                       review_tiering=any(e["type"] == "review_tier" for e in run_evs),
                       fix_rounds=ids & completed, history_dbs=history_dbs)
        for t, state in saved.items():
            graph.tasks[t].state = state
        wall = json.loads(end["payload"])["seconds"]
        cost = sum(u["cost_usd"] for u in usage)
        # a skipped integrate has no usage row, but its gates ran with everything else waiting
        skip_s = sum(json.loads(e["payload"] or "{}").get("seconds", 0.0) for e in run_evs
                     if e["type"] == "integrate_skipped" and json.loads(e["payload"] or "{}").get("serial"))
        ser = (sum(u["seconds"] for u in usage if u["serial"] == 1) + skip_s) / wall if wall else 0.0
        rows.append({"run_seq": st["seq"], "tasks": len(ids), "pred": {k: pred[k] for k in ("wall_s", "cost_usd",
                     "serial_share")}, "actual": {"wall_s": wall, "cost_usd": round(cost, 3), "serial_share": round(ser, 3)},
                     "error": error(pred, wall, cost, ser)})
    def mean(key):
        xs = [abs(r["error"][key]) for r in rows if r["error"][key] is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    within = [r for r in rows if r["error"]["wall"] is not None and abs(r["error"]["wall"]) <= 0.3
              and r["error"]["cost"] is not None and abs(r["error"]["cost"]) <= 0.3]
    return {"runs": rows, "excluded": skipped, "within_30pct": len(within),
            "mean_abs_error": {"wall": mean("wall"), "cost": mean("cost"), "serial_share": mean("serial_share")}}
