from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .schemas import CreateJobResponse, HealthResponse, JobListResponse, JobResponse
from .storage import JobStore, job_to_response
from .worker import WorkerPool


def create_app() -> FastAPI:
    web_dir = Path(__file__).with_name("web")
    settings = load_settings()
    store = JobStore(settings.database_path, settings.jobs_dir)
    store.initialize()
    worker_pool = WorkerPool(settings, store)

    app = FastAPI(title="imagegen-server", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.worker_pool = worker_pool
    app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")

    @app.on_event("startup")
    async def on_startup() -> None:
        await worker_pool.start()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await worker_pool.stop()

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", worker_count=worker_pool.worker_count)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.post("/jobs", response_model=CreateJobResponse, status_code=201)
    async def create_job(
        prompt: str = Form(...),
        image_action: str = Form("auto"),
        model: Optional[str] = Form(None),
        tool_model: Optional[str] = Form(None),
        max_retries: Optional[int] = Form(None),
        retry_delay_seconds: Optional[int] = Form(None),
        reference_images: Optional[list[UploadFile]] = File(None),
    ) -> CreateJobResponse:
        reference_images = reference_images or []
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        if image_action not in {"auto", "generate", "edit"}:
            raise HTTPException(status_code=400, detail="invalid image_action")
        if image_action == "edit" and not reference_images:
            raise HTTPException(
                status_code=400,
                detail="image_action=edit requires at least one reference image",
            )

        effective_max_retries = (
            settings.job_max_retries if max_retries is None else max_retries
        )
        effective_retry_delay = (
            settings.job_retry_delay_seconds
            if retry_delay_seconds is None
            else retry_delay_seconds
        )
        if effective_max_retries < 0:
            raise HTTPException(status_code=400, detail="max_retries must be >= 0")
        if effective_retry_delay < 1:
            raise HTTPException(
                status_code=400, detail="retry_delay_seconds must be >= 1"
            )

        uploaded_files: list[dict[str, object]] = []
        for index, upload in enumerate(reference_images, start=1):
            suffix = Path(upload.filename or "").suffix
            if not suffix:
                guessed = mimetypes.guess_extension(upload.content_type or "")
                suffix = guessed or ".bin"
            filename = f"reference_{index}{suffix.lower()}"
            uploaded_files.append(
                {
                    "filename": filename,
                    "kind": "input",
                    "size_bytes": 0,
                    "upload": upload,
                }
            )

        job_id = str(uuid.uuid4())
        job_dir = store.make_job_dirs(job_id)

        final_input_files: list[dict[str, object]] = []
        for item in uploaded_files:
            upload = item.pop("upload")
            target = job_dir / "input" / str(item["filename"])
            content = await upload.read()
            target.write_bytes(content)
            item["size_bytes"] = len(content)
            final_input_files.append(item)
            await upload.close()

        store.write_request_meta(
            job_id,
            {
                "prompt": prompt.strip(),
                "image_action": image_action,
                "model": model,
                "tool_model": tool_model,
                "max_retries": effective_max_retries,
                "retry_delay_seconds": effective_retry_delay,
                "reference_images": [
                    {
                        "filename": item["filename"],
                        "size_bytes": item["size_bytes"],
                    }
                    for item in final_input_files
                ],
            },
        )
        job = store.create_job(
            job_id=job_id,
            prompt=prompt.strip(),
            image_action=image_action,
            model_override=model.strip() if model else None,
            tool_model_override=tool_model.strip() if tool_model else None,
            max_retries=effective_max_retries,
            retry_delay_seconds=effective_retry_delay,
            input_files=final_input_files,
        )
        store.append_event(
            job_id,
            "job_created",
            {
                "image_action": image_action,
                "input_count": len(final_input_files),
            },
        )
        return CreateJobResponse(id=job["id"], status="queued")

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str, request: Request) -> JobResponse:
        try:
            job = store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return job_to_response(job, str(request.base_url).rstrip("/"))

    @app.post("/jobs/{job_id}/retry", response_model=JobResponse)
    async def retry_job(job_id: str, request: Request) -> JobResponse:
        try:
            job = store.retry_failed_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job_to_response(job, str(request.base_url).rstrip("/"))

    @app.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str) -> Response:
        try:
            store.delete_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get("/jobs", response_model=JobListResponse)
    async def list_jobs(
        request: Request,
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> JobListResponse:
        jobs, total = store.list_jobs(limit=limit, offset=offset)
        base_url = str(request.base_url).rstrip("/")
        items = [job_to_response(job, base_url) for job in jobs]
        return JobListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/files/{job_id}/{kind}/{filename}")
    async def get_file(job_id: str, kind: str, filename: str) -> FileResponse:
        if kind not in {"input", "output"}:
            raise HTTPException(status_code=404, detail="invalid file kind")
        file_path = settings.jobs_dir / job_id / kind / filename
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        media_type, _ = mimetypes.guess_type(str(file_path))
        return FileResponse(file_path, media_type=media_type or "application/octet-stream")

    return app
