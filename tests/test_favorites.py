from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from imagegen_server.app import create_app
from imagegen_server.storage import FAVORITE_TAG, JobStore, job_to_response


class FavoriteStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.server_home = Path(self.temp_dir.name)
        self.database_path = self.server_home / "app.db"
        self.jobs_dir = self.server_home / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.database_path, self.jobs_dir)
        self.store.initialize()

    def create_job(self, *, prompt: str, status: str = "queued") -> dict[str, object]:
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
        if status != "queued":
            with self.store.connect() as connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        updated_at = '2026-04-28T00:00:00+00:00',
                        finished_at = CASE
                            WHEN ? = 'succeeded' THEN '2026-04-28T00:00:00+00:00'
                            ELSE NULL
                        END
                    WHERE id = ?
                    """,
                    (status, status, job["id"]),
                )
        return self.store.get_job(str(job["id"]))

    def test_favorites_filter_counts_and_serialization(self) -> None:
        favorite_job = self.create_job(prompt="favorite me", status="succeeded")
        self.create_job(prompt="not favorite", status="succeeded")
        self.create_job(prompt="failed", status="failed")

        updated = self.store.set_favorite(str(favorite_job["id"]), is_favorite=True)
        response = job_to_response(updated, "https://example.com")

        self.assertEqual(updated["tags"], [FAVORITE_TAG])
        self.assertEqual(response.tags, [FAVORITE_TAG])
        self.assertTrue(response.is_favorite)

        jobs, total, counts = self.store.list_jobs(
            limit=10,
            offset=0,
            status_filter="favorites",
            sort_field="created_at",
            sort_direction="DESC",
        )
        self.assertEqual(total, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], favorite_job["id"])
        self.assertEqual(counts["favorites"], 1)
        self.assertEqual(counts["succeeded"], 2)

    def test_cannot_favorite_non_succeeded_job(self) -> None:
        failed_job = self.create_job(prompt="nope", status="failed")

        with self.assertRaisesRegex(ValueError, "only succeeded jobs can be favorited"):
            self.store.set_favorite(str(failed_job["id"]), is_favorite=True)


class FavoriteApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.server_home = Path(self.temp_dir.name)

    def make_request(
        self,
        path: str,
        *,
        method: str = "GET",
        query_string: bytes = b"",
    ) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "query_string": query_string,
                "headers": [],
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
            }
        )

    def create_app_with_env(self):
        env = {
            "IMAGEGEN_SERVER_HOME": str(self.server_home),
            "IMAGE_API_KEYS_JSON": '[{"name":"key-a","api_key":"sk-test"}]',
            "OPENAI_BASE_URL": "https://api.example.com/v1",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.start_patch = patch(
            "imagegen_server.app.WorkerPool.start",
            new_callable=AsyncMock,
        )
        self.stop_patch = patch(
            "imagegen_server.app.WorkerPool.stop",
            new_callable=AsyncMock,
        )
        self.env_patch.start()
        self.start_patch.start()
        self.stop_patch.start()
        self.addCleanup(self.stop_patch.stop)
        self.addCleanup(self.start_patch.stop)
        self.addCleanup(self.env_patch.stop)
        return create_app()

    def seed_job(self, store: JobStore, *, prompt: str, status: str) -> dict[str, object]:
        job = store.create_job(
            prompt=prompt,
            image_action="generate",
            model_override=None,
            tool_model_override=None,
            max_retries=2,
            retry_delay_seconds=60,
            input_files=[],
        )
        store.make_job_dirs(str(job["id"]))
        with store.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    updated_at = '2026-04-28T01:00:00+00:00',
                    finished_at = CASE
                        WHEN ? = 'succeeded' THEN '2026-04-28T01:00:00+00:00'
                        ELSE NULL
                    END,
                    output_files_json = ?
                WHERE id = ?
                """,
                (
                    status,
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
                    job["id"],
                ),
            )
        return store.get_job(str(job["id"]))

    async def test_favorite_endpoints_and_favorites_filter(self) -> None:
        app = self.create_app_with_env()
        store = app.state.store
        succeeded = self.seed_job(store, prompt="fav me", status="succeeded")
        failed = self.seed_job(store, prompt="no fav", status="failed")

        favorite_route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/jobs/{job_id}/favorite"
            and "POST" in getattr(route, "methods", set())
        )
        unfavorite_route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/jobs/{job_id}/favorite"
            and "DELETE" in getattr(route, "methods", set())
        )
        list_route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/jobs"
            and "GET" in getattr(route, "methods", set())
        )

        favorite_response = await favorite_route.endpoint(
            job_id=str(succeeded["id"]),
            request=self.make_request(
                f"/jobs/{succeeded['id']}/favorite",
                method="POST",
            ),
        )
        self.assertEqual(favorite_response.tags, [FAVORITE_TAG])
        self.assertTrue(favorite_response.is_favorite)

        listing_response = await list_route.endpoint(
            request=self.make_request(
                "/jobs",
                query_string=b"status=favorites",
            ),
            limit=20,
            offset=0,
            status="favorites",
            sort="created_desc",
        )
        self.assertEqual(listing_response.total, 1)
        self.assertEqual(listing_response.counts.favorites, 1)
        self.assertEqual(listing_response.items[0].id, succeeded["id"])

        with self.assertRaises(HTTPException) as rejected:
            await favorite_route.endpoint(
                job_id=str(failed["id"]),
                request=self.make_request(
                    f"/jobs/{failed['id']}/favorite",
                    method="POST",
                ),
            )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(
            rejected.exception.detail,
            "only succeeded jobs can be favorited",
        )

        unfavorite_response = await unfavorite_route.endpoint(
            job_id=str(succeeded["id"]),
            request=self.make_request(
                f"/jobs/{succeeded['id']}/favorite",
                method="DELETE",
            ),
        )
        self.assertEqual(unfavorite_response.tags, [])
        self.assertFalse(unfavorite_response.is_favorite)
