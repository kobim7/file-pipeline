from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./local.db"
    storage_root: str = "./data"
    max_upload_bytes: int = 100 * 1024 * 1024
    retention_hours: int = 24
    worker_poll_seconds: float = 1.0
    log_level: str = "INFO"
    api_keys: str = ""
    step_max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    heartbeat_seconds: float = 10.0
    stale_job_seconds: float = 60.0
    step_timeout_seconds: float = 600.0
    
settings = Settings()
