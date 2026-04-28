from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from imagegen_server.config import load_settings
from imagegen_server.openai_client import OpenAIImageResult, generate_image
from imagegen_server.storage import JobStore


class ProviderRoutingTests(unittest.TestCase):
    def test_load_settings_supports_legacy_and_sdk_key_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "IMAGEGEN_SERVER_HOME": temp_dir,
                "IMAGE_API_KEYS_JSON": """
                [
                  {"name":"legacy-a","api_key":"sk-a"},
                  {"name":"legacy-b","api_key":"sk-b","concurrency":1},
                  {
                    "name":"sdk-c",
                    "api_key":"sk-c",
                    "transport":"openai_sdk",
                    "baseURL":"https://lingsuan.nmyh.cc/v1",
                    "toolModel":"gpt-image-2",
                    "concurrency":5
                  }
                ]
                """,
                "OPENAI_BASE_URL": "https://api.example.com/v1",
            }
            with patch.dict(os.environ, env, clear=False):
                settings = load_settings()

        self.assertEqual(len(settings.api_keys), 3)
        legacy_key = settings.api_keys[0]
        sdk_key = settings.api_keys[2]

        self.assertEqual(legacy_key.transport, "responses_http")
        self.assertIsNone(legacy_key.base_url)
        self.assertIsNone(legacy_key.model)
        self.assertIsNone(legacy_key.tool_model)
        self.assertEqual(legacy_key.concurrency, 1)

        self.assertEqual(sdk_key.transport, "openai_sdk")
        self.assertEqual(sdk_key.base_url, "https://lingsuan.nmyh.cc/v1")
        self.assertEqual(sdk_key.tool_model, "gpt-image-2")
        self.assertEqual(sdk_key.concurrency, 5)

    def test_load_settings_strips_trailing_slash_from_per_key_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "IMAGEGEN_SERVER_HOME": temp_dir,
                "IMAGE_API_KEYS_JSON": """
                [
                  {
                    "name":"sdk-c",
                    "api_key":"sk-c",
                    "transport":"openai_sdk",
                    "base_url":"https://lingsuan.nmyh.cc/v1/"
                  }
                ]
                """,
                "OPENAI_BASE_URL": "https://api.example.com/v1",
            }
            with patch.dict(os.environ, env, clear=False):
                settings = load_settings()

        self.assertEqual(settings.api_keys[0].base_url, "https://lingsuan.nmyh.cc/v1")

    def test_claim_next_job_respects_key_capacity_and_round_robin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server_home = Path(temp_dir)
            jobs_dir = server_home / "jobs"
            store = JobStore(server_home / "app.db", jobs_dir)
            store.initialize()

            created_job_ids: list[str] = []
            for index in range(4):
                job = store.create_job(
                    prompt=f"job-{index}",
                    image_action="generate",
                    model_override=None,
                    tool_model_override=None,
                    max_retries=0,
                    retry_delay_seconds=60,
                    input_files=[],
                )
                created_job_ids.append(str(job["id"]))

            key_order = ["sdk-c", "legacy-a"]
            key_capacities = {"sdk-c": 2, "legacy-a": 1}

            first_sdk_job = store.claim_next_job("sdk-c", key_order, key_capacities)
            self.assertIsNotNone(first_sdk_job)
            self.assertEqual(str(first_sdk_job["id"]), created_job_ids[0])

            blocked_sdk_job = store.claim_next_job("sdk-c", key_order, key_capacities)
            self.assertIsNone(blocked_sdk_job)

            legacy_job = store.claim_next_job("legacy-a", key_order, key_capacities)
            self.assertIsNotNone(legacy_job)
            self.assertEqual(str(legacy_job["id"]), created_job_ids[1])

            second_sdk_job = store.claim_next_job("sdk-c", key_order, key_capacities)
            self.assertIsNotNone(second_sdk_job)
            self.assertEqual(str(second_sdk_job["id"]), created_job_ids[2])

            no_capacity_job = store.claim_next_job("sdk-c", key_order, key_capacities)
            self.assertIsNone(no_capacity_job)

            running_assignments = {
                str(store.get_job(job_id)["assigned_key_name"])
                for job_id in created_job_ids[:3]
            }
            self.assertEqual(running_assignments, {"legacy-a", "sdk-c"})

    def test_generate_image_routes_by_transport(self) -> None:
        expected = OpenAIImageResult(image_bytes=b"png", seen_events=["done"])

        with patch(
            "imagegen_server.openai_client.generate_image_via_responses_http",
            return_value=expected,
        ) as raw_mock:
            with patch(
                "imagegen_server.openai_client.generate_image_via_openai_sdk",
                return_value=expected,
            ) as sdk_mock:
                result = generate_image(
                    transport="openai_sdk",
                    base_url="https://lingsuan.nmyh.cc/v1",
                    api_key="sk-test",
                    model="gpt-5.5",
                    tool_model="gpt-image-2",
                    image_action="generate",
                    prompt="hello",
                    reference_images=[],
                    timeout_seconds=60,
                )

        self.assertEqual(result, expected)
        sdk_mock.assert_called_once()
        raw_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
