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
import datetime
import json
import os
import pathlib
import queue
import re
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as _ET
from typing import Any, IO, Callable, Optional, Tuple, cast

try:
    import termios
    import tty
    import select as _select_mod
    _HAS_TERMIOS = True
    _HAS_SELECT = hasattr(_select_mod, 'select')
except ImportError:
    _HAS_TERMIOS = False
    _HAS_SELECT = False
    _select_mod = None  # type: ignore[assignment]

BAR_WIDTH = 40
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_CMD_PROMPT = "  cmd> "

# ANSI colour helpers
_RESET      = "\033[0m"
_GREEN      = "\033[32m"
_RED        = "\033[31m"
_CYAN       = "\033[36m"
_GREY       = "\033[90m"        # bright-black / dark-grey
_TERRACOTTA = "\033[38;5;166m"  # reddish-orange
_GHOST      = "\033[2;37m"      # dim white — ghost / suggestion text

_CMD_PAUSE  = "/pause"
_CMD_RESUME = "/resume"
_CMD_EXIT   = "/exit"
_COMMANDS   = [_CMD_PAUSE, _CMD_RESUME, _CMD_EXIT]


def _get_suggestion(buf: str) -> str:
    """Return the completion suffix for buf if it uniquely matches a command."""
    if not buf.startswith("/"):
        return ""
    for cmd in _COMMANDS:
        if cmd.startswith(buf) and cmd != buf:
            return cmd[len(buf):]
    return ""


def _render_bar(current: int, total: int) -> str:
    filled = int(BAR_WIDTH * current / total)
    return "=" * filled + (">" if filled < BAR_WIDTH else "=") + " " * max(0, BAR_WIDTH - filled - 1)


