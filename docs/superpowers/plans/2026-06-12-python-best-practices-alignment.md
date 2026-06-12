# Python Best-Practices Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tooling/CI guardrails to `bulk-post`, then split the 1854-line `bulk_post.py` into a `src/bulk_post/` package — without changing the 102 tests.

**Architecture:** Phase 1 adds Ruff, pragmatic mypy, `--version`, `BrokenPipeError` handling, and CI (all non-behavioral except the two additive features). Phase 2 incrementally extracts cohesive modules from the monolith, leaf-first, with a re-exporting `__init__.py` so `import bulk_post; bulk_post.<name>` keeps working. `main()` becomes `-> int` with centralized exit codes; exit-1-on-any-row-failure is preserved.

**Tech Stack:** Python 3.12+, stdlib `argparse`/`unittest`, `pyyaml` (lazy), uv, Ruff, mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-06-12-python-best-practices-alignment-design.md`

**Invariant for every task:** the full suite passes via `uv run python -m unittest discover tests/` and `bulk-post --help` works. Run both after each task before committing. Test counts: 102 at baseline → 104 after Task 3 → 105 after Task 16.

---

## File Structure

**Phase 1 touches:**
- `pyproject.toml` — add `[tool.ruff]`, `[tool.mypy]`, dev deps
- `bulk_post.py` — add `--version` flag + `BrokenPipeError` handling
- `.github/workflows/ci.yml` — new
- `.pre-commit-config.yaml` — new (optional)
- `tests/test_bulk_post.py` — add `--version` test

**Phase 2 final layout:**
```
src/bulk_post/
  __init__.py          # re-exports public + test-referenced names
  __main__.py          # python -m bulk_post
  templating.py  http.py  auth.py  csvio.py  terminal.py
  state.py  workflow.py  runner.py  workflow_runner.py  cli.py
tests/test_bulk_post.py   # unchanged except sys.path → src
pyproject.toml            # src layout
```

**Import rule (prevents cycles):** sibling modules import from each other directly (`from .http import http_request`); they NEVER import from `bulk_post` (the package `__init__`). Only `__init__.py` and `__main__.py` import from the package's modules. Extract leaves first, runners next, `cli` last.

---

# PHASE 1 — Guardrails

## Task 1: Add Ruff config and format the codebase

**Files:**
- Modify: `pyproject.toml`
- Modify: `bulk_post.py` (formatting only), `tests/test_bulk_post.py` (formatting only)

- [ ] **Step 1: Add dev dependencies**

Run:
```bash
uv add --dev ruff
```
Expected: `ruff` added under a dev group in `pyproject.toml`, `uv.lock` updated.

- [ ] **Step 2: Add Ruff config to `pyproject.toml`**

Append:
```toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "C4"]
ignore = ["E501"]  # long help/URL string literals are fine; the formatter governs code layout
```

- [ ] **Step 3: Apply the formatter (isolated commit)**

Run:
```bash
uv run ruff format .
uv run python -m unittest discover tests/
```
Expected: files reformatted; tests still pass (102 OK).

- [ ] **Step 4: Commit the format-only diff**

```bash
git add -A
git commit -m "style: apply ruff format"
```

- [ ] **Step 5: Apply safe lint autofixes**

Run:
```bash
uv run ruff check --fix .
uv run ruff check .
uv run python -m unittest discover tests/
```
Expected: autofixable issues resolved. Remaining warnings are reported. For each remaining warning, either fix it minimally or add `# noqa: <CODE>  # <reason>` on that line. Re-run `ruff check .` until clean.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "style: resolve ruff lint findings"
```

---

## Task 2: Add pragmatic mypy

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dev dependencies**

Run:
```bash
uv add --dev mypy types-PyYAML
```

- [ ] **Step 2: Add mypy config to `pyproject.toml`**

Append:
```toml
[tool.mypy]
python_version = "3.12"
warn_unused_ignores = true
warn_return_any = true
warn_redundant_casts = true
# Pragmatic start: do NOT enable disallow_untyped_defs / strict yet.
# Tighten in a later pass once the package split lands.
```

- [ ] **Step 3: Run mypy and resolve findings**

Run:
```bash
uv run mypy bulk_post.py
```
For each reported error, apply the minimal correct fix: add the missing annotation, narrow a type, or add `# type: ignore[<error-code>]  # <reason>` only where a real third-party/stdlib gap exists. Re-run until clean.

