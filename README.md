# bulk-post

[![CI](https://github.com/true-monte-kristo/bulk-post/actions/workflows/ci.yml/badge.svg)](https://github.com/true-monte-kristo/bulk-post/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Security: report a vulnerability](https://img.shields.io/badge/security-report%20a%20vulnerability-red.svg)](https://github.com/true-monte-kristo/bulk-post/security/advisories/new)

A near-stdlib Python CLI that fires templated HTTP requests driven by CSV data. You define the request — URL, method, body, headers — with `{{placeholder}}` slots, and each CSV row supplies the values that fill them: one request per row, or a multi-step request workflow per row in `--workflow` mode. Supports bearer or basic auth (default: no auth) with automatic 401 re-prompt, a live terminal UI with pause/resume, parallel execution, and a retry file for failed rows. Third-party dependencies: PyYAML and jsonpath-ng — both always installed, but lazily imported (PyYAML only on the `--workflow` code path, jsonpath-ng only on the workflow-variables code path).

## Requirements

- Python 3.12+
- [PyYAML](https://pypi.org/project/PyYAML/) — runtime dependency; lazily imported, only exercised in `--workflow` mode
- [jsonpath-ng](https://pypi.org/project/jsonpath-ng/) — runtime dependency; lazily imported, only exercised when a workflow declares variables

## Installation

### From PyPI

Install from [PyPI](https://pypi.org/project/bulk-post/):

```bash
uv tool install bulk-post   # or: pipx install bulk-post  /  pip install bulk-post
bulk-post --help
```

### With Homebrew

Install from the [tap](https://github.com/true-monte-kristo/homebrew-tap):

```bash
brew tap true-monte-kristo/tap
brew trust true-monte-kristo/tap   # Homebrew 6.x requires trusting third-party taps once
brew install bulk-post
bulk-post --help
```

### From source

Install globally from a checkout with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install .
uv tool install . --reinstall   # re-install after changing the code
```

Or run directly without installing (from the repo root):

```bash
python -m bulk_post --help          # ensure pyyaml and jsonpath-ng are installed (uv run handles this)
```

## Usage

```
bulk-post -u <url-template> -c <csv-file> [options]
```

### Examples

Cancel every invoice in a CSV using DELETE:

```bash
bulk-post \
  -u "https://api.example.com/invoices/{{id}}/cancel" \
  -c invoices.csv \
  -m DELETE
```

PATCH with a JSON body, 200 ms between requests, verbose output:

```bash
bulk-post \
  -u "https://api.example.com/invoices/{{id}}/status" \
  -c invoices.csv \
  -m PATCH \
  -b '{"status": "cancelled", "reason": "{{reason}}"}' \
  -d 200 \
  -v
```

POST form-encoded data:

```bash
bulk-post \
  -u "https://api.example.com/items" \
  -c items.csv \
  -m POST \
  -b "id={{id}}&status={{status}}" \
  -C "application/x-www-form-urlencoded"
```

Resume after a failure at row 47:

```bash
bulk-post -u "https://api.example.com/items/{{id}}" -c items.csv -o 47
```

## CLI flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--url` | `-u` | required* | URL template; `{{col}}` is replaced with the value from that CSV column. *Provide either `--url` or `--workflow` (mutually exclusive) |
| `--csv` | `-c` | required | Path to the input CSV file |
| `--method` | `-m` | `POST` | HTTP method (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, …) |
| `--body` | `-b` | — | Request body; supports `{{col}}` placeholders |
| `--content-type` | `-C` | `application/json` | `Content-Type` header sent with the request body; ignored when no body is provided. When set to a JSON or XML type, the body template is validated once at startup before any requests are sent — the script exits immediately with an error if the template is structurally invalid |
| `--auth-type` | `-a` | `none` | Auth method: `bearer`, `basic`, or `none` |
| `--token` | `-t` | — | Bearer token; used with `--auth-type bearer` (see [Auth](#auth) below) |
| `--user` | `-U` | — | Basic auth credentials as `user:pass`; used with `--auth-type basic` (see [Auth](#auth) below) |
| `--delay` | `-d` | `0` | Milliseconds to wait between requests |
| `--offset` | `-o` | `0` | Skip the first N data rows (useful for resuming after a failure) |
| `--timeout` | `-T` | `30` | Per-request timeout in seconds |
| `--retry-file` | `-r` | `<stem>_failed.csv` | Where to write rows that failed; auto-named from the CSV path if omitted |
| `--verbose` | `-v` | false | Print URL, request/response headers (Authorization masked), body, status, and timing for every row |
| `--header` | `-H` | — | Add a custom request header in `Name: value` format; repeatable. Values support `{{col}}` placeholders |
| `--parallel` | `-p` | false | Process rows concurrently using multiple threads; `--delay` is ignored in this mode |
| `--concurrency-level` | `-n` | CPU count | Number of worker threads; only used with `--parallel` |
| `--debug` | `-D` | false | Print worker thread name on each row log line and show a live debug bar with queue depth, active thread count, and ok/fail counters; only meaningful with `--parallel` |
| `--workflow` | `-w` | — | Path to a workflow YAML file; mutually exclusive with `--url` |
| `--version` | `-V` | — | Print version and exit |

## CSV format

The CSV must have a header row. Column names are used as placeholder names in `--url`, `--body`, and `--header` values. Every `{{placeholder}}` in the URL, body, or header values must match a column name; the script exits with an error if any are missing.

```csv
id,reason
1001,duplicate
1002,customer_request
```

## Auth

Select the auth method with `--auth-type` / `-a` (default: `none`):

### Bearer token

Pass `--auth-type bearer` (or `-a bearer`). Token resolution order: `--token` / `-t` flag → `BULK_TOKEN` env var → interactive prompt at startup.

If the server returns **401** mid-run, the script pauses, prompts for a fresh token, and retries the failed row automatically. This is handy when tokens are short-lived SSO bearer tokens that must be copied manually (e.g. from browser DevTools) and cannot be fetched programmatically.

### Basic auth

Pass `--auth-type basic` (or `-a basic`). Credentials (`user:pass`) are resolved in the same order: `--user` / `-U` flag → `BULK_USER` env var → interactive prompt. On **401**, the script prompts for new credentials and retries.

### No auth (default)

The default when `--auth-type` is omitted (or pass `--auth-type none` / `-a none` explicitly). No `Authorization` header is sent.

## Terminal UI

When running in an interactive terminal, a live bottom bar shows:

- **Progress bar** — `current / total` rows with a visual fill bar
- **Command input** — type a command and press Enter; Tab autocompletes

Available commands:

| Command | Effect |
|---------|--------|
| `/pause` | Pause sending; script waits until you resume |
| `/resume` | Resume after a pause |
| `/exit` | Stop after the current row and print a summary |

In non-TTY mode (piped input, CI, test environments) the bottom bar is skipped and no interactive commands are available.

## Retry file

Rows that fail (network error, non-2xx response, or substitution error) are written to the retry file. By default this is `<csv-stem>_failed.csv` next to the input file. Re-run with `-c <stem>_failed.csv` to retry only those rows.

If no rows fail, the retry file is deleted automatically.

## Workflow mode

Instead of a single `--url` template, you can define a multi-step workflow in a YAML file and run it with `--workflow` / `-w`.

Each CSV row fires all steps in document order. Steps within a row are always sequential; `--parallel` controls per-row concurrency across rows.

### Workflow YAML format

```yaml
workflow:
  description: Optional human-readable description  # skipped at runtime

  groupA:                          # logical grouping for shared auth
    auth:
      type: bearer                 # bearer | basic | none
      token: some_token            # optional — prompted if omitted
    endpoints:
      - step-name:                 # user-chosen name; unique within the group
          url: https://api.example.com/{{id}}
          method: POST             # default POST
          headers:
            Content-Type: application/json
            X-Custom: value
          body: '{"key": "{{col}}"}'
          on_error: stop           # stop (default) | continue
          auth:                    # step-level auth overrides group auth
            type: bearer
            token: override_token

  groupB:
    auth:
      type: basic
      user: alice
      password: secret
    endpoints:
      - another-step:
          url: https://other.example.com/{{id}}
          method: DELETE

  groupC:                          # no auth
    endpoints:
      - no-auth-step:
          url: https://public.example.com/{{id}}
          method: GET
```

Key rules:

- **Execution order** — steps fire in the order they appear in the document (top to bottom across all groups).
- **Group auth** — all steps in a group inherit the group's auth unless they declare their own.
- **`on_error`** — `stop` (default) halts remaining steps for that row and writes it to the retry file; `continue` logs the failure, writes the row, and proceeds to the next step.
- **Placeholders** — `{{col}}` in `url`, `body`, and header values is replaced with the matching CSV column value, same as in single-URL mode.

### Retry and resume

When a step fails, the row is written to the retry file with an extra column `_bulk_post_step` set to the path of the first failed step (e.g. `groupA/step-name`). Re-running with that retry CSV skips all steps before the failed one, resuming mid-workflow automatically.

### Workflow variables

A step can capture a value from an earlier step's JSON response and pass it as a `{{$name}}` placeholder into that step's `url`, `headers`, or `body`. This lets you chain steps — for example, create a resource in step 1 and use the returned `id` in the URL of step 2.

Variables are declared under a `variables:` key at group level (inherited by all endpoints in the group) and/or at endpoint level (overrides the group on name conflict):

```yaml
workflow:
  groupA:
    auth:
      type: bearer
    endpoints:
      - create-item:
          url: https://api.example.com/items
          method: POST
          body: '{"name": "{{name}}"}'

  groupB:
    endpoints:
      - delete-item:
          # Capture the id from create-item's response at endpoint level
          variables:
            $id:
              source: .workflow.groupA.create-item  # leading dot and "workflow." prefix are optional
              jsonPath: $.id                        # JSONPath; first match is used
              nullable: false                       # fail this step if id is missing
          url: https://api.example.com/items/{{$id}}
          method: DELETE
```

**Variable rules:**

- Names must start with `$` (e.g. `$id`) and are referenced as `{{$id}}` in URL, headers, and body.
- `source` is written as `.workflow.<group>.<endpoint>` (or just `<group>/<endpoint>`). It must refer to an endpoint that runs before the current step — forward and self references are rejected at startup.
- `jsonPath` uses full JSONPath syntax (powered by [`jsonpath-ng`](https://pypi.org/project/jsonpath-ng/)). Only the first match is used. A match that is an object or array (non-scalar) fails the step.
- `nullable` defaults to `true`. When `false`, a null value or no-match fails the step (row written to retry file); when `true`, it resolves to an empty string.
- Variable values are scoped to a single CSV row and are never shared across rows.
- All variable declarations are validated at startup — undefined references, bad names, unreachable sources, and invalid JSONPath expressions all cause an immediate exit with a clear error.

**Resume/retry with variables:**

When a row fails, any resolved variable values are persisted into reserved retry-CSV columns named `_bulk_post_var/<source_path>/<name>`. Re-running the retry CSV skips completed steps and reads these persisted values for variables whose source step was skipped.

> **Security note:** retry CSVs may contain response-derived data (potentially sensitive) in plaintext. Do not share or commit retry CSVs that were produced from workflows using variables.

### Example

```bash
bulk-post -w workflow.yaml -c rows.csv
```

## Running tests

```bash
uv run python -m unittest discover tests/
```

Tests use stdlib `unittest`. PyYAML and jsonpath-ng are runtime dependencies (always installed); the workflow-parsing cases use PyYAML and the workflow-variable cases use jsonpath-ng. `uv run` provides both from `uv.lock` automatically; if you run `python -m unittest` directly, do so inside a virtualenv that has both packages.
