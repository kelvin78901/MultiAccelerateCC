"""Handoffs and context packs.

A handoff is *generated* from facts (git diff, test results, debt registry).
The LLM writes exactly two fields: `open_questions` and `notes_for_downstream`.
A context pack is what a worker gets instead of a conversation: its spec, the
handoffs of its dependencies, relevant decisions, and the output protocol.
"""
from __future__ import annotations

import json
import re
import time
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


NON_CODE_EXT = {".md", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml", ".csv", ".html"}


def _is_code_file(path: Optional[str]) -> bool:
    """Docs and data files can legitimately *mention* stub markers (e.g. a review saying 'no TODO(macc)')."""
    if not path:
        return False
    return Path(path).suffix.lower() not in NON_CODE_EXT


VENV_PY = re.compile(r"(?<![\w./~$-])(?:\./)?((?:[~/]|\$HOME|\$\{HOME\})[^\s`'\"]*?)?\.venv/bin/python\d*(?:\.\d+)?")


def interpreter_conflicts(rules: str, claude_md: str) -> list[str]:
    """rules.md and the repo's CLAUDE.md both name `.venv/bin/python`, one absolute and one relative.
    Worktrees have no .venv (it is git-ignored), so the relative one fails there; workers read both."""
    def kinds(text: str) -> set[str]:
        return {"absolute" if m.group(1) else "relative" for m in VENV_PY.finditer(text or "")}
    r, c = kinds(rules), kinds(claude_md)
    if not r or not c or r == c:
        return []
    return [f"rules.md names .venv/bin/python as {'/'.join(sorted(r))}, CLAUDE.md as {'/'.join(sorted(c))}; "
            "worktrees have no .venv, so use one absolute interpreter path in both"]


def parse_verdict(notes: str) -> Optional[str]:
    """APPROVE / REJECT only when stated as a verdict: at the start of a line, optionally after 'VERDICT:'.
    A sentence like 'no reason to reject' does not count."""
    m = re.search(r"(?im)^\s*(?:\*\*)?(?:verdict\s*[:：]\s*)?(?:\*\*)?\s*(APPROVE|REJECT|REQUEST(?:ED)?\s+CHANGES)\b",
                  notes or "")
    if not m:
        return None
    v = m.group(1).upper()
    return "REJECT" if v.startswith("REQUEST") else v


def parse_reopen(notes: str) -> list[str]:
    """Optional `REOPEN: task_a, task_b` line in a REJECT verdict names the tasks to redo."""
    m = re.search(r"(?im)^[ \t]*(?:\*\*)?reopen[ \t]*[:：][ \t]*(?:\*\*)?[ \t]*([A-Za-z0-9_., \t-]+?)[ \t]*$", notes or "")
    if not m:
        return []
    return [x.strip() for x in re.split(r"[,\s]+", m.group(1)) if x.strip()]


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
        elif line.startswith("+") and STUB_PATTERNS.search(line) and _is_code_file(cur_file):
            stub_hits.append({"path": cur_file, "line": line[1:].strip()[:160]})
    return {
        "task_id": task.id,
        "attempt": task.attempts,
        "files_changed": [{"status": s, "path": p} for s, p in files],
        "api_changes": api_changes,
        # seconds (and whether the record was shared) so the operator can see what a gate really cost
        "gates": [{"cmd": g["cmd"], "ok": g["ok"], "seconds": g.get("seconds"), "shared": bool(g.get("shared"))}
                  for g in gate_results],
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


_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")


def classify_path(path: str) -> str:
    """'test', 'doc' or 'source' (review risk counts source lines only)."""
    parts = path.split("/")
    name = parts[-1]
    if any(seg in ("tests", "test", "__tests__", "spec") for seg in parts[:-1]) or name.startswith("test_") \
            or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js", "Test.kt", "Test.java")) \
            or "/fixtures/" in f"/{path}":
        return "test"
    if name.endswith(_DOC_SUFFIXES) or parts[0] in ("docs", "doc"):
        return "doc"
    return "source"


_PUBLIC = re.compile(r"^([+-])\s*(?:async\s+)?(?:def|class|function|fun|interface)\s+([A-Za-z]\w*)"
                     r"|^([+-])\s*@(?:app|router|\w+_router)\.(?:get|post|put|delete|patch)\(\s*[\"']([^\"']+)")


def public_api_delta(diff: str) -> tuple[set[str], set[str]]:
    """Public symbols (defs/classes without a leading underscore, HTTP routes) added and removed by a
    unified diff, source files only. Moving a symbol between files counts as neither."""
    cur, added, removed = None, set(), set()
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            continue
        if line.startswith("--- ") or not cur or classify_path(cur) != "source":
            continue
        m = _PUBLIC.match(line)
        if m:
            sign, sym = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), "route " + m.group(4))
            (added if sign == "+" else removed).add(sym)
    return added - removed, removed - added


