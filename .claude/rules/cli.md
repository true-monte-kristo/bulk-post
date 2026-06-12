# CLI design rules

Project-specific rules for `bulk_post.py`. This is a single-file CLI built on
stdlib `argparse`, exposed as `bulk-post` via `[project.scripts]` (`bulk_post:main`).

## Entry point & structure
- `main()` MUST return an `int` exit code; the entry point ends with `sys.exit(main())`.
- Keep argument parsing (the argparse setup) separate from logic. Parse in/near `main()`, then hand a parsed namespace to the pipeline (`_run` / the run helpers) so logic stays unit-testable without subprocesses.
- Stay dependency-light: prefer stdlib. Do not swap `argparse` for Click/Typer without explicit agreement.

## Exit codes
- `0` = success. Non-zero = failure. argparse exits `2` on a usage error.
- This tool exits **`1` when any row fails** (so CI/scripts can detect partial failures); individual failures are still written to the retry file. Setup/validation errors also exit `1` (raised internally as `_CliError`).

## Streams
- Normal/result output → **stdout**. Progress bars, diagnostics, errors, debug stats → **stderr**. This keeps `bulk-post ... | other` pipelines clean.

## Terminal behavior
- Gate all ANSI / `_BottomBar` / scroll-region behavior behind a real TTY check (`sys.stdout.isatty()` / `termios` availability). Non-TTY (pipes, CI, tests) must degrade to plain output with no interactive bar.
- Honor [`NO_COLOR`](https://no-color.org/): when set, emit no color escapes.
- Handle `KeyboardInterrupt` (Ctrl-C) cleanly → exit `130`, no raw traceback.
- Handle `BrokenPipeError` (downstream closed, e.g. `| head`) silently.

## Config precedence
- Resolve every configurable value in this order: **CLI flag → environment variable → interactive prompt (TTY only) → default.** This matches the existing token/credential resolution (`resolve_token`, `resolve_basic_creds`); follow it for any new config.

## Secrets
- NEVER print tokens, passwords, or credentials. Mask `Authorization` and any sensitive header in verbose output and in the failure log — reuse `_mask_headers`. New code paths that log headers must run them through masking first.

## Adding a flag
- Give every flag a long and a short form, with a sensible default.
- When a flag (or behavior) changes, update **both** the flag table in `CLAUDE.md` and the CLI flags table in `README.md` in the same change.
