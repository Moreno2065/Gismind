"""Unit tests for app.agents.errors — ErrorCode enum + ToolCallError."""

from app.agents.errors import ErrorCode, ToolCallError


class TestErrorCode:
    def test_error_code_values_match_existing_strings(self):
        """验证枚举值与其名称一致，防止意外修改破坏序列化/日志兼容性。

        该测试为 defensive check：ErrorCode 枚举值会被序列化到 SSE error 事件、
        ToolResult.error_code 等契约字段中。若有人重构时不小心改了枚举值（例如
        LOCATION_DRIFT = "location_drift"），本测试会立即捕获。
        """
        assert ErrorCode.LOCATION_DRIFT.value == "LOCATION_DRIFT"
        assert ErrorCode.GEOCODE_FAILED.value == "GEOCODE_FAILED"
        assert ErrorCode.MISSING_LOCATION.value == "MISSING_LOCATION"
        assert ErrorCode.DATA_PARSE_FAILED.value == "DATA_PARSE_FAILED"
        assert ErrorCode.TOOL_EXECUTION_ERROR.value == "TOOL_EXECUTION_ERROR"
        assert ErrorCode.TOOL_NOT_IMPLEMENTED.value == "TOOL_NOT_IMPLEMENTED"
        assert ErrorCode.LLM_UNAVAILABLE.value == "LLM_UNAVAILABLE"
        assert ErrorCode.SCHEMA_VALIDATION_FAILED.value == "SCHEMA_VALIDATION_FAILED"
        assert ErrorCode.INVALID_TOOL_CALL.value == "INVALID_TOOL_CALL"

    def test_tool_call_error_carries_code_and_message(self):
        err = ToolCallError(ErrorCode.LOCATION_DRIFT, "drift 200m")
        assert err.code is ErrorCode.LOCATION_DRIFT
        assert "drift 200m" in str(err)
