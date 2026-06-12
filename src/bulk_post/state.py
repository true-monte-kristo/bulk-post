"""Shared mutable state for parallel worker threads."""

from __future__ import annotations

import threading


class _ParallelState:
    """Shared mutable state for parallel worker threads."""

    def __init__(self, auth_header: str | None) -> None:
        self.lock = threading.Lock()  # protects counters, retry_writer, log_file
        self.auth_lock = threading.Lock()  # serialises 401 interactive prompts
        self.output_lock = threading.Lock()  # serialises all stdout / bar writes
        self.auth_header: str | None = auth_header
        self.ok = 0
        self.failed = 0
        self.processed = 0
        self.in_flight = 0  # rows currently executing an HTTP request
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # set = running; clear = paused


class _WorkflowParallelState:
    """Shared mutable state for parallel workflow worker threads."""

    def __init__(self, auth_headers: dict) -> None:
        self.lock = threading.Lock()
        self.auth_lock = (
            threading.Lock()
        )  # one thread at a time refreshes any credential
        self.output_lock = threading.Lock()
        self.auth_headers: dict = dict(auth_headers)  # step.path -> Optional[str]
        self.ok = 0  # rows where all steps succeeded
        self.failed = 0  # rows where at least one step failed
        self.processed = 0
        self.in_flight = 0
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
