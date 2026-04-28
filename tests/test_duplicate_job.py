from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from imagegen_server.app import create_app
from imagegen_server.storage import JobStore


class DuplicateJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.server_home = Path(self.temp_dir.name)
        self.database_path = self.server_home / "app.db"
        self.jobs_dir = self.server_home / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.database_path, self.jobs_dir)
        self.store.initialize()

    def create_job(
        self,
        *,
        prompt: str,
        image_action: str = "generate",
        input_files: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        job = self.store.create_job(
            prompt=prompt,
            image_action=image_action,
            model_override="gpt-5.5",
            tool_model_override="gpt-image-2",
            max_retries=4,
            retry_delay_seconds=90,
            input_files=input_files or [],
        )
        self.store.make_job_dirs(str(job["id"]))
        return job

    def read_events(self, job_id: str) -> list[dict[str, object]]:
        event_path = self.jobs_dir / job_id / "meta" / "events.log"
        if not event_path.exists():
            return []
        return [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def make_request(self, app, path: str) -> Request:
        return Request(
            {
                "type": "http",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
                "method": "POST",
                "path": path,
                "root_path": "",
                "headers": [],
                "app": app,
            }
        )

    def test_duplicate_job_creates_a_fresh_copy_for_any_status(self) -> None:
        source_statuses = (
            "queued",
            "running",
            "retry_waiting",
            "succeeded",
            "failed",
            "canceled",
        )

        for status in source_statuses:
            with self.subTest(status=status):
                source = self.create_job(prompt=f"job {status}")
                with self.store.connect() as connection:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = ?,
                            attempt_count = 2,
                            assigned_key_name = 'key-a',
                            started_at = '2026-04-28T10:00:00+00:00',
                            finished_at = '2026-04-28T10:10:00+00:00',
                            next_retry_at = '2026-04-28T10:20:00+00:00',
                            last_error = 'source error',
                            output_files_json = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            json.dumps(
                                [
                                    {
                                        "filename": "result.png",
                                        "kind": "output",
                                        "size_bytes": 123,
                                    }
                                ]
                            ),
                            source["id"],
                        ),
                    )

                duplicated = self.store.duplicate_job(str(source["id"]))
                source_after = self.store.get_job(str(source["id"]))

                self.assertNotEqual(duplicated["id"], source["id"])
                self.assertEqual(duplicated["status"], "queued")
                self.assertEqual(duplicated["prompt"], source["prompt"])
                self.assertEqual(duplicated["image_action"], source["image_action"])
                self.assertEqual(duplicated["model"], source["model"])
                self.assertEqual(duplicated["tool_model"], source["tool_model"])
                self.assertEqual(duplicated["max_retries"], source["max_retries"])
                self.assertEqual(
                    duplicated["retry_delay_seconds"],
                    source["retry_delay_seconds"],
                )
                self.assertEqual(duplicated["attempt_count"], 0)
                self.assertIsNone(duplicated["assigned_key_name"])
                self.assertIsNone(duplicated["started_at"])
                self.assertIsNone(duplicated["finished_at"])
                self.assertIsNone(duplicated["next_retry_at"])
                self.assertIsNone(duplicated["last_error"])
                self.assertEqual(duplicated["output_files"], [])
                self.assertEqual(source_after["status"], status)
                self.assertEqual(len(source_after["output_files"]), 1)

    def test_duplicate_job_reuses_shared_reference_inputs(self) -> None:
        image_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
        )
        source = self.store.create_job_with_reference_uploads(
            prompt="edit me again",
            image_action="edit",
            model_override="gpt-5.5",
            tool_model_override="gpt-image-2",
            max_retries=2,
            retry_delay_seconds=60,
            reference_uploads=[
                {
                    "content": image_bytes,
                    "suffix": ".png",
                    "original_filename": "seed.png",
                }
            ],
        )
        self.store.make_job_dirs(str(source["id"]))

        duplicated = self.store.duplicate_job(str(source["id"]))
        source_input = source["input_files"][0]
        duplicated_input = duplicated["input_files"][0]

        self.assertEqual(duplicated_input["filename"], source_input["filename"])
        self.assertEqual(
            duplicated_input["storage_path"],
            source_input["storage_path"],
        )
        self.assertEqual(
            duplicated_input["original_filename"],
            source_input["original_filename"],
        )
        self.assertEqual(
            self.store.resolve_job_file_path(
                str(duplicated["id"]),
                "input",
                str(duplicated_input["filename"]),
            ).read_bytes(),
            image_bytes,
        )
        shared_files = list((self.server_home / "shared" / "reference-images").iterdir())
        self.assertEqual(len(shared_files), 1)

    async def test_duplicate_endpoint_writes_fresh_request_meta_and_event_log(self) -> None:
        image_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
        )
        source = self.store.create_job_with_reference_uploads(
            prompt="duplicate through api",
            image_action="edit",
            model_override="gpt-5.5",
            tool_model_override="gpt-image-2",
            max_retries=1,
            retry_delay_seconds=45,
            reference_uploads=[
                {
                    "content": image_bytes,
                    "suffix": ".png",
                    "original_filename": "seed.png",
                }
            ],
        )
        self.store.make_job_dirs(str(source["id"]))

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
                        if getattr(route, "path", None) == "/jobs/{job_id}/duplicate"
                    )
                    response = await route.endpoint(
                        job_id=str(source["id"]),
                        request=self.make_request(app, f"/jobs/{source['id']}/duplicate"),
                    )

        duplicated_id = response.id
        request_meta = json.loads(
            (self.jobs_dir / duplicated_id / "meta" / "request.json").read_text(
                encoding="utf-8"
            )
        )
        events = self.read_events(duplicated_id)

        self.assertNotEqual(duplicated_id, source["id"])
        self.assertEqual(response.status, "queued")
        self.assertEqual(request_meta["prompt"], source["prompt"])
        self.assertEqual(request_meta["source_job_id"], source["id"])
        self.assertEqual(
            request_meta["reference_images"][0]["original_filename"],
            "seed.png",
        )
        self.assertEqual(events[-1]["message"], "job_duplicated")
        self.assertEqual(events[-1]["details"]["source_job_id"], source["id"])
