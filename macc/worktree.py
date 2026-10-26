"""Git worktree isolation and topologically-ordered merging.

Layout inside the target repo:
    .macc/worktrees/_integration   -> branch macc/integration (merge target, CI gates run here)
    .macc/worktrees/<task_id>      -> branch macc/task/<task_id>, forked from integration

Each worker gets its own worktree. Nothing merges itself: the scheduler merges
finished branches into the integration branch in topological order, one at a
time, and runs gates after each merge. Conflicts are *detected* here and
*resolved* elsewhere (merge agent / original worker / planner).
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

INTEGRATION_BRANCH = "macc/integration"


class GitError(RuntimeError):
    pass


ARTIFACT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
                 ".gradle", "build", "dist", ".tox", ".nox", "target"}
ARTIFACT_FILES = {".DS_Store", ".coverage"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".class", ".o", ".log")


def _is_artifact(p: str) -> bool:
    """Match whole path segments only: `build/x` yes, `tools/rebuild/x.py` no."""
    parts = p.rstrip("/").split("/")
    if any(seg in ARTIFACT_DIRS for seg in parts[:-1]) or parts[-1] in ARTIFACT_DIRS:   # incl. a symlinked dir
        return True
    return parts[-1] in ARTIFACT_FILES or p.endswith(ARTIFACT_SUFFIXES)


def _path_in_glob(p: str, g: str) -> bool:
    import fnmatch
    if fnmatch.fnmatch(p, g):
        return True
    d = g.rstrip("/*")
    return bool(d) and (p == d or p.startswith(d + "/"))


@dataclass
class ScopedCommit:
    committed: bool
    inside: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)   # worker changed these although it does not own them
    ignored: list[str] = field(default_factory=list)   # build artifacts, discarded


@dataclass
class MergeResult:
    ok: bool
    conflicts: list[str] = field(default_factory=list)
    merge_commit: Optional[str] = None
    message: str = ""


class Repo:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.macc_dir = self.root / ".macc"
        self.wt_dir = self.macc_dir / "worktrees"
        self.gate_timeout = 1800                  # seconds per gate command (config gate_timeout)
        # gate results per (tree hash, command) for this process: an identical gate on an identical tree
        # waits for the one in flight or reuses its result instead of running again (large repos: minutes)
        self._gate_lock = threading.Lock()
        self._gate_runs: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------ git
    def git(self, *args: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd or self.root), capture_output=True, text=True
        )
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed in {cwd or self.root}:\n{p.stderr.strip()}")
        return p

    def branch_exists(self, name: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0

    def head(self, cwd: Optional[Path] = None) -> str:
        return self.git("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def diff_files(self, base: str, head: str = "HEAD", cwd: Optional[Path] = None) -> list[dict]:
        """Per changed file: path, status (A/M/D/R/C...), added and deleted line counts (None for binary)."""
        where = cwd or self.integration_path
        status: dict[str, str] = {}
        recs = self.git("diff", "--name-status", "-z", base, head, cwd=where).stdout.split("\0")
        i = 0
        while i < len(recs):
            st = recs[i]
            i += 1
            if not st:
                continue
            if st[0] in "RC":                  # rename/copy: old path, new path
                status[recs[i + 1]] = st[0]
                i += 2
            else:
                status[recs[i]] = st[0]
                i += 1
        out = []
        # -z numstat: "a\td\tpath\0", or for a rename "a\td\t\0old\0new\0"
        recs = self.git("diff", "--numstat", "-z", base, head, cwd=where).stdout.split("\0")
        i = 0
        while i < len(recs):
            rec = recs[i]
            i += 1
            if not rec:
                continue
            a, d, path = rec.split("\t", 2)
            if not path:
                path = recs[i + 1]
                i += 2
            out.append({"path": path, "status": status.get(path, "M"),
                        "added": None if a == "-" else int(a), "deleted": None if d == "-" else int(d)})
        return out

    def tree(self, cwd: Path) -> str:
        """Tree hash of HEAD: equal trees mean identical file contents, whatever the commit history."""
        return self.git("rev-parse", "HEAD^{tree}", cwd=cwd).stdout.strip()

    def default_branch(self) -> Optional[str]:
        """The repo's mainline: origin/HEAD if set, else `main`, else `master`."""
        p = self.git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        if p.returncode == 0 and p.stdout.strip():
            name = p.stdout.strip().split("/", 1)[-1]
            if self.branch_exists(name):
                return name
        for name in ("main", "master"):
            if self.branch_exists(name):
                return name
        return None

    def macc_env(self) -> dict[str, str]:
        """Refs that plans use instead of hard-coded hashes (they go stale when the base moves):
        MACC_INTEGRATION = integration HEAD, MACC_BASE = merge-base(integration, mainline)."""
        if not self.branch_exists(INTEGRATION_BRANCH):
            return {}
        env = {"MACC_INTEGRATION": self.git("rev-parse", INTEGRATION_BRANCH).stdout.strip()}
        main = self.default_branch()
        if main:
            p = self.git("merge-base", INTEGRATION_BRANCH, main, check=False)
            if p.returncode == 0 and p.stdout.strip():
                env["MACC_BASE"] = p.stdout.strip()
        return env

    # ---------------------------------------------------------- integration
    @property
    def integration_path(self) -> Path:
        return self.wt_dir / "_integration"

    def ensure_integration(self, base_ref: str = "HEAD") -> Path:
        self.wt_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_gitignore()
        if not self.branch_exists(INTEGRATION_BRANCH):
            self.git("branch", INTEGRATION_BRANCH, base_ref)
        if not self.integration_path.exists():
            self.git("worktree", "add", str(self.integration_path), INTEGRATION_BRANCH)
        return self.integration_path

    def _ensure_gitignore(self) -> None:
        """Ignore .macc/ via .git/info/exclude: no file appears in the user's tree, so nothing can
        conflict when integration is merged back into main."""
        git_dir = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        if not git_dir.is_absolute():
            git_dir = self.root / git_dir
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if ".macc/" not in existing.splitlines():
            with exclude.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(".macc/\n")

    # ---------------------------------------------------------- task trees
    @staticmethod
    def task_branch(task_id: str) -> str:
        return f"macc/task/{task_id}"

    def task_path(self, task_id: str) -> Path:
        return self.wt_dir / task_id

    def create_task_worktree(self, task_id: str, fresh: bool = True) -> Path:
        """Fork a worktree for the task from the *current* integration head."""
        self.ensure_integration()
        path = self.task_path(task_id)
        branch = self.task_branch(task_id)
        if path.exists():
            if not fresh:
                return path
            self.remove_task_worktree(task_id)
        if self.branch_exists(branch):
            self.git("branch", "-D", branch)
        self.git("worktree", "add", "-b", branch, str(path), INTEGRATION_BRANCH)
        return path

    def remove_task_worktree(self, task_id: str, delete_branch: bool = False) -> None:
        path = self.task_path(task_id)
        if path.exists():
            self.git("worktree", "remove", "--force", str(path), check=False)
        self.git("worktree", "prune", check=False)
        if delete_branch and self.branch_exists(self.task_branch(task_id)):
            self.git("branch", "-D", self.task_branch(task_id))

    def commit_all(self, path: Path, message: str) -> bool:
        """Stage everything in the worktree and commit. Returns False if nothing to commit."""
        self.git("add", "-A", cwd=path)
        if self.git("diff", "--cached", "--quiet", cwd=path, check=False).returncode == 0:
            return False
        self.git("commit", "-q", "-m", message, cwd=path)
        return True

    def dirty_paths(self, path: Path) -> list[str]:
        """Modified/added/deleted/untracked paths relative to the worktree root."""
        # -z: NUL-separated, no quoting/escaping of unicode or spaces; renames give two records
        out = self.git("status", "--porcelain", "-z", "--untracked-files=all", cwd=path).stdout
        records = out.split("\0")
        paths = []
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if not rec:
                continue
            status, p = rec[:2], rec[3:]
            if "R" in status or "C" in status:   # rename/copy: next record is the original path
                i += 1
            paths.append(p)
        return paths

    def commit_scoped(self, path: Path, message: str, owns: list[str],
                      setup_paths: Iterable[str] = ()) -> "ScopedCommit":
        """Stage only paths inside `owns` (build artifacts and whatever worktree_setup created are ignored),
        commit them, and report what was left outside the scope. An empty `owns` means everything is in scope."""
        dirty = self.dirty_paths(path)
        setup = set(setup_paths)
        inside, outside, ignored = [], [], []
        for p in dirty:
            if _is_artifact(p) or p in setup or p.rstrip("/") in setup:
                ignored.append(p)
            elif not owns or any(_path_in_glob(p, g) for g in owns):
                inside.append(p)
            else:
                outside.append(p)
        if inside:
            self.git("add", "-A", "--", *inside, cwd=path)
        committed = False
        if self.git("diff", "--cached", "--quiet", cwd=path, check=False).returncode != 0:
            self.git("commit", "-q", "-m", message, cwd=path)
            committed = True
        if outside or ignored:
            # leave the tree clean for rebase/merge; nothing of these reaches the integration branch
            self.git("checkout", "--", ".", cwd=path, check=False)
            keep = [x for p in sorted(setup) for x in ("-e", "/" + p.rstrip("/"))]   # setup output stays
            self.git("clean", "-fdq", *keep, cwd=path, check=False)
        return ScopedCommit(committed=committed, inside=inside, outside=outside, ignored=ignored)

    # ---------------------------------------------------------------- diffs
    def merge_base(self, path: Path) -> str:
        return self.git("merge-base", INTEGRATION_BRANCH, "HEAD", cwd=path).stdout.strip()

    def changed_files(self, path: Path, base: Optional[str] = None) -> list[tuple[str, str]]:
        base = base or self.merge_base(path)
        out = self.git("diff", "--name-status", base, "HEAD", cwd=path).stdout
        files = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                files.append((parts[0][0], parts[-1]))
        return files

    def diff_text(self, path: Path, base: Optional[str] = None) -> str:
        base = base or self.merge_base(path)
        return self.git("diff", base, "HEAD", cwd=path).stdout

    def output_hash(self, path: Path, base: Optional[str] = None) -> str:
        """Hash of the *content* of the change (patch minus volatile index lines).
        Identical work on a moved base still yields the same hash."""
        patch = self.diff_text(path, base)
        patch = re.sub(r"^index [0-9a-f]+\.\.[0-9a-f]+.*$", "", patch, flags=re.M)
        return hashlib.sha256(patch.encode()).hexdigest()[:16]

    # ---------------------------------------------------------------- merge
    def merge_task(self, task_id: str, message: Optional[str] = None) -> MergeResult:
        """Merge the task branch into integration (in the integration worktree).
        On conflict, abort and report the conflicting paths."""
        ipath = self.ensure_integration()
        branch = self.task_branch(task_id)
        msg = message or f"macc: merge {task_id}"
        p = self.git("merge", "--no-ff", "-m", msg, branch, cwd=ipath, check=False)
        if p.returncode == 0:
            return MergeResult(ok=True, merge_commit=self.head(ipath), message=p.stdout.strip())
        conflicts = [
            l.split("\t", 1)[1] for l in
            self.git("diff", "--name-only", "--diff-filter=U", cwd=ipath, check=False).stdout.splitlines()
            if "\t" in l
        ] or self.git("diff", "--name-only", "--diff-filter=U", cwd=ipath, check=False).stdout.split()
        self.git("merge", "--abort", cwd=ipath, check=False)
        return MergeResult(ok=False, conflicts=conflicts, message=(p.stdout + p.stderr).strip())

    def rebase_task_on_integration(self, task_id: str) -> MergeResult:
        """Try to bring a task branch up to date before merging. Abort on conflict."""
        path = self.task_path(task_id)
        p = self.git("rebase", INTEGRATION_BRANCH, cwd=path, check=False)
        if p.returncode == 0:
            return MergeResult(ok=True, message=p.stdout.strip())
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=path, check=False).stdout.split()
        self.git("rebase", "--abort", cwd=path, check=False)
        return MergeResult(ok=False, conflicts=conflicts, message=(p.stdout + p.stderr).strip())

    # ------------------------------------------------------- conflict merge
    def start_conflict_merge(self, task_id: str) -> list[str]:
        """Merge integration INTO the task worktree leaving conflict markers in place.
        Returns the conflicting paths (empty list = merged cleanly and committed)."""
        path = self.task_path(task_id)
        p = self.git("merge", "--no-commit", "--no-ff", INTEGRATION_BRANCH, cwd=path, check=False)
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=path, check=False).stdout.split()
        if p.returncode == 0 and not conflicts:
            self.git("commit", "-q", "--no-edit", "-m", f"macc({task_id}): merge integration", cwd=path, check=False)
            return []
        return conflicts

    def abort_conflict_merge(self, task_id: str) -> None:
        self.git("merge", "--abort", cwd=self.task_path(task_id), check=False)

    def remaining_conflict_markers(self, task_id: str, files: list[str]) -> list[str]:
        path = self.task_path(task_id)
        bad = []
        for f in files:
            fp = path / f
            if fp.exists() and re.search(r"^(<{7}|={7}|>{7})", fp.read_text(errors="replace"), re.M):
                bad.append(f)
        return bad

    def finish_conflict_merge(self, task_id: str) -> None:
        path = self.task_path(task_id)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", f"macc({task_id}): resolve conflicts with integration", cwd=path)

    def _clean_tree(self, cwd: Path) -> Optional[str]:
        """Tree hash of HEAD when the worktree has no changes at all (else None: results are not shareable)."""
        st = self.git("status", "--porcelain", "--untracked-files=normal", cwd=cwd, check=False)
        if st.returncode != 0 or st.stdout.strip():
            return None
        return self.tree(cwd)

    def run_gates(self, commands: list[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> list[dict]:
        """Run deterministic gate commands (tests, lint, type-check). Returns one record per command.
        On a clean tree a (tree, command) pair runs once per process: concurrent callers wait for it and
        later callers reuse its record (marked "shared": true)."""
        results = []
        where = cwd or self.integration_path
        env = {**os.environ, **self.macc_env()} if commands else None
        tree = self._clean_tree(where) if commands else None
        for cmd in commands:
            rec = self._gate_once(tree, cmd, where, env, timeout or self.gate_timeout)
            results.append(rec)
            if not rec["ok"]:
                break
        return results

    def _gate_once(self, tree: Optional[str], cmd: str, where: Path, env: dict, timeout: int) -> dict:
        key = (tree, cmd) if tree else None
        if key is not None:
            with self._gate_lock:
                slot = self._gate_runs.get(key)
                owner = slot is None
                if owner:
                    slot = self._gate_runs[key] = {"done": threading.Event(), "rec": None}
            if not owner:
                slot["done"].wait()
                return {**slot["rec"], "shared": True}
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(where), capture_output=True, text=True,
                               timeout=timeout, env=env)
            rec = {"cmd": cmd, "ok": p.returncode == 0, "code": p.returncode,
                   "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}
        except subprocess.TimeoutExpired:
            rec = {"cmd": cmd, "ok": False, "code": -1, "stdout": "", "stderr": f"timeout after {timeout} s"}
        rec["seconds"] = round(time.time() - t0, 2)
        if key is not None:
            slot["rec"] = rec
            slot["done"].set()
        return rec

    def run_setup(self, commands: list[str], cwd: Path) -> Optional[dict]:
        """Run worktree_setup commands in a fresh worktree (e.g. link the main checkout's node_modules).
        They get MACC_REPO_ROOT and MACC_WORKTREE. Returns the failing record, or None."""
        env = {**os.environ, **self.macc_env(), "MACC_REPO_ROOT": str(self.root), "MACC_WORKTREE": str(cwd)}
        for cmd in commands:
            t0 = time.time()
            try:
                p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                                   timeout=self.gate_timeout, env=env)
                if p.returncode != 0:
                    return {"cmd": cmd, "code": p.returncode, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:],
                            "seconds": round(time.time() - t0, 2)}
            except subprocess.TimeoutExpired:
                return {"cmd": cmd, "code": -1, "stdout": "", "stderr": "timeout", "seconds": round(time.time() - t0, 2)}
        return None
