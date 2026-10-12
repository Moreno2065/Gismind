"""Process and scheduling regressions for the durable Runtime worker."""

from __future__ import annotations

import threading
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import (
    ArtifactStore,
    Compiler,
    PlannerDraft,
    PlannerStepDraft,
    RuntimeDB,
    RuntimeStore,
    RuntimeSupervisor,
    ToolContractRegistry,
)


def _planned_buffer(tmp_path, *, run_id="r1"):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run(run_id, "s1")
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[116.4,39.9]},"properties":{}}]}'
    source = artifacts.publish_staged(
        artifacts.stage_bytes(payload), origin="upload", artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326}, schema={},
    )
    registry = ToolContractRegistry.with_builtins()
    ref = {"artifact_id": source["artifact_id"], "type": "Vector", "crs": {"kind": "EPSG", "code": 4326}}
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(
        PlannerDraft(run_id=run_id, input_revision=1, tasks=[
            PlannerStepDraft(task_key="buffer", op="vector.buffer", inputs={"vector": ref}, params={"distance_m": 1}),
        ])
    )
    runtime.create_plan("p1", run_id, plan=plan)
    runtime.create_task("buffer", "p1", "buffer", tool_name="vector.buffer")
    return runtime, artifacts, registry


class _ExitAfterCommitQueue:
    def put(self, _message):
        # The executor has already committed Task/Attempt/output/Event rows.
        os._exit(17)


def _commit_without_ipc(*args):
    from app.runtime import queue

    queue._isolated_executor_worker(*args[:-1], _ExitAfterCommitQueue())


def test_parent_recovers_child_commit_when_ipc_manifest_is_lost(tmp_path, monkeypatch):
    from app.runtime import queue

    runtime, artifacts, _ = _planned_buffer(tmp_path)
    monkeypatch.setattr(queue, "_isolated_executor_worker", _commit_without_ipc)
    result = RuntimeSupervisor(runtime, artifacts, isolate=True).run_once("worker-a", run_id="r1")

    assert result is not None and result["status"] == "succeeded"
    assert runtime.get_run("r1")["status"] == "completed"
    assert runtime._read(lambda c: c.execute(
        "SELECT status FROM task_attempts WHERE task_id='buffer'"
    ).fetchone()[0]) == "succeeded"
    assert len(runtime._read(lambda c: c.execute(
        "SELECT artifact_id FROM task_outputs WHERE task_id='buffer'"
    ).fetchall())) == 1
    assert not any(event["event_type"] == "task.retry_wait" for event in runtime.list_events("r1"))


def test_new_worker_reconciles_run_after_child_commit_and_parent_loss(tmp_path):
    from app.runtime import RuntimeExecutor, RuntimeQueue

    runtime, artifacts, _ = _planned_buffer(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("dead-parent", run_id="r1")
    assert claim is not None
    executor = RuntimeExecutor(runtime, artifacts)
    executor._ensure_run_running("r1", claim=claim)
    executor._ensure_task_running("buffer", claim=claim)
    task_payload = json.loads(runtime._read(lambda c: c.execute(
        "SELECT ir_json FROM plans WHERE plan_id='p1'"
    ).fetchone()[0]))["tasks"][0]
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_commit_without_ipc, args=(
        str(runtime.db.path), str(artifacts.data_root), str(artifacts.artifacts_dir),
        str(artifacts.staging_dir), str(artifacts.quarantine_dir), str(artifacts.uploads_dir),
        task_payload, "r1", "buffer", {
            "task_id": claim.task_id, "owner": claim.owner, "token": claim.token,
            "lease_generation": claim.lease_generation, "attempt_id": claim.attempt_id,
        }, None,
    ))
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.kill()
        child.join(timeout=5)
    assert child.exitcode == 17
    assert runtime._read(lambda c: c.execute(
        "SELECT status FROM tasks WHERE task_id='buffer'"
    ).fetchone()[0]) == "succeeded"
    assert runtime.get_run("r1")["status"] == "running"

    restarted = RuntimeSupervisor(runtime, artifacts)
    assert restarted.worker_loop("new-parent", max_tasks=0) == 0
    assert runtime.get_run("r1")["status"] == "completed"
    assert sum(event["event_type"] == "run.completed" for event in runtime.list_events("r1")) == 1


