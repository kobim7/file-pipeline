import logging

import structlog

from app.config import settings


def setup_logging() -> None:
    """JSON logs. Bind job context with: log = structlog.get_logger().bind(job_id=...)."""
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
