# Design: Align bulk-post with Python best practices

**Date:** 2026-06-12
**Status:** Approved (pending spec review)

## Goal

Bring the `bulk-post` CLI into alignment with the project's own best-practices
rules (`PYTHON_BEST_PRACTICES.md`, `.claude/rules/`). Done in two phases, safest
order first: add tooling/CI guardrails, then refactor the structure under their
protection.

## Decisions (locked)

- **Ambition:** full alignment, guardrails before refactor.
- **Structure:** split the single 1854-line `bulk_post.py` into a `src/bulk_post/` package.
- **Exit semantics:** keep **exit 1 when any row fails** (useful for CI/scripting). Correct the `.claude/rules/cli.md` rule, which currently says the opposite — it was a generic default that does not fit this tool.
- **mypy:** pragmatic configuration to start (not `--strict`), tighten later.
- **Sequencing:** incremental — each step keeps the 102 tests green and is committed separately.

## Gap audit (baseline)

Already aligned: `main()` + Ctrl-C → 130, TTY detection, `Authorization` masking,
lazy `pyyaml`, partial type hints, 102 tests, committed `uv.lock`, `requires-python`.

Gaps addressed by this work:
1. No Ruff/mypy/formatter config (style not machine-checked).
2. No CI.
3. No `--version` flag.
4. `main()` returns `None`; ~12 scattered `sys.exit(1)` deep in the pipeline.
5. Arg parsing inline in `_run()` rather than a `build_parser()`.
6. Single 1854-line file (46 top-level units).
7. `BrokenPipeError` not handled.

---

## Phase 1 — Guardrails (non-behavioral)

Adds the safety net that protects Phase 2. No runtime behavior changes except
the two additive items (`--version`, `BrokenPipeError`).

- **Ruff** — `[tool.ruff]` in `pyproject.toml`: line length **88**, rule set `E, F, I, UP, B, SIM, C4`; `ruff format` for formatting. Run autofix once and commit the formatting diff on its own so it is isolated from logic changes.
- **mypy** — `[tool.mypy]`, pragmatic: `warn_unused_ignores`, `warn_return_any`, `warn_redundant_casts`; type the public API. Do **not** enable `disallow_untyped_defs`/`--strict` yet; leave a note to tighten in a later pass.
- **`--version`** — new flag printing the installed version via `importlib.metadata.version("bulk-post")`.
- **`BrokenPipeError`** — handled in `main()` alongside `KeyboardInterrupt` (exit silently / standard behavior when a downstream consumer closes the pipe).
- **CI** — `.github/workflows/ci.yml`: matrix Python **3.12 + 3.13**; steps install uv, `uv sync`, `ruff check`, `ruff format --check`, `mypy`, `uv run python -m unittest discover tests/`.
- **pre-commit** *(optional)* — `.pre-commit-config.yaml` with ruff + ruff-format hooks.

**Phase 1 acceptance:** `ruff check`, `ruff format --check`, `mypy`, and the full
test suite all pass locally and in CI; `bulk-post --version` prints the version.

---

## Phase 2 — Split into `src/bulk_post/` package

**Approach:** incremental extraction — move one cohesive module, run the 102
tests, commit; repeat. Never a big-bang rewrite.

### Backward-compatibility strategy (critical)

The package is named `bulk_post` and `__init__.py` **re-exports the public
names**. Tests use `import bulk_post; bulk_post.substitute(...)` and the entry
point is `bulk_post:main`; both keep working untouched if `__init__.py`
re-exports. This is what allows the refactor without rewriting the 102 tests.

**Requirement:** every name the test suite references as `bulk_post.<name>` must
remain importable from `bulk_post` after the split — the documented public API
(`substitute`, `http_request`, `count_csv_rows`, `resolve_token`,
`prompt_new_token`, `resolve_basic_creds`, `prompt_new_basic_creds`,
`resolve_auth_header`, `parse_workflow`, `_mask_headers`, `_get_suggestion`,
`_run`/`main`) **and** the internals the tests reach into — at least
`_ParallelState`, `_run_parallel_main_loop`, `_poll_cmd`, and the `_CMD_PAUSE` /
`_CMD_RESUME` / `_CMD_EXIT` constants. The authoritative check is that the
unchanged suite imports and passes; grep the tests for `bulk_post\.` during
planning to enumerate the full set.

