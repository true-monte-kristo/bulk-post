# Error handling & logging rules

## Exceptions
- Catch **specific** exceptions (`urllib.error.HTTPError`, `OSError`, `yaml.YAMLError`, …). Use a broad `except Exception` only at the top-level boundary that converts a failure into an exit code or a retry-file entry.
- Never use a bare `except:`.
- Fail loudly and early. Don't swallow errors or continue on a state you can't handle.

## The `(value, err)` convention
- This codebase signals recoverable/expected failures with a `(result, err_or_None)` tuple — see `substitute(...) -> (str, err)` and `parse_workflow(...) -> (steps, err)`. Follow that pattern for parse/validation-style helpers instead of raising, so callers can route the row to the retry file.
- Reserve raised exceptions for truly exceptional / programmer-error conditions.

## Logging vs output
- User-facing results are printed (stdout). For diagnostics, prefer levels driven by `--verbose` rather than scattered unconditional `print()`s. Keep diagnostic/log output on stderr.

## Resources & footguns
- Use context managers (`with open(...) as f:`, locks, sockets) so cleanup is guaranteed even on error.
- No mutable default arguments (`def f(x=[])` / `={}`). Default to `None` and create the container inside.
- Use `pathlib.Path` for filesystem paths rather than manual string joining.
