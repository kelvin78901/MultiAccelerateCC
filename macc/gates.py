"""Integration gate discovery: find the repository's own check/lint/format/typecheck/test targets and time them.

`macc init --discover-gates` reads package.json scripts, Makefile targets and pyproject.toml task tables, runs each
candidate once in the integration worktree and keeps the ones that pass in under half of gate_timeout. A person picks
from that list (`--pick`); nothing is added to integration_gates on its own. REA #6404 needed a fifth round only
because `format:check` (0.6 s) was never a gate.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

KEYWORDS = re.compile(r"check|lint|format|fmt|type|test|knip|tsc|mypy|ruff", re.I)
# watch modes never exit; fixers and writers change the tree; CI aggregates and coverage runs are the full suite
DEFAULT_EXCLUDE = re.compile(r"watch|fix|write|coverage|mutation|update|serve|\bdev\b|(^|:)ci(:|$)|with-build|verbose|"
                             r"prepare",
                             re.I)
_CATEGORY = (("format", re.compile(r"(^|[:_./ ])(format|fmt)([:_./ -]|$)", re.I)),   # not verify:cross-format
             ("lint", re.compile(r"lint|ruff|knip", re.I)),
             ("typecheck", re.compile(r"type|tsc|mypy", re.I)), ("test", re.compile(r"test", re.I)))
_ORDER = {"format": 0, "lint": 1, "typecheck": 2, "check": 3, "test": 4}
RESULTS_FILE = "gate_discovery.json"


def _category(name: str) -> str:
    for cat, rx in _CATEGORY:
        if rx.search(name):
            return cat
    return "check"


def _runner(root: Path) -> str:
    for lock, run in (("pnpm-lock.yaml", "pnpm run"), ("yarn.lock", "yarn run"), ("bun.lockb", "bun run"),
                      ("bun.lock", "bun run")):
        if (root / lock).exists():
            return run
    return "npm run"


def _package_scripts(root: Path) -> list[dict]:
    p = root / "package.json"
    if not p.is_file():
        return []
    try:
        scripts = json.loads(p.read_text()).get("scripts") or {}
    except (ValueError, AttributeError):
        return []
    run = _runner(root)
    out = []
    for name, body in scripts.items():
        if re.match(r"(pre|post)", name) and name[3 if name.startswith("pre") else 4:] in scripts:
            continue                              # lifecycle hook: runs with its main script
        out.append({"name": name, "cmd": f"{run} {name}", "source": "package.json", "body": str(body)})
    return out


def _make_targets(root: Path) -> list[dict]:
    p = next((root / n for n in ("GNUmakefile", "Makefile", "makefile") if (root / n).is_file()), None)
    if p is None:
        return []
    out, seen = [], set()
    for line in p.read_text(errors="replace").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_./ -]*?)\s*::?(?![=:])", line)
        if not m or line.startswith("\t"):
            continue
        for name in m.group(1).split():
            if "%" in name or "$" in name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "cmd": f"make {name}", "source": p.name, "body": ""})
    return out


def _pyproject(root: Path) -> list[dict]:
    p = root / "pyproject.toml"
    if not p.is_file():
        return []
    try:
        import tomllib
    except ImportError:                           # python 3.10: no stdlib toml reader
        return []
    try:
        tool = tomllib.loads(p.read_text()).get("tool", {})
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    out = []

    def add(name, cmd, body=""):
        out.append({"name": name, "cmd": cmd, "source": "pyproject.toml", "body": body})

    for name, body in (tool.get("poe", {}).get("tasks") or {}).items():
        add(name, f"poe {name}", str(body))
    for name, body in (tool.get("pdm", {}).get("scripts") or {}).items():
        if not name.startswith("_"):
            add(name, f"pdm run {name}", str(body))
    for env, spec in (tool.get("hatch", {}).get("envs") or {}).items():
        for name, body in ((spec or {}).get("scripts") or {}).items():
            add(name if env == "default" else f"{env}:{name}",
                f"hatch run {name}" if env == "default" else f"hatch run {env}:{name}", str(body))
    for name, body in (tool.get("taskipy", {}).get("tasks") or {}).items():
        add(name, f"task {name}", str(body))
    # configured tools without a task runner: their plain check invocations
    if "ruff" in tool:
        add("ruff check", "ruff check .")
        add("ruff format --check", "ruff format --check .")
    if "black" in tool:
        add("black --check", "black --check .")
    if "mypy" in tool:
        add("mypy", "mypy .")
    if "pytest" in tool:
        add("pytest", "python -m pytest -q")
    return out


def find_candidates(root: Path, exclude: list[str] = ()) -> tuple[list[dict], list[dict]]:
    """(candidates to time, skipped with reason). Candidates are ordered format, lint, typecheck, check, test so a
    time budget cuts the slow end; scripts with an identical body are timed once (aliases)."""
    extra = [re.compile(x, re.I) for x in exclude]
    keep, skipped, by_body = [], [], {}
    for c in _package_scripts(root) + _make_targets(root) + _pyproject(root):
        name = c["name"]
        if not KEYWORDS.search(name):
            continue
        hit = DEFAULT_EXCLUDE.search(name) or next((x.search(name) for x in extra if x.search(name)), None)
        if hit:
            skipped.append({**c, "reason": f"excluded ({hit.group(0)!r})"})
            continue
        if _category(name) == "format" and not re.search(r"check|--diff|list-different", name + " " + c["body"], re.I):
            skipped.append({**c, "reason": "formats in place (no check mode)"})
            continue
        if c["body"] and (c["source"], c["body"]) in by_body:
            by_body[(c["source"], c["body"])].setdefault("aliases", []).append(c["cmd"])
            continue
        c["category"] = _category(name)
        keep.append(c)
        if c["body"]:
            by_body[(c["source"], c["body"])] = c
    keep.sort(key=lambda c: _ORDER[c["category"]])     # stable: source order within a category
    return keep, skipped


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _dirty(cwd: Path) -> list[str]:
    return [ln[3:] for ln in _git(cwd, "status", "--porcelain", "--untracked-files=normal").stdout.splitlines()]


def _restore(cwd: Path, paths: list[str]) -> None:
    """Undo what a candidate changed in a tree that was clean before it ran (ignored files are not touched)."""
    _git(cwd, "checkout", "--", ".")
    _git(cwd, "clean", "-fdq", "--", *paths)


def run_timed(cmd: str, cwd: Path, env: dict, timeout: float) -> dict:
    """Run one command in its own process group so a timeout also kills test runners' worker processes."""
    t0 = time.time()
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        code = None
    return {"code": code, "seconds": round(time.time() - t0, 2), "tail": (out or "")[-400:]}


