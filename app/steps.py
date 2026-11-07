import codecs
import csv
import gzip
import io
import itertools
import json
import shutil
import zipfile
from contextlib import contextmanager
from pathlib import Path

import ijson
import structlog

log = structlog.get_logger()

JSON_VALUES = {"start_map", "start_array", "string", "number", "boolean", "null"}
FILTER_OPS = {"equals", "not_equals", "contains"}
CSV_DELIMITERS = [",", ";", "\t", "|"]
DATA_SUFFIXES = {".csv", ".json"}
TABLE_TYPES = {"csv", "txt"}  # .txt is read as delimited text, e.g. Excel's "Text (Tab delimited)"
READ_OPTIONS = {"encoding", "delimiter", "has_header"}  # how to read the input, for steps that read records
CHUNK_SIZE = 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024  # 1GB, protects against zip bombs


class StepError(Exception):
    """The input is bad. Retrying won't help."""


def file_type(path: Path) -> str:
    return path.suffix.lstrip(".").lower()


def text_encoding(path: Path) -> str:
    """UTF-8 by default. A BOM at the start tells us otherwise (Excel adds one)."""
    with path.open("rb") as f:
        start = f.read(4)
    if start.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if start.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8"


def detect_delimiter(f) -> str:
    """Pick the delimiter that splits the first rows into the same number of columns.

    Looking only at the header isn't enough: a header like "Last, First";City has a comma
    inside quotes but is delimited with ';'. Falls back to the most common one in the header.
    """
    sample = f.read(65536)
    f.seek(0)
    if len(sample) == 65536 and "\n" in sample:
        sample = sample[: sample.rfind("\n") + 1]  # don't parse a cut-off last line

    best, best_columns = None, 1
    for delimiter in CSV_DELIMITERS:
        try:
            rows = [r for r in itertools.islice(csv.reader(io.StringIO(sample), delimiter=delimiter), 50) if r]
        except csv.Error:
            continue
        if not rows or len(rows[0]) < 2:
            continue
        same_width = sum(len(r) == len(rows[0]) for r in rows) / len(rows)
        if same_width >= 0.9 and len(rows[0]) > best_columns:
            best, best_columns = delimiter, len(rows[0])
    if best:
        return best

    header = sample.lstrip("\r\n").split("\n", 1)[0]
    counts = {d: header.count(d) for d in CSV_DELIMITERS}
    most = max(counts, key=counts.get)
    return most if counts[most] else ","


class _Utf8Stream:
    """Feeds ijson UTF-8 bytes from a file in another encoding."""

    def __init__(self, text_file):
        self.text_file = text_file

    def read(self, size=-1):
        return self.text_file.read(size).encode("utf-8")


@contextmanager
def open_json(path: Path, encoding: str | None = None):
    encoding = encoding or text_encoding(path)
    if encoding == "utf-8":
        with path.open("rb") as f:
            yield f
    else:  # has a BOM: decode it ourselves, ijson only reads plain UTF-8
        with path.open(encoding=encoding) as f:
            yield _Utf8Stream(f)


@contextmanager
def bad_input_is_step_error():
    """Encoding and format errors are problems with the file, so retrying won't help."""
    try:
        yield
    except UnicodeDecodeError as e:
        raise StepError(
            f"file is not valid {e.encoding} text (unexpected byte {e.object[e.start]:#04x}); "
            "save it as UTF-8, or set params.encoding (for example \"cp1252\")"
        ) from e
    except csv.Error as e:
        raise StepError(f"malformed csv (often an unclosed quote): {e}") from e
    except ijson.JSONError as e:
        raise StepError(f"invalid json: {_first_line(e)}") from e


def _first_line(e: Exception) -> str:
    message = e.args[0] if e.args else str(e)
    if isinstance(message, bytes):
        message = message.decode(errors="replace")
    return str(message).strip().splitlines()[0]


# --- reading tables (csv, txt) ---

class Table:
    def __init__(self, header, rows, encoding, delimiter):
        self.header = header
        self.rows = rows  # generator of dicts, one per data row
        self.encoding = encoding
        self.delimiter = delimiter


