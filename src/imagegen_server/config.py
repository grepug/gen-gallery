from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ApiKeyConfig:
    name: str
    api_key: str


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


def _load_codex_provider_defaults(config_path: Path) -> tuple[str, str]:
    text = config_path.read_text(encoding="utf-8")

    provider_match = re.search(r'^model_provider\s*=\s*"([^"]+)"', text, re.M)
    model_match = re.search(r'^model\s*=\s*"([^"]+)"', text, re.M)
    provider_key = provider_match.group(1) if provider_match else "OpenAI"
    model = model_match.group(1) if model_match else "gpt-5.4"

    block_pattern = (
        r'^\[model_providers\.%s\]\n(.*?)(?:\n\[|\Z)' % re.escape(provider_key)
    )
    provider_block = re.search(block_pattern, text, re.M | re.S)
    if not provider_block:
        raise RuntimeError(f"Model provider block not found for '{provider_key}'.")

    base_url_match = re.search(
        r'^base_url\s*=\s*"([^"]+)"',
        provider_block.group(1),
        re.M,
    )
    if not base_url_match:
        raise RuntimeError(f"base_url not found for model provider '{provider_key}'.")

    return model, base_url_match.group(1).rstrip("/")


def _require_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}.")
    return value


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
        seen_names.add(name)
        api_keys.append(ApiKeyConfig(name=name, api_key=api_key))
    return api_keys


def load_settings() -> Settings:
    codex_config_path = Path(
        os.environ.get("CODEX_CONFIG_PATH", str(Path.home() / ".codex" / "config.toml"))
    ).expanduser()
    default_model, default_base_url = _load_codex_provider_defaults(codex_config_path)
    server_home = Path(
        os.environ.get("IMAGEGEN_SERVER_HOME", str(Path.home() / ".imagegen-server"))
    ).expanduser()
    database_path = server_home / "app.db"
    jobs_dir = server_home / "jobs"
    logs_dir = server_home / "logs"

    settings = Settings(
        app_host=os.environ.get("APP_HOST", "127.0.0.1"),
        app_port=_require_int("APP_PORT", 8000, minimum=1),
        server_home=server_home,
        database_path=database_path,
        jobs_dir=jobs_dir,
        logs_dir=logs_dir,
        openai_base_url=os.environ.get("OPENAI_BASE_URL", default_base_url).rstrip("/"),
        openai_model=os.environ.get("OPENAI_MODEL", default_model),
        openai_image_tool_model=os.environ.get(
            "OPENAI_IMAGE_TOOL_MODEL", "gpt-image-2"
        ),
        api_keys=_parse_api_keys(os.environ.get("IMAGE_API_KEYS_JSON")),
        job_max_retries=_require_int("JOB_MAX_RETRIES", 1, minimum=0),
        job_retry_delay_seconds=_require_int(
            "JOB_RETRY_DELAY_SECONDS", 15, minimum=1
        ),
        job_timeout_seconds=_require_int("JOB_TIMEOUT_SECONDS", 600, minimum=1),
        poll_interval_seconds=float(os.environ.get("JOB_POLL_INTERVAL_SECONDS", "1.0")),
    )

    settings.server_home.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings
