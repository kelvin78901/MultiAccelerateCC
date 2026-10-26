"""SQLite state store. Everything the scheduler needs to resume after a crash lives here.

Tables
------
tasks      one row per task (full Task dict as JSON + indexed columns)
events     append-only log: dispatched / finished / failed / stale / decision / ...
handoffs   one per (task_id, attempt): generated facts + the two LLM-written fields
debts      stubs and "minimal fixes" that must be paid before the project is "done"
decisions  ADR-style records of *why* something changed (read by every later worker)
usage      token / cost / duration per worker run (feeds the cost model)
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .graph import Task, TaskGraph

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT,
    type TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS handoffs (
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, attempt)
);
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,         -- stub | minimal_fix | todo
    path TEXT,
    note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    affects TEXT                -- JSON list of task ids
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    worker TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    seconds REAL DEFAULT 0,
    ok INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------------- tasks
    def save_task(self, t: Task) -> None:
        self._conn.execute(
            "INSERT INTO tasks(id,state,kind,data,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, kind=excluded.kind, "
            "data=excluded.data, updated_at=excluded.updated_at",
            (t.id, t.state, t.kind, json.dumps(t.to_dict()), time.time()),
        )

    def save_graph(self, g: TaskGraph) -> None:
        with self.tx():
            for t in g.tasks.values():
                self.save_task(t)

    def load_graph(self) -> TaskGraph:
        rows = self._conn.execute("SELECT data FROM tasks").fetchall()
        g = TaskGraph(Task.from_dict(json.loads(r["data"])) for r in rows)
        if g.tasks:
            g.validate()
        return g

    def delete_task(self, tid: str) -> None:
        self._conn.execute("DELETE FROM tasks WHERE id=?", (tid,))

    # ---------------------------------------------------------------- events
    def event(self, type_: str, task_id: Optional[str] = None, **payload) -> int:
        cur = self._conn.execute(
            "INSERT INTO events(ts,task_id,type,payload) VALUES(?,?,?,?)",
            (time.time(), task_id, type_, json.dumps(payload) if payload else None),
        )
        return int(cur.lastrowid)

    def events(self, task_id: Optional[str] = None, since_seq: int = 0, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM events WHERE seq>?"
        args: list = [since_seq]
        if task_id:
            q += " AND task_id=?"
            args.append(task_id)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args).fetchall()][::-1]

    # -------------------------------------------------------------- handoffs
    def save_handoff(self, task_id: str, attempt: int, input_hash: str, output_hash: Optional[str], data: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO handoffs(task_id,attempt,input_hash,output_hash,data,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, attempt, input_hash, output_hash, json.dumps(data), time.time()),
        )

    def latest_handoff(self, task_id: str) -> Optional[dict]:
        r = self._conn.execute(
            "SELECT * FROM handoffs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)
        ).fetchone()
        if not r:
            return None
        d = json.loads(r["data"])
        d["_attempt"] = r["attempt"]
        d["_input_hash"] = r["input_hash"]
        d["_output_hash"] = r["output_hash"]
        return d

    def handoffs_touching(self, paths: list[str], exclude_task: Optional[str] = None) -> list[dict]:
        """Latest handoff of every task whose files_changed intersects `paths`."""
        out = []
        rows = self._conn.execute(
            "SELECT h.* FROM handoffs h JOIN (SELECT task_id, MAX(attempt) a FROM handoffs GROUP BY task_id) m "
            "ON h.task_id=m.task_id AND h.attempt=m.a"
        ).fetchall()
        wanted = set(paths)
        for r in rows:
            if r["task_id"] == exclude_task:
                continue
            d = json.loads(r["data"])
            if wanted & {f["path"] for f in d.get("files_changed", [])}:
                d["_task_id"] = r["task_id"]
                out.append(d)
        return out

    # ----------------------------------------------------------------- debts
    def add_debt(self, task_id: str, kind: str, note: str, path: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO debts(task_id,kind,path,note,status,created_at) VALUES(?,?,?,?,'open',?)",
            (task_id, kind, path, note, time.time()),
        )
        return int(cur.lastrowid)

    def resolve_debt(self, debt_id: int) -> None:
        self._conn.execute("UPDATE debts SET status='resolved', resolved_at=? WHERE id=?", (time.time(), debt_id))

    def open_debts(self, task_id: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM debts WHERE status='open'"
        args: list = []
        if task_id:
            q += " AND task_id=?"
            args.append(task_id)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args).fetchall()]

    # ------------------------------------------------------------- decisions
    def add_decision(self, title: str, body: str, affects: list[str] | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO decisions(ts,title,body,affects) VALUES(?,?,?,?)",
            (time.time(), title, body, json.dumps(affects or [])),
        )
        return int(cur.lastrowid)

    def decisions(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows[::-1]:
            d = dict(r)
            d["affects"] = json.loads(d["affects"] or "[]")
            out.append(d)
        return out

    # ----------------------------------------------------------------- usage
    def add_usage(self, task_id: str, attempt: int, worker: str, ok: bool, *, model: Optional[str] = None,
                  input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                  cost_usd: float = 0.0, seconds: float = 0.0) -> None:
        self._conn.execute(
            "INSERT INTO usage(task_id,attempt,worker,model,input_tokens,output_tokens,cache_read_tokens,"
            "cost_usd,seconds,ok) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, attempt, worker, model, input_tokens, output_tokens, cache_read_tokens,
             cost_usd, seconds, int(ok)),
        )

    def usage_totals(self) -> dict:
        r = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COALESCE(SUM(cache_read_tokens),0) c, COALESCE(SUM(cost_usd),0) usd, "
            "COALESCE(SUM(seconds),0) s, COUNT(*) runs FROM usage"
        ).fetchone()
        return {"input_tokens": r["i"], "output_tokens": r["o"], "cache_read_tokens": r["c"],
                "cost_usd": r["usd"], "seconds": r["s"], "runs": r["runs"]}

    def usage_by_kind(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT t.kind, COUNT(*) runs, AVG(u.input_tokens+u.output_tokens) avg_tokens, "
            "AVG(u.seconds) avg_seconds, AVG(u.ok) success_rate FROM usage u JOIN tasks t ON t.id=u.task_id "
            "GROUP BY t.kind"
        ).fetchall()
        return [dict(r) for r in rows]
