"""Read-only policy verdict for three ambiguous rename requests in real runs.

The frozen corpus and original strict result remain the first-turn baseline.
This module makes no provider calls and never edits its source evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .prompt_corpus_2000 import CORPUS_SHA256, corpus_digest, load_cases


POLICY_VERSION = "rename-ambiguity-clarification-v2"
POLICY_TEMPLATES = {
    2: "字段名称不合适，改叫新名并输出图层。",
    3: "需要将属性表中的名称列换成新名列。",
    9: "保留所有记录，将字段名称更名为新名。",
}


def policy_digest() -> str:
    specification = json.dumps(
        {"version": POLICY_VERSION, "templates": POLICY_TEMPLATES,
         "required": ["awaiting_input", "CLARIFICATION_REQUIRED", "matching_durable_event_chain",
                      "no_plan", "no_task", "no_output_artifact"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(specification.encode("utf-8")).hexdigest()


def policy_cases(cases):
    selected = [case for case in cases if case.family == "rename" and case.paraphrase in POLICY_TEMPLATES]
    assert len(selected) == 30, "frozen rename template count changed"
    assert all(case.prompt.endswith(POLICY_TEMPLATES[case.paraphrase]) for case in selected), "frozen rename wording changed"
    return selected


def sqlite_readonly_uri(case_root: Path) -> str:
    """Read checkpointed DBs without creating a missing WAL shared-memory file."""
    case_root = Path(case_root)
    database = (case_root / "runtime.db").resolve(strict=True)
    wal = case_root / "runtime.db-wal"
    suffix = "?mode=ro" if wal.is_file() and wal.stat().st_size else "?mode=ro&immutable=1"
    return database.as_uri() + suffix


def inspect_policy_case(case_root: Path, run_id: str, provider_calls: list[dict]) -> dict:
    """Verify durable first-turn clarification and actual provider provenance."""
    case_root = Path(case_root)
    database = case_root / "runtime.db"
    evidence = {"ok": False, "reason": "EVIDENCE_UNREADABLE", "run_status": None,
                "input_artifacts": 0, "output_artifacts": 0, "provider_response_hashes": []}
    try:
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        valid_calls = [call for call in provider_calls if
            isinstance(call.get("response_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", call["response_sha256"])
            and call.get("model") == "deepseek-flash"
            and call.get("provider") not in (None, "", "fake", "mock", "e2e-deterministic")
            and isinstance(call.get("usage"), dict)
            and type(call["usage"].get("prompt_tokens")) is int and call["usage"]["prompt_tokens"] > 0
            and type(call["usage"].get("completion_tokens")) is int and call["usage"]["completion_tokens"] > 0]
        hashes = [call["response_sha256"] for call in valid_calls]
        evidence["provider_response_hashes"] = hashes
        with closing(sqlite3.connect(sqlite_readonly_uri(case_root), uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            runs = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchall()
            attempts = connection.execute(
                "SELECT attempt_id,status,error_code,response_hash,provider,model FROM planner_attempts "
                "WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            events = connection.execute(
                "SELECT seq,event_type,payload_json FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
            plans = connection.execute("SELECT COUNT(*) FROM plans WHERE run_id=?", (run_id,)).fetchone()[0]
            tasks = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            workers = connection.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0]
            outputs = connection.execute("SELECT COUNT(*) FROM task_outputs").fetchone()[0]
            uploaded = connection.execute("SELECT COUNT(*) FROM artifacts WHERE producer_task_id IS NULL").fetchone()[0]
            produced = connection.execute("SELECT COUNT(*) FROM artifacts WHERE producer_task_id IS NOT NULL").fetchone()[0]
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        evidence.update(database_sha256=before, database_unchanged=before == after,
                        run_status=runs[0]["status"] if len(runs) == 1 else None,
                        input_artifacts=uploaded, output_artifacts=produced,
                        plan_count=plans, task_count=tasks, worker_attempt_count=workers,
                        task_output_count=outputs, planner_attempt_count=len(attempts))
        event_chain = []
        for event in events:
            payload = json.loads(event["payload_json"])
            if not isinstance(payload, dict) or type(event["seq"]) is not int:
                raise ValueError("invalid durable event payload or sequence")
            event_chain.append((event["seq"], event["event_type"], payload))
        planner_event = next((item for item in reversed(event_chain) if item[1] == "planner.failed"), None)
        awaiting_event = next((item for item in reversed(event_chain) if item[1] == "run.awaiting_input"), None)
        status_event = next((item for item in reversed(event_chain) if item[1] == "run.status_changed"), None)
        evidence.update(event_count=len(event_chain),
                        planner_event_seq=planner_event[0] if planner_event else None,
                        awaiting_event_seq=awaiting_event[0] if awaiting_event else None,
                        status_event_seq=status_event[0] if status_event else None)
        if before != after:
            evidence["reason"] = "SOURCE_DATABASE_CHANGED"
        elif len(runs) != 1:
            evidence["reason"] = "RUN_MISSING_OR_DUPLICATE"
        elif not valid_calls or len(valid_calls) != len(provider_calls):
            evidence["reason"] = "PROVIDER_RESPONSE_MISSING_OR_INVALID"
        elif not attempts or attempts[-1]["status"] != "failed" or attempts[-1]["error_code"] != "CLARIFICATION_REQUIRED":
            evidence["reason"] = "CLARIFICATION_DIAGNOSTIC_MISSING"
        elif attempts[-1]["response_hash"] not in hashes or attempts[-1]["model"] != "deepseek-flash" or attempts[-1]["provider"] in (None, "", "fake", "mock", "e2e-deterministic"):
            evidence["reason"] = "PLANNER_PROVIDER_PROVENANCE_MISMATCH"
        elif evidence["run_status"] != "awaiting_input":
            evidence["reason"] = "RUN_NOT_AWAITING_INPUT"
        elif not planner_event or planner_event[2].get("attempt_id") != attempts[-1]["attempt_id"] \
                or planner_event[2].get("error_code") != attempts[-1]["error_code"] \
                or planner_event[2].get("status") != "failed":
            evidence["reason"] = "DURABLE_PLANNER_FAILURE_EVENT_MISSING_OR_MISMATCH"
        elif not awaiting_event or awaiting_event[2].get("kind") != "planning_input" \
                or awaiting_event[2].get("code") != attempts[-1]["error_code"] \
                or not isinstance(awaiting_event[2].get("diagnostic"), dict) \
                or awaiting_event[2]["diagnostic"].get("code") != attempts[-1]["error_code"]:
            evidence["reason"] = "DURABLE_AWAITING_EVENT_MISSING_OR_MISMATCH"
        elif not status_event or status_event[2].get("run_id") != run_id \
                or status_event[2].get("status") != evidence["run_status"]:
            evidence["reason"] = "DURABLE_STATUS_EVENT_MISSING_OR_MISMATCH"
        elif not planner_event[0] < awaiting_event[0] < status_event[0]:
            evidence["reason"] = "DURABLE_EVENT_ORDER_MISMATCH"
        elif plans or tasks or workers or outputs or produced:
            evidence["reason"] = "EXECUTION_OR_OUTPUT_EXISTS"
        else:
            evidence.update(ok=True, reason="POLICY_CLARIFICATION_CONFIRMED")
    except (OSError, sqlite3.Error, ValueError) as exc:
        evidence["evidence_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return evidence


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_journal(path: Path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not rows or rows[0].get("type") != "manifest" or rows[0].get("corpus_sha256") != CORPUS_SHA256:
        raise ValueError("source journal does not match the frozen corpus")
    return rows


def _latest_attempts(rows):
    attempts: dict[str, list[tuple[dict, list[dict]]]] = {}
    pending_calls: dict[str, list[dict]] = {}
    for row in rows:
        case_id = row.get("case_id")
        if row.get("type") == "provider_response" and case_id:
            pending_calls.setdefault(case_id, []).append(row)
        elif row.get("type") == "result" and case_id:
            attempts.setdefault(case_id, []).append((row, pending_calls.pop(case_id, [])))
    return {case_id: values[-1] + (len(values),) for case_id, values in attempts.items()}


def adjudicate(source: Path, output: Path) -> dict:
    """Write a separate verdict linked to immutable real-provider source evidence."""
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("policy output must be disjoint from source evidence")
    if output.exists():
        raise FileExistsError("policy output already exists; choose a new directory")
    journal_path, summary_path = source / "results.jsonl", source / "summary.json"
    source_hashes = {"results_sha256": _sha(journal_path), "summary_sha256": _sha(summary_path)}
    rows = _read_journal(journal_path)
    strict = json.loads(summary_path.read_text(encoding="utf-8"))
    frozen = load_cases()
    if len(frozen) != 2000 or corpus_digest(frozen) != CORPUS_SHA256:
        raise ValueError("current frozen corpus integrity check failed")
    if strict.get("corpus_sha256") != CORPUS_SHA256 or strict.get("evidence_valid") is not True:
        raise ValueError("source summary has invalid corpus or evidence")
    if any(row.get("type") == "runtime_drift" for row in rows):
        raise ValueError("source runtime drift invalidated evidence")
    if strict.get("runner_sha256") != rows[0].get("runner_sha256"):
        raise ValueError("source runner manifest and summary differ")
    provider_manifests = [row for row in rows if row.get("type") == "provider_manifest"]
    if len(provider_manifests) != 1 or provider_manifests[0].get("model") != "deepseek-flash":
        raise ValueError("source lacks a DeepSeek Flash provider manifest")
    runtime_fingerprint = provider_manifests[0].get("runtime", {}).get("fingerprint")
    if strict.get("runtime_fingerprint", runtime_fingerprint) != runtime_fingerprint:
        raise ValueError("source runtime fingerprint differs")
    latest = _latest_attempts(rows)
    if (strict.get("completed") != len(latest)
            or strict.get("passed") != sum(result.get("ok") is True for result, _, _ in latest.values())
            or strict.get("failed") != sum(result.get("ok") is False for result, _, _ in latest.values())
            or strict.get("pending") != strict.get("selected", 0) - len(latest)):
        raise ValueError("source strict summary does not match durable results")
    known = {case.case_id: case for case in frozen}
    policy_ids = {case.case_id for case in policy_cases(frozen)}
    verdicts = []
    for case_id, (original, calls, attempt_number) in sorted(latest.items()):
        case = known.get(case_id)
        if case is None or any(original.get(key) != getattr(case, key) for key in ("family", "kind", "prompt", "file_format")):
            raise ValueError(f"source case differs from frozen corpus: {case_id}")
        if type(original.get("ok")) is not bool:
            raise ValueError(f"source strict verdict missing: {case_id}")
        verdict = {"case_id": case_id, "legacy_strict_ok": original["ok"],
                   "policy_applied": case_id in policy_ids, "policy_ok": original["ok"],
                   "reason": "LEGACY_STRICT_RETAINED", "source_run_id": original.get("run_id"),
                   "source_attempt": attempt_number}
        if case_id in policy_ids:
            case_root = source / "cases" / case_id / f"attempt-{attempt_number:02d}"
            if not case_root.resolve().is_relative_to(source):
                raise ValueError("case evidence path escapes source")
            inspection = inspect_policy_case(case_root, original.get("run_id"), calls)
            journal_attempts = original.get("planner_attempts") or []
            linked_journal = bool(journal_attempts and journal_attempts[-1].get("error_code") == "CLARIFICATION_REQUIRED"
                                  and journal_attempts[-1].get("response_hash") in inspection["provider_response_hashes"])
            no_journal_execution = not original.get("worker_attempts") and not original.get("outputs")
            consistent = (original.get("status") == inspection["run_status"]
                          and original.get("model") == "deepseek-flash"
                          and original.get("provider") not in (None, "", "fake", "mock", "e2e-deterministic")
                          and linked_journal and no_journal_execution)
            verdict.update(inspection)
            verdict["policy_ok"] = bool(inspection["ok"] and consistent)
            verdict["reason"] = (inspection["reason"] if inspection["ok"] is False else
                                 "POLICY_CLARIFICATION_CONFIRMED" if consistent else "JOURNAL_DATABASE_MISMATCH")
        verdicts.append(verdict)
    journal_unchanged = source_hashes == {"results_sha256": _sha(journal_path), "summary_sha256": _sha(summary_path)}
    case_evidence_intact = all(v.get("database_unchanged") is True and not v.get("evidence_error")
                               for v in verdicts if v["policy_applied"])
    source_unchanged = journal_unchanged and case_evidence_intact
    evidence_valid = source_unchanged
    selected = strict["selected"]
    completed = len(verdicts)
    passed = sum(v["policy_ok"] for v in verdicts)
    policy_present = sum(v["policy_applied"] for v in verdicts)
    report = {"suite": "retained_real_provider_rename_policy_adjudication",
              "evidence_scope": "read_only_adjudication_not_new_model_calls",
              "policy_version": POLICY_VERSION, "policy_sha256": policy_digest(),
              "adjudicator_sha256": _sha(Path(__file__)), "policy_templates": POLICY_TEMPLATES,
              "corpus_sha256": CORPUS_SHA256, "source_directory": str(source),
              "source_hashes": source_hashes, "source_runtime_fingerprint": runtime_fingerprint,
              "source_provider_manifest": {k: provider_manifests[0].get(k) for k in ("model", "endpoint_hash")},
              "source_runner_version": rows[0].get("runner_version"),
              "source_runner_sha256": rows[0].get("runner_sha256"),
              "source_evidence_unchanged": source_unchanged, "evidence_valid": evidence_valid,
              "provider_calls_made": 0, "source_provider_response_count": strict.get("provider_response_count"),
              "selected": selected, "completed": completed, "policy_pending": selected - completed,
              "policy_passed": passed, "policy_failed": completed - passed,
              "policy_cases_evaluated": policy_present,
              "policy_full_2000_gate": bool(evidence_valid and selected == completed == 2000
                                            and policy_present == 30 and passed == 2000),
              "legacy_strict_passed": strict["passed"], "legacy_strict_failed": strict["failed"],
              "legacy_strict_full_2000_gate": strict.get("full_2000_gate"),
              "legacy_strict_source_summary": strict}
    output.mkdir(parents=True, exist_ok=False)
    with (output / "adjudication.jsonl").open("x", encoding="utf-8") as stream:
        for verdict in verdicts:
            stream.write(json.dumps(verdict, ensure_ascii=False, sort_keys=True) + "\n")
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = adjudicate(args.source, args.output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["evidence_valid"] and report["policy_pending"] == report["policy_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