def test_already_running_worker_reconciles_peer_commit_while_idle(tmp_path, monkeypatch):
    from app.runtime import RuntimeExecutor, RuntimeQueue

    runtime, artifacts, _ = _planned_buffer(tmp_path)
    claim = RuntimeQueue(runtime).claim_next("dead-parent", run_id="r1")
    assert claim is not None
    executor = RuntimeExecutor(runtime, artifacts)
    executor._ensure_run_running("r1", claim=claim)
    executor._ensure_task_running("buffer", claim=claim)
    task_payload = json.loads(runtime._read(lambda c: c.execute(
        "SELECT ir_json FROM plans WHERE plan_id='p1'"
    ).fetchone()[0]))["tasks"][0]

    idle = RuntimeSupervisor(runtime, artifacts)
    startup_complete = threading.Event()
    stop = threading.Event()
    failures = []
    real_recover = idle._recover_terminal_runs

    def observed_recover():
        real_recover()
        startup_complete.set()

    monkeypatch.setattr(idle, "_recover_terminal_runs", observed_recover)

    def run_idle():
        try:
            idle.worker_loop("idle-peer", poll_interval=0.05, stop_event=stop)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run_idle)
    thread.start()
    child = None
    try:
        assert startup_complete.wait(5)
        assert runtime.get_run("r1")["status"] == "running"
        child = multiprocessing.get_context("spawn").Process(target=_commit_without_ipc, args=(
            str(runtime.db.path), str(artifacts.data_root), str(artifacts.artifacts_dir),
            str(artifacts.staging_dir), str(artifacts.quarantine_dir), str(artifacts.uploads_dir),
            task_payload, "r1", "buffer", {
                "task_id": claim.task_id, "owner": claim.owner, "token": claim.token,
                "lease_generation": claim.lease_generation, "attempt_id": claim.attempt_id,
            }, None,
        ))
        child.start()
        child.join(timeout=20)
        assert child.exitcode == 17
        assert _wait_until(lambda: runtime.get_run("r1")["status"] == "completed", timeout=5)
        assert not failures
        assert sum(event["event_type"] == "run.completed" for event in runtime.list_events("r1")) == 1
    finally:
        if child is not None and child.is_alive():
            child.kill()
            child.join(timeout=5)
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_losing_same_owner_start_during_validating_cannot_fail_winner(tmp_path, monkeypatch):
    from app.runtime import tool_handlers

    runtime, artifacts, registry = _planned_buffer(tmp_path)
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    handler_entered = threading.Event()
    release_handler = threading.Event()
    loser_claimed = threading.Event()
    release_loser = threading.Event()
    winner_validating = threading.Event()
    release_winner = threading.Event()
    results = {}
    calls = []
    real_buffer = tool_handlers.handle_vector_buffer
    real_start = runtime.executor_start_task
    real_update = runtime.update_task_status

    def held_buffer(**kwargs):
        calls.append(threading.current_thread().name)
        handler_entered.set()
        assert release_handler.wait(10)
        return real_buffer(**kwargs)

    def start_with_barrier(claim):
        if threading.current_thread().name == "loser":
            loser_claimed.set()
            assert release_loser.wait(10)
        return real_start(claim)

    def validating_with_barrier(task_id, status, **kwargs):
        value = real_update(task_id, status, **kwargs)
        if threading.current_thread().name == "winner" and status == "validating":
            winner_validating.set()
            assert release_winner.wait(10)
        return value

    monkeypatch.setattr(tool_handlers, "handle_vector_buffer", held_buffer)
    monkeypatch.setattr(runtime, "executor_start_task", start_with_barrier)
    monkeypatch.setattr(runtime, "update_task_status", validating_with_barrier)

    def run_named(name):
        try:
            results[name] = supervisor.run_once("same-owner", run_id="r1")
        except Exception as exc:
            results[name] = exc

    winner = threading.Thread(target=run_named, args=("winner",), name="winner")
    loser = threading.Thread(target=run_named, args=("loser",), name="loser")
    winner.start()
    try:
        assert handler_entered.wait(10)
        loser.start()
        assert loser_claimed.wait(10)
        release_handler.set()
        assert winner_validating.wait(10)
        release_loser.set()
        loser.join(timeout=10)
        assert not loser.is_alive()
        assert results["loser"] is None
        assert runtime._read(lambda c: c.execute(
            "SELECT status FROM tasks WHERE task_id='buffer'"
        ).fetchone()[0]) == "validating"
        assert runtime._read(lambda c: c.execute(
            "SELECT status FROM task_attempts WHERE task_id='buffer'"
        ).fetchone()[0]) == "running"
    finally:
        release_handler.set()
        release_loser.set()
        release_winner.set()
        winner.join(timeout=10)
        if loser.is_alive():
            loser.join(timeout=10)
    assert not winner.is_alive()
    assert not isinstance(results["winner"], Exception)
    assert runtime.get_run("r1")["status"] == "completed"
    assert calls == ["winner"]