def review_risk(files: list[dict], open_debts: int, gate_green: Optional[bool],
                max_source_lines: int = 400, new_public_api: int = 0,
                max_new_public_api: int = 5) -> tuple[str, list[str], dict]:
    """Decide how much reviewer a change needs, from facts only.
    Returns (tier 'low'|'high', reasons for 'high' (empty for low), stats)."""
    stats = {"files": len(files), "source_files": 0, "source_lines": 0, "test_lines": 0, "doc_lines": 0,
             "modified_tests": [], "new_public_api": new_public_api}
    for f in files:
        kind = classify_path(f["path"])
        lines = (f["added"] or 0) + (f["deleted"] or 0)
        if kind == "source":
            stats["source_files"] += 1
            stats["source_lines"] += lines
        elif kind == "test":
            stats["test_lines"] += lines
            if f["status"] in ("M", "D", "R") and (f["deleted"] or 0) > 0:
                stats["modified_tests"].append(f["path"])
        else:
            stats["doc_lines"] += lines
    reasons = []
    if new_public_api > max_new_public_api:
        reasons.append(f"{new_public_api} new public symbols/routes (> {max_new_public_api}): new interface")
    if stats["source_lines"] > max_source_lines:
        reasons.append(f"{stats['source_lines']} source lines changed (> {max_source_lines})")
    if stats["modified_tests"]:
        reasons.append(f"existing tests changed: {stats['modified_tests'][:5]}")
    if open_debts:
        reasons.append(f"{open_debts} open debt(s)")
    if gate_green is not True:
        reasons.append("full gate not green on this tree" if gate_green is False
                       else "no full-gate result for this tree")
    return ("high" if reasons else "low"), reasons, stats


def review_facts(stats: dict, tier: str, reasons: list[str], contracts: list[str], debts: list[dict],
                 base: Optional[str]) -> str:
    """What the scheduler already knows, so the reviewer reads the change instead of rediscovering it."""
    lines = ["# Facts for the review (computed by the scheduler; do not re-derive them)",
             f"- change since {base[:12] if base else 'the base'}: {stats['files']} files; source "
             f"{stats['source_files']} files / {stats['source_lines']} lines, tests {stats['test_lines']} lines, "
             f"docs {stats['doc_lines']} lines; {stats.get('new_public_api', 0)} new public symbols/routes. "
             f"`git diff {base[:12] if base else '$MACC_BASE'}` shows it.",
             f"- review tier: {tier}" + (f" ({'; '.join(reasons)})" if reasons else
                                         " (small source change, no new interface, existing tests untouched, "
                                         "gates green)")]
    if stats["modified_tests"]:
        lines.append(f"- existing tests that were changed (check they were not weakened): {stats['modified_tests']}")
    lines.append("- contracts to check the code against: " + (", ".join(contracts) if contracts else "(none)"))
    if debts:
        lines.append("- open debts: " + "; ".join(f"[{d['kind']}] {d['task_id']}: {d['note'][:120]}" for d in debts[:10]))
    else:
        lines.append("- open debts: none")
    lines.append("- Do not re-run the full test suite unless the gate section below says it is missing or failed; "
                 "spend the time on reading the diff, the contract and targeted probes.")
    return "\n".join(lines)


def _full_gate_note(ev: dict, integration_head: Optional[str], integration_tree: Optional[str] = None) -> str:
    """Tell a reviewer what the scheduler already verified, so it does not re-run the whole suite.
    The claim holds only if the gate ran on the tree being reviewed (same tree hash; same HEAD for events
    recorded before trees were)."""
    p = ev["payload"]
    if integration_tree and p.get("tree"):
        moved = p["tree"] != integration_tree
    else:
        moved = bool(integration_head and p.get("head") and p["head"] != integration_head)
    if moved:
        return ("# Latest full integration gate\n"
                f"- it ran on an older tree ({p['head'][:12]}, result {'PASSED' if p.get('ok') else 'FAILED'}); "
                f"the integration branch has moved to {integration_head[:12]} since. Run the full suite yourself.")
    when = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
    lines = ["# Latest full integration gate",
             f"- commands: {p.get('cmds')}",
             f"- result: {'PASSED' if p.get('ok') else 'FAILED at ' + str(p.get('failed_cmd'))}",
             f"- duration: {p.get('seconds')} s (at {when}, after merging {ev['task_id'] or 'the last task of the run'})"]
    if p.get("ok"):
        lines.append("The scheduler already ran the full suite on this tree. Do not re-run it in full; run only "
                     "the extra, targeted checks your review adds.")
    else:
        lines.append("Treat this failure as a blocker unless you show it is unrelated to the reviewed work.")
    return "\n".join(lines)


HEAVY_GATE_S = 60.0   # a measured gate this slow is run by the scheduler only (REA verify:ghidra: 240 s, run twice)


