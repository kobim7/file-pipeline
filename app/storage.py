import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings

CHUNK_SIZE = 1024 * 1024  # 1MB


class FileTooLarge(Exception):
    pass


def full_path(storage_path: str) -> Path:
    return Path(settings.storage_root) / storage_path


def upload_suffix(filename: str) -> str:
    """'.csv' for data.csv, but '.csv.gz' for data.csv.gz, so we still know what's inside."""
    name = Path(filename)
    suffix = name.suffix.lower()
    if suffix == ".gz":
        suffix = Path(name.stem).suffix.lower() + suffix
    return suffix


def save_upload(upload: UploadFile, folder: str) -> tuple[str, int]:
    suffix = upload_suffix(upload.filename or "")
    storage_path = f"{folder}/{uuid.uuid4()}{suffix}"
    dest = full_path(storage_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := upload.file.read(CHUNK_SIZE):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise FileTooLarge()
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    return storage_path, size
