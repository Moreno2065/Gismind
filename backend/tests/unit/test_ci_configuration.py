from pathlib import Path
import re

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_ci_has_real_required_offline_gates_and_never_passes_provider_keys():
    text = (ROOT / ".github/workflows/runtime-ci.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) >= {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"]["GISMIND_LIVE_LLM"] == "0"
    assert workflow["env"]["LLM_API_KEY"] == ""
    assert "secrets." not in text
    assert "continue-on-error" not in text
    assert "npm ci" in text and "npm run test" in text and "npm run build" in text
    assert "--strict-markers" in text and "--basetemp=" in text
    assert set(workflow["jobs"]["backend"]["strategy"]["matrix"]["os"]) == {"ubuntu-24.04", "windows-2025"}
    assert "post-delete" in text and "if: always()" in text


def test_paid_live_ci_is_explicit_separate_and_fails_without_credentials():
    text = (ROOT / ".github/workflows/runtime-live-e2e.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["live"]["environment"] == "live-llm"
    assert "--live" in text and "--max-cost-usd" in text
    assert "continue-on-error" not in text
    assert "DEEPSEEK_API_KEY" in text
    assert "if: always()" in text
    assert "backend/.gismind/live-ci/" in text
    job = workflow["jobs"]["live"]
    assert "LLM_API_KEY" not in job.get("env", {})
    secret_steps = [step for step in job["steps"] if "secrets." in str(step)]
    assert len(secret_steps) == 1
    assert secret_steps[0]["name"] == "Execute live API and semantic gates"
    assert "DEEPSEEK_API_KEY" in secret_steps[0]["env"]["LLM_API_KEY"]


def test_paid_ci_offers_real_robustness_cohort_with_same_budget_and_evidence_guards():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-live-e2e.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["suite"]["options"] == ["prompt2000", "robustness"]
    job = workflow["jobs"]["live"]
    assert job["env"]["GISMIND_LIVE_SUITE"] == "${{ inputs.suite }}"
    steps = job["steps"]
    live = next(step for step in steps if step.get("name") == "Execute live API and semantic gates")
    command = live["run"]
    assert 'if [ "$GISMIND_LIVE_SUITE" = "robustness" ]' in command
    assert "tests.live.runtime_robustness_smoke --live" in command
    assert '--max-cost-usd "$GISMIND_RESERVATION_USD"' in command
    assert "--input-usd-per-million" in command and "--output-usd-per-million" in command
    inventory = next(step for step in steps if step.get("name") == "Validate robustness corpus without spending quota")
    assert inventory["run"] == "python -m tests.live.runtime_robustness_smoke --inventory"
    assert "env" not in inventory
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["path"].strip() == "backend/.gismind/live-ci/"


def test_frontend_ci_writes_junit_without_losing_console_reporter():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["frontend"]
    assert job["defaults"]["run"]["working-directory"] == "frontend"
    commands = [step["run"] for step in job["steps"] if step.get("run", "").startswith("npm run test")]
    assert len(commands) == 1
    assert "npm run test -- " in commands[0]
    assert "--reporter=default" in commands[0]
    assert "--reporter=junit" in commands[0]
    assert "--outputFile=test-results/frontend.xml" in commands[0]


def test_frontend_ci_always_uploads_test_evidence_separately_from_successful_dist():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["frontend"]["steps"]
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    evidence = [step for step in uploads if step.get("with", {}).get("path") == "frontend/test-results/"]
    assert len(evidence) == 1, "frontend failure evidence needs its own always-run upload"
    assert evidence[0]["if"] == "always()"
    assert evidence[0]["with"]["name"] == "frontend-test-results"
    assert evidence[0]["with"]["retention-days"] == "14"
    dist = next(step for step in uploads if step["with"]["name"] == "frontend-dist")
    assert dist.get("if", "success()") == "success()"
    assert dist["with"]["path"] == "frontend/dist/"
    build = next(step for step in steps if step.get("run") == "npm run build")
    assert steps.index(dist) > steps.index(build)


def test_required_ci_artifacts_fail_when_missing():
    offline = yaml.load((ROOT / ".github/workflows/runtime-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    live = yaml.load((ROOT / ".github/workflows/runtime-live-e2e.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    for job in (offline["jobs"]["backend"], offline["jobs"]["frontend"], live["jobs"]["live"]):
        uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
        assert uploads
        assert all(step["with"]["if-no-files-found"] == "error" for step in uploads)


def test_offline_ci_checks_nonempty_test_reports_even_after_test_failure():
    workflow = yaml.load((ROOT / ".github/workflows/runtime-ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    for job_name, report_path, size_check in (
        ("backend", "test-results/backend.xml", "stat().st_size"),
        ("backend", "test-results/coverage.xml", "stat().st_size"),
        ("frontend", "test-results/frontend.xml", "statSync"),
    ):
        checks = [step for step in workflow["jobs"][job_name]["steps"]
                  if step.get("name") == "Require nonempty test evidence"]
        assert len(checks) == 1
        assert checks[0]["if"] == "always()"
        assert report_path in checks[0]["run"]
        assert size_check in checks[0]["run"]


@pytest.mark.parametrize("filename", ["runtime-ci.yml", "runtime-live-e2e.yml"])
def test_ci_installs_only_the_complete_hash_verified_lock(filename):
    workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["backend" if filename == "runtime-ci.yml" else "live"]
    installs = [step["run"] for step in job["steps"] if "pip install" in step.get("run", "")]
    assert len(installs) == 1
    assert "--require-hashes" in installs[0]
    assert "-r requirements-ci.lock" in installs[0]
    assert "-r requirements.txt" not in installs[0]
    setup = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
    assert "backend/requirements-ci.lock" in setup["with"]["cache-dependency-path"]


def test_ci_lock_pins_and_hashes_the_reachable_dependency_closure():
    path = ROOT / "backend/requirements-ci.lock"
    assert path.is_file(), "CI needs a generated transitive hash lock, not only direct constraints"
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    locked = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line.split("--hash", 1)[0].strip())
        assert len(requirement.specifier) == 1
        assert next(iter(requirement.specifier)).operator == "=="
        assert re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", line)
        locked[canonicalize_name(requirement.name)] = requirement
    for line in (ROOT / "backend/constraints-ci.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            constraint = Requirement(line)
            assert locked[canonicalize_name(constraint.name)].specifier == constraint.specifier
    for line in (ROOT / "backend/requirements.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            direct = Requirement(line)
            pinned = locked[canonicalize_name(direct.name)]
            assert next(iter(pinned.specifier)).version in direct.specifier
            assert direct.extras <= pinned.extras
    assert str(locked["starlette"].specifier) == "==0.52.1"
    assert str(locked["anyio"].specifier) == "==4.10.0"
    assert not {"conda", "anaconda", "torch", "hydra-core", "langsmith"} & locked.keys()
    assert locked["pywin32"].marker.evaluate({"sys_platform": "win32"})
    assert not locked["pywin32"].marker.evaluate({"sys_platform": "linux"})
    assert locked["uvloop"].marker.evaluate({"sys_platform": "linux", "platform_python_implementation": "CPython"})
    assert not locked["uvloop"].marker.evaluate({"sys_platform": "win32", "platform_python_implementation": "CPython"})
