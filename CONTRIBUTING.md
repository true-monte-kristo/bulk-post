# Contributing to bulk-post

Thanks for your interest in improving `bulk-post`! This is a small, dependency-light
Python CLI, and contributions that keep it that way are very welcome.

## Development setup

The project is managed with [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/true-monte-kristo/bulk-post
cd bulk-post
uv sync --all-extras --dev   # create the .venv and install dev tools
uv run bulk-post --help      # run the CLI without installing it globally
```

## Before you open a pull request

Run the same checks CI does — all four must pass:

```bash
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy src                  # type-check
uv run python -m unittest discover tests/   # tests
```

Installing the pre-commit hooks runs ruff (lint + format) automatically on commit:

```bash
uv run pre-commit install
```

## Conventions

- **Stdlib first.** The only runtime dependencies are `pyyaml` and `jsonpath-ng`, both
  lazily imported and exercised only on the `--workflow` code path. A plain single-URL
  run must import neither. A new third-party dependency needs explicit justification in
  the PR; add it with `uv add <pkg>` and commit the updated `uv.lock` in the same change.
- **Tests are `unittest`** (project standard, no pytest). Add tests under `tests/`,
  test behavior and the public API rather than private internals, and patch the
  documented seams (e.g. `prompt_new_token`) rather than `input`/`sys.stdin`.
- **Regression-first:** for any bug, add a failing test that reproduces it before the fix.
- **Code style:** `ruff format`, line length 88, 4-space indent, f-strings, `pathlib`,
  no mutable default arguments. Annotate public functions. Follow PEP 8 naming.
- **Keep docs in sync:** when flags, auth, the workflow schema, or the public API change,
  update both `CLAUDE.md` (flag table + Public API surface) and `README.md` in the same PR.
- **Commits** follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:`).

See `CLAUDE.md` and `.claude/rules/` for the full, prescriptive project rules.

## Reporting bugs and requesting features

Open an issue at https://github.com/true-monte-kristo/bulk-post/issues. For bugs, include
the command you ran (with any token/credential redacted), the CSV/workflow shape, and the
observed vs. expected behavior.

## Security

Please do not file security issues as public GitHub issues — see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
