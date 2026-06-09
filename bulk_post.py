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
import csv
import os
import pathlib
import queue
import re
import select
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, Tuple

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False

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

_COMMANDS = ["/pause", "/resume", "/exit"]


def _get_suggestion(buf: str) -> str:
    """Return the completion suffix for buf if it uniquely matches a command."""
    if not buf.startswith("/"):
        return ""
    for cmd in _COMMANDS:
        if cmd.startswith(buf) and cmd != buf:
            return cmd[len(buf):]
    return ""


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
        self._h = 0
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._old_settings = None
        self._paused = threading.Event()
        self._paused.set()  # set = not paused → input thread reads normally

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
        self._paused.clear()
        time.sleep(0.05)  # let the input thread finish its current select
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
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
        filled = int(BAR_WIDTH * current / total)
        bar = "=" * filled + (">" if filled < BAR_WIDTH else "=") + " " * max(0, BAR_WIDTH - filled - 1)
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

    def _input_loop(self) -> None:
        try:
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            return
        try:
            while self._active:
                if not self._paused.wait(timeout=0.1):
                    continue
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if not ch:
                    continue
                if ch in ("\r", "\n"):
                    with self._lock:
                        cmd, self._buf = self._buf, ""
                    self._redraw_cmd()
                    if cmd:
                        self._q.put(cmd)
                elif ch == "\x03":  # Ctrl+C → raise SIGINT on main thread
                    os.kill(os.getpid(), signal.SIGINT)
                elif ch == "\t":  # Tab → accept autocomplete suggestion
                    with self._lock:
                        suggestion = _get_suggestion(self._buf)
                        if suggestion:
                            self._buf += suggestion
                    self._redraw_cmd()
                elif ch in ("\x7f", "\x08"):  # backspace
                    with self._lock:
                        self._buf = self._buf[:-1]
                    self._redraw_cmd()
                elif ch.isprintable():
                    with self._lock:
                        self._buf += ch
                    self._redraw_cmd()
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
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def print_progress(current: int, total: int) -> None:
    if total == 0:
        return
    filled = int(BAR_WIDTH * current / total)
    bar = "=" * filled + (">" if filled < BAR_WIDTH else "=") + " " * max(0, BAR_WIDTH - filled - 1)
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


def resolve_token(flag_value: Optional[str], bar: Optional[_BottomBar] = None) -> str:
    if flag_value:
        return flag_value
    env = os.environ.get("BULK_TOKEN", "").strip()
    if env:
        return env
    if bar:
        bar.pause()
    try:
        token = input("Paste your Bearer token: ").strip()
    except EOFError:
        token = ""
    if bar:
        bar.resume()
    if not token:
        print("[ERROR] No token provided.", file=sys.stderr)
        sys.exit(1)
    return token


def prompt_new_token(bar: Optional[_BottomBar] = None) -> str:
    if bar:
        bar.pause()
    print("\n[AUTH]  Token expired (401). Grab a fresh token from browser DevTools.")
    try:
        token = input("Paste new Bearer token: ").strip()
    except EOFError:
        token = ""
    if bar:
        bar.resume()
    if not token:
        print("[ERROR] No token provided — aborting.", file=sys.stderr)
        sys.exit(1)
    return token


def substitute(template: str, row: dict) -> Tuple[str, Optional[str]]:
    missing = [p for p in PLACEHOLDER_RE.findall(template) if p not in row]
    if missing:
        return template, f"Missing CSV columns for placeholders: {missing}"
    return PLACEHOLDER_RE.sub(lambda m: row[m.group(1)], template), None


def http_request(url: str, token: str, method: str, body: Optional[str], timeout: int = 30) -> Tuple[Optional[int], str, float]:
    """Returns (status_or_None, response_body, elapsed_seconds)."""
    encoded_body = body.encode("utf-8") if body else None
    headers = {"Authorization": f"Bearer {token}"}
    if encoded_body:
        headers["Content-Type"] = "application/json"
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
    """Return a stripped line from stdin if one is ready, else None."""
    if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
        try:
            return sys.stdin.readline().strip()
        except (OSError, EOFError):
            pass
    return None


