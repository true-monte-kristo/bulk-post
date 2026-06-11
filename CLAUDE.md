# bulk-post

Near-stdlib Python CLI (`bulk_post.py`) that fires HTTP requests for each row in a CSV file. Only external dependency is `pyyaml`, lazily imported and required solely for `--workflow` mode.

## Run & install

Managed with [uv](https://docs.astral.sh/uv/).

```bash
python bulk_post.py -u "https://api.example.com/{{id}}/cancel" -c rows.csv -m DELETE
uv tool install .              # install `bulk-post` CLI globally
uv tool install . --reinstall  # re-install after code changes
```

## Tests

```bash
uv run python -m unittest discover tests/   # or plain `python -m unittest discover tests/` inside .venv
```

Test suite needs `pyyaml` (the `TestParseWorkflow` cases load real workflow YAML). `uv run` provides it from `uv.lock`; the non-workflow tests run on stdlib alone.

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

## Auth design

Three auth types via `--auth-type` / `-a` (default `none`):

- **bearer** — `Authorization: Bearer <token>`. Token resolved: `--token` flag → `BULK_TOKEN` env → interactive prompt. On 401, pauses and prompts for a fresh token, then retries.
- **basic** — `Authorization: Basic <base64>`. Credentials resolved: `--user` flag → `BULK_USER` env → interactive prompt. On 401, prompts for new credentials, then retries.
- **none** — no `Authorization` header sent.

Tokens/credentials are Keycloak SSO values obtained from browser DevTools and cannot be fetched programmatically.

## Workflow mode

`--workflow <yaml>` replaces `--url` with a multi-step workflow. Each CSV row fires all steps in document order; steps within a row are sequential, while `--parallel` runs rows concurrently. Steps are grouped for shared auth; each step has `url`/`method`/`headers`/`body`/`on_error` (`stop` default | `continue`) and may override group auth. On failure the row is written to the retry file with a `_bulk_post_step` column (`group/step`); re-running that CSV resumes mid-workflow by skipping completed steps. See `README.md` and `workflow-example.yaml` for the full schema. The parallel path shares `_run_parallel_main_loop` with single-URL mode, so pause/resume/exit behave identically.

## Terminal UI

When running in a real TTY (`termios` available), `_BottomBar` reserves the bottom two rows via an ANSI scroll region: a live progress bar on row `h-1` and a command input on row `h`. With `--debug --parallel`, a third reserved row `h-2` shows live queue depth, active thread count, and ok/fail counters (updated every 0.5 s). In non-TTY debug mode the same stats are printed to stderr. Commands: `/pause`, `/resume`, `/exit` (Tab for autocomplete). In non-TTY mode (pipes, tests) the bar is skipped entirely.

## Public API surface (for tests)

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
- `_run()` — full pipeline; patch `sys.argv` + `sys.stdin.isatty` to test
