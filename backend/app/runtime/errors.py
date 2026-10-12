"""Runtime error codes and transport-safe business failure diagnostics."""
from enum import Enum
from collections.abc import Mapping


class ErrorCode(str, Enum):
    LOCATION_DRIFT = "LOCATION_DRIFT"
    GEOCODE_FAILED = "GEOCODE_FAILED"
    MISSING_LOCATION = "MISSING_LOCATION"
    DATA_PARSE_FAILED = "DATA_PARSE_FAILED"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    TOOL_NOT_IMPLEMENTED = "TOOL_NOT_IMPLEMENTED"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_OOM = "SANDBOX_OOM"
    SANDBOX_FORBIDDEN_IMPORT = "SANDBOX_FORBIDDEN_IMPORT"
    CHECKPOINTER_NOT_FOUND = "CHECKPOINTER_NOT_FOUND"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    INVALID_TOOL_CALL = "INVALID_TOOL_CALL"
    REFINE_LIMIT_EXCEEDED = "REFINE_LIMIT_EXCEEDED"
    LLM_RESPONSE_PARSE_FAILED = "LLM_RESPONSE_PARSE_FAILED"
    INVALID_PARAMS = "INVALID_PARAMS"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"


class ToolCallError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code.value}] {message}")


def execution_error_code(value, default: str = "TOOL_EXECUTION_FAILED") -> str:
    """Extract explicit business codes, never infer process loss from text."""
    for field in ("error_code", "code"):
        code = value.get(field) if isinstance(value, Mapping) else getattr(value, field, None)
        if isinstance(code, Enum):
            code = code.value
        if isinstance(code, str) and code.strip():
            return code.strip()
    if isinstance(value, TimeoutError):
        return "TIMEOUT"
    return default


def bounded_error_message(value) -> str:
    """Keep error IPC comfortably below a small pipe's capacity, in UTF-8 bytes."""
    limit = 4096
    suffix = b"... [truncated]"
    encoded = str(value).encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit - len(suffix)].decode("utf-8", errors="ignore") + suffix.decode("ascii")


class TaskExecutionError(ValueError):
    """ValueError-compatible task failure with a transport-stable code."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)
