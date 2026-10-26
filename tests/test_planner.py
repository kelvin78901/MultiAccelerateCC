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
    g = await generate_plan("build a todo app mock:bad_plan_first", tmp_path, MockWorker(), out)
    assert out.exists() and len(g.tasks) == 5
    data = yaml.safe_load(out.read_text())
    assert data["tasks"][2]["owns"] == ["web/**"]          # the corrected plan


def test_cli_plan_from(tmp_path, capsys):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    rc = main(["--repo", str(tmp_path), "plan", "--from", "todo app", "--worker", "mock", str(tmp_path / "p.yaml")])
    out = capsys.readouterr().out
    assert rc == 0 and "wrote" in out and "graph TD" in out
    assert main(["--repo", str(tmp_path), "plan", str(tmp_path / "p.yaml")]) == 0
