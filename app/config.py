from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./local.db"
    storage_root: str = "./data"
    max_upload_bytes: int = 100 * 1024 * 1024
    retention_hours: int = 24
    worker_poll_seconds: float = 1.0
    log_level: str = "INFO"


settings = Settings()
