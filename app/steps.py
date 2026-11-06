import csv
import gzip
import json
import shutil
import zipfile
from pathlib import Path

import ijson
import structlog

log = structlog.get_logger()

JSON_VALUES = {"start_map", "start_array", "string", "number", "boolean", "null"}
FILTER_OPS = {"equals", "not_equals", "contains"}
CHUNK_SIZE = 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024  # 1GB, protects against zip bombs


class StepError(Exception):
    """The input is bad. Retrying won't help."""


def file_type(path: Path) -> str:
    return path.suffix.lstrip(".").lower()


# --- reading and writing records, one at a time ---

def read_records(path: Path):
    kind = file_type(path)
    if kind == "csv":
        with path.open(newline="", encoding="utf-8") as f:
            for line_no, row in enumerate(csv.DictReader(f), start=2):
                if None in row:
                    raise StepError(f"line {line_no} has more values than the header")
                yield row
    elif kind == "json":
        with path.open("rb") as f:
            for n, record in enumerate(ijson.items(f, "item", use_float=True), start=1):
                if not isinstance(record, dict):
                    raise StepError(f"record {n} is not an object")
                yield record
    else:
        raise StepError(f"can't read records from '.{kind}' files")


def write_records(path: Path, records, fieldnames: list | None = None) -> int:
    kind = file_type(path)
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as out:
        if kind == "json":
            out.write("[")
            for record in records:
                out.write(("," if rows else "") + "\n" + json.dumps(record, ensure_ascii=False))
                rows += 1
            out.write("\n]\n")

        elif kind == "csv":
            writer = None
            for record in records:
                if writer is None:
                    writer = csv.DictWriter(out, fieldnames=fieldnames or list(record))
                    writer.writeheader()
                extra = set(record) - set(writer.fieldnames)
                if extra:
                    raise StepError(f"record {rows + 1} has unexpected fields: {sorted(extra)}")
                writer.writerow({
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in record.items()
                })
                rows += 1
            if writer is None and fieldnames:
                csv.writer(out).writerow(fieldnames)

        else:
            raise StepError(f"can't write records to '.{kind}' files")
    return rows


# --- steps ---

def validate(src: Path, dst: Path, params: dict) -> Path:
    kind = file_type(src)
    expected = params.get("expected_type", kind)
    if kind != expected:
        raise StepError(f"expected a {expected} file, got '.{kind}'")

    if kind == "csv":
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

    elif kind == "json":
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
        raise StepError(f"can't validate '.{kind}' files")

    return src  # validate doesn't change the file


def convert(src: Path, dst: Path, params: dict) -> Path:
    source = file_type(src)
    target = params.get("output_format")
    if target not in ("csv", "json"):
        raise StepError("convert needs output_format: 'csv' or 'json'")
    if source == target:
        return src

    out = dst.with_suffix(f".{target}")
    try:
        rows = write_records(out, read_records(src))
        if rows == 0:
            raise StepError("no records found to convert")
    except Exception:
        out.unlink(missing_ok=True)
        raise

    log.info("file_converted", source=source, target=target, rows=rows)
    return out


def transform(src: Path, dst: Path, params: dict) -> Path:
    columns = params.get("select_columns")
    row_filter = params.get("filter")
    case = params.get("case")
    trim = params.get("trim", False)

    if case not in (None, "upper", "lower"):
        raise StepError("case must be 'upper' or 'lower'")
    if row_filter is not None:
        if "column" not in row_filter or "value" not in row_filter:
            raise StepError("filter needs 'column' and 'value'")
        if row_filter.get("op", "equals") not in FILTER_OPS:
            raise StepError(f"filter op must be one of {sorted(FILTER_OPS)}")

    needed = list(columns or []) + ([row_filter["column"]] if row_filter else [])

    def transformed():
        for n, record in enumerate(read_records(src), start=1):
            if n == 1:
                missing = [c for c in needed if c not in record]
                if missing:
                    raise StepError(f"unknown columns: {missing}")
            if row_filter and not _matches(record.get(row_filter["column"]), row_filter):
                continue
            if columns:
                record = {c: record.get(c) for c in columns}
            if trim or case:
                record = {k: _clean(v, trim, case) for k, v in record.items()}
            yield record

    out = dst.with_suffix(src.suffix)
    try:
        rows = write_records(out, transformed(), fieldnames=columns)
    except Exception:
        out.unlink(missing_ok=True)
        raise

    log.info("file_transformed", rows_kept=rows)
    return out


