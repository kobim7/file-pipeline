"""Worker loop.

Planned queue mechanism: Postgres as the queue.
  SELECT ... FROM jobs WHERE status='PENDING' ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1
lets several workers pull jobs safely without a separate broker.
"""
import signal
import time

import structlog

from app import db
from app.config import settings
from app.logging_setup import setup_logging

setup_logging()
log = structlog.get_logger()

_running = True


def _stop(*_):
    global _running
    _running = False
    log.info("worker_stopping")


def claim_next_job():
    """TODO: claim one PENDING job with FOR UPDATE SKIP LOCKED; return it or None."""
    return None


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.ping()
    log.info("worker_started")
    while _running:
        job = claim_next_job()
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue
        # TODO: run pipeline steps


if __name__ == "__main__":
    main()