def scheduler_checks(task: Task, store: Store, gate_cmds: list[str]) -> str:
    """Which commands the scheduler runs itself, and which of them the worker must not run (measured heavy)."""
    secs = store.command_seconds()
    mine = list(task.done_when) + [c for c in gate_cmds if c not in task.done_when]
    heavy = [(c, secs[c]) for c in mine if secs.get(c, 0.0) >= HEAVY_GATE_S]
    if not gate_cmds and not heavy:
        return ""
    lines = ["# Checks the scheduler runs for you",
             "- When you finish, the scheduler runs your done_when in your worktree"
             + (f"; after the merge it runs the integration gates {list(gate_cmds)}" if gate_cmds else "")
             + ". A failure comes back to you with its output on the next attempt."]
    if heavy:
        lines.append("- Do NOT run these yourself; they are slow and the scheduler runs them anyway (measured): "
                     + ", ".join(f"`{c}` (~{s:.0f} s)" for c, s in heavy)
                     + ". Run quick targeted checks (the tests and files you changed) instead.")
    return "\n".join(lines)


def build_context_pack(task: Task, graph: TaskGraph, store: Store, project_rules: str = "",
                       integration_head: Optional[str] = None, integration_tree: Optional[str] = None,
                       gate_cmds: list[str] = (), workdir: Optional[Path] = None) -> str:
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
        "- Do not re-verify things already proven by passing tests. Run the done_when commands once at the end, "
        "except the ones listed as heavy under 'Checks the scheduler runs for you'.\n"
        "- Fix root causes. If you choose a minimal fix or leave a stub, you MUST list it under `stubs` "
        "so it enters the debt registry. Mark stub code with the comment `TODO(macc)`.\n"
        "- Grep before you assume. Do not rely on memory of files you have not opened in this session.\n"
        "- Commit nothing yourself; the scheduler commits your worktree.\n"
        "- `open_questions` is ONLY for a decision a human or the planner must make that you could not make "
        "yourself (it makes the integrate step run). Anything you resolved, any deviation from the task text "
        "you already handled, and anything the next task should know is information: put it in "
        "`notes_for_downstream`, never in open_questions. Do not repeat what an upstream handoff already says.\n"
        "- End your final message with exactly one block:\n"
        f"{HANDOFF_BEGIN}\n"
        '{"open_questions": "<an undecided choice for a human/planner; empty if none (usually empty)>",\n'
        ' "notes_for_downstream": "<what the next task must know: paths, names, gotchas, deviations you resolved>",\n'
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

    if workdir is not None and task.owns:
        from .exports import nearby_exports
        near = nearby_exports(task.owns, Path(workdir))
        if near:
            parts.append("# Existing exports next to your files (reuse these before writing new helpers)\n"
                         + "\n".join(f"- {line}" for line in near))
    parts.append(
        f"# Your task: {task.id} — {task.title}\n"
        f"kind: {task.kind}\n"
        f"owns: {task.owns}\n"
        f"done_when (each must exit 0): {task.done_when or ['(none declared)']}\n"
        f"allow_stubs: {task.allow_stubs}\n\n"
        f"{task.spec.strip()}"
    )
    checks = scheduler_checks(task, store, list(gate_cmds))
    if checks:
        parts.append(checks)
    if task.kind == "spike":
        parts.append("# Spike output\nDo not write production code. You may search and read the web (the only "
                     "kind of task that can). Write `docs/spikes/" + task.id + ".md` for the tasks after you, who "
                     "cannot go online: the conclusion, the exact facts they need (field layouts, constants, "
                     "error cases, version differences, copied verbatim where it matters), and the source URL "
                     "and section for each fact. Summarise it in notes_for_downstream.")
    if task.kind == "contract":
        downstream = [graph.tasks[d] for d in sorted(graph.descendants(task.id))]
        owned = "\n".join(f"- {d.id}: {d.owns}" for d in downstream if d.owns)
        parts.append("# Contract output\nProduce interfaces/schemas/test plans only. Downstream tasks will "
                     "be written against exactly what you commit. Be explicit about types and error cases.\n"
                     "The plan already fixes which files each downstream task owns; use EXACTLY these paths "
                     "and module names in the contract (do not invent other file names):\n" + owned)
    if task.kind == "merge":
        parts.append("# Merge output\nThe files listed under `owns` contain git conflict markers "
                     "(<<<<<<< HEAD is this task's version, >>>>>>> is the integration branch). Resolve every "
                     "marker so that BOTH intents survive. Touch no other file. Run done_when once at the end.")
    if task.kind == "review":
        parts.append("# Review output\nDo not edit code. Read the diff and tests. Report: root-cause fixes vs "
                     "symptom fixes, missing tests, ownership violations, hidden stubs. notes_for_downstream MUST start "
                     "with a line `VERDICT: APPROVE` or `VERDICT: REJECT`, followed by reasons. On REJECT add a line "
                     "`REOPEN: <task ids>` naming the tasks whose work must be redone (any ancestor task of this "
                     "review; see the upstream handoffs for ids). Without it, the direct dependencies are reopened.")
        fg = store.latest_event("full_gate")
        if fg:
            parts.append(_full_gate_note(fg, integration_head, integration_tree))
    return "\n\n".join(parts) + "\n"