def discover(root: Path, cwd: Path, env: dict, gate_timeout: int, exclude: list[str] = (),
             budget_s: Optional[float] = None, log=print) -> dict:
    """Time every candidate in `cwd` (the integration worktree). Each runs with a limit of half the gate timeout:
    anything slower cannot be a candidate anyway. Returns the record written to .macc/gate_discovery.json."""
    limit = gate_timeout / 2
    cands, skipped = find_candidates(root, exclude)
    if _dirty(cwd):
        raise RuntimeError(f"the integration tree has uncommitted changes; discovery needs a clean tree: {cwd}")
    timed, spent = [], 0.0
    for c in cands:
        if budget_s is not None and spent >= budget_s:
            skipped.append({**c, "reason": f"discovery budget {budget_s:.0f} s spent"})
            continue
        log(f"timing {c['cmd']} ...")
        r = run_timed(c["cmd"], cwd, env, limit)
        spent += r["seconds"]
        changed = _dirty(cwd)
        if changed:
            _restore(cwd, changed)
        if r["code"] is None:
            status = f"over {limit:.0f} s (killed)"
        elif r["code"] != 0:
            status = f"fails (exit {r['code']})"
        elif changed:
            status = f"modifies the tree: {', '.join(changed[:3])}"
        elif r["seconds"] >= limit:
            status = f"over {limit:.0f} s"
        else:
            status = "ok"
        timed.append({**c, **r, "status": status, "ok": status == "ok"})
    tree = _git(cwd, "rev-parse", "HEAD^{tree}").stdout.strip()
    return {"tree": tree, "gate_timeout": gate_timeout, "limit_s": limit, "timed": timed, "skipped": skipped,
            "ts": time.time()}


def candidates(rec: dict) -> list[dict]:
    """The pickable ones: passed and under the limit, fastest first."""
    return sorted((t for t in rec["timed"] if t["ok"]), key=lambda t: t["seconds"])


def format_report(rec: dict) -> list[str]:
    lines = [f"gate candidates (pass in < {rec['limit_s']:.0f} s = half of gate_timeout {rec['gate_timeout']}):"]
    for i, t in enumerate(candidates(rec), 1):
        alias = f"  (= {', '.join(t['aliases'])})" if t.get("aliases") else ""
        lines.append(f"  {i:>2}. {t['seconds']:>7.2f} s  {t['cmd']}{alias}")
    rest = [t for t in rec["timed"] if not t["ok"]]
    if rest:
        lines.append("not candidates:")
        lines += [f"      {t['seconds']:>7.2f} s  {t['cmd']}: {t['status']}" for t in rest]
    if rec["skipped"]:
        lines.append("not timed: " + "; ".join(f"{s['cmd']} ({s['reason']})" for s in rec["skipped"]))
    return lines


def resolve_picks(rec: dict, picks: list[str]) -> tuple[list[dict], list[str]]:
    """Picks are list numbers, script names or full commands (comma-separated allowed). Returns (chosen, errors)."""
    pool = candidates(rec)
    chosen, errors = [], []
    for p in (x.strip() for raw in picks for x in raw.split(",")):
        if not p:
            continue
        if p.isdigit() and 1 <= int(p) <= len(pool):
            hit = pool[int(p) - 1]
        else:
            hit = next((t for t in pool if p in (t["cmd"], t["name"]) or p in t.get("aliases", [])), None)
        if hit is None:
            other = next((t for t in rec["timed"] if p in (t["cmd"], t["name"])), None)
            errors.append(f"{p!r}: " + (other["status"] if other else "not a discovered candidate"))
        elif hit not in chosen:
            chosen.append(hit)
    return chosen, errors