### Module map (derived from the file's existing sections)

| Module | Contents |
|--------|----------|
| `templating.py` | `substitute`, `_validate_body_template`, `_validate_placeholders` |
| `http.py` | `http_request`, `_mask_headers` |
| `auth.py` | `resolve_token`, `prompt_new_token`, `resolve_basic_creds`, `prompt_new_basic_creds`, `resolve_auth_header`, `_make_auth_refresh_fn` |
| `csvio.py` | `count_csv_rows`, `_open_retry_writer`, `_open_log_file`, `_write_failure_log`, `_skip_rows` |
| `terminal.py` | `_BottomBar`, `_get_suggestion`, `_render_bar`, `print_progress`, `_out`, `_progress`, `_stdin_command`, `_wait_for_resume`, `_poll_cmd`, `print_verbose` |
| `state.py` | `_ParallelState`, `_WorkflowParallelState` |
| `workflow.py` | `WorkflowStep`, `parse_workflow`, `_validate_workflow_placeholders`, `_resolve_workflow_auth_headers`, `_fire_workflow_step` |
| `runner.py` | single-URL execution: `_fire`, `_log_row`, `_handle_cmd_in_loop`, `_run_loop`, `_parallel_worker`, `_run_parallel_main_loop`, `_run_parallel` |
| `workflow_runner.py` | workflow execution: `_run_workflow_loop`, `_make_workflow_auth_refresh_fns`, `_workflow_parallel_worker`, `_run_workflow_parallel` |
| `cli.py` | `build_parser()`, `main() -> int`, dispatch/validation, centralized exit codes |
| `__init__.py` | re-export public API + `main` |
| `__main__.py` | `from .cli import main; raise SystemExit(main())` — enables `python -m bulk_post` |

Module boundaries may be adjusted slightly during planning if a dependency
forces it (e.g. `print_verbose` lives in `terminal.py` to avoid `http`→`ui`
coupling); the public API surface above is fixed regardless.

### Behavioral changes folded into Phase 2

- `main() -> int`; entry point becomes `sys.exit(main())`.
- Replace the ~12 scattered `sys.exit(1)` calls with a return value that bubbles up to `main()`; validation/setup errors and "≥1 row failed" both yield exit code 1. argparse usage errors keep their native exit code 2.
- Correct `.claude/rules/cli.md` to state that row failures **do** produce a non-zero exit for this tool.

### Packaging & docs

- `pyproject.toml` switches to `src/` layout; the `bulk-post` console script is preserved.
- `python bulk_post.py` no longer works (file moves into the package). Replace with `python -m bulk_post` / `uv run bulk-post`. Update `README.md` and `CLAUDE.md` (run/install commands, the "single-file" framing, and the Public API surface note about package layout).
- `PYTHON_BEST_PRACTICES.md` stays local/untracked; only the `cli.md` exit-code rule changes.

**Phase 2 acceptance:** all 102 tests pass unchanged via `import bulk_post`;
`bulk-post`, `python -m bulk_post`, and `uv run bulk-post` all work; the
`/resume`-after-drain regression test still passes; Ruff + mypy clean.

---

## Risks & mitigations

- **Ruff autofix churn** — isolate the format-only diff in its own commit.
- **Thread/pause/resume logic moves modules** — the `/resume`-after-drain regression test guards it across the move; run tests after each extraction.
- **Hidden import cycles** when splitting — extract leaf modules first (templating, http, state), then higher-level runners, then cli last.
- **Lost direct-script invocation** — documented and replaced with `python -m bulk_post`.

## Out of scope

- Switching argument parser away from `argparse`.
- Switching test framework away from stdlib `unittest`.
- `mypy --strict` (deferred to a later tightening pass).
- Any change to the HTTP/auth/workflow feature behavior beyond exit-code centralization.
