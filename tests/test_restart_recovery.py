from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from imagegen_server.app import create_app
from imagegen_server.storage import JobStore


class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.server_home = Path(self.temp_dir.name)
        self.database_path = self.server_home / "app.db"
        self.jobs_dir = self.server_home / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.database_path, self.jobs_dir)
        self.store.initialize()

    def create_job(self, *, prompt: str = "test prompt") -> dict[str, object]:
        job = self.store.create_job(
            prompt=prompt,
            image_action="generate",
            model_override=None,
            tool_model_override=None,
            max_retries=2,
            retry_delay_seconds=60,
            input_files=[],
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

    def test_requeue_interrupted_jobs_requeues_running_without_burning_retry(self) -> None:
        running_job = self.create_job(prompt="running")
        waiting_job = self.create_job(prompt="waiting")

        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempt_count = 1,
                    assigned_key_name = 'key-a',
                    started_at = '2026-04-27T10:00:00+00:00'
                WHERE id = ?
                """,
                (running_job["id"],),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'retry_waiting',
                    attempt_count = 2,
                    assigned_key_name = 'key-b',
                    next_retry_at = '2026-04-27T10:10:00+00:00'
                WHERE id = ?
                """,
                (waiting_job["id"],),
            )

        recovered_job_ids = self.store.requeue_interrupted_jobs()

        self.assertEqual(recovered_job_ids, [running_job["id"]])

        recovered = self.store.get_job(str(running_job["id"]))
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["attempt_count"], 0)
        self.assertIsNone(recovered["assigned_key_name"])
        self.assertIsNone(recovered["started_at"])
        self.assertIsNone(recovered["next_retry_at"])
        self.assertEqual(recovered["last_error"], "Interrupted by server restart.")

        waiting = self.store.get_job(str(waiting_job["id"]))
        self.assertEqual(waiting["status"], "retry_waiting")
        self.assertEqual(waiting["attempt_count"], 2)
        self.assertEqual(waiting["assigned_key_name"], "key-b")

        events = self.read_events(str(running_job["id"]))
        self.assertTrue(events)
        self.assertEqual(events[-1]["message"], "attempt_interrupted_by_restart")
        self.assertEqual(
            events[-1]["details"]["reason"],
            "Interrupted by server restart.",
        )

    async def test_app_startup_recovers_running_jobs_before_starting_workers(self) -> None:
        running_job = self.create_job(prompt="startup recovery")
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempt_count = 1,
                    assigned_key_name = 'key-a',
                    started_at = '2026-04-27T10:00:00+00:00'
                WHERE id = ?
                """,
                (running_job["id"],),
            )

        env = {
            "IMAGEGEN_SERVER_HOME": str(self.server_home),
            "IMAGE_API_KEYS_JSON": '[{"name":"key-a","api_key":"sk-test"}]',
            "OPENAI_BASE_URL": "https://api.example.com/v1",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("imagegen_server.app.WorkerPool.start", new_callable=AsyncMock) as start_mock:
                with patch("imagegen_server.app.WorkerPool.stop", new_callable=AsyncMock):
                    app = create_app()
                    await app.router.startup()
                    recovered = app.state.store.get_job(str(running_job["id"]))
                    self.assertEqual(recovered["status"], "queued")
                    self.assertEqual(recovered["attempt_count"], 0)
                    start_mock.assert_awaited_once()
                    await app.router.shutdown()
