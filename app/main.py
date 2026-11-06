import uuid
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app import db, models  # noqa: F401
from app.config import settings
from app.db import get_session
from app.logging_setup import setup_logging
from app.models import FileKind, FileReference, Job, JobStatus, JobStep, StepStatus
from app.schemas import JobCreated, JobOut, StepIn
from app.storage import FileTooLarge, full_path, save_upload


setup_logging()
log = structlog.get_logger()

app = FastAPI(title="File Processing Pipeline")

ALLOWED_EXTENSIONS = {".csv", ".json", ".txt", ".zip", ".gz"}

def require_api_key(x_api_key: str = Header(...)) -> str:
    allowed = {k.strip() for k in settings.api_keys.split(",") if k.strip()}
    if allowed and x_api_key not in allowed:
        raise HTTPException(401, "Invalid API key")
    return x_api_key

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
    x_api_key: str = Depends(require_api_key),
    session: Session = Depends(get_session),
):
    try:
        steps = TypeAdapter(list[StepIn]).validate_json(pipeline)
    except ValidationError as e:
        raise HTTPException(422, f"invalid pipeline: {_pipeline_errors(e)}")
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



def _pipeline_errors(e: ValidationError) -> str:
    problems = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        loc = err["loc"]
        if not loc:
            problems.append(msg)
        elif len(loc) == 1:
            problems.append(f"step {loc[0]}: {msg}")
        else:
            problems.append(f"step {loc[0]}, field '{loc[1]}': {msg}")
    return "; ".join(problems)

@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: uuid.UUID,
    x_api_key: str = Depends(require_api_key),
    session: Session = Depends(get_session),
):
    job = _get_owned_job(session, job_id, x_api_key)
    out = JobOut.model_validate(job)
    if job.status == JobStatus.COMPLETED:
        out.result_url = f"/jobs/{job.id}/result"
    return out


@app.get("/jobs/{job_id}/result")
def download_result(
    job_id: uuid.UUID,
    x_api_key: str = Depends(require_api_key),
    session: Session = Depends(get_session),
):
    job = _get_owned_job(session, job_id, x_api_key)
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(409, f"job is {job.status}, the result is ready only when it's completed")
    return _file_response(job.output_file, job.input_file.original_filename)


@app.get("/jobs/{job_id}/steps/{step_index}/result")
def download_step_result(
    job_id: uuid.UUID,
    step_index: int,
    x_api_key: str = Depends(require_api_key),
    session: Session = Depends(get_session),
):
    job = _get_owned_job(session, job_id, x_api_key)
    step = next((s for s in job.steps if s.step_index == step_index), None)
    if step is None:
        raise HTTPException(404, "Step not found")
    if step.status != StepStatus.COMPLETED:
        raise HTTPException(409, f"step is {step.status}, its result is ready only when it's completed")
    return _file_response(step.output_file, job.input_file.original_filename)


def _get_owned_job(session: Session, job_id: uuid.UUID, api_key: str) -> Job:
    job = session.get(Job, job_id)
    if job is None or job.api_key != api_key:
        raise HTTPException(404, "Job not found")
    return job


def _file_response(file: FileReference, original_name: str | None) -> FileResponse:
    path = full_path(file.storage_path)
    if file.deleted_at is not None or not path.exists():
        raise HTTPException(410, "The file has expired and was deleted")

    base = Path(original_name or "result").name.split(".")[0]
    download_name = base + "".join(Path(file.storage_path).suffixes)
    return FileResponse(path, filename=download_name, media_type=_media_type(download_name))


def _media_type(name: str) -> str:
    if name.endswith(".gz"):
        return "application/gzip"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".csv"):
        return "text/csv"
    return "application/octet-stream"