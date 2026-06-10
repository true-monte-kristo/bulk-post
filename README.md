# bulk-post

A zero-dependency Python CLI that reads a CSV file and fires one HTTP request per row. Supports Bearer token auth with automatic 401 re-prompt, a live terminal UI with pause/resume, and a retry file for failed rows.

## Requirements

- Python 3.12+
- No external packages — stdlib only

## Installation

Install globally with [pipx](https://pipx.pypa.io/):

```bash
pipx install .
bulk-post --help
```

Or run directly without installing:

```bash
python bulk_post.py --help
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
| `--url` | `-u` | required | URL template; `{{col}}` is replaced with the value from that CSV column |
| `--csv` | `-c` | required | Path to the input CSV file |
| `--method` | `-m` | `POST` | HTTP method (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, …) |
| `--body` | `-b` | — | Request body; supports `{{col}}` placeholders |
| `--content-type` | `-C` | `application/json` | `Content-Type` header sent with the request body; ignored when no body is provided. When set to a JSON or XML type, the body template is validated once at startup before any requests are sent — the script exits immediately with an error if the template is structurally invalid |
| `--token` | `-t` | — | Bearer token (see [Token](#token) below) |
| `--delay` | `-d` | `0` | Milliseconds to wait between requests |
| `--offset` | `-o` | `0` | Skip the first N data rows (useful for resuming after a failure) |
| `--timeout` | `-T` | `30` | Per-request timeout in seconds |
| `--retry-file` | `-r` | `<stem>_failed.csv` | Where to write rows that failed; auto-named from the CSV path if omitted |
| `--content-type` | `-C` | `application/json` | `Content-Type` header for the request body |
| `--verbose` | `-v` | false | Print URL, status, response body, and timing for every row |

## CSV format

The CSV must have a header row. Column names are used as placeholder names in `--url` and `--body`. Every `{{placeholder}}` in the URL or body must match a column name; the script exits with an error if any are missing.

```csv
id,reason
1001,duplicate
1002,customer_request
```

## Token

Bearer token resolution order:

1. `--token` / `-t` flag
2. `BULK_TOKEN` environment variable
3. Interactive prompt at startup

If the server returns **401** mid-run, the script pauses, prompts for a fresh token, and retries the failed row automatically. Tokens are Keycloak SSO tokens obtained from browser DevTools and cannot be fetched programmatically.

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

## Running tests

```bash
python -m unittest discover tests/
```

All tests use stdlib `unittest` only — no extra packages required.
