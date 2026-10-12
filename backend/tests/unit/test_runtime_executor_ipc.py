"""Regression tests for the external-handler staging-file IPC boundary."""

from __future__ import annotations


def test_external_handler_result_is_staged_before_parent_join(tmp_path, monkeypatch):
    """Large handler payloads must not travel through a multiprocessing Queue."""
    module = tmp_path / "runtime_ipc_probe.py"
    module.write_text(
        "def produce(size):\n"
        "    return {'status': 'success', 'data': 'x' * size}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from app.runtime.executor import RuntimeExecutor

    class Artifacts:
        def create_staging_path(self, filename):
            return tmp_path / filename

    executor = RuntimeExecutor(runtime=None, artifacts=Artifacts())
    result = executor._invoke_handler(
        "runtime_ipc_probe:produce",
        handler=None,
        kwargs={"size": 4 * 1024 * 1024},
        timeout=20,
    )

    assert result["status"] == "success"
    assert len(result["data"]) == 4 * 1024 * 1024
    assert not list(tmp_path.glob("handler-result*.pkl"))
