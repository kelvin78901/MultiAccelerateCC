"""Ownership gaps: files nobody owns that a plan's change will break.

Plan time: identifiers a contract/code spec names (constants, operation names, exported symbols) are grepped in the
places that enumerate them (scripts/, tests/, src/**/*.test.*, docs/); a hit in a file no task owns is a warning
with a suggested owner. REA #6404 found three such verifiers one round at a time (~$8 of rework).

Run time: when a gate fails, the files its output points at (stack frames, file:line, FAIL lines) are compared with
the task's owns. If every one is outside, a retry cannot fix it: the task is BLOCKED (`fails_outside_owns`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .graph import Kind, TaskGraph

SCAN_DIRS = ("scripts/", "tests/", "docs/")
SCAN_TEST_FILE = re.compile(r"(^|/)src/.*\.test\.[^/]+$")
TEXT_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".py", ".java", ".kt", ".go", ".rs", ".rb", ".md",
                 ".json", ".yaml", ".yml", ".toml", ".sh", ".txt", ".rst", ".c", ".h", ".cpp", ".cs", ".swift"}
MAX_FILE_BYTES = 1_000_000
# an identifier found in more files than this names something generic, not the thing this plan changes
MAX_HITS_PER_IDENT = 25

_UPPER_SNAKE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CAMEL = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b")
_PASCAL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_LOWER_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_PATHLIKE = re.compile(r"\S*/\S*|\S+\.[A-Za-z]{1,5}\b")
# macc's own vocabulary and words that are identifiers in every repository
STOPWORDS = {"MACC_BASE", "MACC_INTEGRATION", "MACC_REPO_ROOT", "MACC_WORKTREE", "done_when", "merge_after",
             "est_minutes", "est_tokens", "allow_stubs", "max_attempts", "open_questions", "notes_for_downstream",
             "scope_note", "integration_gates", "max_parallel", "cost_budget_usd", "JavaScript", "TypeScript",
             "GitHub", "macOS", "iOS", "README_md", "node_modules", "e_g", "i_e", "PascalCase", "camelCase",
             "snake_case"}
MIN_LEN = 6


def spec_identifiers(text: str) -> set[str]:
    """UPPER_SNAKE constants, camelCase / PascalCase symbols and snake_case operation names in a spec
    (paths are removed first: `src/ghidra/GhidraSessionValues.ts` names a file, not a symbol)."""
    flat = _PATHLIKE.sub(" ", text)
    out = set()
    for rx in (_UPPER_SNAKE, _CAMEL, _PASCAL, _LOWER_SNAKE):
        out |= {m for m in rx.findall(flat) if len(m) >= MIN_LEN}
    return out - STOPWORDS


def _owned(path: str, globs: Iterable[str]) -> bool:
    from .worktree import _path_in_glob
    return any(_path_in_glob(path, g) for g in globs)


def _scannable(path: str) -> bool:
    return (path.startswith(SCAN_DIRS) or bool(SCAN_TEST_FILE.search(path))) and Path(path).suffix in TEXT_SUFFIXES


def suggest_owner(g: TaskGraph, exclude: Optional[str] = None) -> str:
    return next((t.id for t in g.tasks.values() if t.kind == Kind.INTEGRATE.value and t.id != exclude), "integrate")


def ownership_gaps(g: TaskGraph, workdir: Path, files: Iterable[str]) -> list[dict]:
    """[{file, idents, suggest}] for files under the scan dirs that mention an identifier from a contract/code spec
    and that no task owns. Identifiers the plan's own task ids use, or that hit too many files, are ignored."""
    idents: set[str] = set()
    for t in g.tasks.values():
        if t.kind in (Kind.CONTRACT.value, Kind.CODE.value):
            idents |= spec_identifiers(t.spec)
    idents -= set(g.tasks)
    if not idents:
        return []
    all_owns = [o for t in g.tasks.values() for o in t.owns]
    word = re.compile(r"\b(" + "|".join(sorted(map(re.escape, idents), key=len, reverse=True)) + r")\b")
    hits: dict[str, set[str]] = {}
    per_ident: dict[str, int] = {}
    for f in files:
        if not _scannable(f):
            continue
        p = workdir / f
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            found = set(word.findall(p.read_text(errors="replace")))
        except OSError:
            continue
        for i in found:
            per_ident[i] = per_ident.get(i, 0) + 1
        if found and not _owned(f, all_owns):
            hits[f] = found
    generic = {i for i, n in per_ident.items() if n > MAX_HITS_PER_IDENT}
    owner = suggest_owner(g)
    affinity = _path_tokens(o for t in g.tasks.values() if t.kind in (Kind.CONTRACT.value, Kind.CODE.value)
                            for o in t.owns)
    out = []
    for f in hits:
        named = sorted(hits[f] - generic)
        if not named:
            continue
        # constants are what enumerations use; a rare identifier says more than one in every other file; a path
        # in the same area as the plan's own files (REA: "ghidra") says more than one next door (hopper)
        score = sum(3.0 if _UPPER_SNAKE.fullmatch(i) else 1.0 / per_ident[i] ** 0.5 for i in named)
        near = sorted(_path_tokens([f]) & affinity)
        score += 1.0 * len(near)
        out.append({"file": f, "idents": named, "suggest": owner, "score": round(score, 2), "near": near})
    out.sort(key=lambda x: (-x["score"], x["file"]))
    return out


