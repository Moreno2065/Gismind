"""Gismind 应用配置。基于 Pydantic Settings，环境变量 > .env > 默认值。"""

from functools import lru_cache
from pathlib import Path
import ctypes
import json
import os
import tempfile
from pydantic_settings import BaseSettings, SettingsConfigDict


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _nearest_existing(path: Path) -> Path:
    current = path.resolve()
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise ValueError(f"cannot locate existing path ancestor: {path}")
        current = parent
    return current


def volume_identity(path: Path) -> str:
    """Return a strict filesystem identity, or fail closed."""
    path = _nearest_existing(path)
    try:
        if os.name == "nt":
            get_volume_path = ctypes.windll.kernel32.GetVolumePathNameW
            get_volume_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            get_volume_path.restype = ctypes.c_bool
            buffer = ctypes.create_unicode_buffer(260)
            if not get_volume_path(str(path), buffer, len(buffer)):
                raise ValueError(f"GetVolumePathNameW failed for {path}")
            return buffer.value.rstrip("\\/").casefold()
        return f"st_dev:{path.stat().st_dev}"
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("unable to confirm filesystem volume") from exc


def same_volume(left: Path, right: Path) -> bool:
    """Strictly determine whether two paths are on the same filesystem volume."""
    return volume_identity(left) == volume_identity(right)


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
    LLM_MODEL: str = "deepseek-flash"
    LLM_TIMEOUT: int = 60
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.3

    # 高德（空字符串默认值避免导入期崩溃，启动校验由 validate_config() 负责）
    AMAP_KEY: str = ""
    AMAP_JS_KEY: str = ""
    AMAP_JS_SECURITY_CODE: str = ""
    AMAP_QPS_LIMIT: int = 5
    AMAP_TIMEOUT: int = 3
    AMAP_MAX_RETRY: int = 3

    # OSM
    OSM_ENDPOINT: str = "https://overpass-api.de/api/interpreter"
    OSM_BACKUP_ENDPOINTS: str = "https://overpass.kumi.systems/api/interpreter"
    OSM_TIMEOUT: int = 3
    OSM_MAX_RETRY: int = 1

    # 应用
    APP_ENV: str = "dev"
    APP_DEBUG: bool = False
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"
    APP_SANDBOX_ENABLED: bool = True
    APP_SANDBOX_TIMEOUT_S: int = 60
    APP_SANDBOX_MEMORY_MB: int = 512
    APP_SANDBOX_NETWORK_ALLOWLIST: str = ""   # 逗号分隔 host:port，空=全部 deny

    APP_PROJECT_ROOT: str = str(_REPO_ROOT)
    APP_DATA_ROOT: str = str(_REPO_ROOT / ".gismind")
    APP_RUNTIME_DB: str = "runtime.db"
    APP_ARTIFACT_DIR: str = "artifacts"
    APP_STAGING_DIR: str = "staging"
    APP_QUARANTINE_DIR: str = "quarantine"
    APP_UPLOAD_DIR: str = "uploads"
    APP_ARTIFACT_GC_GRACE_S: int = 86400

    APP_MAX_LLM_RETRIES: int = 3
    # The Runtime planner is a constrained JSON compiler front-end, not a
    # general reasoning/chat endpoint.  Keep its completion budget small and
    # independently configurable so a verbose model cannot hold a planning
    # lease for the generic LLM maximum.
    APP_PLANNER_MAX_TOKENS: int = 1536
    APP_PLANNER_TIMEOUT_S: int = 45
    APP_PLANNER_TOTAL_TIMEOUT_S: float = 90.0
    APP_PLANNER_MAX_HTTP_CALLS: int = 6

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
    def runtime_paths(self) -> dict[str, str]:
        project_root = Path(self.APP_PROJECT_ROOT).expanduser()
        if not project_root.is_absolute():
            project_root = (Path.cwd() / project_root).resolve()
        else:
            project_root = project_root.resolve()

        def resolve(value: str, parent: Path) -> Path:
            path = Path(value).expanduser()
            return (parent / path).resolve() if not path.is_absolute() else path.resolve()

        data_root = resolve(self.APP_DATA_ROOT, project_root)
        runtime_db = resolve(self.APP_RUNTIME_DB, data_root)
        artifact_dir = resolve(self.APP_ARTIFACT_DIR, data_root)
        staging_dir = resolve(self.APP_STAGING_DIR, data_root)
        quarantine_dir = resolve(self.APP_QUARANTINE_DIR, data_root)
        upload_dir = resolve(self.APP_UPLOAD_DIR, data_root)

        def inside(path: Path, parent: Path) -> bool:
            try:
                path.relative_to(parent)
                return True
            except ValueError:
                return False

        if not inside(data_root, project_root):
            raise ValueError("APP_DATA_ROOT must be within APP_PROJECT_ROOT")
        for path in (runtime_db, artifact_dir, staging_dir, quarantine_dir, upload_dir):
            if not inside(path, data_root):
                raise ValueError("runtime paths must stay within APP_DATA_ROOT")
        if not same_volume(artifact_dir, staging_dir):
            raise ValueError("artifact and staging directories must share a volume")
        paths = {
            "project_root": str(project_root),
            "data_root": str(data_root),
            "runtime_db": str(runtime_db),
            "artifact_dir": str(artifact_dir),
            "staging_dir": str(staging_dir),
            "quarantine_dir": str(quarantine_dir),
            "upload_dir": str(upload_dir),
            "artifact_volume": volume_identity(artifact_dir),
            "staging_volume": volume_identity(staging_dir),
        }
        data_root.mkdir(parents=True, exist_ok=True)
        manifest_path = data_root / "runtime_paths.json"
        fd, temp_name = tempfile.mkstemp(prefix=".runtime_paths.", suffix=".tmp", dir=str(data_root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(paths, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, manifest_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return paths

    resolved_runtime_paths = runtime_paths


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


REQUIRED_KEYS = [
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "AMAP_KEY", "AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE",
]


def validate_config() -> list[str]:
    """启动校验，返回缺失的必填项列表。"""
    missing = []
    for key in REQUIRED_KEYS:
        value = getattr(settings, key, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing
