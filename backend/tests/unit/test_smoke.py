"""冒烟测试：验证项目骨架可导入、配置可加载。"""


def test_config_loads():
    from app.config import settings
    assert settings.APP_ENV in ("dev", "test")
    assert settings.APP_MAX_ITERATIONS == 10
    # A clean checkout must be importable without copying private secrets.
    # Runtime startup validation owns the non-empty credential requirement.
    assert isinstance(settings.LLM_API_KEY, str)
    assert isinstance(settings.AMAP_KEY, str)


def test_validate_config_reports_missing_required_secret(monkeypatch):
    from app.config import settings, validate_config

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    assert "LLM_API_KEY" in validate_config()


def test_config_cors_origins_list():
    from app.config import settings
    assert "http://localhost:5173" in settings.cors_origins_list


def test_models_import():
    from app.models.schemas import (
        ToolResult, PlannerOutput, JudgeDecision,
        POI, ChatRequest, UploadResponse, ErrorEnvelope,
    )
    # 验证 PlannerOutput code-mode 字段
    po = PlannerOutput(thinking="test", code="print('hello')")
    assert po.code == "print('hello')"


def test_main_app_creates():
    from app.main import app
    assert app.title == "Gismind"
    assert app.version == "1.6.0"


def test_health_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "checks" in data
    assert data["version"] == "1.6.0"
