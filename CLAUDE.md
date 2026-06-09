# bulk-post

Pure-stdlib Python CLI (`bulk_post.py`) that fires HTTP requests for each row in a CSV file.

## Run & install

```bash
python bulk_post.py -u "https://api.example.com/{{id}}/cancel" -c rows.csv -m DELETE
pipx install .          # installs `bulk-post` CLI globally
```

## Tests

```bash
python -m unittest discover tests/
```

No external dependencies — stdlib `unittest` only.

## Key flags

| Flag | Short | Default | Notes |
|------|-------|---------|-------|
| `--url` | `-u` | required | `{{col}}` placeholders from CSV |
| `--csv` | `-c` | required | input CSV |
| `--method` | `-m` | POST | HTTP method |
| `--body` | `-b` | — | request body; supports `{{col}}` |
| `--token` | `-t` | — | Bearer token; falls back to `BULK_TOKEN` env, then interactive prompt |
| `--delay` | `-d` | 0 | ms between requests |
| `--offset` | `-o` | 0 | skip first N data rows (resume after failure) |
| `--timeout` | `-T` | 30 | request timeout in seconds |
| `--retry-file` | `-r` | `<stem>_failed.csv` | path for failed-rows CSV |
| `--verbose` | `-v` | false | print request/response/timing per row |

## Token design

Token is a Keycloak SSO token obtained from browser DevTools — cannot be fetched programmatically. On 401, the script pauses and prompts for a fresh token, then retries the failed row.

## Terminal UI

When running in a real TTY (`termios` available), `_BottomBar` reserves the bottom two rows via an ANSI scroll region: a live progress bar on row `h-1` and a command input on row `h`. Commands: `/pause`, `/resume`, `/exit` (Tab for autocomplete). In non-TTY mode (pipes, tests) the bar is skipped entirely.

## Public API surface (for tests)

- `_get_suggestion(buf)` — completion suffix for partial `/command`
- `substitute(template, row)` → `(str, err_or_None)` — `{{var}}` substitution
- `count_csv_rows(path)` → `int` — data rows excluding header; 0 on error
- `resolve_token(flag_value, bar=None)` → `str`
- `http_request(url, token, method, body, timeout=30)` → `(status_or_None, body, elapsed_s)`
- `prompt_new_token(bar=None)` → `str` — patch this in tests, not `input`
- `_run()` — full pipeline; patch `sys.argv` + `sys.stdin.isatty` to test
