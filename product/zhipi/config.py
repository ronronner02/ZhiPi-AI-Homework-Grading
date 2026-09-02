from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_once() -> Path | None:
    """只读取本重构目录下的 .env，且绝不覆盖进程环境变量。"""
    if os.environ.get("ZHIPI_DOCKER") == "1":
        return None
    path = ROOT_DIR / ".env"
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("ZHIPI_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


_DOTENV_SOURCE = _load_dotenv_once()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    host: str
    port: int
    workspace_id: str
    data_dir: Path
    database_path: Path
    files_dir: Path
    access_code: str
    secret_key: str
    secure_cookie: bool
    max_body_bytes: int
    max_image_bytes: int
    max_image_pixels: int
    vlm_api_key: str
    vlm_base_url: str
    vlm_model: str
    vlm_timeout: int
    vlm_daily_limit: int
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout: int
    llm_daily_limit: int

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def vlm_enabled(self) -> bool:
        return bool(self.vlm_api_key and self.vlm_base_url and self.vlm_model)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls) -> "Settings":
        data_raw = str(os.environ.get("ZHIPI_DATA_DIR", "")).strip()
        data_dir = Path(data_raw).expanduser() if data_raw else ROOT_DIR / "runtime"
        settings = cls(
            environment=str(os.environ.get("ZHIPI_ENV", "development")).strip().lower(),
            host=str(os.environ.get("ZHIPI_HOST", "127.0.0.1")).strip(),
            port=_env_int("ZHIPI_PORT", 8010, 1, 65535),
            workspace_id=str(os.environ.get("ZHIPI_WORKSPACE_ID", "default")).strip() or "default",
            data_dir=data_dir.resolve(),
            database_path=(data_dir / "zhipi.sqlite3").resolve(),
            files_dir=(data_dir / "files").resolve(),
            access_code=str(os.environ.get("ZHIPI_ACCESS_CODE", "")).strip(),
            secret_key=str(os.environ.get("ZHIPI_SECRET_KEY", "")).strip(),
            secure_cookie=_env_bool("ZHIPI_SECURE_COOKIE"),
            max_body_bytes=_env_int("ZHIPI_MAX_BODY_MB", 16, 1, 64) * 1024 * 1024,
            max_image_bytes=_env_int("ZHIPI_MAX_IMAGE_MB", 8, 1, 32) * 1024 * 1024,
            max_image_pixels=_env_int(
                "ZHIPI_MAX_IMAGE_PIXELS", 40_000_000, 1_000_000, 100_000_000
            ),
            vlm_api_key=str(os.environ.get("ZHIPI_VLM_API_KEY", "")).strip(),
            vlm_base_url=str(os.environ.get("ZHIPI_VLM_BASE_URL", "")).strip(),
            vlm_model=str(os.environ.get("ZHIPI_VLM_MODEL", "")).strip(),
            vlm_timeout=_env_int("ZHIPI_VLM_TIMEOUT", 90, 5, 600),
            vlm_daily_limit=_env_int("ZHIPI_VLM_DAILY_LIMIT", 0, 0, 1_000_000),
            llm_api_key=str(os.environ.get("ZHIPI_LLM_API_KEY", "")).strip(),
            llm_base_url=str(os.environ.get("ZHIPI_LLM_BASE_URL", "")).strip(),
            llm_model=str(os.environ.get("ZHIPI_LLM_MODEL", "")).strip(),
            llm_timeout=_env_int("ZHIPI_LLM_TIMEOUT", 90, 5, 600),
            llm_daily_limit=_env_int("ZHIPI_LLM_DAILY_LIMIT", 0, 0, 1_000_000),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment not in {"development", "production", "test"}:
            raise ValueError("ZHIPI_ENV 只能是 development / production / test")
        if len(self.workspace_id) > 64 or not all(
            ch.isalnum() or ch in "_-" for ch in self.workspace_id
        ):
            raise ValueError("ZHIPI_WORKSPACE_ID 仅允许 1-64 位字母、数字、下划线或连字符")
        llm_fields = (self.llm_api_key, self.llm_base_url, self.llm_model)
        if any(llm_fields) and not all(llm_fields):
            raise ValueError("真实批改必须同时配置 ZHIPI_LLM_API_KEY/BASE_URL/MODEL")
        vlm_fields = (self.vlm_api_key, self.vlm_base_url, self.vlm_model)
        if any(vlm_fields) and not all(vlm_fields):
            raise ValueError("图片识别必须同时配置 ZHIPI_VLM_API_KEY/BASE_URL/MODEL")
        if self.is_production:
            if not self.access_code:
                raise ValueError("生产环境必须设置 ZHIPI_ACCESS_CODE")
            if len(self.secret_key) < 32 or self.secret_key == "dev-only-change-me":
                raise ValueError("生产环境 ZHIPI_SECRET_KEY 至少 32 位且不能使用开发默认值")
            if self.vlm_enabled and self.vlm_daily_limit <= 0:
                raise ValueError("生产环境启用 VLM 时必须设置 ZHIPI_VLM_DAILY_LIMIT")
            if self.llm_enabled and self.llm_daily_limit <= 0:
                raise ValueError("生产环境启用 LLM 时必须设置 ZHIPI_LLM_DAILY_LIMIT")

    @classmethod
    def for_test(cls, root: Path, **overrides) -> "Settings":
        root = root.resolve()
        values = {
            "environment": "test",
            "host": "127.0.0.1",
            "port": 8010,
            "workspace_id": "test",
            "data_dir": root,
            "database_path": root / "zhipi.sqlite3",
            "files_dir": root / "files",
            "access_code": "",
            "secret_key": "test-secret-key-not-for-production",
            "secure_cookie": False,
            "max_body_bytes": 16 * 1024 * 1024,
            "max_image_bytes": 8 * 1024 * 1024,
            "max_image_pixels": 40_000_000,
            "vlm_api_key": "",
            "vlm_base_url": "",
            "vlm_model": "",
            "vlm_timeout": 10,
            "vlm_daily_limit": 0,
            "llm_api_key": "",
            "llm_base_url": "",
            "llm_model": "",
            "llm_timeout": 10,
            "llm_daily_limit": 0,
        }
        values.update(overrides)
        result = cls(**values)
        result.validate()
        return result