def test_same_owner_competitor_does_not_execute_an_already_started_task(tmp_path, monkeypatch):
    from app.runtime import tool_handlers

    runtime, artifacts, registry = _planned_buffer(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    failures = []
    real_buffer = tool_handlers.handle_vector_buffer

    def held_buffer(**kwargs):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10), "first handler did not receive the release signal"
        return real_buffer(**kwargs)

    monkeypatch.setattr(tool_handlers, "handle_vector_buffer", held_buffer)
    first = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)
    second = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=False, autostart=False)

    def run_first():
        try:
            first.run_once("api", run_id="r1")
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(10), "first handler never started"
        assert second.run_once("api", run_id="r1") is None
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not failures
    assert len(calls) == 1
    assert runtime.get_run("r1")["status"] == "completed"
    assert runtime._read(lambda c: c.execute(
        "SELECT COUNT(*) FROM task_attempts WHERE task_id='buffer'"
    ).fetchone()[0]) == 1


def test_two_api_lifespans_use_distinct_worker_owners(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    first = RuntimeSupervisor(runtime, artifacts)
    second = RuntimeSupervisor(runtime, artifacts)
    app1 = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=first)
    app2 = create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=second)

    with TestClient(app1), TestClient(app2):
        first_owner = first.health_snapshot()["worker_id"]
        second_owner = second.health_snapshot()["worker_id"]
        assert first_owner.startswith("api-")
        assert second_owner.startswith("api-")
        assert first_owner != second_owner


def _wait_until(predicate, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _lock_pid(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))["pid"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def _start_cli_worker(project_root: Path):
    env = os.environ.copy()
    env.update({
        "APP_PROJECT_ROOT": str(project_root),
        "APP_DATA_ROOT": str(project_root / ".gismind"),
        "GISMIND_LIVE_LLM": "0",
        "LLM_API_KEY": "",
    })
    return subprocess.Popen(
        [sys.executable, "-m", "app.runtime.supervisor"],
        cwd=Path.cwd(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _worker_error(child):
    if child.poll() is None:
        return "worker is still running without owning the lock"
    return child.communicate(timeout=5)[1].decode(errors="replace")


def _kill_worker_process(child, worker_pid=None):
    if worker_pid is not None and child.poll() is None:
        try:
            os.kill(worker_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


def test_cli_worker_restarts_after_hard_kill_but_excludes_live_peer(tmp_path):
    lock = tmp_path / ".gismind" / "runtime.worker.lock"
    children = []
    worker_pids = {}
    try:
        first = _start_cli_worker(tmp_path)
        children.append(first)
        assert _wait_until(lambda: _lock_pid(lock) is not None or first.poll() is not None)
        first_pid = _lock_pid(lock)
        assert first_pid is not None and first.poll() is None, _worker_error(first)
        worker_pids[first] = first_pid

        os.kill(first_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        first.wait(timeout=10)
        second = _start_cli_worker(tmp_path)
        children.append(second)
        assert _wait_until(lambda: _lock_pid(lock) != first_pid or second.poll() is not None)
        second_pid = _lock_pid(lock)
        assert second_pid != first_pid and second.poll() is None, _worker_error(second)
        worker_pids[second] = second_pid

        third = _start_cli_worker(tmp_path)
        children.append(third)
        assert third.wait(timeout=10) != 0
        assert _lock_pid(lock) == second_pid
        assert second.poll() is None
    finally:
        for child in children:
            _kill_worker_process(child, worker_pids.get(child))
