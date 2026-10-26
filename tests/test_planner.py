import pytest
import yaml

from macc.cli import main
from macc.planner import PlanError, extract_yaml, generate_plan, validate_plan
from macc.workers import MOCK_PLAN, MOCK_PLAN_BAD, MockWorker


def test_validate_good_and_bad_plans():
    g = validate_plan(yaml.safe_load(MOCK_PLAN))
    assert [t.id for t in g.ready()] == ["contract"]
    with pytest.raises(PlanError, match="overlap"):
        validate_plan(yaml.safe_load(MOCK_PLAN_BAD))
    with pytest.raises(PlanError, match="done_when"):
        validate_plan({"tasks": [{"id": "x", "title": "x", "owns": ["a/**"]}]})
    with pytest.raises(PlanError, match="unknown fields"):
        validate_plan({"tasks": [{"id": "x", "title": "x", "bogus": 1}]})


def test_extract_yaml_variants():
    assert extract_yaml("x\n<<<MACC_PLAN\ntasks: []\nMACC_PLAN>>>\ny") == "tasks: []"
    assert extract_yaml("```yaml\ntasks: []\n```") == "tasks: []"
    with pytest.raises(PlanError):
        extract_yaml("nothing")


async def test_generate_plan_retries_once_on_invalid(tmp_path):
    out = tmp_path / "plan.yaml"
    pr = await generate_plan("build a todo app mock:bad_plan_first", tmp_path, MockWorker(), out)
    g = pr.graph
    assert out.exists() and len(g.tasks) == 5 and len(pr.usage) == 2
    data = yaml.safe_load(out.read_text())
    assert data["tasks"][2]["owns"] == ["web/**"]          # the corrected plan


