from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Iterator, Optional

from .schemas import JobResponse


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: Path, jobs_dir: Path) -> None:
        self.database_path = database_path
        self.jobs_dir = jobs_dir
        self._init_lock = threading.Lock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._init_lock:
            with self.connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        image_action TEXT NOT NULL,
                        model_override TEXT,
                        tool_model_override TEXT,
                        max_retries INTEGER NOT NULL,
                        retry_delay_seconds INTEGER NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        assigned_key_name TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        next_retry_at TEXT,
                        last_error TEXT,
                        avoid_key_name TEXT,
                        input_files_json TEXT NOT NULL,
                        output_files_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scheduler_state (
                        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                        last_assigned_key_name TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO scheduler_state (
                        singleton_id,
                        last_assigned_key_name
                    ) VALUES (1, NULL)
                    """
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "avoid_key_name" not in columns:
                    connection.execute(
                        "ALTER TABLE jobs ADD COLUMN avoid_key_name TEXT"
                    )

    def create_job(
        self,
        *,
        job_id: Optional[str] = None,
        prompt: str,
        image_action: str,
        model_override: Optional[str],
        tool_model_override: Optional[str],
        max_retries: int,
        retry_delay_seconds: int,
        input_files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        job_id = job_id or str(uuid.uuid4())
        now = utcnow()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, prompt, image_action, model_override,
                    tool_model_override, max_retries, retry_delay_seconds,
                    attempt_count, assigned_key_name, created_at, updated_at,
                    started_at, finished_at, next_retry_at, last_error,
                    avoid_key_name,
                    input_files_json, output_files_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    "queued",
                    prompt,
                    image_action,
                    model_override,
                    tool_model_override,
                    max_retries,
                    retry_delay_seconds,
                    now,
                    now,
                    json.dumps(input_files),
                    json.dumps([]),
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode_row(row)

    def get_job_status(self, job_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return str(row["status"])

    def list_jobs(
        self,
        limit: int,
        offset: int,
        *,
        status_filter: str,
        sort_field: str,
        sort_direction: str,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        where_clause = ""
        params: list[Any] = []
        if status_filter == "active":
            where_clause = "WHERE status IN ('queued', 'running', 'retry_waiting')"
        elif status_filter != "all":
            where_clause = "WHERE status = ?"
            params.append(status_filter)

        order_by = f"{sort_field} {sort_direction}, id {sort_direction}"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                {where_clause}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            if where_clause:
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM jobs {where_clause}",
                        params,
                    ).fetchone()[0]
                )
            else:
                total = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            count_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM jobs
                GROUP BY status
                """
            ).fetchall()
        counts = {
            "queued": 0,
            "running": 0,
            "retry_waiting": 0,
            "succeeded": 0,
            "failed": 0,
            "canceled": 0,
        }
        for row in count_rows:
            status = str(row["status"])
            if status in counts:
                counts[status] = int(row["count"])
        return [self._decode_row(row) for row in rows], total, counts

    def claim_next_job(
        self,
        key_name: str,
        key_order: list[str],
    ) -> Optional[dict[str, Any]]:
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            busy_keys = {
                row["assigned_key_name"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT assigned_key_name
                    FROM jobs
                    WHERE status = 'running'
                      AND assigned_key_name IS NOT NULL
                    """
                ).fetchall()
                if row["assigned_key_name"]
            }
            available_keys = [candidate for candidate in key_order if candidate not in busy_keys]
            if key_name not in available_keys:
                connection.execute("COMMIT")
                return None

            last_assigned_row = connection.execute(
                """
                SELECT last_assigned_key_name
                FROM scheduler_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            last_assigned_key = (
                str(last_assigned_row["last_assigned_key_name"])
                if last_assigned_row and last_assigned_row["last_assigned_key_name"]
                else None
            )
            scheduled_keys = self._round_robin_keys(key_order, last_assigned_key)
            preferred_available_key = next(
                (candidate for candidate in scheduled_keys if candidate in available_keys),
                None,
            )
            if preferred_available_key != key_name:
                connection.execute("COMMIT")
                return None

            allow_avoided_fallback = len(available_keys) == 1 and available_keys[0] == key_name
            row = self._select_claimable_job(
                connection=connection,
                key_name=key_name,
                now=now,
                allow_avoided_fallback=allow_avoided_fallback,
            )
            if row is None:
                connection.execute("COMMIT")
                return None

            connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    assigned_key_name = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    next_retry_at = NULL,
                    avoid_key_name = NULL
                WHERE id = ?
                """,
                (key_name, now, now, row["id"]),
            )
            connection.execute(
                """
                UPDATE scheduler_state
                SET last_assigned_key_name = ?
                WHERE singleton_id = 1
                """,
                (key_name,),
            )
            connection.execute("COMMIT")
        return self.get_job(str(row["id"]))

    def mark_retry_waiting(
        self,
        job_id: str,
        error_message: str,
        next_retry_at: str,
        failed_key_name: str,
    ) -> bool:
        now = utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'retry_waiting',
                    assigned_key_name = ?,
                    last_error = ?,
                    next_retry_at = ?,
                    avoid_key_name = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                """,
                (failed_key_name, error_message, next_retry_at, failed_key_name, now, job_id),
            )
        return cursor.rowcount > 0

    def mark_failed(self, job_id: str, error_message: str) -> bool:
        now = utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    last_error = ?,
                    finished_at = ?,
                    updated_at = ?,
                    next_retry_at = NULL,
                    avoid_key_name = NULL
                WHERE id = ?
                  AND status = 'running'
                """,
                (error_message, now, now, job_id),
            )
        return cursor.rowcount > 0

    def mark_succeeded(
        self,
        job_id: str,
        output_files: list[dict[str, Any]],
    ) -> bool:
        now = utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    finished_at = ?,
                    updated_at = ?,
                    last_error = NULL,
                    next_retry_at = NULL,
                    avoid_key_name = NULL,
                    output_files_json = ?
                WHERE id = ?
                  AND status = 'running'
                """,
                (now, now, json.dumps(output_files), job_id),
            )
        return cursor.rowcount > 0

    def requeue_interrupted_jobs(
        self,
        reason: str = "Interrupted by server restart.",
    ) -> list[str]:
        now = utcnow()
        recovered_job_ids: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id
                FROM jobs
                WHERE status = 'running'
                ORDER BY created_at ASC
                """
            ).fetchall()
            recovered_job_ids = [str(row["id"]) for row in rows]
            if recovered_job_ids:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        attempt_count = CASE
                            WHEN attempt_count > 0 THEN attempt_count - 1
                            ELSE 0
                        END,
                        assigned_key_name = NULL,
                        updated_at = ?,
                        started_at = NULL,
                        next_retry_at = NULL,
                        last_error = ?,
                        avoid_key_name = NULL
                    WHERE status = 'running'
                    """,
                    (now, reason),
                )
            connection.execute("COMMIT")
        for job_id in recovered_job_ids:
            self.append_event(
                job_id,
                "attempt_interrupted_by_restart",
                {"reason": reason},
            )
        return recovered_job_ids

    def update_input_files(self, job_id: str, input_files: list[dict[str, Any]]) -> None:
        now = utcnow()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET input_files_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(input_files), now, job_id),
            )

    def retry_failed_job(self, job_id: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["status"] not in {"failed", "canceled"}:
                connection.execute("ROLLBACK")
                raise ValueError("only failed or canceled jobs can be retried")
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    attempt_count = 0,
                    assigned_key_name = NULL,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    next_retry_at = NULL,
                    last_error = NULL,
                    avoid_key_name = NULL,
                    output_files_json = ?
                WHERE id = ?
                """,
                (now, json.dumps([]), job_id),
            )
            connection.execute("COMMIT")

        output_dir = self.jobs_dir / job_id / "output"
        if output_dir.exists():
            shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        result_meta = self.jobs_dir / job_id / "meta" / "result.json"
        if result_meta.exists():
            result_meta.unlink()
        self.append_event(job_id, "manual_retry_requested")
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["status"] == "running":
                connection.execute("ROLLBACK")
                raise ValueError("running jobs cannot be deleted")
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            connection.execute("COMMIT")
        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["status"] not in {"queued", "retry_waiting", "running"}:
                connection.execute("ROLLBACK")
                raise ValueError("only queued, active, or retry-waiting jobs can be canceled")
            connection.execute(
                """
                UPDATE jobs
                SET status = 'canceled',
                    updated_at = ?,
                    finished_at = ?,
                    next_retry_at = NULL,
                    avoid_key_name = NULL,
                    last_error = ?
                WHERE id = ?
                """,
                (now, now, "Canceled by user.", job_id),
            )
            connection.execute("COMMIT")
        self.append_event(job_id, "manual_cancel_requested")
        return self.get_job(job_id)

    def append_event(
        self,
        job_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        event_path = self.jobs_dir / job_id / "meta" / "events.log"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": utcnow(),
            "message": message,
        }
        if details:
            payload["details"] = details
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def write_request_meta(self, job_id: str, payload: dict[str, Any]) -> None:
        self._write_json(self.jobs_dir / job_id / "meta" / "request.json", payload)

    def write_result_meta(self, job_id: str, payload: dict[str, Any]) -> None:
        self._write_json(self.jobs_dir / job_id / "meta" / "result.json", payload)

    def make_job_dirs(self, job_id: str) -> Path:
        job_dir = self.jobs_dir / job_id
        (job_dir / "input").mkdir(parents=True, exist_ok=True)
        (job_dir / "output").mkdir(parents=True, exist_ok=True)
        (job_dir / "meta").mkdir(parents=True, exist_ok=True)
        return job_dir

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _decode_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["input_files"] = json.loads(payload.pop("input_files_json") or "[]")
        payload["output_files"] = json.loads(payload.pop("output_files_json") or "[]")
        payload["model"] = payload.pop("model_override")
        payload["tool_model"] = payload.pop("tool_model_override")
        return payload

    def _round_robin_keys(
        self,
        key_order: list[str],
        last_assigned_key: Optional[str],
    ) -> list[str]:
        if not key_order:
            return []
        if not last_assigned_key or last_assigned_key not in key_order:
            return list(key_order)
        start_index = (key_order.index(last_assigned_key) + 1) % len(key_order)
        return key_order[start_index:] + key_order[:start_index]

    def _select_claimable_job(
        self,
        *,
        connection: sqlite3.Connection,
        key_name: str,
        now: str,
        allow_avoided_fallback: bool,
    ) -> Optional[sqlite3.Row]:
        row = connection.execute(
            """
            SELECT *
            FROM jobs
            WHERE (
                    status = 'queued'
                    OR (status = 'retry_waiting' AND next_retry_at IS NOT NULL AND next_retry_at <= ?)
                  )
              AND (avoid_key_name IS NULL OR avoid_key_name != ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now, key_name),
        ).fetchone()
        if row is not None:
            return row
        if not allow_avoided_fallback:
            return None
        return connection.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'retry_waiting'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
              AND avoid_key_name = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now, key_name),
        ).fetchone()


def job_to_response(job: dict[str, Any], base_url: str) -> JobResponse:
    def _convert(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for item in files:
            converted.append(
                {
                    "filename": item["filename"],
                    "kind": item["kind"],
                    "size_bytes": item["size_bytes"],
                    "url": f"{base_url}/files/{job['id']}/{item['kind']}/{item['filename']}",
                }
            )
        return converted

    return JobResponse(
        id=job["id"],
        status=job["status"],
        prompt=job["prompt"],
        image_action=job["image_action"],
        model=job["model"],
        tool_model=job["tool_model"],
        attempt_count=int(job["attempt_count"]),
        max_retries=int(job["max_retries"]),
        retry_delay_seconds=int(job["retry_delay_seconds"]),
        assigned_key_name=job["assigned_key_name"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        next_retry_at=job["next_retry_at"],
        last_error=job["last_error"],
        input_files=_convert(job["input_files"]),
        output_files=_convert(job["output_files"]),
    )
