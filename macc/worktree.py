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
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

INTEGRATION_BRANCH = "macc/integration"


class GitError(RuntimeError):
    pass


ARTIFACT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
                 ".gradle", "build", "dist", ".tox", ".nox", "target"}
ARTIFACT_FILES = {".DS_Store", ".coverage"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".class", ".o", ".log")


def _is_artifact(p: str) -> bool:
    """Match whole path segments only: `build/x` yes, `tools/rebuild/x.py` no."""
    parts = p.split("/")
    if any(seg in ARTIFACT_DIRS for seg in parts[:-1]):
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

    def commit_scoped(self, path: Path, message: str, owns: list[str]) -> "ScopedCommit":
        """Stage only paths inside `owns` (build artifacts are ignored), commit them, and report
        what was left outside the scope. An empty `owns` means everything is in scope."""
        dirty = self.dirty_paths(path)
        inside, outside, ignored = [], [], []
        for p in dirty:
            if _is_artifact(p):
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
            self.git("clean", "-fdq", cwd=path, check=False)
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

    def run_gates(self, commands: list[str], cwd: Optional[Path] = None, timeout: int = 1800) -> list[dict]:
        """Run deterministic gate commands (tests, lint, type-check). Returns one record per command."""
        results = []
        for cmd in commands:
            try:
                p = subprocess.run(cmd, shell=True, cwd=str(cwd or self.integration_path),
                                   capture_output=True, text=True, timeout=timeout)
                results.append({"cmd": cmd, "ok": p.returncode == 0, "code": p.returncode,
                                "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]})
            except subprocess.TimeoutExpired:
                results.append({"cmd": cmd, "ok": False, "code": -1, "stdout": "", "stderr": "timeout"})
            if not results[-1]["ok"]:
                break
        return results
