"""CSV row counting, retry-file writer, and failure-log helpers."""

from __future__ import annotations

import csv
import datetime
import pathlib
from typing import IO, Any

from .http import _mask_headers


def count_csv_rows(path: str) -> int:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
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


def _out(bar: Any, text: str) -> None:
    if bar:
        bar.write_line(text)
    else:
        print(text)


def _skip_rows(reader: csv.DictReader, count: int, bar: Any) -> None:
    if count:
        _out(bar, f"Skipping {count} rows, starting from row {count + 1}.")
        for _ in range(count):
            next(reader, None)
