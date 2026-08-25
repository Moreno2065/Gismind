"""Real-wiring integration fixtures.

Uses a dedicated Redis database/key prefix and temporary SQLite checkpointer.
Connection failures FAIL hard — no automatic fakeredis fallback.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.agents.checkpointer import (
    get_sqlite_checkpointer,
    reset_sqlite_checkpointer,
)
from app.utils.redis import create_redis_client, make_key, set_redis_instance


import re

# Match real mock usage, not identifiers like resume_patch / with_schema.
BANNED_MOCK_PATTERNS = (
    re.compile(r"^\s*from\s+unittest\.mock\s+import\b", re.M),
    re.compile(r"^\s*import\s+unittest\.mock\b", re.M),
    re.compile(r"^\s*from\s+mock\s+import\b", re.M),
    re.compile(r"\bmonkeypatch\b"),
    re.compile(r"\bMagicMock\b"),
    re.compile(r"\bAsyncMock\b"),
    re.compile(r"\b_build_checkpointable_graph\b"),
    # patch( as a call — not "resume_patch" / function names ending in _patch
    re.compile(r"(?<![\w_])patch\s*\("),
    re.compile(r"@patch\b"),
)

REWRITTEN_INTEGRATION_FILES = (
    "test_sub_agent_loop.py",
    "test_checkpoint_replay.py",
    "test_awaiting_input_e2e.py",
    "test_resume_api.py",
)


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Fail collection if rewritten wiring tests re-introduce mocks/fakes."""
    root = Path(__file__).resolve().parent
    violations: list[str] = []
    for name in REWRITTEN_INTEGRATION_FILES:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        # Strip pure documentation lines that only mention banned names.
        code_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        for pat in BANNED_MOCK_PATTERNS:
            if pat.search(code):
                violations.append(f"{name}: matches banned pattern {pat.pattern!r}")
    if violations:
        pytest.fail(
            "No-mock wiring policy violated:\n  - " + "\n  - ".join(violations)
        )


@pytest.fixture
def real_redis_url() -> str:
    """Dedicated Redis URL for wiring tests; default to DB 15 on local Redis."""
    return os.environ.get("GISMIND_TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def real_redis(real_redis_url, monkeypatch):
    """Connect to real Redis; fail the test if unavailable (no fakeredis).

    Does **not** install a process-wide client override: redis.asyncio clients
    are event-loop affine. HTTP TestClient runs on its own loop, so routes must
    call ``get_redis()`` to create a per-loop client against the same URL.
    This fixture only verifies connectivity and points settings at the test DB.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", real_redis_url)
    # Ensure no fakeredis override leaks into this package.
    set_redis_instance(None)

    client = create_redis_client(real_redis_url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        try:
            await client.aclose()
        except Exception:
            pass
        pytest.fail(
            f"Real Redis required for wiring tests but connection failed: "
            f"{real_redis_url!r} ({type(exc).__name__}: {exc}). "
            f"Set GISMIND_TEST_REDIS_URL or start Redis. No fakeredis fallback."
        )

    try:
        yield client
    finally:
        set_redis_instance(None)
        try:
            await client.aclose()
        except Exception:
            pass


@pytest.fixture
def unique_session_id() -> str:
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def temp_checkpointer(tmp_path):
    """Real SqliteSaver on a temp path; resets process singleton around the test."""
    reset_sqlite_checkpointer()
    db_path = Path(tmp_path) / "wiring-checkpoints.db"
    cp = get_sqlite_checkpointer(db_path)
    try:
        yield cp, db_path
    finally:
        reset_sqlite_checkpointer()


@pytest.fixture
def app_client(real_redis_url, temp_checkpointer, monkeypatch):
    """FastAPI TestClient with real Redis URL + real temp SQLite checkpointer.

    Redis client is **not** injected on ``app.state``: TestClient's event loop
    differs from the pytest-asyncio loop. Routes use ``get_redis()`` which
    creates a loop-local client against ``settings.REDIS_URL`` (pointed at the
    dedicated test DB). Connectivity is verified once before yielding.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import create_app
    from tests.support import DeterministicLLM

    monkeypatch.setattr(settings, "REDIS_URL", real_redis_url)
    set_redis_instance(None)

    async def _ping() -> None:
        client = create_redis_client(real_redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(_ping())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"Real Redis required for wiring tests but connection failed: "
            f"{real_redis_url!r} ({type(exc).__name__}: {exc}). "
            f"Set GISMIND_TEST_REDIS_URL or start Redis. No fakeredis fallback."
        )

    cp, _db = temp_checkpointer
    dispatcher_llm = DeterministicLLM(
        planner=['{"task_plan": {"tasks": []}}']
    )
    # redis_client=None → routes call get_redis() on the request loop.
    application = create_app(
        redis_client=None,
        checkpointer=cp,
        dispatcher_llm=dispatcher_llm,
    )
    with TestClient(application) as client:
        yield client, application, real_redis_url, cp


# NOTE: Do NOT override root ``tests/conftest.py::fake_redis`` for the whole
# integration package. Contract tests (test_api sessions, upload, …) still need
# the root fakeredis autouse. Wiring fixtures (``real_redis`` / ``app_client``)
# already call ``set_redis_instance(None)`` and fail hard if real Redis is down.
