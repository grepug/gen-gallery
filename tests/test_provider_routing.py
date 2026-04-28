from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from imagegen_server.config import load_settings
from imagegen_server.errors import ImageGenerationError
from imagegen_server.openai_client import (
    OpenAIImageResult,
    generate_image,
    generate_image_via_openai_sdk,
)
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

    def test_sdk_transport_uses_images_generate_for_generate_jobs(self) -> None:
        fake_response = SimpleNamespace(
            data=[SimpleNamespace(b64_json="cG5n")]
        )
        fake_client = SimpleNamespace(
            images=SimpleNamespace(
                generate=lambda **kwargs: fake_response,
                edit=lambda **kwargs: fake_response,
            )
        )

        with patch("imagegen_server.openai_client.OpenAI", return_value=fake_client):
            result = generate_image_via_openai_sdk(
                base_url="https://lingsuan.nmyh.cc/v1",
                api_key="sk-test",
                model="gpt-5.5",
                tool_model="gpt-image-2",
                image_action="generate",
                prompt="hello",
                reference_images=[],
                timeout_seconds=60,
            )

        self.assertEqual(result.image_bytes, b"png")
        self.assertEqual(result.seen_events, ["images.generate"])

    def test_sdk_transport_uses_images_edit_for_edit_jobs(self) -> None:
        edit_calls: list[dict[str, object]] = []
        captured_payloads: list[dict[str, object]] = []

        class FakeHttpResponse:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {"data": [{"b64_json": "cG5n"}]}

            def raise_for_status(self) -> None:
                return None

        class FakeHttpClient:
            def build_request(self, method, url, **kwargs):
                edit_calls.append(
                    {
                        "method": method,
                        "url": url,
                        **kwargs,
                    }
                )
                return kwargs

            def send(self, request):
                image_name, image_handle, image_mime = request["files"]["image"]
                mask_name, mask_handle, mask_mime = request["files"]["mask"]
                image_bytes = image_handle.read()
                mask_bytes = mask_handle.read()
                with tempfile.TemporaryDirectory() as inspect_dir:
                    image_path = Path(inspect_dir) / "image.png"
                    mask_path = Path(inspect_dir) / "mask.png"
                    image_path.write_bytes(image_bytes)
                    mask_path.write_bytes(mask_bytes)
                    with Image.open(image_path) as prepared_image:
                        prepared_size = prepared_image.size
                        prepared_mode = prepared_image.mode
                    with Image.open(mask_path) as prepared_mask:
                        prepared_mask_size = prepared_mask.size
                        prepared_mask_mode = prepared_mask.mode
                captured_payloads.append(
                    {
                        "image_size": prepared_size,
                        "image_mode": prepared_mode,
                        "image_name": image_name,
                        "image_mime": image_mime,
                        "mask_size": prepared_mask_size,
                        "mask_mode": prepared_mask_mode,
                        "mask_name": mask_name,
                        "mask_mime": mask_mime,
                        "mask_bytes": mask_bytes,
                    }
                )
                return FakeHttpResponse()

        fake_client = SimpleNamespace(
            images=SimpleNamespace(
                generate=lambda **kwargs: SimpleNamespace(
                    data=[SimpleNamespace(b64_json="cG5n")]
                ),
            ),
            _client=FakeHttpClient(),
            base_url="https://lingsuan.nmyh.cc/v1/",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "ref.jpg"
            Image.new("RGB", (800, 600), (255, 64, 128)).save(
                image_path,
                format="JPEG",
            )
            with patch("imagegen_server.openai_client.OpenAI", return_value=fake_client):
                result = generate_image_via_openai_sdk(
                    base_url="https://lingsuan.nmyh.cc/v1",
                    api_key="sk-test",
                    model="gpt-5.5",
                    tool_model="gpt-image-2",
                    image_action="edit",
                    prompt="hello",
                    reference_images=[image_path],
                    timeout_seconds=60,
                )

        self.assertEqual(result.image_bytes, b"png")
        self.assertEqual(result.seen_events, ["images.edit"])
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(edit_calls[0]["method"], "POST")
        self.assertEqual(
            edit_calls[0]["url"],
            "https://lingsuan.nmyh.cc/v1/images/edits",
        )
        self.assertEqual(edit_calls[0]["data"]["model"], "gpt-image-2")
        self.assertEqual(edit_calls[0]["data"]["prompt"], "hello")
        self.assertEqual(edit_calls[0]["data"]["output_format"], "png")
        self.assertEqual(edit_calls[0]["data"]["size"], "1024x1024")
        self.assertEqual(edit_calls[0]["data"]["response_format"], "b64_json")
        self.assertEqual(
            edit_calls[0]["headers"]["Authorization"],
            "Bearer sk-test",
        )
        self.assertEqual(captured_payloads[0]["image_size"], (1024, 1024))
        self.assertEqual(captured_payloads[0]["image_mode"], "RGBA")
        self.assertEqual(captured_payloads[0]["image_name"], "edit-image.png")
        self.assertEqual(captured_payloads[0]["image_mime"], "image/png")
        self.assertEqual(captured_payloads[0]["mask_size"], (1024, 1024))
        self.assertEqual(captured_payloads[0]["mask_mode"], "RGBA")
        self.assertEqual(captured_payloads[0]["mask_name"], "edit-mask.png")
        self.assertEqual(captured_payloads[0]["mask_mime"], "image/png")
        self.assertTrue(captured_payloads[0]["mask_bytes"].startswith(b"\x89PNG\r\n\x1a\n"))

    def test_sdk_transport_rejects_multiple_reference_images_for_edit_jobs(self) -> None:
        fake_client = SimpleNamespace(
            images=SimpleNamespace(
                generate=lambda **kwargs: SimpleNamespace(
                    data=[SimpleNamespace(b64_json="cG5n")]
                ),
            ),
            _client=SimpleNamespace(
                build_request=lambda *args, **kwargs: kwargs,
                send=lambda request: request,
            ),
            base_url="https://lingsuan.nmyh.cc/v1/",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.png"
            second_path = Path(temp_dir) / "second.png"
            Image.new("RGBA", (400, 400), (255, 255, 255, 255)).save(first_path)
            Image.new("RGBA", (400, 400), (255, 255, 255, 255)).save(second_path)

            with patch("imagegen_server.openai_client.OpenAI", return_value=fake_client):
                with self.assertRaises(ImageGenerationError) as captured:
                    generate_image_via_openai_sdk(
                        base_url="https://lingsuan.nmyh.cc/v1",
                        api_key="sk-test",
                        model="gpt-5.5",
                        tool_model="gpt-image-2",
                        image_action="edit",
                        prompt="hello",
                        reference_images=[first_path, second_path],
                        timeout_seconds=60,
                    )

        self.assertIn("exactly one reference image", str(captured.exception))
        self.assertFalse(captured.exception.retryable)


if __name__ == "__main__":
    unittest.main()
