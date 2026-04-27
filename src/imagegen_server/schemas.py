from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "running", "retry_waiting", "succeeded", "failed"]
ImageAction = Literal["auto", "generate", "edit"]


class JobFile(BaseModel):
    filename: str
    kind: Literal["input", "output"]
    size_bytes: int
    url: str


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    prompt: str
    image_action: ImageAction
    model: Optional[str] = None
    tool_model: Optional[str] = None
    attempt_count: int
    max_retries: int
    retry_delay_seconds: int
    assigned_key_name: Optional[str] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    input_files: list[JobFile] = Field(default_factory=list)
    output_files: list[JobFile] = Field(default_factory=list)


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class CreateJobResponse(BaseModel):
    id: str
    status: JobStatus


class HealthResponse(BaseModel):
    status: str
    worker_count: int