_GENERIC_PATH_TOKENS = {"src", "test", "tests", "docs", "doc", "scripts", "script", "lib", "spec", "specs", "index",
                        "main", "utils", "util", "fixture", "fixtures", "boundary", "unit", "integration", "config"}


def _path_tokens(paths: Iterable[str]) -> set[str]:
    out = set()
    for p in paths:
        for part in re.split(r"[/._*-]+", p):
            for w in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", part):
                w = w.lower()
                if len(w) >= 4 and w not in _GENERIC_PATH_TOKENS and not Path("x." + w).suffix[1:] in TEXT_SUFFIXES:
                    out.add(w)
    return out


def gap_warnings(gaps: list[dict], limit: int = 12) -> list[str]:
    if not gaps:
        return []
    lines = [f"{len(gaps)} file(s) nobody owns mention identifiers this plan's specs name; if the change alters "
             f"them, these break with no task allowed to fix them. Add them to {gaps[0]['suggest']}'s owns (and a spec "
             "line) or confirm they are unaffected. Most likely first:"]
    for gp in gaps[:limit]:
        lines.append(f"  {gp['file']}: {', '.join(gp['idents'][:5])}{' ...' if len(gp['idents']) > 5 else ''}")
    if len(gaps) > limit:
        lines.append(f"  ... {len(gaps) - limit} weaker matches (`macc plan --gaps` lists all)")
    return ["\n".join(lines)]


# ------------------------------------------------------------------ run time: where did the gate fail?
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_P = r"((?:file://)?[\w@.+~/-]*[\w-]\.[A-Za-z][\w]{0,5})"
# a path with a line (file:12, file:12:3, file(12,3)), a stack frame, or a test-runner FAIL/✗ line
_LOCUS = [re.compile(_P + r"(?::\d+|\(\d+,\d+\))"), re.compile(r"\bat .*?\(?" + _P + r"[:)]"),
          re.compile(r"^\s*(?:FAIL|ERROR|✗|×|❯)\s+" + _P, re.M)]


def _to_tracked(raw: str, tracked: set[str]) -> Optional[str]:
    """Map an output path (relative, absolute in any worktree, file:// URL) to a tracked repo path."""
    p = raw[len("file://"):] if raw.startswith("file://") else raw
    p = p[2:] if p.startswith("./") else p
    if p in tracked:
        return p
    parts = p.strip("/").split("/")
    for i in range(1, len(parts)):                # longest suffix that is a tracked file
        cand = "/".join(parts[i:])
        if cand in tracked:
            return cand
    return None


def failure_files(output: str, tracked: set[str]) -> list[str]:
    """Tracked files a failing command's output points at as the failure location (not every path it mentions:
    build logs list scripts that ran fine)."""
    text = _ANSI.sub("", output)
    found = []
    for rx in _LOCUS:
        for m in rx.finditer(text):
            f = _to_tracked(m.group(1), tracked)
            if f and f not in found:
                found.append(f)
    return found


def outside_owns(output: str, tracked: set[str], owns: list[str]) -> list[str]:
    """The failure files when ALL of them are outside `owns` (a retry cannot fix them), else []."""
    files = failure_files(output, tracked)
    if files and not any(_owned(f, owns) for f in files):
        return files
    return []