def _matches(value, row_filter: dict) -> bool:
    value = "" if value is None else str(value)
    target = str(row_filter["value"])
    op = row_filter.get("op", "equals")
    if op == "equals":
        return value == target
    if op == "not_equals":
        return value != target
    return target in value


def _clean(value, trim: bool, case: str | None):
    if not isinstance(value, str):
        return value
    if trim:
        value = value.strip()
    if case == "upper":
        value = value.upper()
    elif case == "lower":
        value = value.lower()
    return value


def compress(src: Path, dst: Path, params: dict) -> Path:
    algorithm = params.get("algorithm", "gzip")
    if algorithm != "gzip":
        raise StepError(f"unsupported algorithm '{algorithm}', only 'gzip' is supported")
    if file_type(src) == "gz":
        raise StepError("file is already gzip-compressed")

    out = dst.with_suffix(src.suffix + ".gz")
    try:
        with src.open("rb") as f, gzip.open(out, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(f, gz, length=CHUNK_SIZE)
    except Exception:
        out.unlink(missing_ok=True)
        raise

    log.info("file_compressed", original_bytes=src.stat().st_size, compressed_bytes=out.stat().st_size)
    return out


def decompress(src: Path, dst: Path, params: dict) -> Path:
    kind = file_type(src)
    try:
        if kind == "gz":
            inner_suffix = Path(src.stem).suffix
            out = dst.with_suffix(inner_suffix)
            with gzip.open(src, "rb") as f:
                size = _copy_limited(f, out)
        elif kind == "zip":
            with zipfile.ZipFile(src) as archive:
                member = _pick_zip_member(archive, params.get("member"))
                out = dst.with_suffix(Path(member).suffix.lower())
                with archive.open(member) as f:
                    size = _copy_limited(f, out)
        else:
            raise StepError(f"can't decompress '.{kind}' files, expected .gz or .zip")
    except (zipfile.BadZipFile, gzip.BadGzipFile, EOFError) as e:
        raise StepError(f"corrupted archive: {e}")

    log.info("file_decompressed", extracted_bytes=size)
    return out


def _pick_zip_member(archive: zipfile.ZipFile, wanted: str | None) -> str:
    files = [
        name for name in archive.namelist()
        if not name.endswith("/")
        and not name.startswith("__MACOSX/")
        and not Path(name).name.startswith("._")
    ]
    if wanted:
        if wanted not in files:
            raise StepError(f"'{wanted}' is not in the archive, it has: {files}")
        return wanted
    if len(files) != 1:
        raise StepError(f"archive has {len(files)} files, pick one with params.member: {files}")
    return files[0]


def _copy_limited(f, out: Path) -> int:
    size = 0
    try:
        with out.open("wb") as o:
            while chunk := f.read(CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_EXTRACTED_BYTES:
                    raise StepError("extracted file is too large")
                o.write(chunk)
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return size


STEPS = {
    "validate": validate,
    "transform": transform,
    "convert": convert,
    "compress": compress,
    "decompress": decompress,
}

# Params each step accepts. Anything else is rejected at upload time.
STEP_PARAMS = {
    "validate": {"expected_type"},
    "transform": {"filter", "select_columns", "case", "trim"},
    "convert": {"output_format"},
    "compress": {"algorithm"},
    "decompress": {"member"},
}