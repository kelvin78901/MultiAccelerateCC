import json
import os
import stat
from pathlib import Path

from macc.graph import Task
from macc.handoff import HANDOFF_BEGIN, HANDOFF_END
from macc.workers import ClaudeWorker

FAKE = """#!/usr/bin/env bash
# fake claude: echo a JSON result like `claude -p --output-format json`
prompt=$(cat)
args="$*"
if [[ "$args" == *"--effort"* && -n "$FAKE_REJECT_EFFORT" ]]; then
  echo "error: unknown option '--effort'" >&2; exit 1
fi
echo "$args" > "$FAKE_ARGS_FILE"
mkdir -p out; echo "$prompt" > out/prompt.txt
result='done\\n{HB}\\n{"open_questions": "", "notes_for_downstream": "wrote out/", "stubs": []}\\n{HE}'
python3 - "$result" <<'PY'
import json, sys
print(json.dumps({"type": "result", "is_error": False, "result": sys.argv[1].replace("\\\\n", "\\n"),
                  "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 900},
                  "total_cost_usd": 0.0123, "modelUsage": {"claude-haiku": {}}}))
PY
""".replace("{HB}", HANDOFF_BEGIN).replace("{HE}", HANDOFF_END)


def make_fake(tmp_path: Path) -> Path:
    p = tmp_path / "claude"
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


async def test_claude_worker_parses_json_and_usage(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    wt = tmp_path / "wt"; wt.mkdir()
    w = ClaudeWorker(binary=str(fake), default_model="haiku")
    t = Task("x", "x", effort="low", kind="review")
    res = await w(t, wt, "CONTEXT PACK")
    assert res.ok and res.fields["_parsed"] and res.fields["notes_for_downstream"] == "wrote out/"
    assert res.input_tokens == 1200 and res.cache_read_tokens == 900 and res.cost_usd == 0.0123
    assert res.model == "claude-haiku"
    args = args_file.read_text()
    assert "--output-format json" in args and "--model haiku" in args and "--effort low" in args
    assert "--allowedTools" in args                  # review kind is read-only
    assert (wt / "out" / "prompt.txt").read_text().strip() == "CONTEXT PACK"


async def test_claude_worker_drops_effort_flag_on_old_cli(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_REJECT_EFFORT", "1")
    ClaudeWorker._dropped_flags = set()
    wt = tmp_path / "wt"; wt.mkdir()
    w = ClaudeWorker(binary=str(fake))
    res = await w(Task("x", "x", effort="high"), wt, "p")
    assert res.ok
    assert "--effort" not in args_file.read_text()
    ClaudeWorker._dropped_flags = set()


async def test_reviewer_may_run_gate_commands(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    wt = tmp_path / "wt"; wt.mkdir()
    w = ClaudeWorker(binary=str(fake))
    w.gate_commands = ["python -m pytest -q", "ruff check ."]
    t = Task("r", "r", kind="review", done_when=["bash scripts/check.sh --all"])
    await w(t, wt, "p")
    args = args_file.read_text()
    assert "Bash(python -m pytest:*)" in args and "Bash(ruff:*)" in args and "Bash(bash scripts/check.sh:*)" in args
    assert "Edit" not in args.split("--allowedTools")[1].split()[0]


async def test_code_worker_gets_full_toolkit(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    wt = tmp_path / "wt"; wt.mkdir()
    await ClaudeWorker(binary=str(fake))(Task("c", "c", owns=["x/**"]), wt, "p")
    allowed = args_file.read_text().split("--allowedTools")[1].split()[0]
    assert "Bash" in allowed.split(",") and "Edit" in allowed.split(",")
