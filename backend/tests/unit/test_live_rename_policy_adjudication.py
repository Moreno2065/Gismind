"""Offline checks of a read-only policy verdict; never a live-model score."""

import importlib
import importlib.util
import json
import shutil
import sqlite3

import pytest


@pytest.fixture
def policy():
    return importlib.import_module("tests.live.adjudicate_rename_policy")


@pytest.fixture
def clarification_evidence(tmp_path):
    case_root = tmp_path / "attempt-01"
    case_root.mkdir()
    connection = sqlite3.connect(case_root / "runtime.db")
    connection.executescript("""
        CREATE TABLE runs (run_id TEXT, status TEXT);
        CREATE TABLE planner_attempts (attempt_id TEXT, run_id TEXT, status TEXT, error_code TEXT,
            response_hash TEXT, provider TEXT, model TEXT);
        CREATE TABLE events (event_id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
            event_type TEXT, payload_json TEXT);
        CREATE TABLE plans (plan_id TEXT, run_id TEXT);
        CREATE TABLE tasks (task_id TEXT, plan_id TEXT);
        CREATE TABLE task_attempts (task_id TEXT);
        CREATE TABLE task_outputs (task_id TEXT, artifact_id TEXT);
        CREATE TABLE artifacts (artifact_id TEXT, producer_task_id TEXT);
        INSERT INTO runs VALUES ('run-1', 'awaiting_input');
        INSERT INTO planner_attempts VALUES
            ('attempt-1', 'run-1', 'failed', 'CLARIFICATION_REQUIRED',
             'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             'openai-compatible', 'deepseek-flash');
        INSERT INTO events (run_id, seq, event_type, payload_json) VALUES
            ('run-1', 6, 'planner.failed',
             '{"attempt_id":"attempt-1","error_code":"CLARIFICATION_REQUIRED","status":"failed","run_version":2}'),
            ('run-1', 7, 'run.awaiting_input',
             '{"kind":"planning_input","code":"CLARIFICATION_REQUIRED","diagnostic":{"code":"CLARIFICATION_REQUIRED","path":"planner.origin"},"path":"planner.origin","run_version":2}'),
            ('run-1', 8, 'run.status_changed',
             '{"run_id":"run-1","status":"awaiting_input","run_version":3}');
        INSERT INTO artifacts VALUES ('uploaded-input', NULL);
    """)
    connection.commit()
    connection.close()
    call = {"response_sha256": "a" * 64, "model": "deepseek-flash",
            "provider": "openai-compatible", "usage": {"prompt_tokens": 200, "completion_tokens": 20}}
    return case_root, call


def test_policy_selects_only_the_three_decided_rename_templates():
    spec = importlib.util.find_spec("tests.live.adjudicate_rename_policy")
    assert spec is not None, "independent policy adjudicator is missing"
    policy = importlib.import_module("tests.live.adjudicate_rename_policy")
    from tests.live.prompt_corpus_2000 import load_cases

    selected = policy.policy_cases(load_cases())
    assert len(selected) == 30
    assert {case.paraphrase for case in selected} == {2, 3, 9}
    assert {case.family for case in selected} == {"rename"}
    assert policy.POLICY_VERSION == "rename-ambiguity-clarification-v2"
    assert len(policy.policy_digest()) == 64


def test_policy_accepts_real_clarification_with_uploaded_input_but_no_output(policy, clarification_evidence):
    case_root, call = clarification_evidence
    assert hasattr(policy, "inspect_policy_case")
    before = (case_root / "runtime.db").read_bytes()
    verdict = policy.inspect_policy_case(case_root, "run-1", [call])
    assert verdict["ok"] is True
    assert verdict["run_status"] == "awaiting_input"
    assert verdict["input_artifacts"] == 1
    assert verdict["output_artifacts"] == 0
    assert verdict["provider_response_hashes"] == ["a" * 64]
    assert verdict["awaiting_event_seq"] == 7
    assert verdict["status_event_seq"] == 8
    assert (case_root / "runtime.db").read_bytes() == before


