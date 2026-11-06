import os
import signal
import socket
import time
import uuid

import structlog
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, ping
from app.logging_setup import setup_logging
from app.models import FileKind, FileReference, Job, JobStatus, StepStatus, utcnow
from app.steps import STEPS, StepError
from app.storage import full_path

setup_logging()
log = structlog.get_logger()

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
running = True


def stop(*_):
    global running
    running = False
    log.info("worker_stopping", worker_id=WORKER_ID)


def claim_next_job() -> uuid.UUID | None:
    with SessionLocal() as session:
        job = session.scalars(
            select(Job)
            .where(Job.status == JobStatus.PENDING)
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()

        if job is None:
            return None

        job.status = JobStatus.PROCESSING
        job.worker_id = WORKER_ID
        job.started_at = utcnow()
        job.heartbeat_at = utcnow()
        session.commit()
        return job.id


def process_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        current = job.input_file

        for step in job.steps:
            if step.status == StepStatus.COMPLETED:
                current = step.output_file
                continue

            structlog.contextvars.bind_contextvars(step=step.step_type, step_index=step.step_index)

            job.current_step_index = step.step_index
            job.heartbeat_at = utcnow()
            step.status = StepStatus.RUNNING
            step.started_at = utcnow()
            step.input_file = current
            step.attempts += 1
            session.commit()
            log.info("step_started")

            src = full_path(current.storage_path)
            dst = full_path(f"work/{job.id}/{step.step_index}-{step.step_type}")
            dst.parent.mkdir(parents=True, exist_ok=True)

            try:
                run = STEPS.get(step.step_type)
                if run is None:
                    raise StepError(f"step '{step.step_type}' is not implemented yet")
                result = run(src, dst, step.params or {})
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error_message = str(e)
                step.completed_at = utcnow()
                for later in job.steps[step.step_index + 1:]:
                    later.status = StepStatus.SKIPPED
                job.status = JobStatus.FAILED
                job.error_message = f"step {step.step_index} ({step.step_type}) failed: {e}"
                job.completed_at = utcnow()
                session.commit()
                log.warning("step_failed", error=str(e))
                return

            if result != src:
                current = FileReference(
                    kind=FileKind.INTERMEDIATE,
                    storage_path=str(result.relative_to(settings.storage_root)),
                    size_bytes=result.stat().st_size,
                )
                session.add(current)

            step.output_file = current
            step.status = StepStatus.COMPLETED
            step.progress_percent = 100
            step.completed_at = utcnow()
            session.commit()
            log.info("step_completed", duration_ms=step.duration_ms)

        if current.kind == FileKind.INTERMEDIATE:
            current.kind = FileKind.OUTPUT
        job.output_file = current
        job.status = JobStatus.COMPLETED
        job.completed_at = utcnow()
        session.commit()
        log.info("job_completed")


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ping()
    log.info("worker_started", worker_id=WORKER_ID)

    while running:
        job_id = claim_next_job()
        if job_id is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(job_id=str(job_id), worker_id=WORKER_ID)
        log.info("job_claimed")
        try:
            process_job(job_id)
        except Exception:
            log.exception("job_crashed")


if __name__ == "__main__":
    main()