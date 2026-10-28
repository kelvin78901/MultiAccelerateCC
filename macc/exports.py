"""Nearby exports for the context pack: what already exists next to the files a task owns, so a worker reuses it.

Bench #6404 D v0.4.0 wrote a 107-line MCP session harness into a new test although the directory's
tests/boundary/mcp/ghidraMcpHarness.ts already exported `connectGhidraMcp` (arm A used it: +60 lines).

Scanned, one level deep, non-test source files only: the directories of the task's owns, `tests/` next to them
(`<dir>/tests`, `<parent>/tests`), and the repository's shared test helper directories (tests/fixtures, tests/support,
tests/helpers, tests/harness, tests/utils). TS/JS: `export` declarations and `export { ... }`; Python: top-level
`def` / `class` not starting with `_`. Helper-looking files (harness, helper, fixture, support, util) come first.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

LIMIT = 60
MAX_NAMES = 10
SOURCE = {".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx", ".py"}
HELPER_DIRS = ("tests/fixtures", "tests/support", "tests/helpers", "tests/harness", "tests/utils")
_TEST = re.compile(r"\.(test|spec)\.[^/]+$|(^|/)test_[^/]+\.py$|_test\.py$|\.d\.[mc]?ts$")
_HELPERISH = re.compile(r"harness|helper|fixture|support|util|common|shared", re.I)
_JS_DECL = re.compile(r"^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:const|let|var|function\*?|class|type|"
                      r"interface|enum|abstract\s+class)\s+([A-Za-z_$][\w$]*)", re.M)
_JS_LIST = re.compile(r"^export\s*(?:type\s*)?\{([^}]*)\}", re.M)
_PY_DECL = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)", re.M)


def _names(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    if path.suffix == ".py":
        return [n for n in _PY_DECL.findall(text) if not n.startswith("_")]
    names = _JS_DECL.findall(text)
    for group in _JS_LIST.findall(text):
        for item in group.split(","):
            item = item.strip()
            if item:
                names.append(item.split(" as ")[-1].strip().removeprefix("type ").strip())
    return list(dict.fromkeys(n for n in names if n and n != "default"))


def _dirs_for(owns: Iterable[str], root: Path) -> list[str]:
    out: list[str] = []

    def add(d: str):
        d = d.strip("/")
        if d not in out and (root / d).is_dir():
            out.append(d)

    for o in owns:
        head = re.split(r"[*?\[]", o, maxsplit=1)[0]
        d = head.rstrip("/") if (head.endswith("/") or (root / head).is_dir()) else str(Path(head).parent)
        if d in ("", "."):
            continue
        add(d)
        add(f"{d}/tests")
        add(f"{Path(d).parent}/tests")
    for h in HELPER_DIRS:
        add(h)
    return out


def nearby_exports(owns: Iterable[str], root: Path, limit: int = LIMIT) -> list[str]:
    """`path: name, name, ...` lines, at most `limit`."""
    lines: list[str] = []
    for d in _dirs_for(list(owns), root):
        files = sorted((p for p in (root / d).iterdir() if p.is_file() and p.suffix in SOURCE
                        and not _TEST.search(p.name)),
                       key=lambda p: (not _HELPERISH.search(p.name), p.name))
        for p in files:
            names = _names(p)
            if not names:
                continue
            more = f" (+{len(names) - MAX_NAMES} more)" if len(names) > MAX_NAMES else ""
            lines.append(f"{p.relative_to(root)}: {', '.join(names[:MAX_NAMES])}{more}")
            if len(lines) >= limit:
                return lines
    return lines