@pytest.mark.parametrize("mutation", [
    "missing_awaiting_event", "wrong_event_run", "wrong_kind", "wrong_event_code",
    "wrong_nested_diagnostic", "wrong_planner_attempt", "missing_status_event",
    "wrong_status_event", "reversed_event_order",
])
def test_policy_fails_closed_without_matching_durable_event_chain(policy, clarification_evidence, mutation):
    case_root, call = clarification_evidence
    with sqlite3.connect(case_root / "runtime.db") as connection:
        if mutation == "missing_awaiting_event":
            connection.execute("DELETE FROM events WHERE event_type='run.awaiting_input'")
        elif mutation == "wrong_event_run":
            connection.execute("UPDATE events SET run_id='other-run' WHERE event_type='run.awaiting_input'")
        elif mutation == "wrong_kind":
            connection.execute("UPDATE events SET payload_json=replace(payload_json, 'planning_input', 'side_effect') WHERE event_type='run.awaiting_input'")
        elif mutation == "wrong_event_code":
            connection.execute("UPDATE events SET payload_json=replace(payload_json, '\"code\":\"CLARIFICATION_REQUIRED\"', '\"code\":\"OTHER_FAILURE\"') WHERE event_type='run.awaiting_input'")
        elif mutation == "wrong_nested_diagnostic":
            payload = json.loads(connection.execute("SELECT payload_json FROM events WHERE event_type='run.awaiting_input'").fetchone()[0])
            payload["diagnostic"]["code"] = "OTHER_FAILURE"
            connection.execute("UPDATE events SET payload_json=? WHERE event_type='run.awaiting_input'", (json.dumps(payload),))
        elif mutation == "wrong_planner_attempt":
            connection.execute("UPDATE events SET payload_json=replace(payload_json, 'attempt-1', 'attempt-other') WHERE event_type='planner.failed'")
        elif mutation == "missing_status_event":
            connection.execute("DELETE FROM events WHERE event_type='run.status_changed'")
        elif mutation == "wrong_status_event":
            connection.execute("UPDATE events SET payload_json=replace(payload_json, 'awaiting_input', 'completed') WHERE event_type='run.status_changed'")
        elif mutation == "reversed_event_order":
            connection.execute("UPDATE events SET seq=5 WHERE event_type='run.status_changed'")
    verdict = policy.inspect_policy_case(case_root, "run-1", [call])
    assert verdict["ok"] is False
    assert verdict["reason"] != "POLICY_CLARIFICATION_CONFIRMED"


def test_checkpointed_database_reads_without_creating_wal_sidecars(policy, clarification_evidence):
    case_root, _ = clarification_evidence
    assert hasattr(policy, "sqlite_readonly_uri")
    assert policy.sqlite_readonly_uri(case_root).endswith("?mode=ro&immutable=1")
    (case_root / "runtime.db-wal").write_bytes(b"uncheckpointed")
    assert policy.sqlite_readonly_uri(case_root).endswith("?mode=ro")


@pytest.mark.parametrize("mutation", [
    "completed", "wrong_diagnostic", "no_provider_response", "wrong_response_hash",
    "plan", "task", "worker", "output_artifact",
])
def test_policy_rejects_execution_or_missing_live_provenance(policy, clarification_evidence, mutation):
    case_root, call = clarification_evidence
    if mutation == "no_provider_response":
        calls = []
    elif mutation == "wrong_response_hash":
        calls = [{**call, "response_sha256": "b" * 64}]
    else:
        calls = [call]
    with sqlite3.connect(case_root / "runtime.db") as connection:
        if mutation == "completed":
            connection.execute("UPDATE runs SET status='completed'")
        elif mutation == "wrong_diagnostic":
            connection.execute("UPDATE planner_attempts SET error_code='OTHER_FAILURE'")
        elif mutation == "plan":
            connection.execute("INSERT INTO plans VALUES ('plan-1', 'run-1')")
        elif mutation == "task":
            connection.execute("INSERT INTO tasks VALUES ('task-1', 'plan-1')")
        elif mutation == "worker":
            connection.execute("INSERT INTO task_attempts VALUES ('task-1')")
        elif mutation == "output_artifact":
            connection.execute("INSERT INTO artifacts VALUES ('produced', 'task-1')")
    verdict = policy.inspect_policy_case(case_root, "run-1", calls)
    assert verdict["ok"] is False
    assert verdict["reason"] != "POLICY_CLARIFICATION_CONFIRMED"


