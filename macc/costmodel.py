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
    run: list = []
    for ev in events + [{"seq": None, "type": "run_started", "task_id": None, "payload": None}]:
        if ev["type"] == "run_started" and run:
            touched = {e["task_id"] for e in run if e["type"] in ("dispatched", "revalidated", "stale_rerun",
                                                                  "integrate_skipped") and e["task_id"] in graph.tasks}
            finished: set[str] = set()
            for e in run:
                tid = e["task_id"]
                if tid not in graph.tasks:
                    continue
                if e["type"] == "dispatched":
                    attempt = json.loads(e["payload"] or "{}").get("attempt", 1)
                    later = graph.descendants(tid)
                    flags.setdefault((tid, attempt), []).append(int(touched - finished - {tid} <= later))
                elif e["type"] in ("done", "revalidated", "integrate_skipped"):
                    finished.add(tid)
            run = []
        if ev["type"] != "run_started" or ev["seq"] is not None:
            run.append(ev)
    updated = 0
    seen: dict[tuple[str, int], int] = {}
    rows = conn.execute("SELECT id, task_id, attempt FROM usage WHERE worker NOT LIKE '%:merge' "
                        "AND task_id IN (SELECT id FROM tasks) ORDER BY id").fetchall()
    for r in rows:
        key = (r["task_id"], r["attempt"])
        i = seen.get(key, 0)
        seen[key] = i + 1
        vals = flags.get(key, [])
        if i < len(vals):
            cur = conn.execute("UPDATE usage SET serial=? WHERE id=? AND serial IS NULL", (vals[i], r["id"]))
            updated += cur.rowcount
    return updated


def history(store: Store, exclude_ids: Iterable[int] = ()) -> dict[str, dict[str, list[float]]]:
    """Per kind: seconds of every worker run, seconds of the runs that were serial points, costs."""
    excl = set(exclude_ids)
    out: dict[str, dict[str, list[float]]] = {}
    for r in store._conn.execute("SELECT id, kind, seconds, cost_usd, serial FROM usage WHERE model IS NOT NULL "
                                 "AND kind IS NOT NULL AND kind != 'plan' AND worker NOT LIKE '%:merge'"):
        if r["id"] in excl:
            continue
        h = out.setdefault(r["kind"], {"seconds": [], "serial_seconds": [], "cost": []})
        h["seconds"].append(r["seconds"])
        h["cost"].append(r["cost_usd"])
        if r["serial"] == 1:
            h["serial_seconds"].append(r["seconds"])
    return out


def full_gate_seconds(store: Store, exclude_seq: Iterable[int] = ()) -> Optional[float]:
    excl = set(exclude_seq)
    xs = [json.loads(r["payload"])["seconds"] for r in store._conn.execute(
        "SELECT seq, payload FROM events WHERE type='full_gate'") if r["seq"] not in excl]
    return statistics.median(xs) if xs else None


def _estimate(kind: str, serial: bool, hist: dict, est_minutes: float) -> tuple[float, float, str]:
    h = hist.get(kind, {})
    everything = [x for v in hist.values() for x in v["seconds"]]
    all_cost = [x for v in hist.values() for x in v["cost"]]
    if serial and len(h.get("serial_seconds", [])) >= MIN_SAMPLES:
        secs, src = statistics.median(h["serial_seconds"]), f"{kind} as serial point (n={len(h['serial_seconds'])})"
    elif len(h.get("seconds", [])) >= MIN_SAMPLES:
        secs, src = statistics.median(h["seconds"]), f"{kind} (n={len(h['seconds'])})"
    elif everything:
        secs, src = statistics.median(everything), f"all kinds (n={len(everything)})"
    else:
        secs, src = est_minutes * 60, "plan estimate (no history)"
    cost = statistics.median(h["cost"]) if len(h.get("cost", [])) >= MIN_SAMPLES else \
        (statistics.median(all_cost) if all_cost else 0.0)
    return secs, cost, src


# ---------------------------------------------------------------------------- predict
def predict(graph: TaskGraph, store: Store, ids: Optional[set[str]] = None, max_parallel: int = 4,
            exclude_usage: Iterable[int] = (), exclude_gate_seq: Iterable[int] = (),
            gates: bool = True) -> dict:
    """Prediction for running `ids` (default: every task not DONE/CANCELLED). A STALE task with gates that
    is not a review is expected to revalidate (no worker, no time)."""
    if ids is None:
        ids = {t.id for t in graph.tasks.values() if t.state not in (State.DONE.value, State.CANCELLED.value)}
    workers = {t for t in ids if not (graph.tasks[t].state == State.STALE.value and graph.tasks[t].done_when
                                      and graph.tasks[t].kind != Kind.REVIEW.value)}
    hist = history(store, exclude_usage)
    serial = static_serial_points(graph, workers)
    per: dict[str, dict] = {}
    for t in workers:
        secs, cost, src = _estimate(graph.tasks[t].kind, t in serial, hist, graph.tasks[t].est_minutes)
        per[t] = {"seconds": round(secs, 1), "cost": round(cost, 4), "serial": t in serial, "source": src}
    dur = {t: per[t]["seconds"] for t in workers}
    phase_times = []
    for _, group in sorted(_phases(graph, workers, serial).items()):
        chain = _longest_chain(graph, group, dur)
        work = sum(dur[t] for t in group)
        phase_times.append({"tasks": sorted(group), "seconds": round(max(chain, work / max(1, max_parallel)), 1)})
    serial_s = sum(dur[t] for t in serial)
    gate_s = 0.0
    if gates:
        fg = full_gate_seconds(store, exclude_gate_seq) or 0.0
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
    when t·(1 − 1/k) > k·floor (floor = the cheapest worker run's fixed time)."""
    hints = []
    for t in pred["serial_tasks"]:
        d = pred["per_task"][t]["seconds"]
        best = max(((k, d * (1 - 1 / k) - k * floor_s) for k in (2, 3, 4)), key=lambda x: x[1])
        if best[1] > 0:
            hints.append(f"{t}: serial point, predicted {d:.0f} s ({d / pred['wall_s']:.0%} of wall); splitting it "
                         f"into {best[0]} parallel parts would save ~{best[1]:.0f} s")
    return hints


def describe(pred: dict) -> str:
    return (f"prediction: wall {pred['wall_s']:.0f} s, cost ${pred['cost_usd']:.2f}, serial share "
            f"{pred['serial_share']:.0%} (serial points: {', '.join(pred['serial_tasks']) or 'none'})")