class _BottomBar:
    """
    Reserves the bottom 2 terminal rows:
      row h-1  — live progress bar
      row h    — command input (always visible while typing)
    All other output scrolls within rows 1..h-2 via an ANSI scroll region.
    Only used when sys.stdin.isatty() and termios is available.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._buf = ""
        self._nav_idx: int = -1   # -1 = free input; 0..len(_COMMANDS)-1 = navigating
        self._saved_buf: str = "" # buffer snapshot taken when entering navigation
        self._h = 0
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._old_settings = None
        self._paused = threading.Event()
        self._paused.set()          # set = not paused → input thread reads normally
        self._paused_ack = threading.Event()  # set by input thread when raw mode exited

    # ------------------------------------------------------------------ public

    def start(self) -> bool:
        import shutil
        self._h = shutil.get_terminal_size((80, 24)).lines
        if self._h < 5:
            return False
        try:
            self._old_settings = termios.tcgetattr(sys.stdin.fileno())
        except termios.error:
            return False
        # Set scroll region (rows 1..h-2) and draw the command prompt
        sys.stdout.write(f"\033[1;{self._h - 2}r\033[{self._h};1H\033[2K{_GREY}{_CMD_PROMPT}{_RESET}")
        sys.stdout.flush()
        self._active = True
        self._thread = threading.Thread(target=self._input_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.2)
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
        # Reset scroll region and clear bottom rows
        sys.stdout.write(f"\033[r\033[{self._h - 1};1H\033[2K\033[{self._h};1H\033[2K")
        sys.stdout.flush()

    def pause(self) -> None:
        """Suspend raw mode so the main thread can call input()."""
        self._paused_ack.clear()
        self._paused.clear()
        # Wait for the input thread to acknowledge it has left raw mode.
        self._paused_ack.wait(timeout=0.5)
        # Place cursor at the bottom of the scroll region for interactive prompts
        sys.stdout.write(f"\033[{self._h - 2};1H\n\r")
        sys.stdout.flush()

    def resume(self) -> None:
        """Re-enter raw mode after input() is done."""
        try:
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            pass
        self._paused.set()
        self._redraw_cmd()

    @contextlib.contextmanager
    def suspended(self):
        """Context manager that pauses raw mode for the duration of a block."""
        self.pause()
        try:
            yield
        finally:
            self.resume()

    def write_line(self, text: str) -> None:
        with self._lock:
            buf = self._buf
        # Move to last scrollable row → \n scrolls the region up → print on fresh row
        sys.stdout.write(
            f"\033[{self._h - 2};1H\n\r{text}{_RESET}\033[K"
            f"\033[{self._h};{len(_CMD_PROMPT) + len(buf) + 1}H"
        )
        sys.stdout.flush()

    def update_progress(self, current: int, total: int) -> None:
        if total == 0:
            return
        bar = _render_bar(current, total)
        line = f"{_CYAN}  [{bar}] {int(100 * current / total):3}%  {current}/{total}{_RESET}"
        with self._lock:
            buf = self._buf
        sys.stdout.write(
            f"\033[{self._h - 1};1H\033[2K{line}"
            f"\033[{self._h};{len(_CMD_PROMPT) + len(buf) + 1}H"
        )
        sys.stdout.flush()

    def poll(self) -> Optional[str]:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------ private

    def _handle_pause_state(self) -> None:
        """Exit raw mode, signal the main thread, wait to resume, re-enter raw mode."""
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
        self._paused_ack.set()
        self._paused.wait()
        try:
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            pass

    def _handle_char(self, ch: str) -> None:
        if ch in ("\r", "\n"):
            with self._lock:
                cmd, self._buf = self._buf, ""
                self._nav_idx = -1
            self._redraw_cmd()
            if cmd:
                self._q.put(cmd)
        elif ch == "\x03":  # Ctrl+C → raise SIGINT on main thread
            os.kill(os.getpid(), signal.SIGINT)
        elif ch == "\x1b":  # escape sequence (arrow keys, etc.)
            self._handle_escape()
        elif ch == "\t":  # Tab → accept autocomplete suggestion
            with self._lock:
                suggestion = _get_suggestion(self._buf)
                if suggestion:
                    self._buf += suggestion
            self._redraw_cmd()
        elif ch in ("\x7f", "\x08"):  # backspace
            with self._lock:
                self._nav_idx = -1
                self._buf = self._buf[:-1]
            self._redraw_cmd()
        elif ch.isprintable():
            with self._lock:
                self._nav_idx = -1
                self._buf += ch
            self._redraw_cmd()

    def _handle_escape(self) -> None:
        # Arrow key sequences are ESC [ A/B (3 bytes). After _input_loop reads
        # the ESC, the remaining bytes are in Python's internal buffer — read
        # them directly rather than via select, which checks the OS fd buffer
        # (already empty) and would give a false-negative.
        bracket = sys.stdin.read(1)
        if bracket != "[":
            return
        code = sys.stdin.read(1)
        if code == "A":
            self._nav_up()
        elif code == "B":
            self._nav_down()

    def _nav_up(self) -> None:
        with self._lock:
            if self._nav_idx == -1:
                self._saved_buf = self._buf
                self._nav_idx = len(_COMMANDS) - 1
            elif self._nav_idx > 0:
                self._nav_idx -= 1
            self._buf = _COMMANDS[self._nav_idx]
        self._redraw_cmd()

    def _nav_down(self) -> None:
        with self._lock:
            if self._nav_idx == -1:
                return
            if self._nav_idx < len(_COMMANDS) - 1:
                self._nav_idx += 1
                self._buf = _COMMANDS[self._nav_idx]
            else:
                self._nav_idx = -1
                self._buf = self._saved_buf
        self._redraw_cmd()

    def _input_loop(self) -> None:
        try:
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            return
        try:
            while self._active:
                if not self._paused.is_set():
                    self._handle_pause_state()
                    continue
                r, _, _ = _select_mod.select([sys.stdin], [], [], 0.05)  # type: ignore[union-attr]
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if not ch:
                    continue
                self._handle_char(ch)
        except Exception:
            pass

    def _redraw_cmd(self) -> None:
        with self._lock:
            buf = self._buf
        suggestion = _get_suggestion(buf)
        typed = f"{_TERRACOTTA}{buf}{_RESET}" if buf.startswith("/") else buf
        ghost = f"{_GHOST}{suggestion}{_RESET}" if suggestion else ""
        # After drawing ghost text move cursor back to end of actual buf
        cursor_col = len(_CMD_PROMPT) + len(buf) + 1
        sys.stdout.write(
            f"\033[{self._h};1H\033[2K{_GREY}{_CMD_PROMPT}{_RESET}{typed}{ghost}"
            f"\033[{self._h};{cursor_col}H"
        )
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_csv_rows(path: str) -> int:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except OSError:
        return 0


def print_progress(current: int, total: int) -> None:
    if total == 0:
        return
    bar = _render_bar(current, total)
    pct = int(100 * current / total)
    print(f"\r  [{bar}] {pct:3}%  {current}/{total}", end="", flush=True)


def _out(bar: Optional[_BottomBar], text: str) -> None:
    if bar:
        bar.write_line(text)
    else:
        print(text)


def _progress(bar: Optional[_BottomBar], current: int, total: int) -> None:
    if bar:
        bar.update_progress(current, total)
    else:
        print_progress(current, total)


def resolve_token(
    flag_value: Optional[str],
    suspend: Optional[Callable] = None,
    resume: Optional[Callable] = None,
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
    suspend: Optional[Callable] = None,
    resume: Optional[Callable] = None,
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
    flag_value: Optional[str],
    suspend: Optional[Callable] = None,
    resume: Optional[Callable] = None,
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
    suspend: Optional[Callable] = None,
    resume: Optional[Callable] = None,
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
    suspend: Optional[Callable] = None,
    resume: Optional[Callable] = None,
) -> Optional[str]:
    if args.auth_type == "none":
        return None
    if args.auth_type == "bearer":
        token = resolve_token(args.token, suspend=suspend, resume=resume)
        return f"Bearer {token}"
    creds = resolve_basic_creds(args.user, suspend=suspend, resume=resume)
    return f"Basic {base64.b64encode(creds.encode()).decode()}"


def substitute(template: str, row: dict) -> Tuple[str, Optional[str]]:
    missing = [p for p in PLACEHOLDER_RE.findall(template) if p not in row]
    if missing:
        return template, f"Missing CSV columns for placeholders: {missing}"
    return PLACEHOLDER_RE.sub(lambda m: row[m.group(1)], template), None


def http_request(url: str, auth_header: Optional[str], method: str, body: Optional[str], timeout: int = 30, content_type: str = "application/json") -> Tuple[Optional[int], str, float]:
    """Returns (status_or_None, response_body, elapsed_seconds)."""
    encoded_body = body.encode("utf-8") if body else None
    headers: dict = {}
    if auth_header:
        headers["Authorization"] = auth_header
    if encoded_body:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=encoded_body, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, response_body, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), time.monotonic() - t0
    except urllib.error.URLError as e:
        return None, f"Connection error: {e.reason}", time.monotonic() - t0
    except TimeoutError:
        return None, f"Request timed out ({timeout}s)", time.monotonic() - t0


def print_verbose(bar: Optional[_BottomBar], method: str, url: str, req_body: Optional[str], status: Optional[int], resp_body: str, elapsed: float) -> None:
    _out(bar, f"  > {method} {url}")
    if req_body:
        _out(bar, f"  > body: {req_body}")
    status_str = str(status) if status is not None else "ERR"
    _out(bar, f"  < {status_str}  ({elapsed * 1000:.0f} ms)")
    if resp_body:
        _out(bar, f"  < {resp_body[:500]}")


def _stdin_command() -> Optional[str]:
    """Return a stripped line from stdin if one is ready, else None. Unix only."""
    if not _HAS_SELECT:
        return None
    if sys.stdin.isatty() and _select_mod.select([sys.stdin], [], [], 0)[0]:
        try:
            return sys.stdin.readline().strip()
        except (OSError, EOFError):
            pass
    return None


def _wait_for_resume() -> bool:
    """Return True if /exit was requested while paused."""
    print("\n[PAUSED]  Type /resume to continue...", flush=True)
    while True:
        time.sleep(0.2)
        cmd = _stdin_command()
        if cmd == _CMD_RESUME:
            print("[RESUMED]", flush=True)
            return False
        if cmd == _CMD_EXIT:
            return True


# ---------------------------------------------------------------------------
# _run helpers
# ---------------------------------------------------------------------------

def _open_retry_writer(retry_path: pathlib.Path, fieldnames: list) -> Tuple[IO[str], Any]:
    """Open the retry CSV and write its header. Returns (file, writer)."""
    f = open(retry_path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    return f, writer


def _open_log_file(log_path: pathlib.Path) -> IO[str]:
    f = open(log_path, "a", encoding="utf-8")
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
    req_body: Optional[str],
    status: Optional[int],
    resp_body: str,
    elapsed: float,
) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    parts = [f"--- {kind}  row {line_num}  {ts} ---"]
    if url:
        parts.append(f"Method:   {method}")
        parts.append(f"URL:      {url}")
        if req_body:
            parts.append(f"Body:     {req_body}")
        status_str = str(status) if status is not None else "ERR"
        parts.append(f"Status:   {status_str}")
        parts.append(f"Elapsed:  {elapsed * 1000:.0f} ms")
        if resp_body:
            parts.append(f"Response: {resp_body.strip()[:1000]}")
    else:
        parts.append(f"Error:    {resp_body}")
    parts.append("")
    log_file.write("\n".join(parts) + "\n")
    log_file.flush()


def _validate_body_template(template: str, content_type: str) -> Optional[str]:
    dummy = PLACEHOLDER_RE.sub("null", template)
    ct = content_type.lower()
    if "json" in ct:
        try:
            json.loads(dummy)
        except json.JSONDecodeError as e:
            return f"Invalid JSON body template: {e}"
    elif "xml" in ct:
        try:
            _ET.fromstring(dummy)
        except _ET.ParseError as e:
            return f"Invalid XML body template: {e}"
    return None


def _fire(
    row: dict,
    args,
    auth_header: Optional[str],
    suspend: Optional[Callable],
    resume: Optional[Callable],
) -> Tuple[Optional[int], str, float, str, Optional[str]]:
    """
    Substitute placeholders, fire the request (with one 401 retry), return
    (status, response_body, elapsed, final_url, new_auth_header_or_None).
    Returns (None, err_message, 0, "", None) on substitution error.
    """
    url, err = substitute(args.url, row)
    if err:
        return None, err, 0.0, "", None

    req_body: Optional[str] = None
    if args.body:
        req_body, err = substitute(cast(str, args.body), row)
        if err:
            return None, err, 0.0, url, None
        ct = args.content_type
    else:
        ct = "application/json"
    status, body, elapsed = http_request(url, auth_header, args.method, req_body, args.timeout, ct)

    new_auth_header: Optional[str] = None
    if status == 401 and args.auth_type != "none":
        if args.auth_type == "bearer":
            refreshed = prompt_new_token(suspend=suspend, resume=resume)
            new_auth_header = f"Bearer {refreshed}"
        else:
            refreshed = prompt_new_basic_creds(suspend=suspend, resume=resume)
            new_auth_header = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
        status, body, elapsed = http_request(url, new_auth_header, args.method, req_body, args.timeout, ct)

    return status, body, elapsed, url, new_auth_header


def _log_row(
    bar: Optional[_BottomBar],
    args,
    line_num: int,
    status: Optional[int],
    body: str,
    elapsed: float,
    url: str,
    req_body: Optional[str],
) -> bool:
    """Print per-row output. Returns True if the row succeeded."""
    method = args.method
    if args.verbose:
        print_verbose(bar, method, url, req_body, status, body, elapsed)

    if status is not None and 200 <= status < 300:
        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
        _out(bar, f"{_GREEN}[OK]    row {line_num}: {status} {url}{elapsed_str}{_RESET}")
        return True
    else:
        short_body = body[:200].replace("\n", " ")
        status_str = str(status) if status is not None else "ERR"
        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
        _out(bar, f"{_RED}[FAIL]  row {line_num}: {status_str} {url}{elapsed_str} | {short_body}{_RESET}")
        return False


def _validate_placeholders(args: argparse.Namespace, fieldnames: Optional[list]) -> None:
    all_placeholders = PLACEHOLDER_RE.findall(args.url) + (PLACEHOLDER_RE.findall(args.body) if args.body else [])
    if not all_placeholders:
        return
    missing = [p for p in all_placeholders if p not in (fieldnames or [])]
    if missing:
        print(f"[ERROR] CSV is missing columns required by placeholders: {missing}", file=sys.stderr)
        sys.exit(1)
    if args.body:
        err = _validate_body_template(args.body, args.content_type)
        if err:
            print(f"[ERROR] {err}", file=sys.stderr)
            sys.exit(1)


def _skip_rows(reader: csv.DictReader, count: int, bar: Optional[_BottomBar]) -> None:
    if count:
        _out(bar, f"Skipping {count} rows, starting from row {count + 1}.")
        for _ in range(count):
            next(reader, None)


def _handle_cmd_in_loop(
    cmd: Optional[str],
    bar: Optional[_BottomBar],
    line_num: int,
    ok: int,
    failed: int,
) -> bool:
    """Return True if the row loop should stop."""
    if cmd == _CMD_EXIT:
        _out(bar, f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}")
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
                    _out(bar, f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}")
                    return True
        else:
            if _wait_for_resume():
                _out(bar, f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}")
                return True
    return False


def _poll_cmd(bar: Optional[_BottomBar]) -> Optional[str]:
    return bar.poll() if bar else _stdin_command()


def _run_loop(
    reader: csv.DictReader,
    args: argparse.Namespace,
    auth_header: Optional[str],
    bar: Optional[_BottomBar],
    suspend: Optional[Callable],
    resume: Optional[Callable],
    retry_writer: Any,
    log_file: IO[str],
    offset: int,
    total_rows: int,
) -> Tuple[int, int]:
    remaining = total_rows - offset
    processed = ok = failed = 0
    for line_num, row in enumerate(reader, start=offset + 2):
        processed += 1
        absolute = offset + processed
        status, body, elapsed, url, new_auth_header = _fire(row, args, auth_header, suspend, resume)
        if new_auth_header:
            auth_header = new_auth_header
        if status is None and not url:
            failed += 1
            retry_writer.writerow(row)
            _out(bar, f"{_RED}[SKIP]  row {line_num}: {body} | row={dict(row)}{_RESET}")
            _write_failure_log(log_file, "SKIP", line_num, args.method, "", None, None, body, 0.0)
            _progress(bar, absolute, total_rows)
            continue
        req_body = None
        if args.body:
            req_body, _ = substitute(args.body, row)
        succeeded = _log_row(bar, args, line_num, status, body, elapsed, url, req_body)
        ok += int(succeeded)
        if not succeeded:
            failed += 1
            retry_writer.writerow(row)
            _write_failure_log(log_file, "FAIL", line_num, args.method, url, req_body, status, body, elapsed)
        _progress(bar, absolute, total_rows)
        cmd = _poll_cmd(bar)
        if _handle_cmd_in_loop(cmd, bar, line_num, ok, failed):
            break
        if args.delay > 0 and processed < remaining:
            time.sleep(args.delay / 1000)
    return ok, failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        _run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


def _run() -> None:
    parser = argparse.ArgumentParser(description="Bulk HTTP requests from CSV rows")
    parser.add_argument("--url", "-u", required=True, help="Target URL, may contain {{variable}} placeholders")
    parser.add_argument("--auth-type", "-a", default="none", choices=["bearer", "basic", "none"],
                        dest="auth_type", help="Auth method: bearer (default), basic, or none")
    parser.add_argument("--token", "-t", default=None, help="Bearer token (overrides BULK_TOKEN env var); used with --auth-type bearer")
    parser.add_argument("--user", "-U", default=None, help="Basic auth credentials as user:pass (overrides BULK_USER env var); used with --auth-type basic")
    parser.add_argument("--csv", "-c", required=True, dest="csv_path", help="Path to CSV file")
    parser.add_argument("--method", "-m", default="POST", help="HTTP method (default: POST)")
    parser.add_argument("--body", "-b", default=None, help="Request body (e.g. JSON string)")
    parser.add_argument("--content-type", "-C", default="application/json", dest="content_type", help="Content-Type header (default: application/json)")
    parser.add_argument("--delay", "-d", type=int, default=0, help="Delay in milliseconds between requests (default: 0)")
    parser.add_argument("--offset", "-o", type=int, default=0, help="Skip first N data rows (default: 0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print request/response details and timing")
    parser.add_argument("--timeout", "-T", type=int, default=30, help="Request timeout in seconds (default: 30)")
    parser.add_argument("--retry-file", "-r", default=None, dest="retry_file",
                        help="Path for failed-rows CSV (default: <input_stem>_failed.csv)")
    args = parser.parse_args()
    args.method = args.method.upper()

    total_rows = count_csv_rows(args.csv_path)
    offset = args.offset

    csv_path = pathlib.Path(args.csv_path)
    retry_path = pathlib.Path(args.retry_file) if args.retry_file else csv_path.parent / f"{csv_path.stem}_failed.csv"
    log_path = retry_path.with_suffix(".log")

    if offset >= total_rows > 0:
        print(f"[ERROR] --offset {offset} is beyond the last row ({total_rows} data rows total).", file=sys.stderr)
        sys.exit(1)

    try:
        csv_file = open(csv_path, newline="", encoding="utf-8")
    except OSError as e:
        print(f"[ERROR] Cannot open CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    # Read only the header to validate placeholders/body before starting the bar
    # or prompting for a token, so errors are always visible in a clean terminal.
    with csv_file:
        fieldnames: list = list(csv.DictReader(csv_file).fieldnames or [])
    _validate_placeholders(args, fieldnames)

    try:
        csv_file = open(csv_path, newline="", encoding="utf-8")
    except OSError as e:
        print(f"[ERROR] Cannot open CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    bar: Optional[_BottomBar] = None
    if sys.stdin.isatty() and _HAS_TERMIOS:
        b = _BottomBar()
        if b.start():
            bar = b

    suspend = bar.pause if bar else None
    resume = bar.resume if bar else None
    auth_header = resolve_auth_header(args, suspend=suspend, resume=resume)

    try:
        with csv_file:
            reader = csv.DictReader(csv_file)
            _skip_rows(reader, offset, bar)
            retry_file, retry_writer = _open_retry_writer(retry_path, fieldnames)
            log_file = _open_log_file(log_path)
            try:
                ok, failed = _run_loop(reader, args, auth_header, bar, suspend, resume, retry_writer, log_file, offset, total_rows)
            finally:
                retry_file.close()
                log_file.close()
    finally:
        if bar:
            bar.stop()

    if failed:
        print(f"\nDone — {total_rows - offset} rows processed: {ok} succeeded, {failed} failed.")
        print(f"Failed rows saved to: {retry_path}")
        print(f"Failure log:          {log_path}")
        sys.exit(1)
    else:
        retry_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        print(f"\nDone — {total_rows - offset} rows processed: {ok} succeeded, 0 failed.")


if __name__ == "__main__":
    main()
