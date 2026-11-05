from pathlib import Path
import uuid
from app.db import get_session
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.models import Job
from app.schemas import JobOut

import structlog
from fastapi import FastAPI

from app import db
from app.config import settings
from app.logging_setup import setup_logging

setup_logging()
log = structlog.get_logger()

app = FastAPI(title="File Processing Pipeline")


@app.on_event("startup")
def on_startup() -> None:
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    db.Base.metadata.create_all(db.engine)  # swap for Alembic later if you want migrations
    log.info("api_started", storage_root=settings.storage_root)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": db.ping()}

@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobOut.model_validate(job)