def test_cli_plan_from(tmp_path, capsys):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    (tmp_path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    rc = main(["--repo", str(tmp_path), "plan", "--from", "todo app", "--worker", "mock", str(tmp_path / "p.yaml")])
    out = capsys.readouterr().out
    assert rc == 0 and "wrote" in out and "graph TD" in out
    assert main(["--repo", str(tmp_path), "plan", str(tmp_path / "p.yaml")]) == 0


def test_plan_warnings():
    from macc.planner import plan_warnings
    g = validate_plan(yaml.safe_load(MOCK_PLAN))
    g.tasks["backend"].done_when = ["python3 -c 'x=1'", "curl 'http://x/%%20'"]
    w = plan_warnings(g, existing_files={"server/app.py", "api/openapi.yaml"}, gates=["python -m pytest"])
    joined = "\n".join(w)
    assert "bare python" in joined and "%%" in joined
    assert "backend: owns 'server/**' which already contains ['server/app.py']" in joined
    assert "contract: owns" in joined               # api/openapi.yaml exists; "Write api/openapi.yaml." never says modify/replace
    g.tasks["contract"].spec = "Replace the existing api/openapi.yaml."
    g.tasks["backend"].done_when = ["python3 -c 'import x'", "python3 -m py_compile a.py"]
    w2 = "\n".join(plan_warnings(g, existing_files={"server/app.py", "api/openapi.yaml"}))
    assert "contract: owns" not in w2 and "bare python" not in w2
    g.tasks["review"].spec = "Write **Verdict: REQUEST CHANGES** or APPROVE"
    assert "verdict vocabulary" in "\n".join(plan_warnings(g))


def test_plan_warnings_flag_commit_hashes():
    from macc.planner import plan_warnings
    g = validate_plan(yaml.safe_load(MOCK_PLAN))
    g.tasks["review"].spec = "Inspect `git diff 776bae5` and the round-2 review at b408ee5c."
    g.tasks["backend"].done_when = ["git diff --quiet 776bae5 -- alr/adapters"]
    g.tasks["frontend"].spec = "MAX_BODY = 1048576; color #deadbeef; uuid-ish word facade; use $MACC_BASE"
    w = "\n".join(plan_warnings(g))
    assert "review: spec/done_when hard-codes commit hash(es) ['776bae5', 'b408ee5c']" in w
    assert "backend: spec/done_when hard-codes commit hash(es) ['776bae5']" in w
    assert "frontend:" not in w                    # digits only, letters only, and $MACC_BASE are fine


def test_planner_prompt_mentions_macc_refs():
    from macc.planner import PLANNER_PROMPT
    assert "$MACC_BASE" in PLANNER_PROMPT and "$MACC_INTEGRATION" in PLANNER_PROMPT


def test_format_history_and_prompt_injection(tmp_path):
    from macc.planner import format_history
    assert "no history yet" in format_history({})
    txt = format_history({"code": {"median_seconds": 37.0, "runs": 21, "median_actual_over_est": 0.031, "tasks": 12},
                          "review": {"median_seconds": 187.0, "runs": 5}})
    assert "- code: median 37 s of worker time per run (21 runs); median actual/est_minutes = 0.031 (12 tasks)" in txt
    assert "- review: median 187 s" in txt and "actual/est" not in txt.split("- review:")[1].split("\n")[0]


async def test_generate_plan_prompt_carries_history(tmp_path):
    seen = []

    class Spy(MockWorker):
        async def __call__(self, task, wt, pack):
            seen.append(pack)
            return await super().__call__(task, wt, pack)

    await generate_plan("feature", tmp_path, Spy(), tmp_path / "p.yaml",
                        history={"code": {"median_seconds": 40.0, "runs": 3}})
    assert "# Measured history in this repo" in seen[0] and "- code: median 40 s" in seen[0]


def test_contract_limits_are_enforced():
    from macc.planner import asks_for_expected_values as asks
    base = yaml.safe_load(MOCK_PLAN)
    validate_plan(base)                                            # the mock contract is 5 min, interface-only
    slow = yaml.safe_load(MOCK_PLAN)
    slow["tasks"][0]["est_minutes"] = 9
    with pytest.raises(PlanError, match="est_minutes 9 > 8"):
        validate_plan(slow)
    greedy = yaml.safe_load(MOCK_PLAN)
    greedy["tasks"][0]["spec"] = "Write api/openapi.yaml. CONTRACT.md includes exact expected outputs on the fixtures."
    with pytest.raises(PlanError, match="asks for expected values"):
        validate_plan(greedy)
    # what a slim contract may say
    for ok in ("The test plan says what to test, not the answers.", "Do not include expected values.",
               "Tests compute their expected results from the fixtures.", "Document the expected output format."):
        assert asks(ok) is None, ok
    for bad in ("Compute the exact expected output on the fixtures.", "Then list the expected values per case."):
        assert asks(bad), bad


def test_planner_prompt_has_slim_contract_and_fixture_rules():
    from macc.planner import PLANNER_PROMPT
    assert "never the expected values" in PLANNER_PROMPT and "at most 8" in PLANNER_PROMPT
    assert "Fixture data shared by several test tasks is its own `kind: code` task" in PLANNER_PROMPT
    assert "10 and 60 estimated minutes" not in PLANNER_PROMPT


def test_impossible_unchanged_guard_is_rejected():
    from macc.planner import impossible_guards
    base = yaml.safe_load(MOCK_PLAN)
    # backend depends on contract, which owns api/**: "api unchanged since base" can never pass for backend
    base["tasks"][1]["done_when"] = ['git diff --quiet "$MACC_BASE" -- api server/legacy && test -f server/x']
    with pytest.raises(PlanError, match=r"backend: done_when asserts \['api'\] unchanged since \$MACC_BASE"):
        validate_plan(base)
    ok = yaml.safe_load(MOCK_PLAN)
    # paths nobody in the plan owns (vendored code, lockfiles) are a legitimate guard
    ok["tasks"][1]["done_when"] = ["git diff --quiet $MACC_BASE -- vendor requirements.txt"]
    assert impossible_guards(validate_plan(ok)) == []
    # a guard on a path only a *downstream* task owns is fine at this task's time
    ok["tasks"][0]["done_when"] = ["test -f api/openapi.yaml", "git diff --quiet ${MACC_BASE} -- server"]
    validate_plan(ok)
