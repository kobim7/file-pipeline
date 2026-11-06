import time
import uuid
from datetime import timedelta
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import FileReference, Job, JobStatus, utcnow
from app.storage import full_path

log = structlog.get_logger()

FINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


def set_expiry(job: Job) -> None:
    """Call when a job reaches a final state. The caller commits."""
    now = utcnow()
    files = {job.input_file} | {s.output_file for s in job.steps if s.output_file}
    for f in files:
        f.expires_at = now + timedelta(hours=settings.intermediate_retention_hours)
    if job.output_file:
        job.output_file.expires_at = now + timedelta(hours=settings.retention_hours)


def delete_expired_files(limit: int = 500) -> int:
    with SessionLocal() as session:
        files = session.scalars(
            select(FileReference)
            .where(FileReference.expires_at < utcnow(), FileReference.deleted_at.is_(None))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for f in files:
            full_path(f.storage_path).unlink(missing_ok=True)
            f.deleted_at = utcnow()
        session.commit()

    if files:
        log.info("expired_files_deleted", count=len(files))
    return len(files)


def delete_orphan_files() -> int:
    root = Path(settings.storage_root)
    too_old = time.time() - settings.orphan_grace_seconds
    deleted = 0

    with SessionLocal() as session:
        for path in root.rglob("*"):
            if not path.is_file() or path.stat().st_mtime > too_old:
                continue
            storage_path = str(path.relative_to(root))
            known = session.scalar(
                select(FileReference.id).where(FileReference.storage_path == storage_path)
            )
            if known is None:
                path.unlink(missing_ok=True)
                deleted += 1

        work = root / "work"
        if work.exists():
            for folder in work.iterdir():
                if folder.is_dir() and not any(folder.iterdir()) and _job_is_done(session, folder.name):
                    folder.rmdir()

    if deleted:
        log.info("orphan_files_deleted", count=deleted)
    return deleted


def _job_is_done(session, folder_name: str) -> bool:
    try:
        job = session.get(Job, uuid.UUID(folder_name))
    except ValueError:
        return True  # not a job folder at all
    return job is None or job.status in FINAL_STATUSES


def run_cleanup() -> None:
    try:
        delete_expired_files()
        delete_orphan_files()
    except Exception:
        log.exception("cleanup_failed")


if __name__ == "__main__":
    from app.logging_setup import setup_logging

    setup_logging()
    run_cleanup()