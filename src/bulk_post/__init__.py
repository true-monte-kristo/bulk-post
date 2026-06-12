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
import contextlib
import csv
import importlib.metadata
import os
import pathlib
import sys

from .auth import (
    _make_auth_refresh_fn as _make_auth_refresh_fn,
)
from .auth import (
    prompt_new_basic_creds as prompt_new_basic_creds,
)
from .auth import (
    prompt_new_token as prompt_new_token,
)
from .auth import (
    resolve_auth_header as resolve_auth_header,
)
from .auth import (
    resolve_basic_creds as resolve_basic_creds,
)
from .auth import (
    resolve_token as resolve_token,
)
from .csvio import (
    _open_log_file,
    _open_retry_writer,
    _skip_rows,
)
from .csvio import (
    _write_failure_log as _write_failure_log,
)
from .csvio import (
    count_csv_rows as count_csv_rows,
)
from .http import _mask_headers as _mask_headers
from .http import http_request as http_request
from .runner import (
    _QUEUE_MAXSIZE as _QUEUE_MAXSIZE,
)
from .runner import (
    _fire as _fire,
)
from .runner import (
    _handle_cmd_in_loop as _handle_cmd_in_loop,
)
from .runner import (
    _log_row as _log_row,
)
from .runner import (
    _parallel_worker as _parallel_worker,
)
from .runner import (
    _run_loop as _run_loop,
)
from .runner import (
    _run_parallel as _run_parallel,
)
from .runner import (
    _run_parallel_main_loop as _run_parallel_main_loop,
)
from .state import _ParallelState as _ParallelState
from .state import _WorkflowParallelState as _WorkflowParallelState
from .templating import (
    PLACEHOLDER_RE as PLACEHOLDER_RE,
)
from .templating import (
    _validate_body_template as _validate_body_template,
)
from .templating import (
    _validate_placeholders,
)
from .templating import (
    substitute as substitute,
)
from .terminal import (
    _CMD_EXIT as _CMD_EXIT,
)
from .terminal import (
    _CMD_PAUSE as _CMD_PAUSE,
)
from .terminal import (
    _CMD_PROMPT as _CMD_PROMPT,
)
from .terminal import (
    _CMD_RESUME as _CMD_RESUME,
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
    _GREEN as _GREEN,
)
from .terminal import (
    _GREY as _GREY,
)
from .terminal import (
    _HAS_TERMIOS,
    _BottomBar,
)
from .terminal import (
    _RED as _RED,
)
from .terminal import (
    _RESET as _RESET,
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
    _out as _out,
)
from .terminal import (
    _poll_cmd as _poll_cmd,
)
from .terminal import (
    _progress as _progress,
)
from .terminal import (
    _render_bar as _render_bar,
)
from .terminal import (
    _stdin_command as _stdin_command,
)
from .terminal import (
    _wait_for_resume as _wait_for_resume,
)
from .terminal import (
    print_progress as print_progress,
)
from .terminal import (
    print_verbose as print_verbose,
)
from .workflow import _WORKFLOW_STEP_COL as _WORKFLOW_STEP_COL
from .workflow import (
    WorkflowStep as WorkflowStep,
)
from .workflow import (
    _fire_workflow_step as _fire_workflow_step,
)
from .workflow import (
    _resolve_workflow_auth_headers as _resolve_workflow_auth_headers,
)
from .workflow import (
    _validate_workflow_placeholders as _validate_workflow_placeholders,
)
from .workflow import (
    parse_workflow as parse_workflow,
)
from .workflow_runner import (
    _make_workflow_auth_refresh_fns as _make_workflow_auth_refresh_fns,
)
from .workflow_runner import (
    _run_workflow_loop as _run_workflow_loop,
)
from .workflow_runner import (
    _run_workflow_parallel as _run_workflow_parallel,
)
from .workflow_runner import (
    _workflow_parallel_worker as _workflow_parallel_worker,
)


def _get_version() -> str:
    try:
        return importlib.metadata.version("bulk-post")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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
