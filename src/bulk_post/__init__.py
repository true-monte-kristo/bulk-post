#!/usr/bin/env python3
"""
Bulk HTTP request script — iterates over a CSV file and fires a request per row.

Usage:
    python bulk_post.py \
        -u "https://example.com/api/invoices/{{id}}/cancel" \
        -c rows.csv \
        -m DELETE \
        -d 200

    python bulk_post.py \
        -u "https://example.com/api/invoices/{{id}}/status" \
        -c rows.csv \
        -m PATCH \
        -b '{"status": "cancelled"}'

Token resolution order: --token/-t flag → BULK_TOKEN env var → interactive prompt.
If the token expires mid-run (401), the script pauses and asks for a new one.

The CSV header must contain columns matching every {{variable}} in the URL.
Failed rows are logged and skipped; execution always continues.
"""

import argparse
import base64
import contextlib
import csv
import dataclasses
import importlib.metadata
import os
import pathlib
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import IO, Any, cast

from .csvio import (
    _open_log_file,
    _open_retry_writer,
    _skip_rows,
    _write_failure_log,
)
from .csvio import (
    count_csv_rows as count_csv_rows,
)
from .http import http_request
from .state import _ParallelState, _WorkflowParallelState
from .templating import (
    PLACEHOLDER_RE as PLACEHOLDER_RE,
)
from .templating import (
    _validate_body_template as _validate_body_template,
)
from .templating import (
    _validate_placeholders,
    substitute,
)
from .terminal import (
    _CMD_EXIT,
    _CMD_PAUSE,
    _CMD_RESUME,
    _GREEN,
    _GREY,
    _HAS_TERMIOS,
    _RED,
    _RESET,
    _BottomBar,
    _out,
    _poll_cmd,
    _progress,
    _wait_for_resume,
    print_verbose,
)
from .terminal import (
    _CMD_PROMPT as _CMD_PROMPT,
)
from .terminal import (
    _COMMANDS as _COMMANDS,
)
from .terminal import (
    _CYAN as _CYAN,
)
from .terminal import (
    _GHOST as _GHOST,
)
from .terminal import (
    _TERRACOTTA as _TERRACOTTA,
)
from .terminal import (
    BAR_WIDTH as BAR_WIDTH,
)
from .terminal import (
    _get_suggestion as _get_suggestion,
)
from .terminal import (
    _render_bar as _render_bar,
)
from .terminal import (
    _stdin_command as _stdin_command,
)
from .terminal import (
    print_progress as print_progress,
)

try:
    import yaml as _yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    _yaml = None  # type: ignore[assignment]

_QUEUE_MAXSIZE = 500  # max rows buffered in memory in parallel mode

_WORKFLOW_STEP_COL = "_bulk_post_step"


def _get_version() -> str:
    try:
        return importlib.metadata.version("bulk-post")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Workflow data structures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WorkflowStep:
    path: str  # "groupA/call-example-api"
    url: str
    method: str
    body: str | None
    content_type: str
    headers: dict  # without Content-Type
    auth_type: str  # "bearer", "basic", "none"
    auth_raw: str  # raw credential: token or user:pass
    on_error: str  # "stop" or "continue"


def parse_workflow(yaml_path: str) -> tuple[list, str | None]:
    """
    Parse a workflow YAML file. Returns (steps, error_or_None).
    Steps are WorkflowStep objects in document order.
    """
    if not _HAS_YAML:
        return [], "PyYAML is required for --workflow. Install with: pip install pyyaml"
    try:
        with open(yaml_path, encoding="utf-8") as f:
            doc = _yaml.safe_load(f)
    except OSError as e:
        return [], f"Cannot open workflow file: {e}"
    except _yaml.YAMLError as e:
        return [], f"Invalid YAML in workflow file: {e}"

    if not isinstance(doc, dict) or "workflow" not in doc:
        return [], "Workflow file must have a top-level 'workflow' key"

    wf = doc["workflow"]
    if not isinstance(wf, dict):
        return [], "'workflow' must be a mapping"

    steps: list = []
    seen_paths: set = set()

    for group_name, group_data in wf.items():
        if group_name == "description":
            continue
        if not isinstance(group_data, dict):
            return [], f"Group '{group_name}' must be a mapping"

        group_auth = group_data.get("auth", {}) or {}
        group_auth_type = (group_auth.get("type") or "none").lower()
        if group_auth_type == "bearer":
            group_auth_raw = group_auth.get("token") or ""
        elif group_auth_type == "basic":
            user = group_auth.get("user") or ""
            pw = group_auth.get("password") or ""
            group_auth_raw = f"{user}:{pw}" if (user or pw) else ""
        else:
            group_auth_type = "none"
            group_auth_raw = ""

        endpoints = group_data.get("endpoints")
        if not endpoints:
            continue
        if not isinstance(endpoints, list):
            return [], f"Group '{group_name}'.endpoints must be a list"

        for entry in endpoints:
            if not isinstance(entry, dict):
                return [], f"Each endpoint in group '{group_name}' must be a mapping"

            # The endpoint name is the first key whose value is itself a dict,
            # OR the entry itself may be a flat dict (indentation style without a
            # nested name key) — in that case derive a synthetic name.
            name = None
            ep_data = None
            for k, v in entry.items():
                if isinstance(v, dict):
                    name = k
                    ep_data = v
                    break

            if ep_data is None:
                # Flat style: the entry dict IS the endpoint data; use first key as name.
                name = next(iter(entry))
                ep_data = entry
                # Remove the name key if it has no dict value (it's just the name string)
                ep_data = {
                    k: v for k, v in entry.items() if k != name or isinstance(v, dict)
                }
                if not ep_data:
                    ep_data = entry  # treat whole dict as data

            name = name or "unnamed"
            path = f"{group_name}/{name}"

            # Ensure unique paths within the group
            base_path = path
            suffix = 1
            while path in seen_paths:
                path = f"{base_path}_{suffix}"
                suffix += 1
            seen_paths.add(path)

            url = ep_data.get("url") or entry.get("url") or ""
            if not url:
                return [], f"Endpoint '{path}' is missing 'url'"

            method = (ep_data.get("method") or entry.get("method") or "POST").upper()
            body = ep_data.get("body") or entry.get("body") or None

            raw_headers = dict(ep_data.get("headers") or entry.get("headers") or {})
            # Extract Content-Type from headers (case-insensitive)
            content_type = "application/json"
            ct_key = next((k for k in raw_headers if k.lower() == "content-type"), None)
            if ct_key:
                content_type = raw_headers.pop(ct_key)

            # Step-level auth overrides group auth
            step_auth = ep_data.get("auth") or entry.get("auth") or None
            if step_auth and isinstance(step_auth, dict):
                auth_type = (step_auth.get("type") or "none").lower()
                if auth_type == "bearer":
                    auth_raw = step_auth.get("token") or ""
                elif auth_type == "basic":
                    user = step_auth.get("user") or ""
                    pw = step_auth.get("password") or ""
                    auth_raw = f"{user}:{pw}" if (user or pw) else ""
                else:
                    auth_type = "none"
                    auth_raw = ""
            else:
                auth_type = group_auth_type
                auth_raw = group_auth_raw

            on_error = (
                ep_data.get("on_error") or entry.get("on_error") or "stop"
            ).lower()
            if on_error not in ("stop", "continue"):
                on_error = "stop"

            steps.append(
                WorkflowStep(
                    path=path,
                    url=url,
                    method=method,
                    body=body,
                    content_type=content_type,
                    headers=raw_headers,
                    auth_type=auth_type,
                    auth_raw=auth_raw,
                    on_error=on_error,
                )
            )

    if not steps:
        return [], "Workflow defines no endpoints"
    return steps, None


