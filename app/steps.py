import csv
import json
from pathlib import Path

import ijson
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


def convert(src: Path, dst: Path, params: dict) -> Path:
    source = src.suffix.lstrip(".").lower()
    target = params.get("output_format")
    if target not in ("csv", "json"):
        raise StepError("convert needs output_format: 'csv' or 'json'")
    if source == target:
        return src

    out = dst.with_suffix(f".{target}")
    try:
        if source == "csv" and target == "json":
            rows = _csv_to_json(src, out)
        elif source == "json" and target == "csv":
            rows = _json_to_csv(src, out)
        else:
            raise StepError(f"can't convert .{source} to {target}")
    except Exception:
        out.unlink(missing_ok=True)
        raise

    log.info("file_converted", source=source, target=target, rows=rows)
    return out


def _csv_to_json(src: Path, out: Path) -> int:
    rows = 0
    with src.open(newline="", encoding="utf-8") as f, out.open("w", encoding="utf-8") as o:
        o.write("[")
        for row in csv.DictReader(f):
            if rows:
                o.write(",")
            o.write("\n" + json.dumps(row, ensure_ascii=False))
            rows += 1
        o.write("\n]\n")
    return rows


def _json_to_csv(src: Path, out: Path) -> int:
    rows = 0
    with src.open("rb") as f, out.open("w", newline="", encoding="utf-8") as o:
        writer = None
        for record in ijson.items(f, "item", use_float=True):
            if not isinstance(record, dict):
                raise StepError(f"record {rows + 1} is not an object")
            if writer is None:
                writer = csv.DictWriter(o, fieldnames=list(record))
                writer.writeheader()
            extra = set(record) - set(writer.fieldnames)
            if extra:
                raise StepError(f"record {rows + 1} has unexpected fields: {sorted(extra)}")
            writer.writerow({
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                for k, v in record.items()
            })
            rows += 1
    if rows == 0:
        raise StepError("json must be a non-empty list of objects")
    return rows


STEPS = {
    "validate": validate,
    "convert": convert,
}