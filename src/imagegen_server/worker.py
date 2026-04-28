from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import ApiKeyConfig, Settings
from .errors import ImageGenerationError
from .openai_client import generate_image
from .storage import JobStore, utcnow


@dataclass
class WorkerContext:
    key_config: ApiKeyConfig
    slot_index: int
    settings: Settings
    store: JobStore


class WorkerPool:
    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store
        self._key_order = [key_config.name for key_config in settings.api_keys]
        self._key_capacities = {
            key_config.name: key_config.concurrency for key_config in settings.api_keys
        }
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._stop_event.clear()
        for key_config in self.settings.api_keys:
            for slot_index in range(key_config.concurrency):
                context = WorkerContext(
                    key_config=key_config,
                    slot_index=slot_index,
                    settings=self.settings,
                    store=self.store,
                )
                task = asyncio.create_task(
                    self._worker_loop(context),
                    name=f"worker-{key_config.name}-{slot_index + 1}",
                )
                self._tasks.append(task)

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def worker_count(self) -> int:
        return sum(key_config.concurrency for key_config in self.settings.api_keys)

    async def _worker_loop(self, context: WorkerContext) -> None:
        while not self._stop_event.is_set():
            job = await asyncio.to_thread(
                context.store.claim_next_job,
                context.key_config.name,
                self._key_order,
                self._key_capacities,
            )
            if job is None:
                await asyncio.sleep(context.settings.poll_interval_seconds)
                continue

            await self._run_job(context, job)

    async def _run_job(self, context: WorkerContext, job: dict) -> None:
        while True:
            job_id = job["id"]
            await asyncio.to_thread(
                context.store.append_event,
                job_id,
                "attempt_started",
                {
                    "attempt_count": job["attempt_count"],
                    "key_name": context.key_config.name,
                    "worker_slot": context.slot_index + 1,
                    "transport": context.key_config.transport,
                },
            )

            try:
                result = await asyncio.to_thread(
                    generate_image,
                    transport=context.key_config.transport,
                    base_url=context.key_config.base_url
                    or context.settings.openai_base_url,
                    api_key=context.key_config.api_key,
                    model=job["model"]
                    or context.key_config.model
                    or context.settings.openai_model,
                    tool_model=job["tool_model"]
                    or context.key_config.tool_model
                    or context.settings.openai_image_tool_model,
                    image_action=job["image_action"],
                    prompt=job["prompt"],
                    reference_images=[
                        context.store.resolve_input_file_path(job_id, item)
                        for item in job["input_files"]
                    ],
                    timeout_seconds=context.settings.job_timeout_seconds,
                )
            except ImageGenerationError as exc:
                await asyncio.to_thread(
                    context.store.append_event,
                    job_id,
                    "attempt_failed",
                    {
                        "attempt_count": job["attempt_count"],
                        "failed_key_name": context.key_config.name,
                        "worker_slot": context.slot_index + 1,
                        "retryable": exc.retryable,
                        "immediate_retry_on_other_key": exc.immediate_retry_on_other_key,
                        "error": str(exc),
                    },
                )
                should_retry = exc.retryable and job["attempt_count"] <= job["max_retries"]
                if not should_retry:
                    await asyncio.to_thread(context.store.mark_failed, job_id, str(exc))
                    return

                retry_delay_seconds = self._compute_retry_delay_seconds(job, exc)
                retry_at_dt = datetime.now(timezone.utc) + timedelta(
                    seconds=retry_delay_seconds
                )
                retry_at = retry_at_dt.isoformat()
                did_requeue = await asyncio.to_thread(
                    context.store.mark_retry_waiting,
                    job_id,
                    str(exc),
                    retry_at,
                    context.key_config.name,
                )
                if not did_requeue:
                    return
                await asyncio.to_thread(
                    context.store.append_event,
                    job_id,
                    "attempt_requeued",
                    {
                        "attempt_count": job["attempt_count"],
                        "failed_key_name": context.key_config.name,
                        "worker_slot": context.slot_index + 1,
                        "retry_at": retry_at,
                        "retry_delay_seconds": retry_delay_seconds,
                        "immediate_retry_on_other_key": exc.immediate_retry_on_other_key,
                        "avoid_key_name": context.key_config.name,
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    context.store.append_event,
                    job_id,
                    "attempt_failed",
                    {
                        "attempt_count": job["attempt_count"],
                        "worker_slot": context.slot_index + 1,
                        "retryable": False,
                        "error": str(exc),
                    },
                )
                await asyncio.to_thread(context.store.mark_failed, job_id, str(exc))
                return

            current_status = await asyncio.to_thread(context.store.get_job_status, job_id)
            if current_status == "canceled":
                await asyncio.to_thread(
                    context.store.append_event,
                    job_id,
                    "attempt_discarded_after_cancel",
                    {
                        "attempt_count": job["attempt_count"],
                        "key_name": context.key_config.name,
                        "worker_slot": context.slot_index + 1,
                    },
                )
                return

            output_dir = context.settings.jobs_dir / job_id / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / "result.png"
            output_path.write_bytes(result.image_bytes)
            output_files = [
                {
                    "filename": output_path.name,
                    "kind": "output",
                    "size_bytes": output_path.stat().st_size,
                }
            ]
            did_succeed = await asyncio.to_thread(
                context.store.mark_succeeded, job_id, output_files
            )
            if not did_succeed:
                if output_path.exists():
                    output_path.unlink()
                await asyncio.to_thread(
                    context.store.append_event,
                    job_id,
                    "attempt_discarded_after_cancel",
                    {
                        "attempt_count": job["attempt_count"],
                        "key_name": context.key_config.name,
                    },
                )
                return
            await asyncio.to_thread(
                context.store.write_result_meta,
                job_id,
                {
                    "finished_at": utcnow(),
                    "output_files": output_files,
                    "seen_events": result.seen_events,
                },
            )
            await asyncio.to_thread(
                context.store.append_event,
                job_id,
                "attempt_succeeded",
                {
                    "attempt_count": job["attempt_count"],
                    "output_filename": output_path.name,
                    "worker_slot": context.slot_index + 1,
                },
            )
            return

    def _compute_retry_delay_seconds(
        self,
        job: dict,
        error: ImageGenerationError,
    ) -> int:
        if error.immediate_retry_on_other_key:
            return 0
        base_delay = int(job["retry_delay_seconds"])
        attempt_count = int(job["attempt_count"])
        retry_index = max(attempt_count, 1)
        return base_delay * retry_index