def _validate_workflow_placeholders(steps: list, fieldnames: list) -> str | None:
    """Return an error string if any step references a missing CSV column, else None."""
    for step in steps:
        placeholders = PLACEHOLDER_RE.findall(step.url)
        if step.body:
            placeholders += PLACEHOLDER_RE.findall(step.body)
        for val in step.headers.values():
            placeholders += PLACEHOLDER_RE.findall(str(val))
        missing = [p for p in placeholders if p not in fieldnames]
        if missing:
            return f"Step '{step.path}': CSV is missing columns for placeholders: {missing}"
    return None


def _resolve_workflow_auth_headers(
    steps: list,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> tuple[dict, str | None]:
    """
    Resolve auth for all workflow steps. Returns (auth_headers_by_path, error_or_None).
    Deduplicates: same (auth_type, auth_raw) prompts only once.
    """
    resolved: dict = {}  # (auth_type, auth_raw) -> auth_header string or None
    result: dict = {}  # step.path -> auth_header string or None

    for step in steps:
        key = (step.auth_type, step.auth_raw)
        if key not in resolved:
            if step.auth_type == "none":
                resolved[key] = None
            elif step.auth_type == "bearer":
                token = resolve_token(
                    step.auth_raw or None, suspend=suspend, resume=resume
                )
                resolved[key] = f"Bearer {token}"
            else:
                creds = resolve_basic_creds(
                    step.auth_raw or None, suspend=suspend, resume=resume
                )
                resolved[key] = f"Basic {base64.b64encode(creds.encode()).decode()}"
        result[step.path] = resolved[key]

    return result, None


def _fire_workflow_step(
    step: WorkflowStep,
    row: dict,
    auth_header: str | None,
    timeout: int,
    auth_refresh_fn: Callable | None = None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> tuple[int | None, str, float, str, str | None, str | None, dict, dict]:
    """
    Fire a single workflow step for one CSV row.
    Returns (status, body, elapsed, final_url, new_auth_header_or_None, req_body, req_headers, resp_headers).
    Returns (None, err_message, 0, "", None, None, {}, {}) on substitution error.
    """
    url, err = substitute(step.url, row)
    if err:
        return None, err, 0.0, "", None, None, {}, {}

    req_body: str | None = None
    if step.body:
        req_body, err = substitute(step.body, row)
        if err:
            return None, err, 0.0, url, None, None, {}, {}

    extra_headers: dict = {}
    for k, v in step.headers.items():
        val, herr = substitute(str(v), row)
        if herr:
            return None, herr, 0.0, url, None, None, {}, {}
        extra_headers[k] = val

    status, body, elapsed, req_headers, resp_headers = http_request(
        url,
        auth_header,
        step.method,
        req_body,
        timeout,
        step.content_type,
        extra_headers,
    )

    new_auth_header: str | None = None
    if status == 401 and step.auth_type != "none":
        if auth_refresh_fn is not None:
            new_auth_header = auth_refresh_fn(auth_header)
        elif step.auth_type == "bearer":
            refreshed = prompt_new_token(suspend=suspend, resume=resume)
            new_auth_header = f"Bearer {refreshed}"
        else:
            refreshed = prompt_new_basic_creds(suspend=suspend, resume=resume)
            new_auth_header = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
        if new_auth_header:
            status, body, elapsed, req_headers, resp_headers = http_request(
                url,
                new_auth_header,
                step.method,
                req_body,
                timeout,
                step.content_type,
                extra_headers,
            )

    return (
        status,
        body,
        elapsed,
        url,
        new_auth_header,
        req_body,
        req_headers,
        resp_headers,
    )


def resolve_token(
    flag_value: str | None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if flag_value:
        return flag_value
    env = os.environ.get("BULK_TOKEN", "").strip()
    if env:
        return env
    if suspend:
        suspend()
    try:
        token = input("Paste your Bearer token: ").strip()
    except EOFError:
        token = ""
    if resume:
        resume()
    if not token:
        print("[ERROR] No token provided.", file=sys.stderr)
        sys.exit(1)
    return token


def prompt_new_token(
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if suspend:
        suspend()
    print("\n[AUTH]  Token expired (401). Grab a fresh token from browser DevTools.")
    try:
        token = input("Paste new Bearer token: ").strip()
    except EOFError:
        token = ""
    if resume:
        resume()
    if not token:
        print("[ERROR] No token provided — aborting.", file=sys.stderr)
        sys.exit(1)
    return token


def resolve_basic_creds(
    flag_value: str | None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if flag_value:
        return flag_value
    env = os.environ.get("BULK_USER", "").strip()
    if env:
        return env
    if suspend:
        suspend()
    try:
        creds = input("Basic auth credentials (user:pass): ").strip()
    except EOFError:
        creds = ""
    if resume:
        resume()
    if not creds:
        print("[ERROR] No credentials provided.", file=sys.stderr)
        sys.exit(1)
    return creds


def prompt_new_basic_creds(
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if suspend:
        suspend()
    print("\n[AUTH]  Credentials rejected (401). Enter new credentials.")
    try:
        creds = input("Basic auth credentials (user:pass): ").strip()
    except EOFError:
        creds = ""
    if resume:
        resume()
    if not creds:
        print("[ERROR] No credentials provided — aborting.", file=sys.stderr)
        sys.exit(1)
    return creds


def resolve_auth_header(
    args: argparse.Namespace,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str | None:
    if args.auth_type == "none":
        return None
    if args.auth_type == "bearer":
        token = resolve_token(args.token, suspend=suspend, resume=resume)
        return f"Bearer {token}"
    creds = resolve_basic_creds(args.user, suspend=suspend, resume=resume)
    return f"Basic {base64.b64encode(creds.encode()).decode()}"


# ---------------------------------------------------------------------------
# _run helpers
# ---------------------------------------------------------------------------


def _make_auth_refresh_fn(
    args,
    state: "_ParallelState",
    suspend: Callable | None,
    resume: Callable | None,
) -> Callable:
    """Return a thread-safe 401-refresh closure for parallel workers."""

    def refresh(old_auth_header: str | None) -> str | None:
        with state.auth_lock:
            # Another thread already refreshed while we waited for the lock.
            if state.auth_header != old_auth_header:
                return state.auth_header
            with state.output_lock:
                if suspend:
                    suspend()
                try:
                    if args.auth_type == "bearer":
                        refreshed = prompt_new_token()
                        new = f"Bearer {refreshed}"
                    else:
                        refreshed = prompt_new_basic_creds()
                        new = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
                finally:
                    if resume:
                        resume()
            state.auth_header = new
            return new

    return refresh


def _fire(
    row: dict,
    args,
    auth_header: str | None,
    suspend: Callable | None,
    resume: Callable | None,
    auth_refresh_fn: Callable | None = None,
) -> tuple[int | None, str, float, str, str | None, str | None, dict, dict]:
    """
    Substitute placeholders, fire the request (with one 401 retry), return
    (status, response_body, elapsed, final_url, new_auth_header_or_None, req_body, req_headers, resp_headers).
    Returns (None, err_message, 0, "", None, None, {}, {}) on substitution error.
    """
    url, err = substitute(args.url, row)
    if err:
        return None, err, 0.0, "", None, None, {}, {}

    req_body: str | None = None
    if args.body:
        req_body, err = substitute(cast(str, args.body), row)
        if err:
            return None, err, 0.0, url, None, None, {}, {}
        ct = args.content_type
    else:
        ct = "application/json"

    extra_headers: dict = {}
    for raw in args.header or []:
        name, _, val_tmpl = raw.partition(": ")
        val, herr = substitute(val_tmpl, row)
        if herr:
            return None, herr, 0.0, url, None, None, {}, {}
        extra_headers[name] = val

    status, body, elapsed, req_headers, resp_headers = http_request(
        url, auth_header, args.method, req_body, args.timeout, ct, extra_headers
    )

    new_auth_header: str | None = None
    if status == 401 and args.auth_type != "none":
        if auth_refresh_fn is not None:
            new_auth_header = auth_refresh_fn(auth_header)
        elif args.auth_type == "bearer":
            refreshed = prompt_new_token(suspend=suspend, resume=resume)
            new_auth_header = f"Bearer {refreshed}"
        else:
            refreshed = prompt_new_basic_creds(suspend=suspend, resume=resume)
            new_auth_header = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
        status, body, elapsed, req_headers, resp_headers = http_request(
            url, new_auth_header, args.method, req_body, args.timeout, ct, extra_headers
        )

    return (
        status,
        body,
        elapsed,
        url,
        new_auth_header,
        req_body,
        req_headers,
        resp_headers,
    )


def _log_row(
    bar: _BottomBar | None,
    args,
    line_num: int,
    status: int | None,
    body: str,
    elapsed: float,
    url: str,
    req_body: str | None,
    req_headers: dict,
    resp_headers: dict,
    thread_tag: str = "",
    method_override: str | None = None,
    row_label: str | None = None,
) -> bool:
    """Print per-row output. Returns True if the row succeeded."""
    method = method_override if method_override is not None else args.method
    label = row_label if row_label is not None else f"row {line_num}"
    if args.verbose:
        print_verbose(
            bar, method, url, req_body, req_headers, status, body, resp_headers, elapsed
        )

    if status is not None and 200 <= status < 300:
        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
        _out(
            bar,
            f"{_GREEN}{thread_tag}[OK]    {label}: {status} {url}{elapsed_str}{_RESET}",
        )
        return True
    else:
        short_body = body[:200].replace("\n", " ")
        status_str = str(status) if status is not None else "ERR"
        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
        _out(
            bar,
            f"{_RED}{thread_tag}[FAIL]  {label}: {status_str} {url}{elapsed_str} | {short_body}{_RESET}",
        )
        return False


def _handle_cmd_in_loop(
    cmd: str | None,
    bar: _BottomBar | None,
    line_num: int,
    ok: int,
    failed: int,
) -> bool:
    """Return True if the row loop should stop."""
    if cmd == _CMD_EXIT:
        _out(
            bar,
            f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}",
        )
        return True
    if cmd == _CMD_PAUSE:
        if bar:
            bar.write_line("[PAUSED]  Type /resume to continue...")
            while True:
                time.sleep(0.1)
                paused_cmd = bar.poll()
                if paused_cmd == _CMD_RESUME:
                    bar.write_line("[RESUMED]")
                    break
                if paused_cmd == _CMD_EXIT:
                    _out(
                        bar,
                        f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}",
                    )
                    return True
        else:
            if _wait_for_resume():
                _out(
                    bar,
                    f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}",
                )
                return True
    return False


def _run_loop(
    reader: csv.DictReader,
    args: argparse.Namespace,
    auth_header: str | None,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: IO[str],
    offset: int,
    total_rows: int,
) -> tuple[int, int, int]:
    remaining = total_rows - offset
    processed = ok = failed = 0
    for line_num, row in enumerate(reader, start=offset + 1):
        processed += 1
        absolute = offset + processed
        (
            status,
            body,
            elapsed,
            url,
            new_auth_header,
            req_body,
            req_headers,
            resp_headers,
        ) = _fire(row, args, auth_header, suspend, resume)
        if new_auth_header:
            auth_header = new_auth_header
        if status is None and not url:
            failed += 1
            retry_writer.writerow(row)
            _out(bar, f"{_RED}[SKIP]  row {line_num}: {body} | row={dict(row)}{_RESET}")
            _write_failure_log(
                log_file,
                "SKIP",
                line_num,
                args.method,
                "",
                None,
                {},
                None,
                body,
                {},
                0.0,
            )
            _progress(bar, absolute, total_rows)
            continue
        succeeded = _log_row(
            bar,
            args,
            line_num,
            status,
            body,
            elapsed,
            url,
            req_body,
            req_headers,
            resp_headers,
        )
        ok += int(succeeded)
        if not succeeded:
            failed += 1
            retry_writer.writerow(row)
            _write_failure_log(
                log_file,
                "FAIL",
                line_num,
                args.method,
                url,
                req_body,
                req_headers,
                status,
                body,
                resp_headers,
                elapsed,
            )
        _progress(bar, absolute, total_rows)
        cmd = _poll_cmd(bar)
        if _handle_cmd_in_loop(cmd, bar, line_num, ok, failed):
            break
        if args.delay > 0 and processed < remaining:
            time.sleep(args.delay / 1000)
    return ok, failed, processed


def _parallel_worker(
    work_queue: "queue.Queue[tuple[int, dict]]",
    args: argparse.Namespace,
    state: _ParallelState,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: "IO[str]",
    total_rows: int,
    auth_refresh_fn: Callable,
    debug: bool = False,
) -> None:
    thread_tag = f"[{threading.current_thread().name}] " if debug else ""
    while True:
        if state.stop_event.is_set():
            break
        state.pause_event.wait()
        try:
            item = work_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is None:  # poison pill — no more rows
            work_queue.task_done()
            break
        line_num, row = item

        with state.lock:
            auth_header = state.auth_header
            state.in_flight += 1

        try:
            (
                status,
                body,
                elapsed,
                url,
                new_auth_header,
                req_body,
                req_headers,
                resp_headers,
            ) = _fire(
                row,
                args,
                auth_header,
                suspend=None,
                resume=None,
                auth_refresh_fn=auth_refresh_fn,
            )
        finally:
            with state.lock:
                state.in_flight -= 1

        if new_auth_header:
            with state.lock:
                state.auth_header = new_auth_header

        with state.lock:
            state.processed += 1
            absolute = state.processed

        if status is None and not url:
            with state.output_lock:
                _out(
                    bar,
                    f"{_RED}{thread_tag}[SKIP]  row {line_num}: {body} | row={dict(row)}{_RESET}",
                )
                _progress(bar, absolute, total_rows)
            with state.lock:
                state.failed += 1
                retry_writer.writerow(row)
                _write_failure_log(
                    log_file,
                    "SKIP",
                    line_num,
                    args.method,
                    "",
                    None,
                    {},
                    None,
                    body,
                    {},
                    0.0,
                )
        else:
            with state.output_lock:
                succeeded = _log_row(
                    bar,
                    args,
                    line_num,
                    status,
                    body,
                    elapsed,
                    url,
                    req_body,
                    req_headers,
                    resp_headers,
                    thread_tag=thread_tag,
                )
                _progress(bar, absolute, total_rows)
            with state.lock:
                if succeeded:
                    state.ok += 1
                else:
                    state.failed += 1
                    retry_writer.writerow(row)
                    _write_failure_log(
                        log_file,
                        "FAIL",
                        line_num,
                        args.method,
                        url,
                        req_body,
                        req_headers,
                        status,
                        body,
                        resp_headers,
                        elapsed,
                    )

        work_queue.task_done()


def _run_parallel_main_loop(
    threads: list,
    producer_thread: threading.Thread,
    state: "_ParallelState | _WorkflowParallelState",
    bar: _BottomBar | None,
    debug: bool,
    work_queue: "queue.Queue",
) -> None:
    """Drive the command-poll / pause / exit / debug-bar loop for parallel runs."""
    _debug_ts = 0.0
    _exiting = False
    _pausing = False
    _last_inflight_n = -1
    try:
        while any(t.is_alive() for t in threads):
            cmd = _poll_cmd(bar)

            if cmd == _CMD_EXIT and not _exiting:
                state.stop_event.set()
                state.pause_event.set()
                _exiting = True
                _pausing = False
                _last_inflight_n = -1
            elif cmd == _CMD_PAUSE and not _exiting and not _pausing:
                state.pause_event.clear()
                _pausing = True
                _last_inflight_n = -1
            elif cmd == _CMD_RESUME and _pausing:
                state.pause_event.set()
                _pausing = False
                with state.output_lock:
                    _out(bar, "[RESUMED]")

            if _exiting or _pausing:
                with state.lock:
                    n = state.in_flight
                if n != _last_inflight_n:
                    _last_inflight_n = n
                    word = "request" if n == 1 else "requests"
                    with state.output_lock:
                        if _exiting:
                            if n > 0:
                                _out(
                                    bar,
                                    f"{_GREY}[EXIT]  Waiting for {n} in-flight {word} to finish...{_RESET}",
                                )
                            else:
                                _out(bar, f"{_GREY}[EXIT]  Stopping...{_RESET}")
                        else:
                            if n > 0:
                                _out(
                                    bar,
                                    f"[PAUSING]  Waiting for {n} in-flight {word} to finish...",
                                )
                            else:
                                # Stay in the paused state until the user explicitly
                                # resumes/exits. Re-printing is suppressed by the
                                # _last_inflight_n guard above, so _pausing must NOT
                                # be cleared here or /resume would become a no-op.
                                _out(bar, "[PAUSED]  Type /resume to continue...")
            if debug:
                now = time.monotonic()
                if now - _debug_ts >= 0.5:
                    _debug_ts = now
                    n_active = sum(t.is_alive() for t in threads)
                    pending = work_queue.qsize()
                    with state.lock:
                        ok_n, fail_n = state.ok, state.failed
                    dbg = f"  [debug]  Q: {pending} pending  |  threads: {n_active}/{len(threads)}  |  ok: {ok_n}  fail: {fail_n}"
                    if bar:
                        bar.update_debug(dbg)
                    else:
                        print(dbg, file=sys.stderr)
            time.sleep(0.05)
    finally:
        producer_thread.join()
        for t in threads:
            t.join()


def _run_parallel(
    reader: csv.DictReader,
    args: argparse.Namespace,
    auth_header: str | None,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: "IO[str]",
    offset: int,
    total_rows: int,
) -> tuple[int, int, int]:
    effective_rows = total_rows - offset
    if effective_rows <= 0:
        return 0, 0, 0

    work_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    state = _ParallelState(auth_header)
    auth_refresh_fn = _make_auth_refresh_fn(args, state, suspend, resume)
    n_workers = min(args.concurrency_level, effective_rows)
    debug = getattr(args, "debug", False)

    def _producer() -> None:
        for line_num, row in enumerate(reader, start=offset + 1):
            while not state.stop_event.is_set():
                try:
                    work_queue.put((line_num, row), timeout=0.1)
                    break
                except queue.Full:
                    continue
            if state.stop_event.is_set():
                break
        for _ in range(n_workers):
            while not state.stop_event.is_set():
                try:
                    work_queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    producer_thread = threading.Thread(target=_producer, daemon=True, name="producer")

    threads = [
        threading.Thread(
            target=_parallel_worker,
            name=f"worker-{i + 1}",
            args=(
                work_queue,
                args,
                state,
                bar,
                suspend,
                resume,
                retry_writer,
                log_file,
                total_rows,
                auth_refresh_fn,
                debug,
            ),
            daemon=True,
        )
        for i in range(n_workers)
    ]
    producer_thread.start()
    for t in threads:
        t.start()

    _run_parallel_main_loop(threads, producer_thread, state, bar, debug, work_queue)
    return state.ok, state.failed, state.processed


# ---------------------------------------------------------------------------
# Workflow runners
# ---------------------------------------------------------------------------


def _run_workflow_loop(
    reader: csv.DictReader,
    steps: list,
    args: argparse.Namespace,
    auth_headers: dict,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: IO[str],
    offset: int,
    total_rows: int,
    fieldnames: list,
) -> tuple[int, int, int]:
    """Sequential workflow runner: all steps for each row before moving to the next."""
    remaining = total_rows - offset
    processed = ok_rows = failed_rows = 0

    for line_num, row in enumerate(reader, start=offset + 1):
        processed += 1
        absolute = offset + processed

        resume_at = row.get(_WORKFLOW_STEP_COL)
        reached_resume = resume_at is None
        row_failed = False
        first_failed_step: str | None = None

        for step in steps:
            if not reached_resume:
                if step.path == resume_at:
                    reached_resume = True
                else:
                    continue

            step_auth = auth_headers.get(step.path)
            label = f"row {line_num} [{step.path}]"

            (
                status,
                body,
                elapsed,
                url,
                new_auth,
                req_body,
                req_headers,
                resp_headers,
            ) = _fire_workflow_step(
                step,
                row,
                step_auth,
                args.timeout,
                suspend=suspend,
                resume=resume,
            )

            if new_auth is not None:
                auth_headers[step.path] = new_auth

            if status is None and not url:
                # substitution error
                _out(bar, f"{_RED}[SKIP]  {label}: {body} | row={dict(row)}{_RESET}")
                _write_failure_log(
                    log_file,
                    "SKIP",
                    line_num,
                    step.method,
                    "",
                    None,
                    {},
                    None,
                    body,
                    {},
                    0.0,
                )
                row_failed = True
                if first_failed_step is None:
                    first_failed_step = step.path
                if step.on_error == "stop":
                    break
                continue

            succeeded = _log_row(
                bar,
                args,
                line_num,
                status,
                body,
                elapsed,
                url,
                req_body,
                req_headers,
                resp_headers,
                method_override=step.method,
                row_label=label,
            )
            if not succeeded:
                _write_failure_log(
                    log_file,
                    "FAIL",
                    line_num,
                    step.method,
                    url,
                    req_body,
                    req_headers,
                    status,
                    body,
                    resp_headers,
                    elapsed,
                )
                row_failed = True
                if first_failed_step is None:
                    first_failed_step = step.path
                if step.on_error == "stop":
                    break

        _progress(bar, absolute, total_rows)

        if row_failed:
            failed_rows += 1
            retry_row = dict(row)
            retry_row.pop(_WORKFLOW_STEP_COL, None)
            retry_row[_WORKFLOW_STEP_COL] = first_failed_step
            retry_writer.writerow(retry_row)
        else:
            ok_rows += 1

        cmd = _poll_cmd(bar)
        if _handle_cmd_in_loop(cmd, bar, line_num, ok_rows, failed_rows):
            break
        if args.delay > 0 and processed < remaining:
            time.sleep(args.delay / 1000)

    return ok_rows, failed_rows, processed


def _make_workflow_auth_refresh_fns(
    steps: list,
    state: _WorkflowParallelState,
    suspend: Callable | None,
    resume: Callable | None,
) -> dict:
    """Return a dict of step.path -> auth-refresh closure for parallel workflow workers."""
    fns: dict = {}
    for step in steps:
        path = step.path

        def make_fn(s: WorkflowStep) -> Callable:
            def refresh(old_auth: str | None) -> str | None:
                with state.auth_lock:
                    if state.auth_headers.get(s.path) != old_auth:
                        return state.auth_headers.get(s.path)
                    with state.output_lock:
                        if suspend:
                            suspend()
                        try:
                            if s.auth_type == "bearer":
                                new_token = prompt_new_token()
                                new = f"Bearer {new_token}"
                            else:
                                new_creds = prompt_new_basic_creds()
                                new = f"Basic {base64.b64encode(new_creds.encode()).decode()}"
                        finally:
                            if resume:
                                resume()
                    state.auth_headers[s.path] = new
                    return new

            return refresh

        fns[path] = make_fn(step)
    return fns


def _workflow_parallel_worker(
    work_queue: "queue.Queue[tuple[int, dict] | None]",
    steps: list,
    args: argparse.Namespace,
    state: _WorkflowParallelState,
    bar: _BottomBar | None,
    retry_writer: Any,
    log_file: "IO[str]",
    total_rows: int,
    auth_refresh_fns: dict,
    debug: bool = False,
) -> None:
    thread_tag = f"[{threading.current_thread().name}] " if debug else ""
    while True:
        if state.stop_event.is_set():
            break
        state.pause_event.wait()
        try:
            item = work_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is None:
            work_queue.task_done()
            break
        line_num, row = item

        with state.lock:
            state.in_flight += 1

        try:
            resume_at = row.get(_WORKFLOW_STEP_COL)
            reached_resume = resume_at is None
            row_failed = False
            first_failed_step: str | None = None

            for step in steps:
                if state.stop_event.is_set():
                    break
                if not reached_resume:
                    if step.path == resume_at:
                        reached_resume = True
                    else:
                        continue

                with state.lock:
                    step_auth = state.auth_headers.get(step.path)

                label = f"row {line_num} [{step.path}]"
                (
                    status,
                    body,
                    elapsed,
                    url,
                    new_auth,
                    req_body,
                    req_headers,
                    resp_headers,
                ) = _fire_workflow_step(
                    step,
                    row,
                    step_auth,
                    args.timeout,
                    auth_refresh_fn=auth_refresh_fns.get(step.path),
                )

                if new_auth is not None:
                    with state.lock:
                        state.auth_headers[step.path] = new_auth

                if status is None and not url:
                    with state.output_lock:
                        _out(
                            bar,
                            f"{_RED}{thread_tag}[SKIP]  {label}: {body} | row={dict(row)}{_RESET}",
                        )
                    with state.lock:
                        _write_failure_log(
                            log_file,
                            "SKIP",
                            line_num,
                            step.method,
                            "",
                            None,
                            {},
                            None,
                            body,
                            {},
                            0.0,
                        )
                    row_failed = True
                    if first_failed_step is None:
                        first_failed_step = step.path
                    if step.on_error == "stop":
                        break
                    continue

                with state.output_lock:
                    succeeded = _log_row(
                        bar,
                        args,
                        line_num,
                        status,
                        body,
                        elapsed,
                        url,
                        req_body,
                        req_headers,
                        resp_headers,
                        thread_tag=thread_tag,
                        method_override=step.method,
                        row_label=label,
                    )
                if not succeeded:
                    with state.lock:
                        _write_failure_log(
                            log_file,
                            "FAIL",
                            line_num,
                            step.method,
                            url,
                            req_body,
                            req_headers,
                            status,
                            body,
                            resp_headers,
                            elapsed,
                        )
                    row_failed = True
                    if first_failed_step is None:
                        first_failed_step = step.path
                    if step.on_error == "stop":
                        break

        finally:
            with state.lock:
                state.in_flight -= 1
                state.processed += 1
                absolute = state.processed

        with state.output_lock:
            _progress(bar, absolute, total_rows)

        with state.lock:
            if row_failed:
                state.failed += 1
                retry_row = dict(row)
                retry_row.pop(_WORKFLOW_STEP_COL, None)
                retry_row[_WORKFLOW_STEP_COL] = first_failed_step
                retry_writer.writerow(retry_row)
            else:
                state.ok += 1

        work_queue.task_done()


def _run_workflow_parallel(
    reader: csv.DictReader,
    steps: list,
    args: argparse.Namespace,
    auth_headers: dict,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: "IO[str]",
    offset: int,
    total_rows: int,
) -> tuple[int, int, int]:
    effective_rows = total_rows - offset
    if effective_rows <= 0:
        return 0, 0, 0

    work_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    state = _WorkflowParallelState(auth_headers)
    auth_refresh_fns = _make_workflow_auth_refresh_fns(steps, state, suspend, resume)
    n_workers = min(args.concurrency_level, effective_rows)
    debug = getattr(args, "debug", False)

    def _producer() -> None:
        for line_num, row in enumerate(reader, start=offset + 1):
            while not state.stop_event.is_set():
                try:
                    work_queue.put((line_num, row), timeout=0.1)
                    break
                except queue.Full:
                    continue
            if state.stop_event.is_set():
                break
        for _ in range(n_workers):
            while not state.stop_event.is_set():
                try:
                    work_queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    producer_thread = threading.Thread(target=_producer, daemon=True, name="producer")
    threads = [
        threading.Thread(
            target=_workflow_parallel_worker,
            name=f"worker-{i + 1}",
            args=(
                work_queue,
                steps,
                args,
                state,
                bar,
                retry_writer,
                log_file,
                total_rows,
                auth_refresh_fns,
                debug,
            ),
            daemon=True,
        )
        for i in range(n_workers)
    ]
    producer_thread.start()
    for t in threads:
        t.start()

    _run_parallel_main_loop(threads, producer_thread, state, bar, debug, work_queue)
    return state.ok, state.failed, state.processed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        _run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # Downstream consumer (e.g. `| head`) closed the pipe. Exit cleanly
        # without a traceback. Redirect stdout to devnull so interpreter
        # shutdown doesn't re-raise on flush.
        with contextlib.suppress(OSError):
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, sys.stdout.fileno())
            os.close(fd)
        sys.exit(0)


def _run() -> None:
    parser = argparse.ArgumentParser(description="Bulk HTTP requests from CSV rows")
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {_get_version()}"
    )
    parser.add_argument(
        "--url",
        "-u",
        default=None,
        help="Target URL, may contain {{variable}} placeholders",
    )
    parser.add_argument(
        "--workflow",
        "-w",
        default=None,
        metavar="WORKFLOW_YAML",
        help="Path to a workflow YAML file defining multiple HTTP steps per row",
    )
    parser.add_argument(
        "--auth-type",
        "-a",
        default="none",
        choices=["bearer", "basic", "none"],
        dest="auth_type",
        help="Auth method: bearer, basic, or none (default)",
    )
    parser.add_argument(
        "--token",
        "-t",
        default=None,
        help="Bearer token (overrides BULK_TOKEN env var); used with --auth-type bearer",
    )
    parser.add_argument(
        "--user",
        "-U",
        default=None,
        help="Basic auth credentials as user:pass (overrides BULK_USER env var); used with --auth-type basic",
    )
    parser.add_argument(
        "--csv", "-c", required=True, dest="csv_path", help="Path to CSV file"
    )
    parser.add_argument(
        "--method", "-m", default="POST", help="HTTP method (default: POST)"
    )
    parser.add_argument(
        "--body", "-b", default=None, help="Request body (e.g. JSON string)"
    )
    parser.add_argument(
        "--content-type",
        "-C",
        default="application/json",
        dest="content_type",
        help="Content-Type header (default: application/json)",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=int,
        default=0,
        help="Delay in milliseconds between requests (default: 0)",
    )
    parser.add_argument(
        "--offset",
        "-o",
        type=int,
        default=0,
        help="Skip first N data rows (default: 0)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print request/response details and timing",
    )
    parser.add_argument(
        "--timeout",
        "-T",
        type=int,
        default=30,
        help="Request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retry-file",
        "-r",
        default=None,
        dest="retry_file",
        help="Path for failed-rows CSV (default: <input_stem>_failed.csv)",
    )
    parser.add_argument(
        "--parallel",
        "-p",
        action="store_true",
        default=False,
        help="Process rows in parallel using multiple threads",
    )
    parser.add_argument(
        "--concurrency-level",
        "-n",
        type=int,
        default=os.cpu_count() or 4,
        dest="concurrency_level",
        help=f"Number of parallel worker threads (default: {os.cpu_count() or 4}); only used with --parallel",
    )
    parser.add_argument(
        "--header",
        "-H",
        action="append",
        default=None,
        metavar="NAME: VALUE",
        help="Add a custom request header; repeatable. Value supports {{col}} placeholders.",
    )
    parser.add_argument(
        "--debug",
        "-D",
        action="store_true",
        default=False,
        help="Print diagnostic info per row (thread name, queue depth); only meaningful with --parallel",
    )
    args = parser.parse_args()

    # Mutual exclusivity and presence checks
    if args.url and args.workflow:
        print("[ERROR] --url and --workflow are mutually exclusive.", file=sys.stderr)
        sys.exit(1)
    if not args.url and not args.workflow:
        print(
            "[ERROR] One of --url (-u) or --workflow (-w) is required.", file=sys.stderr
        )
        sys.exit(1)

    args.method = args.method.upper()
    workflow_mode = args.workflow is not None

    offset = args.offset

    csv_path = pathlib.Path(args.csv_path)
    retry_path = (
        pathlib.Path(args.retry_file)
        if args.retry_file
        else csv_path.parent / f"{csv_path.stem}_failed.csv"
    )
    log_path = retry_path.with_suffix(".log")

    # Single pass: read header and count rows before starting the bar or prompting
    # for credentials, so validation errors are always visible in a clean terminal.
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            _r = csv.DictReader(f)
            fieldnames: list = list(_r.fieldnames or [])
            total_rows = sum(1 for _ in _r)
    except OSError as e:
        print(f"[ERROR] Cannot open CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse and validate workflow before starting UI or prompting credentials
    steps: list = []
    if workflow_mode:
        steps, werr = parse_workflow(args.workflow)
        if werr:
            print(f"[ERROR] {werr}", file=sys.stderr)
            sys.exit(1)
        # fieldnames for retry CSV: original columns + _bulk_post_step at the end
        # (strip any existing _bulk_post_step column first to avoid duplicates)
        base_fields = [f for f in fieldnames if f != _WORKFLOW_STEP_COL]
        retry_fieldnames = base_fields + [_WORKFLOW_STEP_COL]
        verr = _validate_workflow_placeholders(steps, fieldnames)
        if verr:
            print(f"[ERROR] {verr}", file=sys.stderr)
            sys.exit(1)
    else:
        _validate_placeholders(args, fieldnames)
        retry_fieldnames = fieldnames

    if offset >= total_rows > 0:
        print(
            f"[ERROR] --offset {offset} is beyond the last row ({total_rows} data rows total).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        csv_file = open(csv_path, newline="", encoding="utf-8")  # noqa: SIM115  # lifecycle managed by the outer `with csv_file:` block below
    except OSError as e:
        print(f"[ERROR] Cannot open CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    if args.parallel and args.delay > 0:
        print("[INFO] --delay is ignored in parallel mode.", file=sys.stderr)

    if args.debug and not args.parallel:
        print("[INFO] --debug has no effect without --parallel.", file=sys.stderr)

    bar: _BottomBar | None = None
    if sys.stdin.isatty() and _HAS_TERMIOS:
        b = _BottomBar(debug_mode=args.debug and args.parallel)
        if b.start():
            bar = b

    suspend = bar.pause if bar else None
    resume = bar.resume if bar else None

    # Resolve auth (workflow handles its own per-step auth resolution below)
    auth_header: str | None = None
    if not workflow_mode:
        auth_header = resolve_auth_header(args, suspend=suspend, resume=resume)

    try:
        with csv_file:
            reader = csv.DictReader(csv_file)
            _skip_rows(reader, offset, bar)
            retry_file, retry_writer = _open_retry_writer(retry_path, retry_fieldnames)
            log_file = _open_log_file(log_path)
            try:
                if workflow_mode:
                    auth_headers, _ = _resolve_workflow_auth_headers(
                        steps, suspend=suspend, resume=resume
                    )
                    if args.parallel:
                        ok, failed, processed = _run_workflow_parallel(
                            reader,
                            steps,
                            args,
                            auth_headers,
                            bar,
                            suspend,
                            resume,
                            retry_writer,
                            log_file,
                            offset,
                            total_rows,
                        )
                    else:
                        ok, failed, processed = _run_workflow_loop(
                            reader,
                            steps,
                            args,
                            auth_headers,
                            bar,
                            suspend,
                            resume,
                            retry_writer,
                            log_file,
                            offset,
                            total_rows,
                            retry_fieldnames,
                        )
                elif args.parallel:
                    ok, failed, processed = _run_parallel(
                        reader,
                        args,
                        auth_header,
                        bar,
                        suspend,
                        resume,
                        retry_writer,
                        log_file,
                        offset,
                        total_rows,
                    )
                else:
                    ok, failed, processed = _run_loop(
                        reader,
                        args,
                        auth_header,
                        bar,
                        suspend,
                        resume,
                        retry_writer,
                        log_file,
                        offset,
                        total_rows,
                    )
            finally:
                retry_file.close()
                log_file.close()
    finally:
        if bar:
            bar.stop()

    if failed:
        print(f"\nDone — {processed} rows processed: {ok} succeeded, {failed} failed.")
        print(f"Failed rows saved to: {retry_path}")
        print(f"Failure log:          {log_path}")
        sys.exit(1)
    else:
        retry_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        print(f"\nDone — {processed} rows processed: {ok} succeeded, 0 failed.")


if __name__ == "__main__":
    main()
