from __future__ import annotations

import uvicorn

from .app import create_app
from .config import load_settings


def run() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(),
        host=settings.app_host,
        port=settings.app_port,
        log_level="info",
    )
