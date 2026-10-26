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
        gi = self.root / ".gitignore"
        line = ".macc/"
        existing = gi.read_text() if gi.exists() else ""
        if line not in existing.splitlines():
            with gi.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(line + "\n")

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
