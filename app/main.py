import uuid
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app import db, models  # noqa: F401
from app.config import settings
from app.db import get_session
from app.logging_setup import setup_logging
from app.models import FileKind, FileReference, Job, JobStep
from app.schemas import JobCreated, JobOut, StepIn
from app.storage import FileTooLarge, full_path, save_upload


setup_logging()
log = structlog.get_logger()

app = FastAPI(title="File Processing Pipeline")

ALLOWED_EXTENSIONS = {".csv", ".json", ".txt", ".zip", ".gz"}


@app.on_event("startup")
def on_startup() -> None:
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    db.Base.metadata.create_all(db.engine)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": db.ping()}


@app.post("/jobs", status_code=202, response_model=JobCreated)
def create_job(
    file: UploadFile,
    pipeline: str = Form(...),
    x_api_key: str = Header(...),
    session: Session = Depends(get_session),
):
    try:
        steps = TypeAdapter(list[StepIn]).validate_json(pipeline)
    except ValidationError as e:
        raise HTTPException(422, f"invalid pipeline: {e}")
    if not steps:
        raise HTTPException(422, "pipeline must have at least one step")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"file type '{ext}' is not supported")

    try:
        storage_path, size = save_upload(file, "inputs")
    except FileTooLarge:
        raise HTTPException(413, "file is larger than the allowed limit")

    if size == 0:
        full_path(storage_path).unlink(missing_ok=True)
        raise HTTPException(422, "file is empty")

    input_file = FileReference(
        kind=FileKind.INPUT,
        storage_path=storage_path,
        original_filename=file.filename,
        size_bytes=size,
        content_type=file.content_type,
    )
    job = Job(
        api_key=x_api_key,
        pipeline=[s.model_dump() for s in steps],
        input_file=input_file,
    )
    job.steps = [
        JobStep(step_index=i, step_type=s.step, params=s.params)
        for i, s in enumerate(steps)
    ]

    try:
        session.add(job)
        session.commit()
    except Exception:
        full_path(storage_path).unlink(missing_ok=True)
        raise

    log.info("job_created", job_id=str(job.id), size_bytes=size, steps=[s.step for s in steps])
    return JobCreated(id=job.id, status=job.status)


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobOut.model_validate(job)