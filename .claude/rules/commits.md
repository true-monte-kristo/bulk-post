# Commit & PR conventions

Releases are automated with release-please (see `RELEASING.md`). It derives the
next version from commit history, and PRs are **squash-merged using the PR title**
as the commit subject — so the **PR title MUST be a valid Conventional Commit**.
The `Lint PR title` check enforces this and is a required status check on `master`.

## Format

`type(optional-scope): description` — e.g. `fix(runner): account for --offset in parallel progress bar`.

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
`build`, `ci`, `chore`, `revert`.

## Version impact (only feat/fix/breaking trigger a release)

| Type | Effect |
|------|--------|
| `feat:` | minor bump (pre-1.0: still minor) |
| `fix:` | patch bump |
| `feat!:` or a `BREAKING CHANGE:` footer | breaking (minor while pre-1.0) |
| `docs` / `style` / `refactor` / `perf` / `test` / `build` / `ci` / `chore` | merges, but no version bump |

Pick `feat`/`fix` only when behavior or the public API changes; use the specific
type otherwise (`ci` for workflows, `build` for packaging, `docs` for docs,
`test` for tests, `chore` for dependency/maintenance work).

## When opening a PR

- The `gh pr create` **title** MUST follow the format above (it becomes the
  squash commit subject release-please parses).
- Squash merge uses "PR title and description", so the PR **body** becomes the
  commit body — keep it clean, and put any `BREAKING CHANGE:` / `Release-As: x.y.z`
  footers there.
- Never bump the version or create tags/releases by hand; merging the
  release-please PR does that. See `RELEASING.md`.
