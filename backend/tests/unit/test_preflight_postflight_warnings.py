"""Unit tests for postflight warnings (empty result / feature_count anomaly)."""
from __future__ import annotations

import pytest

from app.agents.preflight.postflight import run_postflight
from app.agents.workspace.state import WorkspaceState


class FakeResult:
    """Minimal stand-in for a ToolResult-like object."""
    def __init__(self, data: dict | None = None):
        self.data = data or {}


class TestEmptyResult:
    def test_empty_features_list(self):
        """features=[] should emit empty_result warning."""
        result = FakeResult({"features": []})
        issues = run_postflight("test_tool", result, {})
        assert len(issues) == 1
        assert issues[0].code == "empty_result"
        assert issues[0].stage == "postflight"
        assert issues[0].severity == "warning"

    def test_non_empty_features_no_warning(self):
        """features with items should not emit empty_result."""
        result = FakeResult({"features": [{"type": "Feature"}]})
        issues = run_postflight("test_tool", result, {})
        assert len(issues) == 0

    def test_no_features_key(self):
        """No features key in data should not emit empty_result."""
        result = FakeResult({"other": "data"})
        issues = run_postflight("test_tool", result, {})
        assert len(issues) == 0

    def test_null_data(self):
        """Null/None data attribute should not crash."""
        result = FakeResult(None)
        issues = run_postflight("test_tool", result, {})
        assert len(issues) == 0

    def test_result_without_data_attr(self):
        """Object without .data attribute should not crash."""
        issues = run_postflight("test_tool", object(), {})
        assert len(issues) == 0


class TestFeatureCountAnomaly:
    def test_feature_count_exceeds_input_tenfold(self):
        """Output feature_count > 10x input should emit anomaly warning."""
        ws = WorkspaceState({})
        ws.add_layer(name="input_layer", kind="vector", metadata={"feature_count": 5})
        result = FakeResult({"features": [{"type": "Feature"}] * 100, "feature_count": 100})
        issues = run_postflight("test_tool", result, {
            "workspace": ws,
            "kwargs": {"input_ref": "input_layer"},
        })
        anomaly = [i for i in issues if i.code == "feature_count_anomaly"]
        assert len(anomaly) == 1
        assert anomaly[0].severity == "warning"
        assert "100" in anomaly[0].message
        assert "5" in anomaly[0].message

    def test_feature_count_within_reasonable_range(self):
        """Output feature_count close to input should not emit anomaly."""
        ws = WorkspaceState({})
        ws.add_layer(name="input_layer", kind="vector", metadata={"feature_count": 100})
        result = FakeResult({"features": [{"type": "Feature"}] * 150, "feature_count": 150})
        issues = run_postflight("test_tool", result, {
            "workspace": ws,
            "kwargs": {"input_ref": "input_layer"},
        })
        anomaly = [i for i in issues if i.code == "feature_count_anomaly"]
        assert len(anomaly) == 0

    def test_feature_count_missing_input_ref(self):
        """No input_ref should skip anomaly check."""
        ws = WorkspaceState({})
        ws.add_layer(name="input_layer", kind="vector", metadata={"feature_count": 5})
        result = FakeResult({"features": [{"type": "Feature"}] * 100})
        issues = run_postflight("test_tool", result, {
            "workspace": ws,
            "kwargs": {},  # no input_ref
        })
        anomaly = [i for i in issues if i.code == "feature_count_anomaly"]
        assert len(anomaly) == 0

    def test_feature_count_from_len_features(self):
        """feature_count derived from len(features) should also trigger anomaly."""
        ws = WorkspaceState({})
        ws.add_layer(name="input_layer", kind="vector", metadata={"feature_count": 2})
        result = FakeResult({"features": [{"type": "Feature"}] * 50})  # no explicit feature_count
        issues = run_postflight("test_tool", result, {
            "workspace": ws,
            "kwargs": {"input_ref": "input_layer"},
        })
        anomaly = [i for i in issues if i.code == "feature_count_anomaly"]
        assert len(anomaly) == 1


class TestPostflightInRunner:
    """Test postflight warnings injection through runner."""

    def test_run_with_preflight_injects_warnings(self):
        """run_with_preflight should inject postflight warnings into result.data."""
        from app.agents.preflight.runner import run_with_preflight

        class FakeHandler:
            """Fn that run_with_preflight calls as fn(*args, **kwargs)."""
            @staticmethod
            def call():
                from dataclasses import dataclass
                @dataclass
                class ToolResult:
                    data: dict
                return ToolResult(data={"features": []})

        result = run_with_preflight("test", "test_action", FakeHandler.call, (), {}, None)
        assert result.data.get("postflight_warnings") is not None
        assert len(result.data["postflight_warnings"]) >= 1
        assert "结果为空" in result.data["postflight_warnings"][0] or "empty" in result.data["postflight_warnings"][0].lower()

    def test_run_with_preflight_no_warnings_on_ok_result(self):
        """run_with_preflight should not inject warnings when result is OK."""
        from app.agents.preflight.runner import run_with_preflight

        class FakeHandler:
            @staticmethod
            def call():
                from dataclasses import dataclass
                @dataclass
                class ToolResult:
                    data: dict
                return ToolResult(data={"features": [{"type": "Feature"}]})

        result = run_with_preflight("test", "test_action", FakeHandler.call, (), {}, None)
        warnings = result.data.get("postflight_warnings", [])
        assert len(warnings) == 0
