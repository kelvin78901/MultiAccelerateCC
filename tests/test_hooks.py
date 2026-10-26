import json
import subprocess
import sys

from macc.hooks import install


def run_guard(guard, payload, owns):
    return subprocess.run([sys.executable, str(guard)], input=json.dumps(payload), capture_output=True,
                          text=True, env={"MACC_OWNS": owns, "PATH": ""})


def test_guard_blocks_outside_and_allows_inside(tmp_path):
    settings = install(tmp_path / ".macc")
    cfg = json.loads(settings.read_text())
    assert cfg["hooks"]["PreToolUse"][0]["matcher"].startswith("Write|Edit")
    guard = tmp_path / ".macc" / "hooks" / "guard_owns.py"
    cwd = str(tmp_path)

    ok = run_guard(guard, {"cwd": cwd, "tool_name": "Write", "tool_input": {"file_path": f"{cwd}/server/a.py"}}, "server/**")
    assert ok.returncode == 0
    ok2 = run_guard(guard, {"cwd": cwd, "tool_name": "Edit", "tool_input": {"file_path": "api/schema.yaml"}}, "api/schema.yaml|docs/**")
    assert ok2.returncode == 0
    bad = run_guard(guard, {"cwd": cwd, "tool_name": "Write", "tool_input": {"file_path": f"{cwd}/web/x.ts"}}, "server/**")
    assert bad.returncode == 2 and "outside this task's ownership" in bad.stderr
    outside = run_guard(guard, {"cwd": cwd, "tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}, "server/**")
    assert outside.returncode == 2
    # no ownership declared -> never blocks
    free = run_guard(guard, {"cwd": cwd, "tool_name": "Write", "tool_input": {"file_path": "anything"}}, "")
    assert free.returncode == 0
