from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from imagegen_server.storage import JobStore


class ReferenceImageDedupeTests(unittest.TestCase):
    def test_duplicate_reference_uploads_share_one_stored_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server_home = Path(temp_dir)
            jobs_dir = server_home / "jobs"
            store = JobStore(server_home / "app.db", jobs_dir)
            store.initialize()

            image_bytes = (
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01"
            )

            first_filename, first_storage_path = store.store_reference_image(
                image_bytes,
                ".png",
            )
            second_filename, second_storage_path = store.store_reference_image(
                image_bytes,
                ".png",
            )

            self.assertEqual(first_filename, second_filename)
            self.assertEqual(first_storage_path, second_storage_path)

            shared_dir = server_home / "shared" / "reference-images"
            shared_files = list(shared_dir.iterdir())
            self.assertEqual(len(shared_files), 1)
            self.assertEqual(shared_files[0].name, first_filename)

            input_files = [
                {
                    "filename": first_filename,
                    "kind": "input",
                    "size_bytes": len(image_bytes),
                    "storage_path": first_storage_path,
                }
            ]
            for prompt in ("first", "second"):
                job_id = str(uuid.uuid4())
                store.make_job_dirs(job_id)
                store.create_job(
                    job_id=job_id,
                    prompt=prompt,
                    image_action="edit",
                    model_override=None,
                    tool_model_override=None,
                    max_retries=0,
                    retry_delay_seconds=60,
                    input_files=input_files,
                )
                resolved = store.resolve_job_file_path(job_id, "input", first_filename)
                self.assertEqual(resolved.read_bytes(), image_bytes)


if __name__ == "__main__":
    unittest.main()
