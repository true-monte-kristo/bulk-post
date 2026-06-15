# Workflow response-chaining variables — design

Date: 2026-06-15
Status: approved (brainstorming) — pending spec review

## Overview

Add **variables** to `--workflow` mode so a later step can use data from an
earlier step's HTTP response within the same CSV row. A variable is declared at
**group** or **endpoint** level, captures a value from a previous step's response
via a **JSONPath** expression, and is referenced with `{{$name}}` anywhere a
`{{col}}` placeholder works today (URL, headers, body).

Variable lifetime is **one CSV row**: the response store is created fresh per row
and never outlives a single iteration. Because each row is processed start-to-finish
by a single worker thread (`_workflow_parallel_worker`) and steps within a row are
sequential, the per-row response store is **thread-confined** — no locking, no shared
mutable state.

## Locked decisions

1. **JSONPath engine:** full JSONPath via `jsonpath-ng`, imported **lazily** inside
   the workflow resolution path (mirrors the existing `pyyaml` pattern). Added with
   `uv add jsonpath-ng`; `uv.lock` committed in the same change.
2. **Resume correctness:** persist captured variable values into the retry CSV so a
   resumed row resolves correctly without re-firing side-effecting calls.
3. **Persist key:** reserved-prefix column `_bulk_post_var/<source_path>/<name>`
   (source path + variable name), collision-proof across scopes.
4. **Secrets-on-disk:** always persist (resume needs the real value; masking would
   break it); document that retry CSVs may contain response-derived data.
5. **Value rendering:** resolved scalar → plain text; `null`/no-match → empty string
   if `nullable`, else the step fails; object/array match → the step fails
   (non-scalar). Consistent with how `{{col}}` already drops a raw string in.

## YAML schema

A `variables:` mapping may appear at group level and/or endpoint level. Each entry
maps a `$`-prefixed name to a definition:

```yaml
variables:
  $id:
    source: .workflow.groupB.call-another-example-api  # see normalization below
    jsonPath: $.id                                      # full JSONPath expression
    nullable: false                                     # optional, default: true
```

- **Reference syntax:** `{{$name}}` in URL path, header values, or body.
- **Scope & override:** a step sees its **endpoint** variables overlaid on its
  **group** variables; on a name conflict the endpoint definition wins.
- **`source` normalization:** strip a leading `.`, strip an optional `workflow.`
  prefix, split the remainder on `.` into `group.endpoint`, then map to the internal
  step path `group/endpoint` (the same id used for `_bulk_post_step`). Group and
  endpoint names containing `.` are unsupported as a source target (documented).

## Data model

```python
@dataclasses.dataclass
class VarDef:
    name: str          # "$id"
    source_path: str   # normalized "groupB/call-another-example-api"
    json_path: str     # raw JSONPath expression (validated at parse time)
    nullable: bool      # default True

# WorkflowStep gains one field:
    variables: dict[str, VarDef]   # merged scope (group ∪ endpoint), endpoint wins
```

The merge happens at parse time so each `WorkflowStep` carries the fully-resolved
variable scope it needs; the runtime never re-reads group structure.

## Parsing & static validation (`parse_workflow`)

Parse `variables` blocks at both levels, merge into each step's `variables`, and
validate at startup (fail fast, before any HTTP request). Errors use the existing
`(steps, error_or_None)` return convention:

- Variable names must start with `$`.
- Every `{{$name}}` referenced by a step has a matching definition in scope.
- Each variable's `source` resolves to **exactly one** step. Ambiguous source
  (a duplicate endpoint name that the parser suffixed `_1`, `_2`, …) → error asking
  the user to rename.
- The source step is **earlier** in document order than every step that references
  the variable. Forward/self references are statically guaranteed to be null → error.
- `jsonPath` parses via `jsonpath_ng.parse(...)` → else error.
- `_validate_workflow_placeholders` needs **no change**: `{{$..}}` does not match the
  column regex `\{\{(\w+)\}\}` (because `$` is not `\w`), so variables are never
  mis-flagged as missing CSV columns.

Unused variable definitions are allowed (no error).

## Substitution

`{{$var}}` and `{{col}}` are **disjoint** token sets: the existing column regex
`PLACEHOLDER_RE = \{\{(\w+)\}\}` cannot match `{{$id}}` because `$` is not `\w`. The
public `substitute(template, row)` stays **unchanged** (single-URL mode keeps using it
as-is).

A new variable regex `VAR_RE = \{\{(\$\w+)\}\}` and a workflow-only variable pass are
added. Order, applied to each templated field (URL / header value / body):

1. Run the existing column substitution (`{{col}}`) first. `{{$var}}` tokens are
   disjoint and survive this pass untouched.
2. Replace each surviving `{{$name}}` with its rendered value (see Runtime
   resolution). The result is **not** re-scanned, so a variable value that itself
   contains `{{...}}`-looking text is never re-expanded.

A failed non-nullable variable returns the same `(value, error)` shape that
`substitute` already uses for a missing column, so the row is routed to the retry file
through the existing path with no new control flow.

## Runtime resolution

- Each row keeps a thread-confined `responses: dict[str, str]` mapping
  `step_path → raw response body`, populated after each step that produced an HTTP
  response (status not `None`).
- Before a step substitutes, resolve each in-scope variable:
  1. **Locate source body:** live `responses[source_path]` → else persisted value
     (see Resume) → else `None`.
  2. **Extract:** `json.loads(body)`, apply the compiled JSONPath, take the **first
     match**.
  3. **Render (raw string):**
     - scalar (str/int/float/bool) → plain text (`"abc"`, `42`, `true`).
     - no match / explicit JSON `null` / source body not valid JSON → empty string if
       `nullable=true`; otherwise the step **fails** with a clear message.
     - object/array match → the step **fails** ("variable `$x` resolved to a
       non-scalar").
- `_fire_workflow_step` gains a `responses` parameter (and the persisted-vars view)
  and performs variable resolution before its existing column substitution. Its return
  already includes `body`; the caller stores `responses[step.path] = body` after each
  fire. Both runners (`_run_workflow_loop`, `_workflow_parallel_worker`) create the
  per-row `responses` dict and thread it through their step loop.

## Resume & persistence

- On row failure, for every variable whose **source step completed this run**, write
  the resolved value into the reserved column `_bulk_post_var/<source_path>/<name>` of
  the retry CSV. These columns are stripped and re-derived on each pass, exactly like
  the existing `_bulk_post_step`.
- **Resolution order** on a resumed run: live response this run → persisted column →
  null. A resumed step whose source ran this session uses the live value; one whose
  source was skipped uses the persisted value.
- **Security note (documented):** retry CSVs may contain response-derived data
  (potentially sensitive). They should not be shared or committed. Masking is not
  applied because resume requires the real value.

## Dependency

`jsonpath-ng` is imported lazily inside the resolution code path. If unavailable the
workflow run returns a clean error mirroring the pyyaml message:

> jsonpath-ng is required for workflow variables. Install with: pip install jsonpath-ng

Non-workflow use and workflows without variables require no third-party package
beyond the existing lazy pyyaml.

## Error surfaces (summary)

- **Parse/validate (startup, exit 1 via `_CliError`/`(steps, err)`):** bad var name,
  undefined reference, ambiguous/forward/self source, unparseable jsonPath, missing
  dependency.
- **Runtime (per-step, routed to retry file like a substitution error):** non-nullable
  variable resolved to null/no-match/unparseable-source; non-scalar match.

## Public API surface changes

- `parse_workflow` unchanged signature; `WorkflowStep` gains `variables`.
- New: `VarDef` dataclass.
- A variable-resolution helper (e.g. `resolve_variables(step, responses) -> (dict, err)`)
  exposed for unit tests, re-exported from `__init__`.
- `CLAUDE.md` "Public API surface" updated for any new exported names.

## Testing

stdlib `unittest`, mirroring `TestParseWorkflow` (which already needs a third-party
parser). New cases:

- **Parse/validate:** scope merge + endpoint override; name missing `$`; undefined
  reference; forward reference; self reference; ambiguous source; invalid jsonPath;
  missing `jsonpath-ng`.
- **Resolution:** top-level scalar; nested path; `null` + `nullable=true` → empty;
  `null` + `nullable=false` → fail; non-scalar → fail; source body not JSON.
- **Resume:** persisted variable survives a re-run and resolves; resolution-order
  precedence (live beats persisted).
- Keep stdlib-only tests stdlib-only; variable tests may require `jsonpath-ng`
  (declared like the pyyaml requirement for workflow tests).

## Docs to update (same change)

- `CLAUDE.md`: Workflow mode section + Public API surface + dependency note.
- `README.md`: workflow schema (variables) + new dependency + retry-CSV security note.
- `workflow-example.yaml`: already demonstrates the feature; reconcile `source`/
  `jsonPath` field casing and the `nullable` default with this spec.

## Known limitations (v1)

- Multiple JSONPath matches: only the **first** is used.
- Group/endpoint names containing `.` cannot be used as a `source` target.
- Retry CSVs carry response-derived data in plaintext (documented).

## Out of scope (YAGNI)

- Variables sourcing from CSV columns (use `{{col}}`).
- Transformations/expressions on captured values beyond JSONPath extraction.
- Variables crossing CSV rows or persisting beyond a single run.
- Splicing non-scalar JSON sub-trees into bodies (errors in v1).
