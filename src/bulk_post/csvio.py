"""CSV row counting, retry-file writer, and failure-log helpers."""

from __future__ import annotations

import csv
import datetime
import pathlib
from typing import IO, Any

from .http import _mask_headers
from .terminal import _BottomBar, _out


def detect_delimiter(path: str) -> str:
    """Best-effort detect the CSV delimiter, considering ``, ; \\t |``.

    Returns the detected single-character delimiter, or ``","`` when the file is
    empty, unreadable, or the delimiter cannot be determined — so comma files
    behave exactly as before.
    """
    try:
        with open(path, newline="", encoding="utf-8") as f:
            sample = f.read(65536)
    except OSError:
        return ","
    if not sample.strip():
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return ","
    return dialect.delimiter


def count_csv_rows(path: str) -> int:
    """Count data rows (excluding the header); returns 0 if the file can't be read."""
    try:
        delimiter = detect_delimiter(path)
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f, delimiter=delimiter))
    except OSError:
        return 0


def _open_retry_writer(
    retry_path: pathlib.Path, fieldnames: list
) -> tuple[IO[str], Any]:
    """Open the retry CSV and write its header. Returns (file, writer)."""
    f = open(retry_path, "w", newline="", encoding="utf-8")  # noqa: SIM115  # caller owns lifecycle
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    return f, writer


def _open_log_file(log_path: pathlib.Path) -> IO[str]:
    """Open (append) the failure log and write a run-start banner. Caller closes it."""
    f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115  # caller owns lifecycle
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f.write(f"\n{'=' * 60}\nRun started {ts}\n{'=' * 60}\n")
    f.flush()
    return f


def _write_failure_log(
    log_file: IO[str],
    kind: str,
    line_num: int,
    method: str,
    url: str,
    req_body: str | None,
    req_headers: dict,
    status: int | None,
    resp_body: str,
    resp_headers: dict,
    elapsed: float,
) -> None:
    """Append one human-readable failure entry to the log.

    Writes method/URL/headers/body/status/timing/response for request failures,
    or just the error message for substitution skips (``url == ""``). Request
    headers are masked via ``_mask_headers``.
    """
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    parts = [f"--- {kind}  row {line_num}  {ts} ---"]
    if url:
        parts.append(f"Method:   {method}")
        parts.append(f"URL:      {url}")
        if req_headers:
            parts.append("Req-Headers:")
            for k, v in _mask_headers(req_headers).items():
                parts.append(f"  {k}: {v}")
        if req_body:
            parts.append(f"Body:     {req_body}")
        status_str = str(status) if status is not None else "ERR"
        parts.append(f"Status:   {status_str}")
        parts.append(f"Elapsed:  {elapsed * 1000:.0f} ms")
        if resp_headers:
            parts.append("Resp-Headers:")
            for k, v in resp_headers.items():
                parts.append(f"  {k}: {v}")
        if resp_body:
            parts.append(f"Response: {resp_body.strip()[:1000]}")
    else:
        parts.append(f"Error:    {resp_body}")
    parts.append("")
    log_file.write("\n".join(parts) + "\n")
    log_file.flush()


def _skip_rows(reader: csv.DictReader, count: int, bar: _BottomBar | None) -> None:
    """Advance ``reader`` past ``count`` data rows (used to implement ``--offset``)."""
    if count:
        _out(bar, f"Skipping {count} rows, starting from row {count + 1}.")
        for _ in range(count):
            next(reader, None)
