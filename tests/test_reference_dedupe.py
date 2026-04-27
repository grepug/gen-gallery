from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from imagegen_server.openai_client import make_data_url
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
            self.assertTrue(make_data_url(shared_files[0]).startswith("data:image/png;base64,"))

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

    def test_reference_filename_uses_actual_image_type_over_uploaded_suffix(self) -> None:
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
            job_id = str(uuid.uuid4())
            store.make_job_dirs(job_id)
            job = store.create_job_with_reference_uploads(
                job_id=job_id,
                prompt="png with wrong suffix",
                image_action="edit",
                model_override=None,
                tool_model_override=None,
                max_retries=0,
                retry_delay_seconds=60,
                reference_uploads=[
                    {
                        "content": image_bytes,
                        "suffix": ".jpg",
                        "original_filename": "wrong.jpg",
                    }
                ],
            )

            input_file = job["input_files"][0]
            self.assertTrue(input_file["filename"].endswith(".png"))
            self.assertTrue(
                make_data_url(store.resolve_job_file_path(job_id, "input", input_file["filename"])).startswith(
                    "data:image/png;base64,"
                )
            )

    def test_delete_job_removes_shared_file_only_after_last_reference(self) -> None:
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
            filename, storage_path = store.store_reference_image(image_bytes, ".bin")
            input_files = [
                {
                    "filename": filename,
                    "kind": "input",
                    "size_bytes": len(image_bytes),
                    "storage_path": storage_path,
                }
            ]

            job_ids: list[str] = []
            for prompt in ("first", "second"):
                job_id = str(uuid.uuid4())
                job_ids.append(job_id)
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

            shared_path = server_home / storage_path
            self.assertTrue(shared_path.exists())
            self.assertTrue(make_data_url(shared_path).startswith("data:image/png;base64,"))

            store.delete_job(job_ids[0])
            self.assertTrue(shared_path.exists())

            store.delete_job(job_ids[1])
            self.assertFalse(shared_path.exists())

    def test_failed_create_does_not_leak_new_shared_file(self) -> None:
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
            job_id = str(uuid.uuid4())
            store.make_job_dirs(job_id)
            store.create_job_with_reference_uploads(
                job_id=job_id,
                prompt="first",
                image_action="edit",
                model_override=None,
                tool_model_override=None,
                max_retries=0,
                retry_delay_seconds=60,
                reference_uploads=[
                    {
                        "content": b"not the same image",
                        "suffix": ".bin",
                        "original_filename": "seed.bin",
                    }
                ],
            )

            duplicate_storage = store.prepare_reference_image(image_bytes, ".png")[1]
            with self.assertRaises(Exception):
                store.create_job_with_reference_uploads(
                    job_id=job_id,
                    prompt="duplicate id",
                    image_action="edit",
                    model_override=None,
                    tool_model_override=None,
                    max_retries=0,
                    retry_delay_seconds=60,
                    reference_uploads=[
                        {
                            "content": image_bytes,
                            "suffix": ".png",
                            "original_filename": "duplicate.png",
                        }
                    ],
                )

            self.assertFalse((server_home / duplicate_storage).exists())


if __name__ == "__main__":
    unittest.main()