def _wait_for_resume() -> None:
    print("\n[PAUSED]  Type /resume to continue...", flush=True)
    while True:
        time.sleep(0.2)
        cmd = _stdin_command()
        if cmd == "/resume":
            print("[RESUMED]", flush=True)
            return


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
    parser.add_argument("--token", "-t", default=None, help="Bearer token (overrides BULK_TOKEN env var)")
    parser.add_argument("--csv", "-c", required=True, dest="csv_path", help="Path to CSV file")
    parser.add_argument("--method", "-m", default="POST", help="HTTP method (default: POST)")
    parser.add_argument("--body", "-b", default=None, help="Request body (e.g. JSON string)")
    parser.add_argument("--delay", "-d", type=int, default=0, help="Delay in milliseconds between requests (default: 0)")
    parser.add_argument("--offset", "-o", type=int, default=0, help="Skip first N data rows (default: 0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print request/response details and timing")
    parser.add_argument("--timeout", "-T", type=int, default=30, help="Request timeout in seconds (default: 30)")
    parser.add_argument("--retry-file", "-r", default=None, dest="retry_file",
                        help="Path for failed-rows CSV (default: <input_stem>_failed.csv)")
    args = parser.parse_args()

    token = resolve_token(args.token)
    method = args.method.upper()
    placeholders = PLACEHOLDER_RE.findall(args.url)
    total_rows = count_csv_rows(args.csv_path)
    offset = args.offset

    csv_stem = pathlib.Path(args.csv_path).stem
    csv_dir = pathlib.Path(args.csv_path).parent
    retry_path = args.retry_file if args.retry_file else str(csv_dir / f"{csv_stem}_failed.csv")

    if offset >= total_rows and total_rows > 0:
        print(f"[ERROR] --offset {offset} is beyond the last row ({total_rows} data rows total).", file=sys.stderr)
        sys.exit(1)

    try:
        csv_file = open(args.csv_path, newline="", encoding="utf-8")
    except OSError as e:
        print(f"[ERROR] Cannot open CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    bar: Optional[_BottomBar] = None
    if sys.stdin.isatty() and _HAS_TERMIOS:
        b = _BottomBar()
        if b.start():
            bar = b

    try:
        with csv_file:
            reader = csv.DictReader(csv_file)

            all_placeholders = placeholders + (PLACEHOLDER_RE.findall(args.body) if args.body else [])
            if all_placeholders:
                missing_headers = [p for p in all_placeholders if p not in (reader.fieldnames or [])]
                if missing_headers:
                    print(f"[ERROR] CSV is missing columns required by placeholders: {missing_headers}", file=sys.stderr)
                    sys.exit(1)

            if offset:
                _out(bar, f"Skipping {offset} rows, starting from row {offset + 1}.")
                for _ in range(offset):
                    next(reader, None)

            remaining = total_rows - offset
            processed = ok = failed = 0
            fieldnames = reader.fieldnames or []
            retry_file = open(retry_path, "w", newline="", encoding="utf-8")
            retry_writer = csv.DictWriter(retry_file, fieldnames=fieldnames)
            retry_writer.writeheader()

            try:
                for line_num, row in enumerate(reader, start=offset + 2):  # line 1 is header, +offset skipped
                    processed += 1
                    absolute = offset + processed
                    url, err = substitute(args.url, row)
                    if not err and args.body:
                        req_body, err = substitute(args.body, row)
                    else:
                        req_body = args.body

                    if err:
                        failed += 1
                        retry_writer.writerow(row)
                        _out(bar, f"{_RED}[SKIP]  row {line_num}: {err} | row={dict(row)}{_RESET}")
                        _progress(bar, absolute, total_rows)
                        continue

                    status, body, elapsed = http_request(url, token, method, req_body, args.timeout)

                    if status == 401:
                        token = prompt_new_token(bar)
                        status, body, elapsed = http_request(url, token, method, req_body, args.timeout)

                    if args.verbose:
                        print_verbose(bar, method, url, req_body, status, body, elapsed)

                    if status is not None and 200 <= status < 300:
                        ok += 1
                        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
                        _out(bar, f"{_GREEN}[OK]    row {line_num}: {status} {url}{elapsed_str}{_RESET}")
                    else:
                        failed += 1
                        retry_writer.writerow(row)
                        short_body = body[:200].replace("\n", " ")
                        status_str = str(status) if status is not None else "ERR"
                        elapsed_str = f"  ({elapsed * 1000:.0f} ms)" if not args.verbose else ""
                        _out(bar, f"{_RED}[FAIL]  row {line_num}: {status_str} {url}{elapsed_str} | {short_body}{_RESET}")

                    _progress(bar, absolute, total_rows)

                    cmd = bar.poll() if bar else _stdin_command()
                    if cmd == "/exit":
                        _out(bar, f"{_GREY}[EXIT]  Stopping after row {line_num} ({ok} ok, {failed} failed so far).{_RESET}")
                        break
                    elif cmd == "/pause":
                        if bar:
                            bar.write_line("[PAUSED]  Type /resume to continue...")
                            while True:
                                time.sleep(0.1)
                                resume_cmd = bar.poll()
                                if resume_cmd == "/resume":
                                    bar.write_line("[RESUMED]")
                                    break
                        else:
                            _wait_for_resume()

                    if args.delay > 0 and processed < remaining:
                        time.sleep(args.delay / 1000)
            finally:
                retry_file.close()
    finally:
        if bar:
            bar.stop()

    if failed:
        print(f"\nDone — {remaining} rows processed: {ok} succeeded, {failed} failed.")
        print(f"Failed rows saved to: {retry_path}")
        sys.exit(1)
    else:
        pathlib.Path(retry_path).unlink(missing_ok=True)
        print(f"\nDone — {remaining} rows processed: {ok} succeeded, 0 failed.")


if __name__ == "__main__":
    main()
