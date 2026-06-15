"""Workflow execution: sequential and parallel multi-step runners.

Each CSV row runs all workflow steps in document order. ``_run_workflow_loop``
does this one row at a time; ``_run_workflow_parallel`` reuses the same
producer / bounded-queue / worker model and shared ``_run_parallel_main_loop``
as the single-URL runner (see ``runner`` for the threading and locking
contract), with ``_WorkflowParallelState`` holding per-step auth headers.

On a step failure the row is written to the retry file with a
``_bulk_post_step`` column naming the first failed step, so a re-run resumes
mid-workflow; ``on_error: continue`` instead proceeds to the remaining steps.
"""

from __future__ import annotations

import argparse
import base64
import csv
import queue
import threading
import time
from collections.abc import Callable
from typing import IO, Any

from .auth import prompt_new_basic_creds, prompt_new_token
from .csvio import _write_failure_log
from .runner import (
    _QUEUE_MAXSIZE,
    _handle_cmd_in_loop,
    _log_row,
    _run_parallel_main_loop,
)
from .state import _WorkflowParallelState
from .terminal import _RED, _RESET, _BottomBar, _out, _poll_cmd, _progress
from .variables import persist_vars
from .workflow import _WORKFLOW_STEP_COL, WorkflowStep, _fire_workflow_step


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
        responses: dict = {}

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
                responses=responses,
            )

            if new_auth is not None:
                auth_headers[step.path] = new_auth

            if status is not None:
                responses[step.path] = body

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
            retry_row.update(persist_vars(steps, responses))
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
    work_queue: queue.Queue[tuple[int, dict] | None],
    steps: list,
    args: argparse.Namespace,
    state: _WorkflowParallelState,
    bar: _BottomBar | None,
    retry_writer: Any,
    log_file: IO[str],
    total_rows: int,
    auth_refresh_fns: dict,
    debug: bool = False,
) -> None:
    """Worker thread: run every step of one row, pulled off ``work_queue``.

    Mirrors ``runner._parallel_worker`` but iterates a row's steps in order,
    honoring per-step ``on_error`` (stop vs. continue) and resume via
    ``_WORKFLOW_STEP_COL``. Brackets the row with ``state.in_flight`` +/- 1,
    reads/writes per-step auth under ``state.lock``, and serializes output via
    ``state.output_lock``. Exits on the ``None`` poison pill or ``stop_event``.
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
            responses: dict = {}

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
                    responses=responses,
                )

                if new_auth is not None:
                    with state.lock:
                        state.auth_headers[step.path] = new_auth

                if status is not None:
                    responses[step.path] = body

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
                retry_row.update(persist_vars(steps, responses))
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
    log_file: IO[str],
    offset: int,
    total_rows: int,
) -> tuple[int, int, int]:
    """Run the workflow concurrently across rows (steps stay sequential per row).

    Spawns a producer and ``min(--concurrency-level, rows)`` workers sharing a
    bounded queue, then drives ``_run_parallel_main_loop`` on the main thread.
    Returns ``(ok, failed, processed)`` counted by row — a row is ``ok`` only if
    all of its steps succeeded.
    """
    effective_rows = total_rows - offset
    if effective_rows <= 0:
        return 0, 0, 0

    work_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    state = _WorkflowParallelState(auth_headers)
    auth_refresh_fns = _make_workflow_auth_refresh_fns(steps, state, suspend, resume)
    n_workers = min(args.concurrency_level, effective_rows)
    debug = getattr(args, "debug", False)

    def _producer() -> None:
        """Stream rows onto the queue, then one poison pill per worker.

        Back-pressures on ``queue.Full`` and stops early on ``state.stop_event``.
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
