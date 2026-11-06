import csv
import ijson
from pathlib import Path

import structlog

log = structlog.get_logger()
JSON_VALUES = {"start_map", "start_array", "string", "number", "boolean", "null"}

class StepError(Exception):
    """The input is bad. Retrying won't help."""


def validate(src: Path, dst: Path, params: dict) -> Path:
    file_type = src.suffix.lstrip(".").lower()
    expected = params.get("expected_type", file_type)
    if file_type != expected:
        raise StepError(f"expected a {expected} file, got '.{file_type}'")

    if file_type == "csv":
        with src.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                raise StepError("csv file has no header row")
            rows = 0
            for line_no, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) != len(header):
                    raise StepError(
                        f"line {line_no}: expected {len(header)} columns, got {len(row)}"
                    )
                rows += 1
        log.info("file_validated", file_type="csv", columns=header, rows=rows)

    elif file_type == "json":
            records = 0
            with src.open("rb") as f:
                try:
                    for prefix, event, _ in ijson.parse(f):
                        if prefix == "item" and event in JSON_VALUES:
                            records += 1
                except ijson.JSONError as e:
                    raise StepError(f"invalid json: {str(e).splitlines()[0]}")
            log.info("file_validated", file_type="json", records=records)

    else:
        raise StepError(f"can't validate '.{file_type}' files")

    return src  # validate doesn't change the file


STEPS = {
    "validate": validate,
}