@contextmanager
def open_table(path: Path, options: dict | None = None):
    """The one place that decides how a csv/txt file splits into columns and rows."""
    options = options or {}
    kind = file_type(path)
    encoding = _encoding_option(options) or text_encoding(path)
    has_header = options.get("has_header", True)
    if not isinstance(has_header, bool):
        raise StepError("has_header must be true or false")

    with bad_input_is_step_error(), path.open(newline="", encoding=encoding) as f:
        delimiter = options.get("delimiter") or detect_delimiter(f)
        detected = "delimiter" not in options
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise StepError("delimiter must be a single character, for example \",\" or \"\\t\"")

        # strict: an unclosed or misplaced quote is an error, not a value that swallows the file
        reader = csv.reader(f, delimiter=delimiter, strict=True)
        first = next((row for row in reader if not _blank(row)), None)
        if first is None:
            raise StepError(f"{kind} file is empty")

        if has_header:
            header = [name.strip() for name in first]
            pending = []
        else:
            header = [f"column_{i}" for i in range(1, len(first) + 1)]
            pending = [first]
        _check_header(header)

        if kind == "txt" and detected and len(header) < 2:
            raise StepError(
                "txt files are read as delimited tables and need at least 2 columns, "
                "this looks like plain text (if it is a table, set params.delimiter)"
            )

        def rows():
            for row in itertools.chain(pending, reader):
                if _blank(row):
                    continue
                if len(row) != len(header):
                    raise StepError(f"line {reader.line_num}: expected {len(header)} columns, got {len(row)}")
                yield dict(zip(header, row))

        yield Table(header, rows(), encoding, delimiter)


def _blank(row: list) -> bool:
    return not row or (len(row) == 1 and not row[0].strip())


def _check_header(header: list[str]) -> None:
    for i, name in enumerate(header, start=1):
        if not name:
            raise StepError(f"column {i} has no name in the header row (or set params.has_header to false)")
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise StepError(f"duplicate column names in the header: {duplicates}")


def _encoding_option(options: dict) -> str | None:
    encoding = options.get("encoding")
    if encoding is None:
        return None
    try:
        codecs.lookup(encoding)
    except (LookupError, TypeError):
        raise StepError(f"unknown encoding '{encoding}'")
    return encoding


# --- reading and writing records, one at a time ---

def read_records(path: Path, options: dict | None = None):
    options = options or {}
    kind = file_type(path)
    if kind in TABLE_TYPES:
        with open_table(path, options) as table:
            yield from table.rows
    elif kind == "json":
        with bad_input_is_step_error(), open_json(path, _encoding_option(options)) as f:
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

        elif kind in TABLE_TYPES:
            delimiter = "\t" if kind == "txt" else ","
            writer = None
            for record in records:
                if writer is None:
                    writer = csv.DictWriter(out, fieldnames=fieldnames or list(record), delimiter=delimiter)
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
                csv.writer(out, delimiter=delimiter).writerow(fieldnames)

        else:
            raise StepError(f"can't write records to '.{kind}' files")
    return rows


# --- steps ---

def validate(src: Path, dst: Path, params: dict) -> Path:
    kind = file_type(src)
    expected = params.get("expected_type", kind)
    if kind != expected:
        raise StepError(f"expected a {expected} file, got '.{kind}'")

    if kind in TABLE_TYPES:
        with open_table(src, params) as table:
            rows = sum(1 for _ in table.rows)
        log.info("file_validated", file_type=kind, encoding=table.encoding, delimiter=table.delimiter,
                 columns=table.header, rows=rows)

    elif kind == "json":
        records = 0
        with bad_input_is_step_error(), open_json(src, _encoding_option(params)) as f:
            for prefix, event, _ in ijson.parse(f):
                if prefix == "item" and event in JSON_VALUES:
                    records += 1
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
        rows = write_records(out, read_records(src, params))
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
        for n, record in enumerate(read_records(src, params), start=1):
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
            if not inner_suffix:
                raise StepError("can't tell what's inside the .gz file, name it like data.csv.gz")
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
            raise StepError(f"'{wanted}' is not in the archive ({_preview(files)})")
        return wanted

    data_files = [name for name in files if Path(name).suffix.lower() in DATA_SUFFIXES]
    if not data_files:
        # .txt only counts when there's nothing better, so a readme.txt next to data.csv is ignored
        data_files = [name for name in files if Path(name).suffix.lower() == ".txt"]
    if len(data_files) == 1:
        return data_files[0]
    if not data_files:
        raise StepError(f"archive has no csv, json or txt files ({_preview(files)})")
    raise StepError(
        f"archive has {len(data_files)} data files, pick one with params.member ({_preview(data_files)})"
    )


def _preview(names: list[str], limit: int = 10) -> str:
    """Keep error messages short: a zip can hold thousands of files."""
    shown = ", ".join(names[:limit])
    more = f", and {len(names) - limit} more" if len(names) > limit else ""
    return f"{len(names)} files: {shown}{more}"


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
    "validate": {"expected_type"} | READ_OPTIONS,
    "transform": {"filter", "select_columns", "case", "trim"} | READ_OPTIONS,
    "convert": {"output_format"} | READ_OPTIONS,
    "compress": {"algorithm"},
    "decompress": {"member"},
}