@pytest.fixture
def retained_partial_run(tmp_path, clarification_evidence):
    from tests.live.prompt_corpus_2000 import CORPUS_SHA256, load_cases

    case = next(c for c in load_cases() if c.case_id == "zh-rename-00-02")
    case_root, call = clarification_evidence
    source = tmp_path / "real-run"
    destination = source / "cases" / case.case_id / "attempt-01"
    destination.parent.mkdir(parents=True)
    shutil.copytree(case_root, destination)
    rows = [
        {"type": "manifest", "corpus_sha256": CORPUS_SHA256,
         "runner_version": "real-api-artifact-v4", "runner_sha256": "b" * 64},
        {"type": "provider_manifest", "model": "deepseek-flash", "endpoint_hash": "c" * 64,
         "runtime": {"fingerprint": "d" * 64}},
        {"type": "provider_response", "case_id": case.case_id, **call},
        {"type": "result", "case_id": case.case_id, "family": case.family,
         "kind": case.kind, "prompt": case.prompt, "file_format": case.file_format,
         "run_id": "run-1", "status": "awaiting_input", "ok": False,
         "model": "deepseek-flash", "provider": "openai-compatible",
         "planner_attempts": [{"error_code": "CLARIFICATION_REQUIRED", "response_hash": "a" * 64}],
         "worker_attempts": [], "outputs": []},
    ]
    (source / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summary = {"corpus_sha256": CORPUS_SHA256, "runner_sha256": "b" * 64,
               "selected": 1, "completed": 1, "passed": 0, "failed": 1, "pending": 0,
               "evidence_valid": True, "full_2000_gate": False}
    (source / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return source, case


def test_policy_sidecar_keeps_strict_verdict_and_source_immutable(policy, retained_partial_run, tmp_path):
    source, case = retained_partial_run
    assert hasattr(policy, "adjudicate")
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    output = tmp_path / "policy-verdict"
    report = policy.adjudicate(source, output)
    assert report["legacy_strict_passed"] == 0
    assert report["legacy_strict_failed"] == 1
    assert report["legacy_strict_full_2000_gate"] is False
    assert report["policy_passed"] == 1
    assert report["policy_failed"] == 0
    assert report["policy_full_2000_gate"] is False
    assert report["policy_cases_evaluated"] == 1
    assert report["provider_calls_made"] == 0
    assert report["source_evidence_unchanged"] is True
    assert report["source_hashes"]["results_sha256"]
    verdict = json.loads((output / "adjudication.jsonl").read_text(encoding="utf-8"))
    assert verdict["case_id"] == case.case_id
    assert verdict["legacy_strict_ok"] is False and verdict["policy_ok"] is True
    assert verdict["provider_response_hashes"] == ["a" * 64]
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
    with pytest.raises((ValueError, FileExistsError)):
        policy.adjudicate(source, output)


def test_policy_sidecar_rejects_untrusted_corpus_without_writing(policy, retained_partial_run, tmp_path):
    source, _ = retained_partial_run
    journal = source / "results.jsonl"
    journal.write_text(journal.read_text(encoding="utf-8").replace('"corpus_sha256": "', '"corpus_sha256": "invalid-', 1), encoding="utf-8")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="corpus"):
        policy.adjudicate(source, output)
    assert not output.exists()


def test_missing_durable_case_evidence_invalidates_policy_report(policy, retained_partial_run, tmp_path):
    source, case = retained_partial_run
    database = source / "cases" / case.case_id / "attempt-01" / "runtime.db"
    database.write_bytes(b"corrupt database")
    report = policy.adjudicate(source, tmp_path / "invalid-evidence")
    assert report["policy_failed"] == 1
    assert report["evidence_valid"] is False
    assert report["policy_full_2000_gate"] is False
