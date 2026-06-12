# Testing & dependency rules

## Testing
- Framework is stdlib **`unittest`** (project standard) — do not introduce pytest without agreement.
- Run with: `uv run python -m unittest discover tests/`.
- Add tests under `tests/`, grouped in a `TestCase` per unit, mirroring the public API.
- Test **behavior and the public API**, not private implementation details. Patch the documented seams (e.g. `prompt_new_token`, `prompt_new_basic_creds`) rather than `input`/`sys.stdin` directly.
- **Regression-first:** for any bug, write a failing test that reproduces it *before* writing the fix, and keep that test.
- Keep tests runnable without a TTY (patch `sys.stdin.isatty`); the bottom bar must be skipped in tests.

## Dependencies
- Stdlib first. The only third-party runtime dep is `pyyaml`, imported **lazily** inside the workflow code path and required solely for `--workflow`. Preserve that laziness so non-workflow use and most tests need no third-party packages.
- A new third-party dependency needs explicit justification. Add it with `uv add <pkg>` and commit the updated `uv.lock` in the same change.
- This is an application → **commit `uv.lock`** for reproducible installs. Keep `requires-python` in `pyproject.toml` current.

## Keep docs in sync
- When flags, auth, workflow schema, or the public API surface change, update `CLAUDE.md` (flag table + Public API surface) and `README.md` in the same change.
