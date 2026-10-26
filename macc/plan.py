"""Plan files: YAML in, TaskGraph out. The plan is the *initial* graph; the store is the truth afterwards."""
from __future__ import annotations

from pathlib import Path

import yaml

from .graph import Task, TaskGraph


def load_plan(path: str | Path) -> tuple[TaskGraph, dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    defaults = data.get("defaults", {})
    tasks = []
    for item in data.get("tasks", []):
        merged = {**defaults, **item}
        tasks.append(Task(**merged))
    g = TaskGraph(tasks)
    g.validate()
    g.refresh_hashes()
    return g, data.get("config", {})


def dump_plan(g: TaskGraph, path: str | Path) -> None:
    keep = ["id", "title", "spec", "deps", "owns", "kind", "effort", "model", "est_tokens",
            "est_minutes", "done_when", "allow_stubs", "max_attempts"]
    tasks = [{k: v for k, v in t.to_dict().items() if k in keep and v not in (None, [], "", False)}
             for t in (g.tasks[i] for i in g.topo_order())]
    Path(path).write_text(yaml.safe_dump({"tasks": tasks}, sort_keys=False, allow_unicode=True))