- [ ] **Step 4: Verify tests still pass**

Run: `uv run python -m unittest discover tests/`
Expected: 102 OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: add pragmatic mypy config and resolve type findings"
```

---

## Task 3: Add `--version` flag

**Files:**
- Modify: `bulk_post.py` (near top for the helper; in `_run()` parser for the flag)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bulk_post.py`:
```python
class TestVersionFlag(unittest.TestCase):
    def test_version_flag_prints_and_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.argv", ["bulk-post", "--version"]):
                bulk_post._run()
        self.assertEqual(ctx.exception.code, 0)

    def test_get_version_returns_string(self):
        self.assertIsInstance(bulk_post._get_version(), str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_bulk_post.TestVersionFlag -v`
Expected: FAIL (`_get_version` not defined).

- [ ] **Step 3: Implement the version helper**

Add near the top of `bulk_post.py` (after stdlib imports):
```python
import importlib.metadata


def _get_version() -> str:
    try:
        return importlib.metadata.version("bulk-post")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
```

- [ ] **Step 4: Add the flag to the parser**

In `_run()`, immediately after `parser = argparse.ArgumentParser(...)`:
```python
    parser.add_argument("--version", "-V", action="version",
                        version=f"%(prog)s {_get_version()}")
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run python -m unittest tests.test_bulk_post.TestVersionFlag -v`
Expected: PASS. Then full suite: `uv run python -m unittest discover tests/` → OK.

- [ ] **Step 6: Commit**

```bash
git add bulk_post.py tests/test_bulk_post.py
git commit -m "feat: add --version flag"
```

---

## Task 4: Handle `BrokenPipeError` in `main()`

**Files:**
- Modify: `bulk_post.py` (`main()`, ~line 1689)

- [ ] **Step 1: Update `main()`**

Replace the current `main()` body's `try/except KeyboardInterrupt` with:
```python
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
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
```
(`main()` stays `-> None` for now; it becomes `-> int` in Task 16.)

- [ ] **Step 2: Verify tests pass**

Run: `uv run python -m unittest discover tests/`
Expected: 102 OK.

- [ ] **Step 3: Commit**

```bash
git add bulk_post.py
git commit -m "fix: handle BrokenPipeError cleanly in main()"
```

---

## Task 5: Add CI (and optional pre-commit)

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.pre-commit-config.yaml` (optional)

- [ ] **Step 1: Create the CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}
      - name: Sync
        run: uv sync --all-extras --dev
      - name: Ruff lint
        run: uv run ruff check .
      - name: Ruff format check
        run: uv run ruff format --check .
      - name: mypy
        run: uv run mypy bulk_post.py
      - name: Tests
        run: uv run python -m unittest discover tests/
```

- [ ] **Step 2: Create pre-commit config (optional)**

`.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.10.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

- [ ] **Step 3: Validate the workflow locally**

Run the same steps CI runs:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy bulk_post.py && uv run python -m unittest discover tests/
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml .pre-commit-config.yaml
git commit -m "ci: add lint + type-check + test workflow"
```

> **Note:** the `mypy bulk_post.py` path in CI changes to `mypy src` in Task 16. Update it then.

---

# PHASE 2 — Split into `src/bulk_post/` package

> Each extraction task: create the new module (with its sibling imports at the top), move the named functions/classes **verbatim** out of `__init__.py`, replace them in `__init__.py` with a re-export `from .<module> import <names>`, then run tests and commit. Sibling modules must not import from the package root.

## Task 6: Move to `src/` layout (no split yet)

