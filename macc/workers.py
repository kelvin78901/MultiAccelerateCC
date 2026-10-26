"""Worker adapters. A worker is an async function: (task, worktree_path, context_pack) -> WorkerResult.

ClaudeWorker  runs `claude -p` headless with JSON output (token usage comes back in the result).
MockWorker    deterministic stand-in for tests and for exercising the scheduler at zero token cost.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .graph import Task
from .handoff import HANDOFF_BEGIN, HANDOFF_END, parse_worker_output


@dataclass
class WorkerResult:
    ok: bool
    output_text: str = ""
    fields: dict = field(default_factory=dict)      # parsed handoff block
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: Optional[str] = None
    preempted: bool = False


WorkerFn = Callable[[Task, Path, str], Awaitable[WorkerResult]]


class ClaudeWorker:
    """Headless Claude Code. The context pack is the prompt; CLAUDE.md in the worktree still applies."""

    def __init__(self, binary: str = "claude", default_model: Optional[str] = None,
                 extra_args: Optional[list[str]] = None, timeout: int = 3600,
                 permission_mode: str = "acceptEdits", settings_file: Optional[Path] = None):
        self.binary = binary
        self.default_model = default_model
        self.extra_args = extra_args or []
        self.timeout = timeout
        self.permission_mode = permission_mode
        self.settings_file = settings_file          # hooks (ownership guard) passed via --settings
        self.name = "claude"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _cmd(self, task: Task) -> list[str]:
        cmd = [self.binary, "-p", "--output-format", "json", "--permission-mode", self.permission_mode]
        model = task.model or self.default_model
        if model:
            cmd += ["--model", model]
        if task.effort:
            cmd += ["--effort", task.effort]
        if self.settings_file:
            cmd += ["--settings", str(self.settings_file)]
        if task.kind == "review":
            cmd += ["--allowedTools", "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*)"]
        cmd += self.extra_args
        return cmd

    # flags an older CLI may reject; dropped once on "unknown option" and remembered for the process
    _dropped_flags: set = set()
    _OPTIONAL_FLAGS = ("--effort", "--settings")

    async def __call__(self, task: Task, wt_path: Path, context_pack: str) -> WorkerResult:
        res = await self._invoke(task, wt_path, context_pack)
        if not res.ok and res.error and "unknown option" in res.error.lower():
            for flag in self._OPTIONAL_FLAGS:
                if flag in res.error and flag not in ClaudeWorker._dropped_flags:
                    ClaudeWorker._dropped_flags.add(flag)
                    return await self._invoke(task, wt_path, context_pack)
        return res

    async def _invoke(self, task: Task, wt_path: Path, context_pack: str) -> WorkerResult:
        t0 = time.time()
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow nested invocation from inside a Claude session
        env["MACC_OWNS"] = "|".join(task.owns)
        env["MACC_TASK"] = task.id
        cmd = self._cmd(task)
        for flag in ClaudeWorker._dropped_flags:
            if flag in cmd:
                i = cmd.index(flag)
                del cmd[i:i + 2]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(wt_path), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(context_pack.encode()), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return WorkerResult(ok=False, error="timeout", seconds=time.time() - t0)
        except asyncio.CancelledError:
            proc.kill()
            return WorkerResult(ok=False, error="preempted", preempted=True, seconds=time.time() - t0)
        secs = time.time() - t0
        text = out.decode(errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return WorkerResult(ok=proc.returncode == 0, output_text=text, fields=parse_worker_output(text),
                                seconds=secs, error=None if proc.returncode == 0 else err.decode(errors="replace")[-2000:])
        result_text = data.get("result", "") if isinstance(data, dict) else ""
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        return WorkerResult(
            ok=proc.returncode == 0 and not data.get("is_error", False),
            output_text=result_text,
            fields=parse_worker_output(result_text),
            model=(data.get("model") or (data.get("modelUsage") and next(iter(data["modelUsage"]), None))),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cost_usd=float(data.get("total_cost_usd", 0.0)),
            seconds=secs,
            error=None if proc.returncode == 0 else err.decode(errors="replace")[-2000:],
        )


class MockWorker:
    """Writes a file into its owned path, sleeps `delay`, optionally fails on first attempt.

    Spec conventions (for tests): the task spec may contain
        `mock:fail_first`   -> fail on attempt 1, succeed afterwards
        `mock:stub`         -> declare a stub in the handoff
        `mock:sleep=<sec>`  -> override delay
        `mock:write=<path>` -> write this path instead of the default
    """

    def __init__(self, delay: float = 0.05, tokens: int = 1000):
        self.delay = delay
        self.tokens = tokens
        self.name = "mock"
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    async def __call__(self, task: Task, wt_path: Path, context_pack: str) -> WorkerResult:
        self.calls.append(task.id)
        t0 = time.time()
        delay = self.delay
        for tok in task.spec.split():
            if tok.startswith("mock:sleep="):
                delay = float(tok.split("=", 1)[1])
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return WorkerResult(ok=False, error="preempted", preempted=True, seconds=time.time() - t0)
        if "mock:fail_first" in task.spec and task.attempts <= 1:
            return WorkerResult(ok=False, error="mock failure", seconds=time.time() - t0,
                                input_tokens=self.tokens, output_tokens=self.tokens // 4)
        path = None
        for tok in task.spec.split():
            if tok.startswith("mock:write="):
                path = tok.split("=", 1)[1]
        if path is None:
            own = task.owns[0] if task.owns else f"{task.id}.txt"
            path = own.replace("**", f"{task.id}.txt").replace("*", task.id)
        target = wt_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        stub = "mock:stub" in task.spec
        body = f"# {task.id} attempt {task.attempts}\n" + ("# TODO(macc) stubbed\n" if stub else "")
        target.write_text(body)
        fields = {"open_questions": "", "notes_for_downstream": f"{task.id} wrote {path}",
                  "stubs": [f"{path}: stubbed by mock"] if stub else []}
        text = f"done\n{HANDOFF_BEGIN}\n{json.dumps(fields)}\n{HANDOFF_END}"
        return WorkerResult(ok=True, output_text=text, fields=parse_worker_output(text), model="mock",
                            input_tokens=self.tokens, output_tokens=self.tokens // 4,
                            seconds=time.time() - t0)
