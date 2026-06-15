# bulk-post

Near-stdlib Python package (`src/bulk_post/`) that fires templated HTTP requests: the request (URL/method/body/headers) carries `{{placeholder}}` slots filled from each CSV row — one request per row, or a multi-step workflow per row in `--workflow` mode. Entry point is `bulk_post.cli:main`, exposed as the `bulk-post` console script. The code is split across submodules: `cli`, `http`, `auth`, `templating`, `csvio`, `terminal`, `state`, `workflow`, `runner`, `workflow_runner`; `__init__` re-exports all public names. Runtime dependencies (always installed): `pyyaml`, lazily imported — only exercised on the `--workflow` code path; `jsonpath-ng`, lazily imported — only exercised on the workflow-variables code path.

## Run & install

Managed with [uv](https://docs.astral.sh/uv/).

```bash
python -m bulk_post -u "https://api.example.com/{{id}}/cancel" -c rows.csv -m DELETE
uv tool install .              # install `bulk-post` CLI globally
uv tool install . --reinstall  # re-install after code changes
uv run bulk-post --help        # run without installing (uv handles the .venv)
```

## Tests

```bash
uv run python -m unittest discover tests/   # or plain `python -m unittest discover tests/` inside .venv
```

Test suite needs `pyyaml` (the `TestParseWorkflow` cases load real workflow YAML) and `jsonpath-ng` (the workflow variable tests use it). Both are runtime dependencies always present in the environment; `uv run` provides them from `uv.lock`. Tests that exercise neither workflow path use stdlib alone.

## Code style & conventions

- Format with `ruff format` (or Black); lint with [Ruff](https://docs.astral.sh/ruff/); type-check with mypy/pyright. Annotate public functions.
- Line length 88, 4-space indent. Imports grouped stdlib → third-party → local; no wildcard imports.
- Prefer f-strings and `pathlib`; never use mutable default arguments.

### Naming (PEP 8)

| Kind | Convention | Example |
|------|------------|---------|
| Module / file | `lower_snake_case.py` | `cli.py` |
| Class / Exception | `PascalCase` | `_ParallelState` |
| Function / variable | `lower_snake_case` | `resolve_token` |
| Constant | `UPPER_SNAKE_CASE` | `_CMD_PAUSE` |
| Non-public | leading underscore | `_poll_cmd`, `_BottomBar` |

- Booleans read as predicates: `is_paused`, `has_token`. Avoid ambiguous names `l`, `O`, `I`.

## Key flags

| Flag | Short | Default | Notes |
|------|-------|---------|-------|
| `--url` | `-u` | required* | `{{col}}` placeholders from CSV; *mutually exclusive with `--workflow` |
| `--workflow` | `-w` | — | path to a workflow YAML (multi-step mode); mutually exclusive with `--url` |
| `--csv` | `-c` | required | input CSV |
| `--method` | `-m` | POST | HTTP method |
| `--body` | `-b` | — | request body; supports `{{col}}` |
| `--content-type` | `-C` | `application/json` | Content-Type for the body; JSON/XML templates validated at startup |
| `--auth-type` | `-a` | `none` | auth method: `bearer`, `basic`, or `none` |
| `--token` | `-t` | — | Bearer token; falls back to `BULK_TOKEN` env, then interactive prompt |
| `--user` | `-U` | — | Basic auth `user:pass`; falls back to `BULK_USER` env, then interactive prompt |
| `--delay` | `-d` | 0 | ms between requests |
| `--offset` | `-o` | 0 | skip first N data rows (resume after failure) |
| `--timeout` | `-T` | 30 | request timeout in seconds |
| `--retry-file` | `-r` | `<stem>_failed.csv` | path for failed-rows CSV |
| `--verbose` | `-v` | false | print req/resp headers (Authorization masked), body, status, timing per row |
| `--header` | `-H` | — | Add a custom request header as `Name: value`; repeatable. Values support `{{col}}` placeholders. |
| `--parallel` | `-p` | false | run rows concurrently using multiple threads; `--delay` ignored |
| `--concurrency-level` | `-n` | CPU count | worker thread count; only used with `--parallel` |
| `--debug` | `-D` | false | print thread name on each row and show a live debug bar (queue depth, active threads, ok/fail counts); only meaningful with `--parallel` |
| `--version` | `-V` | — | print version and exit |

## Auth design

Three auth types via `--auth-type` / `-a` (default `none`):

- **bearer** — `Authorization: Bearer <token>`. Token resolved: `--token` flag → `BULK_TOKEN` env → interactive prompt. On 401, pauses and prompts for a fresh token, then retries.
- **basic** — `Authorization: Basic <base64>`. Credentials resolved: `--user` flag → `BULK_USER` env → interactive prompt. On 401, prompts for new credentials, then retries.
- **none** — no `Authorization` header sent.

Tokens/credentials are Keycloak SSO values obtained from browser DevTools and cannot be fetched programmatically.

## Workflow mode

`--workflow <yaml>` replaces `--url` with a multi-step workflow. Each CSV row fires all steps in document order; steps within a row are sequential, while `--parallel` runs rows concurrently. Steps are grouped for shared auth; each step has `url`/`method`/`headers`/`body`/`on_error` (`stop` default | `continue`) and may override group auth. On failure the row is written to the retry file with a `_bulk_post_step` column (`group/step`); re-running that CSV resumes mid-workflow by skipping completed steps. See `README.md` and `workflow-example.yaml` for the full schema. The parallel path shares `_run_parallel_main_loop` with single-URL mode, so pause/resume/exit behave identically.

### Workflow response-chaining variables

A step can capture a value from an earlier step's JSON response and reuse it as a `{{$name}}` placeholder in that step's `url`, `headers`, or `body`.

**Declaration** — variables are declared under a `variables:` mapping at group level and/or endpoint level. Endpoint variables override group variables on name conflict. Each variable entry maps a name to a definition:

```yaml
variables:
  $id:
    source: .workflow.groupA.step-name   # leading dot and "workflow." prefix are optional
    jsonPath: $.data.id                  # full JSONPath; first match is used
    nullable: false                      # default true; false → step fails on null/no-match
```

- **`source`** — identifies the earlier endpoint whose JSON response to read, written as `.workflow.<group>.<endpoint>` (or just `<group>/<endpoint>`). The source must resolve to exactly one endpoint that runs before the referencing step; forward and self references are rejected at startup.
- **`jsonPath`** — a full JSONPath expression powered by `jsonpath-ng` (e.g. `$.id`, `$.items[0].id`). The first match is used.
- **`nullable`** — defaults to `true`. If the value is null or the path has no match and `nullable: false`, the step fails (row routed to the retry file). If `nullable: true`, it resolves to an empty string. A match that is an object or array (non-scalar) always fails the step.
- Variable names must start with `$` (e.g. `$id`), and are referenced as `{{$id}}` in URL, headers, and body.

**Lifetime** — variable values are scoped to a single CSV row. The response store is created fresh per row and never crosses rows; with `--parallel` each row is processed by a single worker, so variables are thread-confined.

**Validation** — happens at startup before any HTTP requests: undefined `{{$name}}` references, malformed names, sources that don't resolve to an earlier endpoint, and unparseable JSONPath expressions all produce a clear error and exit `1`.

**Resume/retry** — on row failure, resolved variable values are persisted into reserved retry-CSV columns named `_bulk_post_var/<source_path>/<name>`. Re-running that retry CSV skips completed steps and reads persisted values for variables whose source step was skipped. **Security note:** retry CSVs may contain response-derived data (potentially sensitive) in plaintext and should not be shared or committed.

**Dependency** — `jsonpath-ng` is a runtime dependency (always installed), lazily imported only on the workflow-variables code path. A plain single-URL run imports neither `pyyaml` nor `jsonpath-ng`, but both are present in the environment.

## Terminal UI

When running in a real TTY (`termios` available), `_BottomBar` reserves the bottom two rows via an ANSI scroll region: a live progress bar on row `h-1` and a command input on row `h`. With `--debug --parallel`, a third reserved row `h-2` shows live queue depth, active thread count, and ok/fail counters (updated every 0.5 s). In non-TTY debug mode the same stats are printed to stderr. Commands: `/pause`, `/resume`, `/exit` (Tab for autocomplete). In non-TTY mode (pipes, tests) the bar is skipped entirely.

## Public API surface (for tests)

All names below are importable directly from `bulk_post` (e.g. `from bulk_post import substitute`) — `__init__.py` re-exports everything from the submodules.

- `main()` / `_run()` → `int` exit code: `0` all rows succeeded; `1` any row failed or setup/validation error (`_CliError`); argparse usage errors → `2`
- `build_parser()` → `argparse.ArgumentParser` — construct the CLI parser without running it
- `_get_suggestion(buf)` — completion suffix for partial `/command`
- `substitute(template, row)` → `(str, err_or_None)` — `{{var}}` substitution
- `count_csv_rows(path)` → `int` — data rows excluding header; 0 on error
- `parse_workflow(yaml_path)` → `(steps, err_or_None)` — load/validate a workflow YAML into an ordered step list (lazily imports `pyyaml`)
- `resolve_token(flag_value, suspend=None, resume=None)` → `str` — bearer token from flag → `BULK_TOKEN` env → prompt
- `prompt_new_token(suspend=None, resume=None)` → `str` — patch this in tests, not `input`
- `resolve_basic_creds(flag_value, suspend=None, resume=None)` → `str` — `user:pass` from flag → `BULK_USER` env → prompt
- `prompt_new_basic_creds(suspend=None, resume=None)` → `str` — patch this in tests, not `input`
- `resolve_auth_header(args, suspend=None, resume=None)` → `Optional[str]` — returns full `Authorization` header value or `None` for auth_type `none`
- `http_request(url, auth_header, method, body, timeout=30, content_type="application/json")` → `(status_or_None, body, elapsed_s, req_headers, resp_headers)`
- `_mask_headers(headers)` → `dict` — replaces `Authorization` values with `*****`
- `VarDef` — dataclass: `name`, `source_path`, `json_path`, `nullable`; represents a single declared workflow variable
- `resolve_variables(step, responses, row)` → `(dict, err_or_None)` — resolve all variables for a step from the response store and/or persisted retry-CSV columns
- `persist_vars(steps, responses)` → `dict` — build the `_bulk_post_var/…` retry-CSV columns from the current row's response store
- `substitute_vars(template, var_values)` → `(str, err_or_None)` — replace `{{$name}}` placeholders with resolved variable values
- `render_template(template, row, var_values)` → `(str, err_or_None)` — full rendering: CSV column substitution then variable substitution
- `workflow_var_columns(steps)` → `list` — return the list of `_bulk_post_var/…` column names expected in a retry CSV for the given step list
- `validate_jsonpath(expr)` → `Optional[str]` — return an error string if `expr` is not a valid JSONPath, else `None` (lazily imports `jsonpath-ng`)

## Project rules

Detailed, prescriptive rules for building features (auto-imported into context):

@.claude/rules/cli.md
@.claude/rules/error-handling.md
@.claude/rules/testing-and-deps.md
