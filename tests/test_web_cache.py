from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from imagegen_server.app import create_app
from imagegen_server.storage import JobStore, job_to_response


class WebCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.server_home = Path(self.temp_dir.name)
        self.database_path = self.server_home / "app.db"
        self.jobs_dir = self.server_home / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.database_path, self.jobs_dir)
        self.store.initialize()

    def test_job_file_urls_include_version_token(self) -> None:
        job = self.store.create_job(
            prompt="cache me",
            image_action="generate",
            model_override=None,
            tool_model_override=None,
            max_retries=2,
            retry_delay_seconds=60,
            input_files=[],
        )
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    finished_at = '2026-04-27T12:00:00+00:00',
                    updated_at = '2026-04-27T12:00:00+00:00',
                    output_files_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        [
                            {
                                "filename": "result.png",
                                "kind": "output",
                                "size_bytes": 123,
                            }
                        ]
                    ),
                    job["id"],
                ),
            )
        response = job_to_response(self.store.get_job(str(job["id"])), "https://example.com")
        self.assertEqual(
            response.output_files[0].url,
            f"https://example.com/files/{job['id']}/output/result.png?v=2026-04-27T12:00:00+00:00",
        )

    async def test_file_endpoint_sets_browser_cache_headers(self) -> None:
        job = self.store.create_job(
            prompt="cache file",
            image_action="generate",
            model_override=None,
            tool_model_override=None,
            max_retries=2,
            retry_delay_seconds=60,
            input_files=[],
        )
        output_dir = self.jobs_dir / str(job["id"]) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "result.png").write_bytes(b"png")

        env = {
            "IMAGEGEN_SERVER_HOME": str(self.server_home),
            "IMAGE_API_KEYS_JSON": '[{"name":"key-a","api_key":"sk-test"}]',
            "OPENAI_BASE_URL": "https://api.example.com/v1",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("imagegen_server.app.WorkerPool.start", new_callable=AsyncMock):
                with patch("imagegen_server.app.WorkerPool.stop", new_callable=AsyncMock):
                    app = create_app()
                    route = next(
                        route
                        for route in app.router.routes
                        if getattr(route, "path", None) == "/files/{job_id}/{kind}/{filename}"
                    )
                    response = await route.endpoint(
                        job_id=str(job["id"]),
                        kind="output",
                        filename="result.png",
                    )

        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )
