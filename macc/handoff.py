"""Handoffs and context packs.

A handoff is *generated* from facts (git diff, test results, debt registry).
The LLM writes exactly two fields: `open_questions` and `notes_for_downstream`.
A context pack is what a worker gets instead of a conversation: its spec, the
handoffs of its dependencies, relevant decisions, and the output protocol.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .graph import Task, TaskGraph
from .store import Store
from .worktree import Repo

# Markers the worker must emit at the end of its final message.
HANDOFF_BEGIN = "<<<MACC_HANDOFF"
HANDOFF_END = "MACC_HANDOFF>>>"

API_PATTERNS = re.compile(
    r"^[+-]\s*(?:export\s+)?(?:async\s+)?(?:def|class|fun|interface|type|function|public|@Composable)\b"
)
STUB_PATTERNS = re.compile(r"NotImplemented|TODO\(macc\)|MACC_STUB|raise NotImplementedError|throw UnsupportedOperationException", re.I)


def parse_worker_output(text: str) -> dict:
    """Extract the JSON block the worker emits between markers. Tolerant of missing/invalid blocks."""
    m = re.search(re.escape(HANDOFF_BEGIN) + r"(.*?)" + re.escape(HANDOFF_END), text, re.S)
    if not m:
        return {"open_questions": "", "notes_for_downstream": "", "stubs": [], "_parsed": False}
    try:
        d = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return {"open_questions": m.group(1).strip()[:2000], "notes_for_downstream": "", "stubs": [], "_parsed": False}
    d.setdefault("open_questions", "")
    d.setdefault("notes_for_downstream", "")
    d.setdefault("stubs", [])
    d["_parsed"] = True
    return d


def generate_handoff(repo: Repo, task: Task, wt_path: Path, gate_results: list[dict],
                     worker_fields: dict, base: Optional[str] = None) -> dict:
    files = repo.changed_files(wt_path, base)
    diff = repo.diff_text(wt_path, base)
    api_changes = sorted({l.strip() for l in diff.splitlines() if API_PATTERNS.match(l)})[:60]
    stub_hits = []
    cur_file = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur_file = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and STUB_PATTERNS.search(line):
            stub_hits.append({"path": cur_file, "line": line[1:].strip()[:160]})
    return {
        "task_id": task.id,
        "attempt": task.attempts,
        "files_changed": [{"status": s, "path": p} for s, p in files],
        "api_changes": api_changes,
        "gates": [{"cmd": g["cmd"], "ok": g["ok"]} for g in gate_results],
        "gates_passed": all(g["ok"] for g in gate_results) if gate_results else None,
        "stubs_detected": stub_hits,
        "stubs_declared": worker_fields.get("stubs", []),
        "open_questions": worker_fields.get("open_questions", ""),
        "notes_for_downstream": worker_fields.get("notes_for_downstream", ""),
    }


def _summ_handoff(h: dict) -> str:
    files = ", ".join(f["path"] for f in h.get("files_changed", [])[:15])
    lines = [f"- files: {files or '(none)'}"]
    if h.get("api_changes"):
        lines.append("- api changes:\n    " + "\n    ".join(h["api_changes"][:20]))
    if h.get("stubs_declared") or h.get("stubs_detected"):
        stubs = h.get("stubs_declared") or [s["line"] for s in h.get("stubs_detected", [])]
        lines.append("- STUBS (not implemented yet): " + "; ".join(map(str, stubs))[:600])
    if h.get("notes_for_downstream"):
        lines.append(f"- notes: {h['notes_for_downstream'][:800]}")
    if h.get("open_questions"):
        lines.append(f"- open questions: {h['open_questions'][:400]}")
    return "\n".join(lines)


def merge_conflict_spec(task: Task, conflicts: list[str], others: list[dict]) -> str:
    lines = [f"Resolve merge conflicts between task `{task.id}` ({task.title}) and the integration branch.",
             "", "Conflicting files:"] + [f"- {c}" for c in conflicts] + ["", f"This task's intent:\n{task.spec.strip()}", ""]
    if others:
        lines.append("Other tasks that changed these files (their handoffs):")
        for h in others:
            lines.append(f"## {h['_task_id']}\n{_summ_handoff(h)}")
    return "\n".join(lines)


def build_context_pack(task: Task, graph: TaskGraph, store: Store, project_rules: str = "") -> str:
    """The *only* thing a worker reads besides the repo itself. Short, stable prefix first (cacheable),
    task-specific material last."""
    parts: list[str] = []
    if project_rules.strip():
        parts.append("# Project rules\n" + project_rules.strip())

    parts.append(
        "# How you work (macc worker protocol)\n"
        "- You are one worker in a parallel build. Other workers edit other paths concurrently.\n"
        f"- You may ONLY modify paths matching: {task.owns or ['(any, no ownership declared)']}. "
        "If you need a change elsewhere, do not make it; describe it in open_questions.\n"
        "- Do not re-verify things already proven by passing tests. Run the done_when commands once at the end.\n"
        "- Fix root causes. If you choose a minimal fix or leave a stub, you MUST list it under `stubs` "
        "so it enters the debt registry. Mark stub code with the comment `TODO(macc)`.\n"
        "- Grep before you assume. Do not rely on memory of files you have not opened in this session.\n"
        "- Commit nothing yourself; the scheduler commits your worktree.\n"
        "- End your final message with exactly one block:\n"
        f"{HANDOFF_BEGIN}\n"
        '{"open_questions": "<things a human or planner must decide; empty if none>",\n'
        ' "notes_for_downstream": "<what the next task must know: paths, names, gotchas>",\n'
        ' "stubs": ["<path>: <what is stubbed and why>"]}\n'
        f"{HANDOFF_END}"
    )

    decisions = store.decisions(limit=20)
    relevant = [d for d in decisions if not d["affects"] or task.id in d["affects"]
                or any(a in graph.ancestors(task.id) for a in d["affects"])]
    if relevant:
        parts.append("# Decisions in force (do not undo these)\n" + "\n".join(
            f"- {d['title']}: {d['body'][:400]}" for d in relevant[-10:]))

    dep_sections = []
    for d in task.deps:
        h = store.latest_handoff(d)
        dt = graph.tasks[d]
        if h:
            dep_sections.append(f"## {d}: {dt.title}\n{_summ_handoff(h)}")
        else:
            dep_sections.append(f"## {d}: {dt.title}\n- (done, no handoff recorded)")
    if dep_sections:
        parts.append("# Upstream handoffs\n" + "\n\n".join(dep_sections))

    debts = store.open_debts()
    mine = [x for x in debts if x["task_id"] in task.deps or x["task_id"] == task.id]
    if mine:
        parts.append("# Open debts touching your inputs\n" + "\n".join(
            f"- [{x['kind']}] {x['path'] or ''} {x['note']}" for x in mine[:15]))

    parts.append(
        f"# Your task: {task.id} — {task.title}\n"
        f"kind: {task.kind}\n"
        f"owns: {task.owns}\n"
        f"done_when (each must exit 0): {task.done_when or ['(none declared)']}\n"
        f"allow_stubs: {task.allow_stubs}\n\n"
        f"{task.spec.strip()}"
    )
    if task.kind == "spike":
        parts.append("# Spike output\nDo not write production code. Write your conclusion to "
                     "`docs/spikes/" + task.id + ".md` and summarise it in notes_for_downstream.")
    if task.kind == "contract":
        parts.append("# Contract output\nProduce interfaces/schemas/test plans only. Downstream tasks will "
                     "be written against exactly what you commit. Be explicit about types and error cases.")
    if task.kind == "merge":
        parts.append("# Merge output\nThe files listed under `owns` contain git conflict markers "
                     "(<<<<<<< HEAD is this task's version, >>>>>>> is the integration branch). Resolve every "
                     "marker so that BOTH intents survive. Touch no other file. Run done_when once at the end.")
    if task.kind == "review":
        parts.append("# Review output\nDo not edit code. Read the diff and tests. Report: root-cause fixes vs "
                     "symptom fixes, missing tests, ownership violations, hidden stubs. Verdict in notes_for_downstream: "
                     "APPROVE or REJECT with reasons.")
    return "\n\n".join(parts) + "\n"