**Files:**
- Move: `bulk_post.py` → `src/bulk_post/__init__.py`
- Create: `src/bulk_post/__main__.py`
- Modify: `pyproject.toml`, `tests/test_bulk_post.py`

- [ ] **Step 1: Create the package and move the module**

Run:
```bash
mkdir -p src/bulk_post
git mv bulk_post.py src/bulk_post/__init__.py
```

- [ ] **Step 2: Add `__main__.py`**

`src/bulk_post/__main__.py`:
```python
from bulk_post import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Update `pyproject.toml` to src layout**

Ensure these sections exist (replace any existing packages config):
```toml
[project.scripts]
bulk-post = "bulk_post:main"

[tool.setuptools.packages.find]
where = ["src"]
```
Update the mypy path if it targeted a file: `[tool.mypy]` stays, but Task 16 will switch CI to `mypy src`.

- [ ] **Step 4: Point the test import at `src/`**

In `tests/test_bulk_post.py`, change the path insert from the repo root to `src`:
```python
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import bulk_post
```

- [ ] **Step 5: Reinstall and run everything**

Run:
```bash
uv sync
uv run python -m unittest discover tests/
uv run bulk-post --version
uv run python -m bulk_post --help
```
Expected: 104 OK; version prints; `python -m bulk_post` shows help.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: move bulk_post.py into src/bulk_post package"
```

---

## Task 7: Extract `templating.py`

**Files:**
- Create: `src/bulk_post/templating.py`
- Modify: `src/bulk_post/__init__.py`

- [ ] **Step 1: Create the module**

`src/bulk_post/templating.py` — header then move `substitute`, `_validate_body_template`, `_validate_placeholders` verbatim:
```python
"""Placeholder substitution and template/placeholder validation."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional
import xml.etree.ElementTree as ET

# ... moved verbatim: substitute(), _validate_body_template(), _validate_placeholders()
```
(Include exactly the imports each moved function uses; remove unused ones — `ruff check` will tell you.)

- [ ] **Step 2: Re-export from `__init__.py`**

Delete those three function definitions from `__init__.py` and add near the top:
```python
from .templating import (
    substitute,
    _validate_body_template,
    _validate_placeholders,
)
```

- [ ] **Step 3: Verify and commit**

Run:
```bash
uv run ruff check . && uv run python -m unittest discover tests/
```
Expected: clean + 104 OK.
```bash
git add -A && git commit -m "refactor: extract templating module"
```

---

## Task 8: Extract `http.py`

