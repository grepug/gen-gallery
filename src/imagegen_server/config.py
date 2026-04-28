from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ApiKeyConfig:
    name: str
    api_key: str
    transport: str
    base_url: Optional[str]
    model: Optional[str]
    tool_model: Optional[str]
    concurrency: int


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    server_home: Path
    database_path: Path
    jobs_dir: Path
    logs_dir: Path
    openai_base_url: str
    openai_model: str
    openai_image_tool_model: str
    api_keys: list[ApiKeyConfig]
    job_max_retries: int
    job_retry_delay_seconds: int
    job_timeout_seconds: int
    poll_interval_seconds: float


def _require_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}.")
    return value


def _require_str(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_api_keys(raw: Optional[str]) -> list[ApiKeyConfig]:
    if not raw:
        raise RuntimeError("IMAGE_API_KEYS_JSON is required.")

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("IMAGE_API_KEYS_JSON must be valid JSON.") from exc

    if not isinstance(decoded, list) or not decoded:
        raise RuntimeError("IMAGE_API_KEYS_JSON must be a non-empty JSON array.")

    api_keys: list[ApiKeyConfig] = []
    seen_names: set[str] = set()
    for index, item in enumerate(decoded):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"IMAGE_API_KEYS_JSON item {index} must be an object."
            )
        name = str(item.get("name", "")).strip()
        api_key = str(item.get("api_key", "")).strip()
        if not name:
            raise RuntimeError(f"IMAGE_API_KEYS_JSON item {index} is missing name.")
        if not api_key:
            raise RuntimeError(f"IMAGE_API_KEYS_JSON item {index} is missing api_key.")
        if name in seen_names:
            raise RuntimeError(f"Duplicate API key name: {name}")
        raw_transport = _optional_str(item.get("transport")) or "responses_http"
        if raw_transport not in {"responses_http", "openai_sdk"}:
            raise RuntimeError(
                f"IMAGE_API_KEYS_JSON item {index} has unsupported transport: {raw_transport}"
            )
        raw_concurrency = item.get("concurrency", 1)
        try:
            concurrency = int(raw_concurrency)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"IMAGE_API_KEYS_JSON item {index} concurrency must be an integer."
            ) from exc
        if concurrency < 1:
            raise RuntimeError(
                f"IMAGE_API_KEYS_JSON item {index} concurrency must be >= 1."
            )
        seen_names.add(name)
        api_keys.append(
            ApiKeyConfig(
                name=name,
                api_key=api_key,
                transport=raw_transport,
                base_url=_optional_str(item.get("base_url") or item.get("baseURL")),
                model=_optional_str(item.get("model")),
                tool_model=_optional_str(
                    item.get("tool_model") or item.get("toolModel")
                ),
                concurrency=concurrency,
            )
        )
    return api_keys


def load_settings() -> Settings:
    server_home = Path(
        os.environ.get("IMAGEGEN_SERVER_HOME", str(Path.home() / ".imagegen-server"))
    ).expanduser()
    database_path = server_home / "app.db"
    jobs_dir = server_home / "jobs"
    logs_dir = server_home / "logs"
    platform_port = os.environ.get("PORT", "").strip()
    app_port_raw = os.environ.get("APP_PORT", "").strip() or platform_port or "8000"
    app_host = os.environ.get("APP_HOST", "").strip()
    if not app_host:
        app_host = "0.0.0.0" if platform_port else "127.0.0.1"

    settings = Settings(
        app_host=app_host,
        app_port=_require_int("APP_PORT", int(app_port_raw), minimum=1),
        server_home=server_home,
        database_path=database_path,
        jobs_dir=jobs_dir,
        logs_dir=logs_dir,
        openai_base_url=_require_str("OPENAI_BASE_URL").rstrip("/"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5",
        openai_image_tool_model=os.environ.get(
            "OPENAI_IMAGE_TOOL_MODEL", "gpt-image-2"
        ),
        api_keys=_parse_api_keys(os.environ.get("IMAGE_API_KEYS_JSON")),
        job_max_retries=_require_int("JOB_MAX_RETRIES", 2, minimum=0),
        job_retry_delay_seconds=_require_int(
            "JOB_RETRY_DELAY_SECONDS", 60, minimum=1
        ),
        job_timeout_seconds=_require_int("JOB_TIMEOUT_SECONDS", 600, minimum=1),
        poll_interval_seconds=float(os.environ.get("JOB_POLL_INTERVAL_SECONDS", "1.0")),
    )

    settings.server_home.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings
