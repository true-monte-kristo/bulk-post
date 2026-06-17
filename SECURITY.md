# Security Policy

## Supported versions

`bulk-post` is at an early stage (`0.x`). Security fixes are applied to the latest
release on the `master` branch only.

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Instead, report them privately via one of:

- GitHub's [private vulnerability reporting](https://github.com/true-monte-kristo/bulk-post/security/advisories/new)
  (Security → Report a vulnerability), or
- email to true.monte.kristo@gmail.com.

Please include a description of the issue, steps to reproduce, and the impact you
foresee. We aim to acknowledge reports within a few days and will keep you updated on
remediation progress. Please give us a reasonable window to release a fix before any
public disclosure.

## Handling credentials and sensitive data

`bulk-post` sends authenticated HTTP requests, so a few operational notes matter for
secure use:

- **Tokens and credentials are never printed.** Verbose output and the failure log mask
  the `Authorization` header. Avoid passing secrets on the command line where they may be
  captured in shell history — prefer the `BULK_TOKEN` / `BULK_USER` environment variables
  or the interactive prompt.
- **Retry CSVs may contain sensitive data.** In `--workflow` mode, response-chaining
  variables are persisted into the retry CSV (the `_bulk_post_var/…` columns) in
  plaintext. These files can contain response-derived data and **should not be shared,
  committed, or left on shared machines.** Delete them once a run has been successfully
  resumed.
- **TLS verification** follows Python's defaults; do not disable certificate validation
  when targeting production systems.
