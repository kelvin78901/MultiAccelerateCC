"""Deterministic guards injected into every worker via Claude Code hooks.

Ownership is a *prompt* in v0.1. Here it becomes a hard constraint: a PreToolUse hook
blocks Write/Edit/MultiEdit/NotebookEdit on any path outside the task's `owns` globs
(exit code 2, reason fed back to the model). Bash is not intercepted; `git diff` in the
handoff step still reveals out-of-scope writes, and the scheduler flags them.

Files are written under <repo>/.macc/hooks/ (git-ignored) and passed to `claude -p`
with `--settings <file>`. The task's globs travel in the MACC_OWNS environment variable.
"""
from __future__ import annotations

import json
from pathlib import Path

GUARD = r'''#!/usr/bin/env python3
"""macc ownership guard (PreToolUse). Blocks file edits outside MACC_OWNS globs."""
import fnmatch, json, os, sys

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    owns = [g for g in os.environ.get("MACC_OWNS", "").split("|") if g]
    if not owns:
        return 0
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    rel = os.path.relpath(os.path.normpath(full), cwd)
    if rel.startswith(".."):
        sys.stderr.write(f"macc: {rel} is outside the worktree; blocked\n")
        return 2
    for g in owns:
        if fnmatch.fnmatch(rel, g):
            return 0
        d = g.rstrip("/*")
        if d and (rel == d or rel.startswith(d + "/")):
            return 0
    sys.stderr.write(
        f"macc: {rel} is outside this task's ownership {owns}. Do not modify it; "
        "describe the needed change in open_questions instead.\n")
    return 2

if __name__ == "__main__":
    sys.exit(main())
'''


def install(macc_dir: Path) -> Path:
    """Write guard + settings; return the settings file path to pass as --settings."""
    hooks_dir = macc_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    guard = hooks_dir / "guard_owns.py"
    guard.write_text(GUARD)
    guard.chmod(0o755)
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": f"python3 {guard}", "timeout": 10}],
                }
            ]
        }
    }
    sfile = hooks_dir / "settings.json"
    sfile.write_text(json.dumps(settings, indent=2))
    return sfile


def owns_env(owns: list[str]) -> str:
    return "|".join(owns)
