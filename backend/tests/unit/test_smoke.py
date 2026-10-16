"""冒烟测试：验证项目骨架可导入、配置可加载。"""

import pytest


def test_config_loads(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "test")
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "AMAP_KEY"):
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert settings.APP_ENV == "test"
    assert settings.LLM_API_KEY == settings.LLM_BASE_URL == settings.AMAP_KEY == ""
    assert isinstance(settings.APP_DEBUG, bool)
    assert isinstance(settings.APP_PORT, int) and 0 < settings.APP_PORT < 65536
    assert isinstance(settings.LLM_TIMEOUT, int) and settings.LLM_TIMEOUT > 0
    assert isinstance(settings.LLM_MAX_TOKENS, int) and settings.LLM_MAX_TOKENS > 0
    assert isinstance(settings.LLM_MODEL, str) and settings.LLM_MODEL


@pytest.mark.parametrize("api_key", ["", " \t"])
def test_unconfigured_planner_fails_before_opening_provider_transport(monkeypatch, api_key):
    from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerError

    def forbidden_transport(*args, **kwargs):
        raise AssertionError("Missing credentials must not open a provider connection")

    monkeypatch.setattr("app.runtime.llm_planner.httpx.AsyncClient", forbidden_transport)
    planner = OpenAICompatiblePlanner(api_key=api_key, base_url="https://provider.invalid/v1",
        model="deepseek-flash", timeout_seconds=1, max_tokens=10, max_retries=0)
    with pytest.raises(PlannerError) as error:
        planner._call([{"role": "user", "content": "JSON"}])
    assert error.value.code == "LLM_NOT_CONFIGURED"


def test_config_cors_origins_list():
    from app.config import settings
    assert "http://localhost:5173" in settings.cors_origins_list


def test_main_app_creates():
    from app.main import app
    assert app.title == "Gismind"
    assert app.version == "2.0.0"


def test_health_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "checks" in data
    assert data["version"] == "2.0.0"
