"""Single-URL row execution: sequential and parallel runners.

Sequential mode (``_run_loop``) fires one row at a time on the calling thread,
honoring ``--delay`` and the interactive /pause /resume /exit commands.

Parallel mode (``_run_parallel``) uses a producer/consumer thread pool::

    reader -> _producer -> work_queue (bounded) -> N _parallel_worker threads

One ``None`` "poison pill" per worker is enqueued after the last row to signal
end-of-input. ``_run_parallel_main_loop`` runs on the main thread alongside the
workers and owns the UI: it polls commands, prints the in-flight countdown and
debug bar, then joins the producer and workers on exit.

Concurrency contract (see also ``_ParallelState``):

- ``state.lock`` guards the counters (ok/failed/processed/in_flight), the shared
  ``auth_header``, and writes to ``retry_writer`` / ``log_file``.
- ``state.output_lock`` serializes all stdout / bottom-bar writes.
- ``state.pause_event`` gates the workers (set = running, clear = paused);
  ``state.stop_event`` tells workers and the producer to finish.

Workers are daemon threads, and the producer back-pressures on a bounded queue
so a large CSV is never fully loaded into memory.
"""

from __future__ import annotations

import argparse
import base64
import csv
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import IO, Any, cast

from .auth import _make_auth_refresh_fn, prompt_new_basic_creds, prompt_new_token
from .csvio import _write_failure_log
from .http import http_request
from .state import _ParallelState, _WorkflowParallelState
from .templating import substitute
from .terminal import (
    _CMD_EXIT,
    _CMD_PAUSE,
    _CMD_RESUME,
    _GREEN,
    _GREY,
    _RED,
    _RESET,
    _BottomBar,
    _out,
    _poll_cmd,
    _progress,
    _wait_for_resume,
    print_verbose,
)

_QUEUE_MAXSIZE = 500  # max rows buffered in memory in parallel mode


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
        content_type = args.content_type
    else:
        content_type = "application/json"

    extra_headers: dict = {}
    for raw in args.header or []:
        name, _, val_tmpl = raw.partition(": ")
        val, herr = substitute(val_tmpl, row)
        if herr:
            return None, herr, 0.0, url, None, None, {}, {}
        extra_headers[name] = val

    status, body, elapsed, req_headers, resp_headers = http_request(
        url,
        auth_header,
        args.method,
        req_body,
        args.timeout,
        content_type,
        extra_headers,
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
            url,
            new_auth_header,
            args.method,
            req_body,
            args.timeout,
            content_type,
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
    """Run rows sequentially on the calling thread.

    Fires each row via ``_fire``, logs the outcome, writes failures to the retry
    file and failure log, updates progress, and applies ``--delay`` between rows.
    Honors the interactive /pause and /exit commands. Returns
    ``(ok, failed, processed)``.
    """
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
    work_queue: queue.Queue[tuple[int, dict]],
    args: argparse.Namespace,
    state: _ParallelState,
    bar: _BottomBar | None,
    suspend: Callable | None,
    resume: Callable | None,
    retry_writer: Any,
    log_file: IO[str],
    total_rows: int,
    auth_refresh_fn: Callable,
    debug: bool = False,
) -> None:
    """Worker thread: pull ``(line_num, row)`` items off ``work_queue`` and fire them.

    Runs until it receives the ``None`` poison pill or ``state.stop_event`` is
    set, blocking on ``state.pause_event`` while paused. Brackets each request
    with ``state.in_flight`` +/- 1 (under ``state.lock``), publishes any
    refreshed 401 auth header back to ``state.auth_header``, and records the
    outcome (counters, retry file, failure log) under the lock. All console
    output goes through ``state.output_lock``.
    """
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
    state: _ParallelState | _WorkflowParallelState,
    bar: _BottomBar | None,
    debug: bool,
    work_queue: queue.Queue,
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
                    n_in_flight = state.in_flight
                if n_in_flight != _last_inflight_n:
                    _last_inflight_n = n_in_flight
                    word = "request" if n_in_flight == 1 else "requests"
                    with state.output_lock:
                        if _exiting:
                            if n_in_flight > 0:
                                _out(
                                    bar,
                                    f"{_GREY}[EXIT]  Waiting for {n_in_flight} in-flight {word} to finish...{_RESET}",
                                )
                            else:
                                _out(bar, f"{_GREY}[EXIT]  Stopping...{_RESET}")
                        else:
                            if n_in_flight > 0:
                                _out(
                                    bar,
                                    f"[PAUSING]  Waiting for {n_in_flight} in-flight {word} to finish...",
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
    log_file: IO[str],
    offset: int,
    total_rows: int,
) -> tuple[int, int, int]:
    """Run rows concurrently with a producer/worker thread pool.

    Spawns one producer (streaming the CSV onto a bounded queue) and
    ``min(--concurrency-level, rows)`` worker threads, then drives the UI loop
    on the main thread until all work drains. ``--delay`` is ignored in this
    mode. Returns ``(ok, failed, processed)``.
    """
    effective_rows = total_rows - offset
    if effective_rows <= 0:
        return 0, 0, 0

    work_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    state = _ParallelState(auth_header)
    auth_refresh_fn = _make_auth_refresh_fn(args, state, suspend, resume)
    n_workers = min(args.concurrency_level, effective_rows)
    debug = getattr(args, "debug", False)

    def _producer() -> None:
        """Stream rows onto the queue, then one poison pill per worker.

        Back-pressures (retries on ``queue.Full``) to keep memory bounded, and
        stops early if ``state.stop_event`` is set (e.g. on /exit).
        """
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
