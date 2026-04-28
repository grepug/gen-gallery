from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .schemas import (
    CreateJobResponse,
    HealthResponse,
    ImportArchiveResponse,
    JobListResponse,
    JobResponse,
)
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

    valid_status_filters = {"all", "active", "succeeded", "failed", "canceled", "favorites"}
    sort_options = {
        "created_desc": ("created_at", "DESC"),
        "created_asc": ("created_at", "ASC"),
        "updated_desc": ("updated_at", "DESC"),
        "updated_asc": ("updated_at", "ASC"),
    }

    @app.on_event("startup")
    async def on_startup() -> None:
        await asyncio.to_thread(store.requeue_interrupted_jobs)
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
        for upload in reference_images:
            suffix = Path(upload.filename or "").suffix
            if not suffix:
                guessed = mimetypes.guess_extension(upload.content_type or "")
                suffix = guessed or ".bin"
            content = await upload.read()
            uploaded_files.append(
                {
                    "content": content,
                    "suffix": suffix,
                    "original_filename": upload.filename or f"reference{suffix.lower()}",
                }
            )
            await upload.close()

        job_id = str(uuid.uuid4())
        store.make_job_dirs(job_id)
        job = store.create_job_with_reference_uploads(
            job_id=job_id,
            prompt=prompt.strip(),
            image_action=image_action,
            model_override=model.strip() if model else None,
            tool_model_override=tool_model.strip() if tool_model else None,
            max_retries=effective_max_retries,
            retry_delay_seconds=effective_retry_delay,
            reference_uploads=uploaded_files,
        )
        final_input_files = list(job["input_files"])

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
                        "storage_path": item["storage_path"],
                        "content_hash": item["content_hash"],
                        "original_filename": item["original_filename"],
                    }
                    for item in final_input_files
                ],
            },
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

    @app.post("/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(job_id: str, request: Request) -> JobResponse:
        try:
            job = store.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job_to_response(job, str(request.base_url).rstrip("/"))

    @app.post("/jobs/{job_id}/favorite", response_model=JobResponse)
    async def favorite_job(job_id: str, request: Request) -> JobResponse:
        try:
            job = store.set_favorite(job_id, is_favorite=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job_to_response(job, str(request.base_url).rstrip("/"))

    @app.delete("/jobs/{job_id}/favorite", response_model=JobResponse)
    async def unfavorite_job(job_id: str, request: Request) -> JobResponse:
        try:
            job = store.set_favorite(job_id, is_favorite=False)
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
        status: str = Query("all"),
        sort: str = Query("created_desc"),
    ) -> JobListResponse:
        if status not in valid_status_filters:
            raise HTTPException(status_code=400, detail="invalid status filter")
        if sort not in sort_options:
            raise HTTPException(status_code=400, detail="invalid sort option")
        sort_field, sort_direction = sort_options[sort]
        jobs, total, counts = store.list_jobs(
            limit=limit,
            offset=offset,
            status_filter=status,
            sort_field=sort_field,
            sort_direction=sort_direction,
        )
        base_url = str(request.base_url).rstrip("/")
        items = [job_to_response(job, base_url) for job in jobs]
        return JobListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            counts=counts,
        )

    @app.get("/files/{job_id}/{kind}/{filename}")
    async def get_file(job_id: str, kind: str, filename: str) -> FileResponse:
        if kind not in {"input", "output"}:
            raise HTTPException(status_code=404, detail="invalid file kind")
        try:
            file_path = store.resolve_job_file_path(job_id, kind, filename)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        media_type, _ = mimetypes.guess_type(str(file_path))
        return FileResponse(
            file_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.post("/admin/import-archive", response_model=ImportArchiveResponse)
    async def import_archive(
        archive: UploadFile = File(...),
        import_token: Optional[str] = Header(None, alias="X-Import-Token"),
    ) -> ImportArchiveResponse:
        expected_token = os.environ.get("DATA_IMPORT_TOKEN", "").strip()
        if not expected_token:
            raise HTTPException(status_code=404, detail="not found")
        if import_token != expected_token:
            raise HTTPException(status_code=401, detail="invalid import token")

        temp_root = Path(tempfile.mkdtemp(prefix="archive-import-", dir=settings.server_home))
        extract_dir = temp_root / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        archive_path = temp_root / "import.tar.gz"
        worker_was_running = bool(worker_pool._tasks)
        workers_restarted = False

        try:
            with archive_path.open("wb") as handle:
                while True:
                    chunk = await archive.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            await archive.close()

            await asyncio.to_thread(_extract_archive, archive_path, extract_dir)
            imported_db = extract_dir / "app.db"
            imported_jobs = extract_dir / "jobs"
            imported_logs = extract_dir / "logs"
            imported_shared = extract_dir / "shared"
            if not imported_db.is_file():
                raise HTTPException(status_code=400, detail="archive is missing app.db")
            if not imported_jobs.is_dir():
                raise HTTPException(status_code=400, detail="archive is missing jobs/")

            imported_job_count = await asyncio.to_thread(_count_jobs, imported_db)
            await worker_pool.stop()
            await asyncio.to_thread(
                _replace_runtime_data,
                settings.server_home,
                imported_db,
                imported_jobs,
                imported_logs if imported_logs.is_dir() else None,
                imported_shared if imported_shared.is_dir() else None,
            )
            store.initialize()
            if worker_was_running:
                await worker_pool.start()
                workers_restarted = True
            return ImportArchiveResponse(
                status="ok",
                imported_job_count=imported_job_count,
            )
        finally:
            if worker_was_running and not workers_restarted and not worker_pool._tasks:
                await worker_pool.start()
            if archive and not archive.file.closed:
                await archive.close()
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)

    return app


def _extract_archive(archive_path: Path, extract_dir: Path) -> None:
    root = extract_dir.resolve()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            target = (extract_dir / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("archive contains invalid paths")
        for member in archive.getmembers():
            archive.extract(member, extract_dir)


def _count_jobs(database_path: Path) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    finally:
        connection.close()


def _replace_runtime_data(
    server_home: Path,
    imported_db: Path,
    imported_jobs: Path,
    imported_logs: Optional[Path],
    imported_shared: Optional[Path],
) -> None:
    destinations = [
        (imported_db, server_home / "app.db"),
        (imported_jobs, server_home / "jobs"),
    ]
    if imported_logs is not None:
        destinations.append((imported_logs, server_home / "logs"))
    if imported_shared is not None:
        destinations.append((imported_shared, server_home / "shared"))

    for _, destination in destinations:
        if destination.exists():
            if destination.is_dir():
                for child in sorted(destination.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                destination.rmdir()
            else:
                destination.unlink()

    for source, destination in destinations:
        source.replace(destination)
