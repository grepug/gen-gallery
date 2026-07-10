#!/usr/bin/env python3
"""Generate an image through an OpenAI-compatible Images API."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".my-image-gen"
CONFIG_PATH = CONFIG_DIR / "config.json"
IMAGES_DIR = CONFIG_DIR / "images"
DEFAULT_MODEL = "gpt-image-2"


def today_string() -> str:
    return date.today().strftime("%Y%m%d")


def _config_paths() -> tuple[Path, Path]:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    root = home / ".my-image-gen"
    return root / "config.json", root / "images"


def load_or_create_config() -> dict[str, str]:
    config_path, _ = _config_paths()
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取配置文件 {config_path}: {exc}") from exc
        if all(isinstance(config.get(key), str) and config[key].strip() for key in ("api_key", "base_url")):
            return {
                "api_key": config["api_key"].strip(),
                "base_url": config["base_url"].strip().rstrip("/"),
                "model": str(config.get("model") or DEFAULT_MODEL).strip(),
            }

    print("首次使用，请配置图片 API。")
    api_key = getpass.getpass("API Key: ").strip()
    base_url = input("Base URL: ").strip().rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("API Key 和 Base URL 都不能为空。")
    config = {"api_key": api_key, "base_url": base_url, "model": DEFAULT_MODEL}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    return config


def _slugify(prompt: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", prompt).strip("-").lower()
    return (slug[:60].rstrip("-") or "image")


def _image_from_response(response: dict[str, Any]) -> tuple[bytes, str]:
    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError("API 响应中没有图片数据。")
    item = data[0]
    if item.get("b64_json"):
        try:
            return base64.b64decode(item["b64_json"], validate=True), ""
        except (ValueError, TypeError) as exc:
            raise RuntimeError("API 返回的 b64_json 无效。") from exc
    if item.get("url"):
        try:
            with urllib.request.urlopen(str(item["url"]), timeout=600) as response_file:
                return response_file.read(), str(item["url"])
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"无法下载图片 URL: {exc}") from exc
    raise RuntimeError("API 响应中没有 b64_json 或 url 图片数据。")


def _extension(image_bytes: bytes, source_url: str = "") -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    suffix = Path(source_url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else ".bin"


def save_image(response: dict[str, Any], prompt: str) -> Path:
    image_bytes, source_url = _image_from_response(response)
    _, images_dir = _config_paths()
    images_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{today_string()}-{_slugify(prompt)}"
    suffix = _extension(image_bytes, source_url)
    output = images_dir / f"{stem}{suffix}"
    counter = 2
    while output.exists():
        output = images_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    output.write_bytes(image_bytes)
    return output


def request_image(config: dict[str, str], prompt: str) -> dict[str, Any]:
    payload = {
        "model": config["model"],
        "prompt": prompt,
        "response_format": "b64_json",
    }
    request = urllib.request.Request(
        config["base_url"] + "/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"图片 API 请求失败（HTTP {exc.code}）：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"图片 API 请求超时或网络失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("图片 API 返回的不是有效 JSON。") from exc
    if not isinstance(body, dict):
        raise RuntimeError("图片 API 返回格式不正确。")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an image with an OpenAI-compatible Images API.")
    parser.add_argument("prompt", help="Image generation prompt")
    args = parser.parse_args()
    try:
        config = load_or_create_config()
        output = save_image(request_image(config, args.prompt), args.prompt)
    except (KeyboardInterrupt, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已保存：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
