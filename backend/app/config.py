"""Gismind 应用配置。基于 Pydantic Settings，环境变量 > .env > 默认值。"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM（空字符串默认值避免导入期崩溃，启动校验由 validate_config() 负责）
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "k2.7-code"
    LLM_TIMEOUT: int = 60
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.3

    # 高德（空字符串默认值避免导入期崩溃，启动校验由 validate_config() 负责）
    AMAP_KEY: str = ""
    AMAP_JS_KEY: str = ""
    AMAP_JS_SECURITY_CODE: str = ""
    AMAP_QPS_LIMIT: int = 5
    # Public GIS providers often complete just over three seconds on a
    # developer connection.  Five seconds still bounds a single-provider
    # request while avoiding a false "unavailable" result for valid replies.
    AMAP_TIMEOUT: int = 5
    AMAP_MAX_RETRY: int = 3

    # OSM
    OSM_ENDPOINT: str = "https://overpass-api.de/api/interpreter"
    OSM_BACKUP_ENDPOINTS: str = "https://overpass.kumi.systems/api/interpreter"
    OSM_TIMEOUT: int = 5
    OSM_MAX_RETRY: int = 1

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_POI: int = 86400
    CACHE_TTL_OSM: int = 172800
    CACHE_TTL_GEOCODE: int = 604800

    # Celery
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # 应用
    APP_ENV: str = "dev"
    APP_DEBUG: bool = False
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_MAX_ITERATIONS: int = 10
    APP_CONTEXT_WINDOW: int = 5000
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"
    APP_CHECKPOINT_DB: str = ".gismind/checkpoints.db"
    APP_INLINE_TIMEOUT_S: int = 30
    APP_ROOT_MAX_ITERATIONS: int = 30
    APP_MAX_COST_TOKENS: int = 100000
    APP_SANDBOX_ENABLED: bool = True
    APP_SANDBOX_TIMEOUT_S: int = 60
    APP_SANDBOX_MEMORY_MB: int = 512
    APP_SANDBOX_NETWORK_ALLOWLIST: str = ""   # 逗号分隔 host:port，空=全部 deny

    APP_WORKSPACE_DIR: str = "./workspace"    # 工作区输出目录（路径白名单根目录）

    APP_MAX_LLM_RETRIES: int = 3

    # 上传
    UPLOAD_MAX_SIZE: int = 50
    UPLOAD_ZIP_MAX_TOTAL_SIZE: int = 500
    UPLOAD_ZIP_MAX_FILE_COUNT: int = 100
    UPLOAD_TTL_S: int = 86400

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def celery_broker_url_resolved(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


REQUIRED_KEYS = [
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "AMAP_KEY", "AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE",
    "REDIS_URL",
]


def validate_config() -> list[str]:
    """启动校验，返回缺失的必填项列表。"""
    missing = []
    for key in REQUIRED_KEYS:
        value = getattr(settings, key, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing
