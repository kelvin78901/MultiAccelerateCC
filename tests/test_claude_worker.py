import json
import os
import stat
from pathlib import Path

from macc.graph import Task
from macc.handoff import HANDOFF_BEGIN, HANDOFF_END
from macc.workers import ClaudeWorker

FAKE = """#!/usr/bin/env bash
# fake claude: echo a JSON result like `claude -p --output-format json`
# prompt is argv[2] when given positionally (claude -p "<prompt>" ...), else stdin
if [[ "$1" == "-p" && "$2" != --* ]]; then prompt="$2"; shift 2; else shift 1; prompt=$(cat); fi
args="$*"
if [[ -n "$FAKE_TRANSIENT" ]]; then echo "Error: no stdin data received in 3s" >&2; exit 1; fi
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
    assert "Bash(python:*)" in args and "Bash(ruff:*)" in args and "Bash(bash scripts/check.sh:*)" in args
    assert "Edit" not in args.split("--allowedTools")[1].split()[0]


async def test_code_worker_gets_full_toolkit(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    wt = tmp_path / "wt"; wt.mkdir()
    await ClaudeWorker(binary=str(fake))(Task("c", "c", owns=["x/**"]), wt, "p")
    allowed = args_file.read_text().split("--allowedTools")[1].split()[0]
    assert "Bash" in allowed.split(",") and "Edit" in allowed.split(",")


async def test_transient_failure_detected(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    monkeypatch.setenv("FAKE_ARGS_FILE", str(tmp_path / "a.txt"))
    monkeypatch.setenv("FAKE_TRANSIENT", "1")
    wt = tmp_path / "wt"; wt.mkdir()
    res = await ClaudeWorker(binary=str(fake))(Task("c", "c"), wt, "p")
    assert not res.ok and res.transient


async def test_claude_worker_exports_env_provider_vars(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text('#!/usr/bin/env bash\nenv | grep "^MACC_" | sort > "$PWD/env.txt"\n'
                    'echo \'{"type":"result","is_error":false,"result":"ok","usage":{}}\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    wt = tmp_path / "wt"; wt.mkdir()
    w = ClaudeWorker(binary=str(fake))
    w.env_provider = lambda: {"MACC_BASE": "abc1234", "MACC_INTEGRATION": "def5678", "MACC_TASK": "spoofed"}
    res = await w(Task("t1", "t", owns=["x/**"]), wt, "p")
    assert res.ok
    lines = (wt / "env.txt").read_text().splitlines()
    assert "MACC_BASE=abc1234" in lines and "MACC_INTEGRATION=def5678" in lines
    assert "MACC_TASK=t1" in lines and "MACC_OWNS=x/**" in lines   # task identity is not overridable


async def test_reviewer_allowlist_handles_env_prefixed_gates(tmp_path, monkeypatch):
    fake = make_fake(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
    wt = tmp_path / "wt"; wt.mkdir()
    w = ClaudeWorker(binary=str(fake))
    w.gate_commands = ["PYTHONPATH=. /p/.venv/bin/python -m pytest -q tests"]
    await w(Task("r", "r", kind="review", done_when=["FOO=1 BAR=2 make check"]), wt, "p")
    allowed = args_file.read_text().split("--allowedTools ")[1].split(" --")[0].strip().split(",")
    assert "Bash(/p/.venv/bin/python:*)" in allowed                    # the interpreter itself (probes)
    assert "Bash(PYTHONPATH=. /p/.venv/bin/python:*)" in allowed       # and with the gate's env prefix
    assert "Bash(make check:*)" in allowed and "Bash(FOO=1 BAR=2 make check:*)" in allowed
    assert "Bash(PYTHONPATH=.:*)" not in allowed                       # the old accidental rule
    assert not any(a.startswith(("Edit", "Write")) for a in allowed)


def test_network_tools_by_kind():
    w = ClaudeWorker()
    def tools(kind):
        cmd = w._cmd(Task("t", "t", kind=kind))
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",") if "--disallowedTools" in cmd else []
        return allowed, denied
    a, d = tools("spike")
    assert {"WebSearch", "WebFetch"} <= set(a) and not d
    a, d = tools("plan")
    assert "WebSearch" in a and "WebFetch" not in a and d == ["WebFetch"]
    for kind in ("code", "contract", "integrate", "merge", "review"):
        a, d = tools(kind)
        assert not {"WebSearch", "WebFetch"} & set(a) and set(d) == {"WebFetch", "WebSearch"}, kind
