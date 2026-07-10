from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills.my_image_gen.scripts import generate_image


class MyImageGenSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_home.cleanup)
        self.home = Path(self.temp_home.name)

    def test_first_run_prompts_and_saves_private_config(self) -> None:
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False), patch(
            "builtins.input", side_effect=["https://example.test/v1"]
        ), patch("getpass.getpass", return_value="secret"):
            config = generate_image.load_or_create_config()

        self.assertEqual(config["api_key"], "secret")
        self.assertEqual(config["base_url"], "https://example.test/v1")
        self.assertEqual(config["model"], "gpt-image-2")
        self.assertEqual(
            (self.home / ".my-image-gen/config.json").stat().st_mode & 0o777,
            0o600,
        )

    def test_existing_config_is_reused_without_prompting(self) -> None:
        config_path = self.home / ".my-image-gen/config.json"
        config_path.parent.mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "api_key": "secret",
                    "base_url": "https://example.test/v1/",
                    "model": "custom-image",
                }
            )
        )

        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False), patch(
            "builtins.input"
        ) as input_mock, patch("getpass.getpass") as getpass_mock:
            config = generate_image.load_or_create_config()

        self.assertEqual(config["base_url"], "https://example.test/v1")
        input_mock.assert_not_called()
        getpass_mock.assert_not_called()

    def test_response_payload_and_collision_safe_original_format_output(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes"
        encoded = base64.b64encode(image_bytes).decode()
        output_dir = self.home / ".my-image-gen/images"
        output_dir.mkdir(parents=True)
        (output_dir / "20260710-dog-drinking.png").write_bytes(b"old")

        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False), patch(
            "skills.my_image_gen.scripts.generate_image.today_string",
            return_value="20260710",
        ):
            output = generate_image.save_image(
                {"data": [{"b64_json": encoded}]}, "dog drinking"
            )

        self.assertEqual(output.name, "20260710-dog-drinking-2.png")
        self.assertEqual(output.read_bytes(), image_bytes)