**Files:** Create `src/bulk_post/http.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `http_request`, `_mask_headers` verbatim. Header:
```python
"""HTTP request execution and header masking."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Optional
```

- [ ] **Step 2: Re-export**

In `__init__.py`, delete those defs and add `from .http import http_request, _mask_headers`.

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract http module"
```

---

## Task 9: Extract `state.py`

**Files:** Create `src/bulk_post/state.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `_ParallelState`, `_WorkflowParallelState` verbatim. Header:
```python
"""Shared mutable state for parallel worker threads."""
from __future__ import annotations

import threading
from typing import Optional
```

- [ ] **Step 2: Re-export**

In `__init__.py`, delete those classes and add `from .state import _ParallelState, _WorkflowParallelState`.

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract state module"
```

---

## Task 10: Extract `csvio.py`

**Files:** Create `src/bulk_post/csvio.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `count_csv_rows`, `_open_retry_writer`, `_open_log_file`, `_write_failure_log`, `_skip_rows` verbatim. Header:
```python
"""CSV row counting, retry-file writer, and failure log helpers."""
from __future__ import annotations

import csv
import pathlib
from typing import IO, Any, Optional
```
`_skip_rows` and `_write_failure_log` take a `bar`/output argument; keep their signatures unchanged (the caller passes the object). Do not import `terminal` here — these helpers receive what they need as parameters.

- [ ] **Step 2: Re-export**

`from .csvio import count_csv_rows, _open_retry_writer, _open_log_file, _write_failure_log, _skip_rows`

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract csvio module"
```

---

## Task 11: Extract `terminal.py`

**Files:** Create `src/bulk_post/terminal.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `_BottomBar`, `_get_suggestion`, `_render_bar`, `print_progress`, `_out`, `_progress`, `_stdin_command`, `_wait_for_resume`, `_poll_cmd`, `print_verbose` verbatim, plus the `_CMD_PAUSE`/`_CMD_RESUME`/`_CMD_EXIT`/`_COMMANDS` constants and any `termios`/`_HAS_TERMIOS`/`select` setup they rely on. Header (adjust to actual usage):
```python
"""Terminal UI: bottom bar, progress rendering, command polling, verbose output."""
from __future__ import annotations

import sys
from typing import Optional

from .http import _mask_headers  # print_verbose masks auth headers
```
Keep the TTY/`termios` import guard exactly as in the original.

- [ ] **Step 2: Re-export**

In `__init__.py` add:
```python
from .terminal import (
    _BottomBar, _get_suggestion, _render_bar, print_progress, _out, _progress,
    _stdin_command, _wait_for_resume, _poll_cmd, print_verbose,
    _CMD_PAUSE, _CMD_RESUME, _CMD_EXIT, _COMMANDS,
)
```

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract terminal UI module"
```

---

## Task 12: Extract `auth.py`

**Files:** Create `src/bulk_post/auth.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `resolve_token`, `prompt_new_token`, `resolve_basic_creds`, `prompt_new_basic_creds`, `resolve_auth_header`, `_make_auth_refresh_fn` verbatim. Header:
```python
"""Token / basic-auth resolution and 401 refresh callbacks."""
from __future__ import annotations

import argparse
import base64
import getpass
import os
from typing import Callable, Optional
```
`_make_auth_refresh_fn` references the prompt functions — keep them in this module so the call is intra-module.

- [ ] **Step 2: Re-export**

```python
from .auth import (
    resolve_token, prompt_new_token, resolve_basic_creds,
    prompt_new_basic_creds, resolve_auth_header, _make_auth_refresh_fn,
)
```

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract auth module"
```

---

## Task 13: Extract `workflow.py`

**Files:** Create `src/bulk_post/workflow.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `WorkflowStep`, `parse_workflow`, `_validate_workflow_placeholders`, `_resolve_workflow_auth_headers`, `_fire_workflow_step` verbatim. Header:
```python
"""Workflow YAML parsing, validation, and single-step execution."""
from __future__ import annotations

from typing import Optional, Tuple

from .http import http_request
from .templating import substitute
from .auth import resolve_auth_header
# NOTE: keep `import yaml` LAZY inside parse_workflow (do not move it to module top).
```
Preserve the lazy `import yaml as _yaml` inside `parse_workflow` exactly.

- [ ] **Step 2: Re-export**

```python
from .workflow import (
    WorkflowStep, parse_workflow, _validate_workflow_placeholders,
    _resolve_workflow_auth_headers, _fire_workflow_step,
)
```

- [ ] **Step 3: Verify and commit**

Confirm laziness preserved (importing the package must not import yaml):
```bash
uv run python -c "import sys, bulk_post; assert 'yaml' not in sys.modules, 'yaml imported too early'"
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract workflow module"
```

---

## Task 14: Extract `runner.py`

**Files:** Create `src/bulk_post/runner.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move single-URL execution verbatim: `_fire`, `_log_row`, `_handle_cmd_in_loop`, `_run_loop`, `_parallel_worker`, `_run_parallel_main_loop`, `_run_parallel`. Header (import from siblings, never from the package root):
```python
"""Single-URL row execution: sequential and parallel runners."""
from __future__ import annotations

import argparse
import queue
import threading
import time
from typing import Optional

from .http import http_request
from .templating import substitute
from .auth import _make_auth_refresh_fn
from .state import _ParallelState
from .csvio import _write_failure_log, _skip_rows
from .terminal import (
    _BottomBar, _out, _progress, _poll_cmd, _wait_for_resume,
    _CMD_EXIT, _CMD_PAUSE, _CMD_RESUME,
)
```
`_handle_cmd_in_loop` lives in **this** module (it is in the move list above); it uses `_wait_for_resume`/`_poll_cmd` imported from `terminal`. It is NOT in `terminal`.

- [ ] **Step 2: Re-export**

```python
from .runner import (
    _fire, _log_row, _run_loop, _parallel_worker,
    _run_parallel_main_loop, _run_parallel, _handle_cmd_in_loop,
)
```

- [ ] **Step 3: Verify and commit**

The `/resume`-after-drain regression test lives here — confirm it passes.
```bash
uv run python -m unittest tests.test_bulk_post.TestResumeAfterDrain -v
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract single-URL runner module"
```

---

## Task 15: Extract `workflow_runner.py`

**Files:** Create `src/bulk_post/workflow_runner.py`; modify `__init__.py`.

- [ ] **Step 1: Create the module**

Move `_run_workflow_loop`, `_make_workflow_auth_refresh_fns`, `_workflow_parallel_worker`, `_run_workflow_parallel` verbatim. Header:
```python
"""Workflow execution: sequential and parallel multi-step runners."""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from .workflow import _fire_workflow_step, _resolve_workflow_auth_headers
from .auth import prompt_new_token, prompt_new_basic_creds
from .state import _WorkflowParallelState
from .csvio import _write_failure_log, _skip_rows
from .terminal import _BottomBar, _out, _progress, _poll_cmd
from .runner import _run_parallel_main_loop  # shared parallel loop lives in runner.py
```
`_run_workflow_parallel` calls `_run_parallel_main_loop` (defined in `runner.py`), so this module depends on `runner` — keep the dependency one-way (runner must not import workflow_runner).

- [ ] **Step 2: Re-export**

```python
from .workflow_runner import (
    _run_workflow_loop, _make_workflow_auth_refresh_fns,
    _workflow_parallel_worker, _run_workflow_parallel,
)
```

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run python -m unittest discover tests/
git add -A && git commit -m "refactor: extract workflow runner module"
```

---

## Task 16: Extract `cli.py`, make `main() -> int` with centralized exit codes

**Files:** Create `src/bulk_post/cli.py`; modify `src/bulk_post/__init__.py`, `.github/workflows/ci.yml`.

- [ ] **Step 1: Add a failing test for the exit-code contract**

Add to `tests/test_bulk_post.py`:
```python
class TestMainExitCodes(unittest.TestCase):
    def test_missing_url_and_workflow_returns_1(self):
        # _run must signal failure via return code, not bare sys.exit deep inside
        with patch("sys.argv", ["bulk-post", "-c", "nonexistent.csv"]), \
             patch("sys.stdin.isatty", return_value=False):
            code = bulk_post.main()
        self.assertEqual(code, 1)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run python -m unittest tests.test_bulk_post.TestMainExitCodes -v`
Expected: FAIL (`main()` returns `None` / `SystemExit` propagates).

- [ ] **Step 3: Move parsing+dispatch into `cli.py` and centralize exit codes**

Create `src/bulk_post/cli.py` containing `build_parser()`, `_run()`, and `main()`:
```python
"""CLI entry point: argument parsing, dispatch, and exit codes."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys

