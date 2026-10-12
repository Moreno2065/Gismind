"""Restricted Runtime code task executor.

Only the explicit ``__result__`` value is returned. Generated code cannot call
Runtime handlers or smuggle RPC/tool registries into the child process.
"""

from __future__ import annotations

import ast
import base64
import json
from typing import Any

from app.runtime.code_mode.runner import run_in_sandbox


def execute_source(source: str, *, inputs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Execute code with explicitly named, read-only Artifact payload bytes.

    ``inputs`` is deliberately reduced to bytes plus immutable metadata before
    entering the sandbox. No host path, RuntimeStore, handler or RPC object is
    exposed. The user program receives ``inputs`` (a mapping of declared names
    to ``bytes``) and ``artifact_metadata`` (artifact IDs/hashes/media types).
    """
    encoded_inputs: dict[str, dict[str, Any]] = {}
    for name, item in (inputs or {}).items():
        if not isinstance(name, str) or not name:
            raise ValueError("code input names must be non-empty strings")
        if not isinstance(item, dict) or not isinstance(item.get("data"), bytes):
            raise ValueError("code inputs must be resolved Artifact byte payloads")
        encoded_inputs[name] = {
            "data_b64": base64.b64encode(item["data"]).decode("ascii"),
            "artifact_id": item.get("artifact_id"),
            "content_hash": item.get("content_hash"),
            "media_type": item.get("media_type"),
            "type": item.get("type"),
        }
    marker = "__RUNTIME_RESULT__"
    encoded_literal = repr(json.dumps(encoded_inputs, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    wrapper = (
        f"__artifact_inputs__ = json.loads({encoded_literal})\n"
        "inputs = {name: base64.b64decode(value['data_b64']) for name, value in __artifact_inputs__.items()}\n"
        "artifact_metadata = {name: {key: value[key] for key in ('artifact_id','content_hash','media_type','type')} for name, value in __artifact_inputs__.items()}\n"
        + source + "\n" + f"print({marker!r} + repr(globals().get('__result__')))\n"
    )
    result = run_in_sandbox(wrapper)
    payload: Any = None
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            try:
                payload = ast.literal_eval(line[len(marker):])
            except (ValueError, SyntaxError):
                payload = line[len(marker):]
    if result.error_code:
        return {"status": "error", "error_code": result.error_code, "message": result.stderr or result.stdout,
                "data": {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}}
    if payload is None:
        payload = {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    return {"status": "success", "data": payload, "source": "sandbox"}


__all__ = ["execute_source"]
