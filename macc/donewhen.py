"""done_when is derived by the program; the planner only suggests.

Every failed bench #6404 macc run came from a planner-written done_when: a directory passed to a test runner that
takes files (v0.4.1), and an end-to-end real-Ghidra check on a worker task that cannot pass before the other tasks
merge (v0.4.2, v0.4.3). With `test_cmd_patterns` in .macc/config.json, `macc plan` rewrites done_when at load:

- a code task gets one command per test-command pattern for the test files it owns (existing ones and the ones its
  owns name by path), plus the planner's commands that are on the whitelist;
- the whitelist is the integration gates, the commands `macc init --discover-gates` timed green, and the patterns
  themselves applied to exact files (each file tracked, or owned by the task; never a directory);
- anything else the planner wrote is dropped ("dropped: ...");
- a whitelisted command measured >= HEAVY_S that is not an integration gate runs only in integrate: it is removed
  from other tasks and moved to integrate;
- integrate keeps whitelisted commands (heavy ones included) and gets the owned test files that mention
  identifiers the contract/code specs name.

Patterns: "npm run test:focused -- {files}" (all files at once), "node --check {file}" (one command per file), or
{"cmd": ..., "paths": [globs]} to route files to the runner that accepts them (first match wins).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .graph import Kind, Task, TaskGraph

HEAVY_S = 60.0
MAX_INTEGRATE_TESTS = 40


@dataclass
class Pattern:
    cmd: str
    paths: list[str] = field(default_factory=list)

    @property
    def per_file(self) -> bool:
        return "{file}" in self.cmd

    @property
    def prefix(self) -> str:
        return self.cmd.split("{file", 1)[0].rstrip()

    def accepts(self, path: str) -> bool:
        from .worktree import _path_in_glob
        return not self.paths or any(_path_in_glob(path, g) for g in self.paths)

    def render(self, files: list[str]) -> list[str]:
        import shlex
        quoted = [shlex.quote(f) for f in files]
        if self.per_file:
            return [self.cmd.replace("{file}", q) for q in quoted]
        return [self.cmd.replace("{files}", " ".join(quoted))]


def parse_patterns(raw: Iterable) -> list[Pattern]:
    out = []
    for p in raw or []:
        if isinstance(p, str):
            out.append(Pattern(p))
        elif isinstance(p, dict) and p.get("cmd"):
            out.append(Pattern(p["cmd"], list(p.get("paths") or [])))
    return [p for p in out if "{files}" in p.cmd or "{file}" in p.cmd]


def split_chain(cmd: str) -> list[str]:
    """`a && b && c` -> [a, b, c]: each part is judged (and kept) on its own."""
    return [c.strip() for c in re.split(r"\s*&&\s*", cmd) if c.strip()]


@dataclass
class Rewrite:
    task: str
    kept: list[str]
    derived: list[str]
    dropped: list[str]
    moved: list[str]


class Deriver:
    def __init__(self, g: TaskGraph, patterns: list[Pattern], gates: Iterable[str], discovered: Iterable[str],
                 command_s: dict, tracked: Iterable[str], workdir: Optional[Path] = None):
        self.g = g
        self.patterns = patterns
        self.gates = {c for g_ in gates for c in split_chain(g_)}
        self.whitelist = self.gates | {c for d in discovered for c in split_chain(d)}
        self.secs = command_s or {}
        self.tracked = set(tracked)
        self.dirs = {"/".join(f.split("/")[:i]) for f in self.tracked for i in range(1, f.count("/") + 1)}
        self.workdir = workdir

    # ------------------------------------------------------------ matching
    def _pattern_match(self, cmd: str, t: Task) -> bool:
        import shlex
        for p in self.patterns:
            if not cmd.startswith(p.prefix + " ") and cmd != p.prefix:
                continue
            try:
                args = shlex.split(cmd[len(p.prefix):])
            except ValueError:
                return False
            if not args or (p.per_file and len(args) != 1):
                continue
            if all(self._file_ok(a, t) and p.accepts(a) for a in args):
                return True
        return False

    def _file_ok(self, path: str, t: Task) -> bool:
        from .ownership import _owned
        if path in self.dirs or path.endswith("/") or any(c in path for c in "*?["):
            return False                                  # a directory or a glob: runners here take files
        return path in self.tracked or _owned(path, t.owns)

    def files_in(self, cmds: list[str]) -> set[str]:
        """Files the kept pattern commands already run (so a derived command does not run them again)."""
        import shlex
        out: set[str] = set()
        for c in cmds:
            for p in self.patterns:
                if c.startswith(p.prefix + " "):
                    try:
                        out |= set(shlex.split(c[len(p.prefix):]))
                    except ValueError:
                        pass
        return out

    def whitelisted(self, cmd: str, t: Task) -> bool:
        return cmd in self.whitelist or self._pattern_match(cmd, t)

    def heavy(self, cmd: str) -> bool:
        return cmd not in self.gates and self.secs.get(cmd, 0.0) >= HEAVY_S

    # ------------------------------------------------------------ derivation
    def owned_tests(self, t: Task) -> list[str]:
        from .ownership import TEST_FILE, _owned
        existing = [f for f in self.tracked if TEST_FILE.search(f) and _owned(f, t.owns)]
        named = [o for o in t.owns if TEST_FILE.search(o) and not any(c in o for c in "*?[")]
        return sorted(set(existing) | set(named))

    def commands_for(self, files: list[str]) -> list[str]:
        groups: dict[int, list[str]] = {}
        for f in files:
            i = next((i for i, p in enumerate(self.patterns) if not p.per_file and p.accepts(f)), None)
            if i is not None:
                groups.setdefault(i, []).append(f)
        return [c for i in sorted(groups) for c in self.patterns[i].render(sorted(groups[i]))]

    def integrate_tests(self, t: Task) -> list[str]:
        """Owned test files that mention identifiers the contract/code specs name (bench D v0.4.0: the routing
        test that used set_comment as its "unavailable" example)."""
        if self.workdir is None:
            return []
        from .ownership import _owned, spec_identifiers, tests_mentioning
        idents: set[str] = set()
        changed: list[str] = []
        for x in self.g.tasks.values():
            if x.kind in (Kind.CONTRACT.value, Kind.CODE.value):
                idents |= spec_identifiers(x.spec)
                changed += [o for o in x.owns if not any(c in o for c in "*?[")]
        owned = [f for f in self.tracked if _owned(f, t.owns)]
        return tests_mentioning(self.workdir, owned, idents - set(self.g.tasks), changed, MAX_INTEGRATE_TESTS)

    def rewrite(self) -> list[Rewrite]:
        out = []
        integ = next((t for t in self.g.tasks.values() if t.kind == Kind.INTEGRATE.value), None)
        moved_to_integ: list[str] = []
        for t in self.g.tasks.values():
            if t.kind not in (Kind.CODE.value, Kind.INTEGRATE.value):
                continue
            kept, dropped, moved = [], [], []
            for c in t.done_when:
                for part in split_chain(c):
                    if not self.whitelisted(part, t):
                        dropped.append(part)
                    elif t.kind != Kind.INTEGRATE.value and self.heavy(part):
                        moved.append(part)
                    elif part not in kept:
                        kept.append(part)
            covered = self.files_in(kept)
            if t.kind == Kind.CODE.value:
                derived = self.commands_for([f for f in self.owned_tests(t) if f not in covered])
                if not kept and not derived and self.gates:
                    # nothing targeted: the quickest integration gate still proves the tree builds
                    derived = [min(self.gates, key=lambda c: (self.secs.get(c, 1e9), c))]
            else:
                derived = self.commands_for([f for f in self.integrate_tests(t) if f not in covered])
            t.done_when = kept + derived
            moved_to_integ += [m for m in moved if m not in moved_to_integ]
            out.append(Rewrite(t.id, kept, derived, dropped, moved))
        if integ is not None:
            integ.done_when += [m for m in moved_to_integ if m not in integ.done_when]
        return out


def derive_done_when(g: TaskGraph, cfg: dict, command_s: dict, discovered: Iterable[str], tracked: Iterable[str],
                     workdir: Optional[Path] = None) -> tuple[list[Rewrite], list[str]]:
    """Rewrite done_when in place when `test_cmd_patterns` is configured. Returns (per-task rewrites, lines for the
    operator: "dropped: ..." warnings and "moved: ..." notes). A no-op without patterns."""
    patterns = parse_patterns(cfg.get("test_cmd_patterns"))
    if not patterns:
        return [], []
    d = Deriver(g, patterns, cfg.get("integration_gates") or [], discovered, command_s, tracked, workdir)
    rewrites = d.rewrite()
    lines = []
    for r in rewrites:
        lines += [f"dropped: {r.task}: {c!r} (not a whitelisted gate or test-command pattern on exact files)"
                  for c in r.dropped]
        lines += [f"moved to integrate: {r.task}: {c!r} (measured >= {HEAVY_S:.0f} s, not an integration gate)"
                  for c in r.moved]
    return rewrites, lines


def discovered_commands(macc_dir: Path) -> list[str]:
    import json
    p = macc_dir / "gate_discovery.json"
    if not p.is_file():
        return []
    try:
        return [t["cmd"] for t in json.loads(p.read_text()).get("timed", []) if t.get("ok")]
    except (ValueError, KeyError, TypeError):
        return []