from .runner import _run_loop, _run_parallel
from .workflow_runner import _run_workflow_loop, _run_workflow_parallel
# ... other sibling imports the dispatch needs (auth, csvio, terminal, templating, workflow)
# IMPORTANT: cli.py must NOT do `from . import ...` (that would import the package
# __init__, which imports cli — a cycle). Import only from sibling modules.


class _CliError(Exception):
    """Raised for setup/validation failures; carries an exit code."""
    def __init__(self, code: int = 1) -> None:
        super().__init__()
        self.code = code


def _get_version() -> str:
    """Moved here from __init__ in this task."""
    try:
        return importlib.metadata.version("bulk-post")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bulk HTTP requests from CSV rows")
    parser.add_argument("--version", "-V", action="version",
                        version=f"%(prog)s {_get_version()}")
    # ... move every add_argument(...) call verbatim from the old _run() ...
    return parser


def _run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # ... move the rest of the old _run() body here ...
    # Replace EVERY `sys.exit(1)` in this body with `raise _CliError(1)`
    # (keep the preceding `print(..., file=sys.stderr)` lines as-is).
    # Replace the final failure branch `sys.exit(1)` with `return 1`
    # and the success path with `return 0`.
    ...


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except _CliError as exc:
        return exc.code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
```
Mechanical rule for the moved `_run()` body: each existing `sys.exit(1)` becomes `raise _CliError(1)`; the final "rows failed" `sys.exit(1)` becomes `return 1`; the success print path ends with `return 0`. **Exit-1-on-any-row-failure is preserved.**

- [ ] **Step 4: Slim `__init__.py` to re-exports + entry shim**

`__init__.py` should now contain only the `from .<module> import ...` re-export lines (templating, http, auth, csvio, terminal, state, workflow, runner, workflow_runner) plus:
```python
from .cli import build_parser, main, _run, _get_version
```
(`_get_version` no longer lives in `__init__` — it moved to `cli.py` in Step 3; this line re-exports it so `bulk_post._get_version` still resolves.)
Update `__main__.py` to:
```python
import sys
from bulk_post import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Update CI mypy target**

