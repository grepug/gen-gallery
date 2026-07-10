from __future__ import annotations

import base64
import unittest

from imagegen_server.openai_client import extract_image_payload


class ImagePayloadTests(unittest.TestCase):
    def test_extracts_base64_image_from_images_api_response(self) -> None:
        image_bytes = b"png-bytes"
        response = {"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]}

        self.assertEqual(extract_image_payload(response), image_bytes)

    def test_rejects_text_only_response_as_missing_image(self) -> None:
        response = {"output": [{"type": "message", "content": [{"text": "done"}]}]}

        with self.assertRaisesRegex(RuntimeError, "No image payload"):
            extract_image_payload(response)
