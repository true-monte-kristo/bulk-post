# Releasing

Releases are automated with
[release-please](https://github.com/googleapis/release-please). You normally do
**not** edit the version, tag anything, or touch PyPI/Homebrew by hand.

## Day-to-day

1. Open a feature PR with a **Conventional Commit title** (the squash-merge uses
   it as the commit message):
   - `fix: …` → patch bump (`0.1.1` → `0.1.2`)
   - `feat: …` → minor bump (`0.1.1` → `0.2.0`)
   - `feat!: …` or a `BREAKING CHANGE:` footer → minor bump while pre-1.0
     (SemVer treats 0.x specially; see "Cutting 1.0.0" below)
   - `chore:` / `docs:` / `ci:` / `style:` / `refactor:` / `test:` → no release on
     their own, but they still show up in the changelog when a release happens
   The `Lint PR title` check enforces this format.
2. Merge the PR to `master`.
3. release-please opens (or updates) a **release PR** titled like
   `chore(main): release 0.2.0`. It accumulates every merged change into
   `CHANGELOG.md` and bumps `pyproject.toml` (and `uv.lock`). Leave it open and
   keep merging features — it keeps itself current.

## Cutting a release

**Merge the release PR.** That is the whole release. It triggers:

1. release-please creates the git tag `vX.Y.Z` and the GitHub Release.
2. `publish.yml` builds and uploads to **PyPI** (Trusted Publishing / OIDC).
3. The `homebrew` job bumps the **tap** (`true-monte-kristo/homebrew-tap`)
   — url, sha256, and the version test — after PyPI has the sdist.

Then: `brew update && brew upgrade bulk-post`, or `pip install -U bulk-post`.

## One-time setup (required for the automation to work)

- **`RELEASE_TOKEN` secret** (repo → Settings → Secrets → Actions). A
  fine-grained PAT (or GitHub App token) with:
  - this repo: **Contents: read/write**, **Pull requests: read/write**
  - `homebrew-tap`: **Contents: read/write**

  It is used by release-please (so its PR triggers CI and its Release triggers
  publishing — the default `GITHUB_TOKEN` cannot do either) and by the Homebrew
  bump job (cross-repo push).
- **Squash-merge** should be the merge method (Settings → General → Pull
  Requests), so the PR title becomes the commit message release-please reads.
- Branch protection on `master` stays on; releases flow through the release PR,
  so nothing pushes to `master` directly.

## Cutting 1.0.0

While pre-1.0, breaking changes bump the minor version. To graduate to `1.0.0`,
merge a PR (or commit) with a `Release-As: 1.0.0` footer in its message.

## Manual fallbacks

- **Dependencies changed** in a release: the formula's `resource` blocks are not
  auto-regenerated. Run `brew update-python-resources Formula/bulk-post.rb` in
  the tap and commit.
- **Re-publish / repair**: `publish.yml` uses `skip-existing: true`, so re-runs
  are safe (already-uploaded versions are skipped, not errored).
