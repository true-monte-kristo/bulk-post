#!/usr/bin/env python3
"""
Bulk HTTP request runner — fires templated requests whose {{placeholder}} slots are
filled from each CSV row: one request per row, or a multi-step workflow per row in
--workflow mode.

Usage:
    python -m bulk_post \
        -u "https://example.com/api/invoices/{{id}}/cancel" \
        -c rows.csv \
        -m DELETE \
        -d 200

    python -m bulk_post \
        -u "https://example.com/api/invoices/{{id}}/status" \
        -c rows.csv \
        -m PATCH \
        -b '{"status": "cancelled"}'

Token resolution order: --token/-t flag → BULK_TOKEN env var → interactive prompt.
If the token expires mid-run (401), the script pauses and asks for a new one.

The CSV header must contain columns matching every {{variable}} in the URL.
Failed rows are logged and skipped; execution always continues.
"""

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
from .cli import (
    _get_version as _get_version,
)
from .cli import (
    _run as _run,
)
from .cli import (
    build_parser as build_parser,
)
from .cli import (
    main as main,
)
from .csvio import (
    _open_log_file as _open_log_file,
)
from .csvio import (
    _open_retry_writer as _open_retry_writer,
)
from .csvio import (
    _skip_rows as _skip_rows,
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
    _validate_placeholders as _validate_placeholders,
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
    _HAS_TERMIOS as _HAS_TERMIOS,
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
    _BottomBar as _BottomBar,
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
