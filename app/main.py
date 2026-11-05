from pathlib import Path

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

# TODO: POST /jobs (streaming upload), GET /jobs/{id}, GET /jobs/{id}/result,
#       POST /jobs/{id}/cancel
