"""Terminal UI: bottom bar, progress rendering, command polling, verbose output."""

from __future__ import annotations

import contextlib
import io
import os
import queue
import signal
import sys
import threading
import time
from typing import Any, cast

from .http import _mask_headers

try:
    import select as _select_mod
    import termios
    import tty

    _HAS_TERMIOS = True
    _HAS_SELECT = hasattr(_select_mod, "select")
except ImportError:
    _HAS_TERMIOS = False
    _HAS_SELECT = False
    _select_mod = None  # type: ignore[assignment]

BAR_WIDTH = 40
_CMD_PROMPT = "  cmd> "

# ANSI colour helpers
_RESET = "\033[0m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_GREY = "\033[90m"  # bright-black / dark-grey
_TERRACOTTA = "\033[38;5;166m"  # reddish-orange
_GHOST = "\033[2;37m"  # dim white — ghost / suggestion text

_CMD_PAUSE = "/pause"
_CMD_RESUME = "/resume"
_CMD_EXIT = "/exit"
_COMMANDS = [_CMD_PAUSE, _CMD_RESUME, _CMD_EXIT]


def _get_suggestion(buf: str) -> str:
    """Return the completion suffix for buf if it uniquely matches a command."""
    if not buf.startswith("/"):
        return ""
    for cmd in _COMMANDS:
        if cmd.startswith(buf) and cmd != buf:
            return cmd[len(buf) :]
    return ""


def _render_bar(current: int, total: int) -> str:
    """Render the ``====>`` progress-bar string of width ``BAR_WIDTH``."""
    filled = int(BAR_WIDTH * current / total)
    return (
        "=" * filled
        + (">" if filled < BAR_WIDTH else "=")
        + " " * max(0, BAR_WIDTH - filled - 1)
    )


class _BottomBar:
    """
    Reserves the bottom 2 terminal rows:
      row h-1  — live progress bar
      row h    — command input (always visible while typing)
    All other output scrolls within rows 1..h-2 via an ANSI scroll region.
    Only used when sys.stdin.isatty() and termios is available.
    """

    def __init__(self, debug_mode: bool = False) -> None:
        self._cmd_queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._buf = ""
        self._nav_idx: int = -1  # -1 = free input; 0..len(_COMMANDS)-1 = navigating
        self._saved_buf: str = ""  # buffer snapshot taken when entering navigation
        self._height = 0
        self._scroll_end = 0  # last row of scroll region; set in start()
        self._debug_mode = debug_mode
        self._active = False
        self._thread: threading.Thread | None = None
        self._old_settings: list[Any] | None = None
        self._paused = threading.Event()
        self._paused.set()  # set = not paused → input thread reads normally
        self._paused_ack = threading.Event()  # set by input thread when raw mode exited
        self._stdout_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def start(self) -> bool:
        """Reserve the bottom rows, enter raw mode, and start the input thread.

        Returns False (caller falls back to no bar) if the terminal is too short
        or raw mode can't be enabled.
        """
        import shutil

        self._height = shutil.get_terminal_size((80, 24)).lines
        if self._height < (6 if self._debug_mode else 5):
            return False
        try:
            self._old_settings = termios.tcgetattr(sys.stdin.fileno())
        except termios.error:
            return False
        self._scroll_end = self._height - (3 if self._debug_mode else 2)
        with self._stdout_lock:
            sys.stdout.write(
                f"\033[1;{self._scroll_end}r\033[{self._height};1H\033[2K{_GREY}{_CMD_PROMPT}{_RESET}"
            )
            sys.stdout.flush()
        self._active = True
        self._thread = threading.Thread(target=self._input_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the input thread, restore terminal settings, and clear the bottom rows."""
        self._active = False
        if self._thread:
            self._thread.join(timeout=0.2)
        if self._old_settings is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings
                )
        # Reset scroll region and clear bottom rows
        if self._debug_mode:
            clear = f"\033[r\033[{self._height - 2};1H\033[2K\033[{self._height - 1};1H\033[2K\033[{self._height};1H\033[2K"
        else:
            clear = (
                f"\033[r\033[{self._height - 1};1H\033[2K\033[{self._height};1H\033[2K"
            )
        with self._stdout_lock:
            sys.stdout.write(clear)
            sys.stdout.flush()

    def pause(self) -> None:
        """Suspend raw mode so the main thread can call input()."""
        self._paused_ack.clear()
        self._paused.clear()
        # Wait for the input thread to acknowledge it has left raw mode.
        self._paused_ack.wait(timeout=0.5)
        # Place cursor at the bottom of the scroll region for interactive prompts
        with self._stdout_lock:
            sys.stdout.write(f"\033[{self._scroll_end};1H\n\r")
            sys.stdout.flush()

    def resume(self) -> None:
        """Re-enter raw mode after input() is done."""
        with contextlib.suppress(termios.error):
            tty.setraw(sys.stdin.fileno())
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
        """Print ``text`` in the scroll region above the bar, preserving the cmd line."""
        with self._lock:
            buf = self._buf
        # Move to last scrollable row → \n scrolls the region up → print on fresh row
        with self._stdout_lock:
            sys.stdout.write(
                f"\033[{self._scroll_end};1H\n\r{text}{_RESET}\033[K"
                f"\033[{self._height};{len(_CMD_PROMPT) + len(buf) + 1}H"
            )
            sys.stdout.flush()

    def update_progress(self, current: int, total: int) -> None:
        """Redraw the progress-bar row (h-1) without disturbing the cmd line."""
        if total == 0:
            return
        bar = _render_bar(current, total)
        line = f"{_CYAN}  [{bar}] {int(100 * current / total):3}%  {current}/{total}{_RESET}"
        with self._lock:
            buf = self._buf
        with self._stdout_lock:
            sys.stdout.write(
                f"\033[{self._height - 1};1H\033[2K{line}"
                f"\033[{self._height};{len(_CMD_PROMPT) + len(buf) + 1}H"
            )
            sys.stdout.flush()

    def update_debug(self, text: str) -> None:
        """Redraw the debug row (h-2) in ``--debug`` mode; no-op otherwise."""
        if not self._debug_mode:
            return
        with self._lock:
            buf = self._buf
        with self._stdout_lock:
            sys.stdout.write(
                f"\033[{self._height - 2};1H\033[2K{_GREY}{text}{_RESET}"
                f"\033[{self._height};{len(_CMD_PROMPT) + len(buf) + 1}H"
            )
            sys.stdout.flush()

    def poll(self) -> str | None:
        """Return the next command typed into the bar, or None if none is pending."""
        try:
            return self._cmd_queue.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------ private

    def _handle_pause_state(self) -> None:
        """Exit raw mode, signal the main thread, wait to resume, re-enter raw mode."""
        if self._old_settings is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings
                )
        self._paused_ack.set()
        self._paused.wait()
        with contextlib.suppress(termios.error):
            tty.setraw(sys.stdin.fileno())

    def _handle_char(self, ch: str) -> None:
        """Dispatch one input char: Enter submits, Tab completes, arrows navigate, etc."""
        if ch in ("\r", "\n"):
            with self._lock:
                cmd, self._buf = self._buf, ""
                self._nav_idx = -1
            with contextlib.suppress(Exception):
                self._redraw_cmd()
            if cmd:
                self._cmd_queue.put(cmd)
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
        """Decode an arrow-key escape sequence (ESC [ A/B) into nav up/down."""
        # Arrow keys arrive as ESC [ A/B (3 bytes). _input_loop reads ESC via
        # sys.stdin.buffer.read1(1) (binary), so the remaining [ and A/B bytes
        # stay in BufferedReader._read_buf (same OS read chunk) — not in
        # TextIOWrapper._decoded_chars, which select can't see.
        # Check _read_buf first; fall back to select on the OS fd.
        # A bare ESC has nothing in either place → return immediately, don't block.
        raw: io.BufferedReader = cast(io.BufferedReader, sys.stdin.buffer)

        def _next_available() -> bool:
            pending = getattr(raw, "_read_buf", None)
            if pending is not None and len(pending) > 0:
                return True
            r, _, _ = _select_mod.select([raw], [], [], 0.05)
            return bool(r)

        if not _next_available():
            return  # bare ESC — nothing follows, ignore
        b = raw.read1(1)
        if not b:
            return
        bracket = b.decode("utf-8", errors="replace")
        if bracket != "[":
            return
        if not _next_available():
            return
        b = raw.read1(1)
        if not b:
            return
        code = b.decode("utf-8", errors="replace")
        if code == "A":
            self._nav_up()
        elif code == "B":
            self._nav_down()

    def _nav_up(self) -> None:
        """Cycle the cmd buffer up through the command list (saving free input first)."""
        with self._lock:
            if self._nav_idx == -1:
                self._saved_buf = self._buf
                self._nav_idx = len(_COMMANDS) - 1
            elif self._nav_idx > 0:
                self._nav_idx -= 1
            self._buf = _COMMANDS[self._nav_idx]
        self._redraw_cmd()

    def _nav_down(self) -> None:
        """Cycle the cmd buffer down the command list; restore free input at the end."""
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
        """Input-thread loop: read stdin in raw mode and feed chars to ``_handle_char``."""
        try:
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            return
        raw: io.BufferedReader = cast(io.BufferedReader, sys.stdin.buffer)
        try:
            while self._active:
                if not self._paused.is_set():
                    self._handle_pause_state()
                    continue
                r, _, _ = _select_mod.select([raw], [], [], 0.05)
                if not r:
                    continue
                try:
                    b = raw.read1(1)
                    if not b:
                        continue
                    self._handle_char(b.decode("utf-8", errors="replace"))
                except Exception:
                    pass
        except Exception:
            pass

    def _redraw_cmd(self) -> None:
        """Repaint the command-input row with the current buffer + ghost suggestion."""
        with self._lock:
            buf = self._buf
        suggestion = _get_suggestion(buf)
        typed = f"{_TERRACOTTA}{buf}{_RESET}" if buf.startswith("/") else buf
        ghost = f"{_GHOST}{suggestion}{_RESET}" if suggestion else ""
        # After drawing ghost text move cursor back to end of actual buf
        cursor_col = len(_CMD_PROMPT) + len(buf) + 1
        with self._stdout_lock:
            sys.stdout.write(
                f"\033[{self._height};1H\033[2K{_GREY}{_CMD_PROMPT}{_RESET}{typed}{ghost}"
                f"\033[{self._height};{cursor_col}H"
            )
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_progress(current: int, total: int) -> None:
    """Print a one-line ``\\r`` progress bar to stdout (no-bar / non-TTY fallback)."""
    if total == 0:
        return
    bar = _render_bar(current, total)
    pct = int(100 * current / total)
    print(f"\r  [{bar}] {pct:3}%  {current}/{total}", end="", flush=True)


def _out(bar: _BottomBar | None, text: str) -> None:
    """Write a line via the bottom bar if present, else plain ``print``."""
    if bar:
        bar.write_line(text)
    else:
        print(text)


def _progress(bar: _BottomBar | None, current: int, total: int) -> None:
    """Update the progress display via the bottom bar if present, else stdout."""
    if bar:
        bar.update_progress(current, total)
    else:
        print_progress(current, total)


def print_verbose(
    bar: _BottomBar | None,
    method: str,
    url: str,
    req_body: str | None,
    req_headers: dict,
    status: int | None,
    resp_body: str,
    resp_headers: dict,
    elapsed: float,
) -> None:
    """Print the full request and response (auth headers masked) for ``--verbose``."""
    _out(bar, f"  > {method} {url}")
    for k, v in _mask_headers(req_headers).items():
        _out(bar, f"  > {k}: {v}")
    if req_body:
        _out(bar, f"  > body: {req_body}")
    status_str = str(status) if status is not None else "ERR"
    _out(bar, f"  < {status_str}  ({elapsed * 1000:.0f} ms)")
    for k, v in resp_headers.items():
        _out(bar, f"  < {k}: {v}")
    if resp_body:
        _out(bar, f"  < {resp_body[:500]}")


def _stdin_command() -> str | None:
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


def _poll_cmd(bar: _BottomBar | None) -> str | None:
    """Return a pending command from the bottom bar (TTY) or stdin (non-TTY)."""
    return bar.poll() if bar else _stdin_command()
