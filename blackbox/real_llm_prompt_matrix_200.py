"""Run the authored 200-prompt multilingual matrix through public HTTP/SSE APIs.

This is intentionally a black-box runner: it imports only reusable client-side
helpers and never imports backend application modules.  Each upload-backed
prompt receives fresh file IDs so a long run cannot silently depend on an
expiring upload cache.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from prompt_matrix_200 import CASES, PromptCase
from real_llm_prompt_suite import (
    Case,
    build_fixtures,
    parse_sse,
    stream_chat,
    upload_fixture,
    validate,
)


def selected_cases(spec: str) -> list[PromptCase]:
    """Select all cases or a comma-separated list/range such as M001-M012."""
    if not spec or spec.casefold() == "all":
        return list(CASES)

    wanted: set[str] = set()
    for part in spec.split(","):
        value = part.strip().upper()
        if not value:
            continue
        if "-" in value:
            start, end = value.split("-", 1)
            if start.startswith("M") and end.startswith("M") and start[1:].isdigit() and end[1:].isdigit():
                wanted.update(f"M{index:03d}" for index in range(int(start[1:]), int(end[1:]) + 1))
                continue
        if value.isdigit():
            value = f"M{int(value):03d}"
        wanted.add(value)
    return [case for case in CASES if case.id in wanted]


def as_validation_case(case: PromptCase) -> Case:
    return Case(
        id=case.id,
        prompt=case.prompt,
        expected_tools=dict(case.expected_tools),
        upload_names=list(case.upload_names),
        session_group=case.session_group,
        expect_map=case.expect_map,
    )


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:1000]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", default="all", help="all, M001,M002, or M001-M012")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--case-deadline", type=float, default=300.0)
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    base_url = args.base_url.rstrip("/")
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cases = selected_cases(args.cases)
    if not cases:
        parser.error("--cases did not select any matrix prompt")

    user_id = f"codex-prompt-matrix-{uuid.uuid4().hex[:10]}"
    headers = {"X-User-Id": user_id}
    session_ids: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    # Keep generated fixture files inside the project result directory.  This
    # works in constrained developer environments as well as normal shells.
    with tempfile.TemporaryDirectory(prefix=".matrix-fixtures-", dir=output_path.parent) as temp_dir:
        fixtures = build_fixtures(Path(temp_dir))
        timeout = httpx.Timeout(connect=20.0, read=min(args.timeout, 60.0), write=60.0, pool=20.0)
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            try:
                health_response = client.get(f"{base_url}/api/health", headers=headers)
                health = {"status_code": health_response.status_code, "body": _response_body(health_response)}
            except httpx.HTTPError as exc:
                health = {"status_code": 0, "body": {"client_error": f"{type(exc).__name__}: {exc}"}}
            print("HEALTH", json.dumps(health, ensure_ascii=False), flush=True)

            for case in cases:
                started = time.time()
                upload_ids: list[str] = []
                upload_errors: list[str] = []
                upload_evidence: dict[str, Any] = {}
                for name in case.upload_names:
                    fixture_path = fixtures.get(name)
                    if fixture_path is None:
                        upload_errors.append(f"fixture unavailable: {name}")
                        continue
                    upload_result = upload_fixture(client, base_url, headers, fixture_path)
                    upload_evidence[name] = upload_result
                    body = upload_result.get("body")
                    file_id = body.get("file_id") if isinstance(body, dict) else None
                    if upload_result["status_code"] == 200 and file_id:
                        upload_ids.append(str(file_id))
                    else:
                        upload_errors.append(f"upload {name} failed: HTTP {upload_result['status_code']} {body}")

                group = case.session_group or f"case-{case.id}"
                session_id = session_ids.setdefault(group, f"matrix-{group}-{uuid.uuid4().hex[:10]}")
                payload: dict[str, Any] = {"session_id": session_id, "message": case.prompt}
                if upload_ids:
                    payload["upload_file_ids"] = upload_ids

                try:
                    stream = stream_chat(client, base_url, headers, payload, args.case_deadline)
                    events = parse_sse(stream["text"])
                    stream_errors = list(upload_errors)
                    if stream["timed_out"]:
                        stream_errors.append(
                            f"wall-clock timeout after {stream['elapsed_s']}s; cancel={stream['cancel_result']}"
                        )
                    if stream["read_error"]:
                        stream_errors.append(f"SSE read failed: {stream['read_error']}")
                    status, reasons, evidence = validate(
                        as_validation_case(case), stream["status_code"], events, stream_errors,
                    )
                    record = {
                        "id": case.id,
                        "intent": case.intent,
                        "language": case.language,
                        "boundary": case.boundary,
                        "prompt": case.prompt,
                        "status": status,
                        "reasons": reasons,
                        "duration_s": round(time.time() - started, 2),
                        "session_id": session_id,
                        "upload_ids": upload_ids,
                        "uploads": upload_evidence,
                        "http_status": stream["status_code"],
                        "stream": {key: value for key, value in stream.items() if key != "text"},
                        "evidence": evidence,
                        "raw_sse": stream["text"],
                    }
                except Exception as exc:  # noqa: BLE001
                    record = {
                        "id": case.id,
                        "intent": case.intent,
                        "language": case.language,
                        "boundary": case.boundary,
                        "prompt": case.prompt,
                        "status": "failed",
                        "reasons": [f"client exception: {type(exc).__name__}: {exc}", *upload_errors],
                        "duration_s": round(time.time() - started, 2),
                        "session_id": session_id,
                        "upload_ids": upload_ids,
                        "uploads": upload_evidence,
                    }
                results.append(record)
                output_path.write_text(
                    json.dumps(
                        {"health": health, "user_id": user_id, "selected": [item.id for item in cases], "cases": results},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(
                    f"CASE {case.id} {record['status'].upper()} {record['duration_s']}s",
                    "; ".join(record.get("reasons") or []) or "ok",
                    flush=True,
                )
                if args.stop_on_failure and record["status"] != "passed":
                    break

    passed = sum(item["status"] == "passed" for item in results)
    failed = len(results) - passed
    print(f"SUMMARY passed={passed} failed={failed} total={len(results)} output={output_path}")
    return 0 if failed == 0 and len(results) == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