In `.github/workflows/ci.yml` change `uv run mypy bulk_post.py` → `uv run mypy src`.

- [ ] **Step 6: Run the full gate**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run python -m unittest discover tests/
uv run bulk-post --version && uv run python -m bulk_post --help
```
Expected: all clean; new `TestMainExitCodes` passes; 105 tests OK.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: extract cli module; main() returns int with centralized exit codes"
```

---

## Task 17: Correct the exit-code rule in `.claude/rules/cli.md`

**Files:** Modify `.claude/rules/cli.md`

- [ ] **Step 1: Replace the misleading exit-code bullet**

Change the rule that says row failures should not cause a non-zero exit to:
```markdown
- `0` = success. Non-zero = failure. argparse exits `2` on a usage error.
- This tool exits **`1` when any row fails** (so CI/scripts can detect partial failures); individual failures are still written to the retry file. Reserve other non-zero codes for setup/validation errors (raised as `_CliError`).
```

- [ ] **Step 2: Commit**

```bash
git add .claude/rules/cli.md
git commit -m "docs: correct exit-code rule to match tool behavior (exit 1 on row failure)"
```

---

## Task 18: Update README and CLAUDE.md for the package layout

**Files:** Modify `README.md`, `CLAUDE.md`

- [ ] **Step 1: Update run/install commands**

In both files, replace `python bulk_post.py ...` invocations with `python -m bulk_post ...` (or `uv run bulk-post ...`). Note that direct-file invocation no longer exists.

- [ ] **Step 2: Update structure/API notes**

- `CLAUDE.md`: update the intro line that calls it a single-file CLI to describe the `src/bulk_post/` package; in "Public API surface", note names are importable from the `bulk_post` package (re-exported in `__init__`). Update the Tests command if needed (`uv run python -m unittest discover tests/`).
- `README.md`: update the Installation and "run directly without installing" sections to use `python -m bulk_post`.

- [ ] **Step 3: Final full verification**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run python -m unittest discover tests/
```
Expected: all clean; 105 tests OK.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: update README and CLAUDE.md for src/bulk_post package layout"
```

---

## Done criteria

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and `uv run python -m unittest discover tests/` all pass.
- `bulk-post`, `python -m bulk_post`, and `uv run bulk-post` all work; `--version` prints.
- All original 102 tests pass unchanged (only the `sys.path` line edited) plus the new `--version` and exit-code tests.
- The `/resume`-after-drain regression test still passes.
- Importing `bulk_post` does not import `yaml` (laziness preserved).
- Exit-1-on-any-row-failure preserved; `.claude/rules/cli.md` matches.
