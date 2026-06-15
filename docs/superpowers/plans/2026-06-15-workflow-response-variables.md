# Workflow Response-Chaining Variables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workflow step use data captured from an earlier step's HTTP response within the same CSV row, via `{{$var}}` placeholders backed by JSONPath.

**Architecture:** Variables are declared at group/endpoint level and merged onto each `WorkflowStep` at parse time. At runtime each row keeps a thread-confined `responses` dict (`step_path → raw body`); before a step renders, its in-scope variables are resolved (JSONPath extract → scalar render) and substituted as a second pass after column substitution. On row failure, resolved values are persisted into reserved retry-CSV columns so resume stays correct without re-firing side-effecting calls.

**Tech Stack:** Python 3.12 stdlib, `pyyaml` (existing, lazy), new `jsonpath-ng` (lazy, workflow-variables path only). Tests: stdlib `unittest`. Managed with `uv`.

**Repo note:** this directory is **not** a git repository (`git: false`). Either run `git init` once before starting, or skip the `git commit` step in each task. All other steps stand alone.

---

## File Structure

- **Create** `src/bulk_post/variables.py` — `VarDef`, JSONPath compile/extract, scalar render, `resolve_variables`, `persist_vars`, `_var_col`, `_WORKFLOW_VAR_PREFIX`, `validate_jsonpath`. Imports nothing from `workflow`/`templating` (takes primitives) to avoid import cycles.
- **Modify** `src/bulk_post/templating.py` — add `VAR_RE`, `substitute_vars`, `render_template`. `substitute` stays unchanged.
- **Modify** `src/bulk_post/workflow.py` — parse `variables` blocks (group + endpoint), normalize `source`, merge scope onto `WorkflowStep.variables`, validate (names/refs/source/order/jsonpath), add `workflow_var_columns`, thread `responses` through `_fire_workflow_step`.
- **Modify** `src/bulk_post/workflow_runner.py` — per-row `responses` dict in both runners; store bodies; persist vars on failure.
- **Modify** `src/bulk_post/cli.py` — build `retry_fieldnames` including var columns; strip pre-existing var columns from base fields.
- **Modify** `src/bulk_post/__init__.py` — re-export new public names.
- **Modify** `tests/test_bulk_post.py` — new `TestCase`s.
- **Modify** `CLAUDE.md`, `README.md`, `workflow-example.yaml` — docs/schema.

Dependency direction: `cli → workflow → {variables, templating}`; `templating → (nothing new)`; `variables → (stdlib + lazy jsonpath_ng)`. No cycles.

---

## Task 1: Add the jsonpath-ng dependency

**Files:**
- Modify: `pyproject.toml:5`
- Modify: `uv.lock` (generated)

- [ ] **Step 1: Add the dependency with uv**

Run:
```bash
uv add jsonpath-ng
```
Expected: `pyproject.toml` `dependencies` becomes `["pyyaml", "jsonpath-ng"]` and `uv.lock` updates.

- [ ] **Step 2: Verify it imports**

Run:
```bash
uv run python -c "import jsonpath_ng; print(jsonpath_ng.parse('$.a.b'))"
```
Expected: prints a parsed-path repr, no error.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add jsonpath-ng for workflow variables"
```

---

## Task 2: JSONPath extraction + scalar rendering (`variables.py` core)

**Files:**
- Create: `src/bulk_post/variables.py`
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bulk_post.py`:
```python
class TestVariableExtraction(unittest.TestCase):
    def _extract(self, body, expr):
        from bulk_post.variables import _compile_jsonpath, _extract

        return _extract(body, _compile_jsonpath(expr))

    def test_top_level_scalar(self):
        self.assertEqual(self._extract('{"id": 42}', "$.id"), 42)

    def test_nested_scalar(self):
        self.assertEqual(self._extract('{"a": {"b": "x"}}', "$.a.b"), "x")

    def test_no_match_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract('{"id": 1}', "$.missing"), _NULL)

    def test_explicit_null_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract('{"id": null}', "$.id"), _NULL)

    def test_object_match_is_nonscalar(self):
        from bulk_post.variables import _NONSCALAR

        self.assertIs(self._extract('{"a": {"b": 1}}', "$.a"), _NONSCALAR)

    def test_unparseable_body_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract("not json", "$.id"), _NULL)

    def test_empty_body_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract("", "$.id"), _NULL)


class TestRenderScalar(unittest.TestCase):
    def test_string_passthrough(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar("abc"), "abc")

    def test_int(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar(42), "42")

    def test_bool_lowercase(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar(True), "true")
        self.assertEqual(_render_scalar(False), "false")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_bulk_post.TestVariableExtraction tests.test_bulk_post.TestRenderScalar -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bulk_post.variables'`.

- [ ] **Step 3: Create `src/bulk_post/variables.py`**

```python
"""Workflow response-chaining variables: definition, JSONPath extraction, resolution.

Imports nothing from ``workflow``/``templating`` (takes primitives) so it sits at
the bottom of the workflow import graph. ``jsonpath-ng`` is imported lazily, only
when a workflow actually declares variables.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from typing import Any

_WORKFLOW_VAR_PREFIX = "_bulk_post_var/"


class _Null:
    """Sentinel: no match / explicit JSON null / unparseable source body."""


class _NonScalar:
    """Sentinel: JSONPath matched an object or array (unsupported in v1)."""


_NULL = _Null()
_NONSCALAR = _NonScalar()


@dataclasses.dataclass
class VarDef:
    """One workflow variable: capture ``json_path`` from ``source_path``'s response."""

    name: str  # "$id" (leading $ included)
    source_path: str  # normalized step path, e.g. "groupB/call-x"
    json_path: str  # raw JSONPath expression
    nullable: bool = True


def _var_col(source_path: str, name: str) -> str:
    """Reserved retry-CSV column name for a persisted variable value."""
    return f"{_WORKFLOW_VAR_PREFIX}{source_path}/{name}"


@functools.lru_cache(maxsize=256)
def _compile_jsonpath(expr: str) -> Any:
    """Compile and cache a JSONPath expression (lazily importing jsonpath-ng)."""
    import jsonpath_ng  # noqa: PLC0415

    return jsonpath_ng.parse(expr)


def validate_jsonpath(expr: str) -> str | None:
    """Return an error string if ``expr`` is unparseable or the dep is missing."""
    try:
        import jsonpath_ng  # noqa: PLC0415
    except ImportError:
        return (
            "jsonpath-ng is required for workflow variables. "
            "Install with: pip install jsonpath-ng"
        )
    try:
        jsonpath_ng.parse(expr)
    except Exception as e:  # validation boundary: many parser error types
        return f"Invalid jsonPath {expr!r}: {e}"
    return None


def _extract(body: str, compiled: Any) -> Any:
    """Apply a compiled JSONPath to a raw JSON body. Returns a scalar or a sentinel.

    Returns ``_NULL`` for no match / explicit null / unparseable body, ``_NONSCALAR``
    for an object/array match, otherwise the matched scalar value.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _NULL
    matches = compiled.find(data)
    if not matches:
        return _NULL
    value = matches[0].value
    if value is None:
        return _NULL
    if isinstance(value, (dict, list)):
        return _NONSCALAR
    return value


def _render_scalar(value: Any) -> str:
    """Render a JSON scalar as text (booleans as JSON-style lowercase)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_bulk_post.TestVariableExtraction tests.test_bulk_post.TestRenderScalar -v`
Expected: PASS (all 10).

- [ ] **Step 5: Commit**

```bash
git add src/bulk_post/variables.py tests/test_bulk_post.py
git commit -m "feat: JSONPath extraction and scalar rendering for workflow variables"
```

---

## Task 3: Variable substitution pass (`templating.py`)

**Files:**
- Modify: `src/bulk_post/templating.py:10` (add regex + functions)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bulk_post.py`:
```python
class TestSubstituteVars(unittest.TestCase):
    def test_replaces_variable(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{$id}}", {"$id": "42"})
        self.assertIsNone(err)
        self.assertEqual(out, "/users/42")

    def test_missing_variable_returns_error(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{$id}}", {})
        self.assertIsNotNone(err)
        self.assertIn("$id", err)

    def test_no_variables_passthrough(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{id}}", {})
        self.assertIsNone(err)
        self.assertEqual(out, "/users/{{id}}")  # {{id}} is a column, not a var


class TestRenderTemplate(unittest.TestCase):
    def test_columns_then_vars(self):
        from bulk_post import render_template

        out, err = render_template(
            "/{{region}}/users/{{$id}}", {"region": "eu"}, {"$id": "7"}
        )
        self.assertIsNone(err)
        self.assertEqual(out, "/eu/users/7")

    def test_var_value_with_braces_not_reexpanded(self):
        from bulk_post import render_template

        out, err = render_template("/{{$x}}", {"region": "eu"}, {"$x": "{{region}}"})
        self.assertIsNone(err)
        self.assertEqual(out, "/{{region}}")  # var value is NOT re-scanned

    def test_missing_column_error_propagates(self):
        from bulk_post import render_template

        out, err = render_template("/{{region}}", {}, {})
        self.assertIsNotNone(err)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_bulk_post.TestSubstituteVars tests.test_bulk_post.TestRenderTemplate -v`
Expected: FAIL with `ImportError: cannot import name 'substitute_vars'`.

- [ ] **Step 3: Add to `src/bulk_post/templating.py`**

After the `PLACEHOLDER_RE` line (line 10) add:
```python
VAR_RE = re.compile(r"\{\{(\$\w+)\}\}")
```

After the `substitute` function (after line 22) add:
```python
def substitute_vars(template: str, var_values: dict) -> tuple[str, str | None]:
    """Replace every ``{{$name}}`` in ``template`` with ``var_values[$name]``.

    Returns ``(result, None)`` on success, or ``(template, error)`` if any
    referenced variable is absent (a defensive check; parse-time validation
    should make this unreachable in normal runs).
    """
    missing = [m for m in VAR_RE.findall(template) if m not in var_values]
    if missing:
        return template, f"Unresolved workflow variables: {missing}"
    return VAR_RE.sub(lambda m: var_values[m.group(1)], template), None


def render_template(
    template: str, row: dict, var_values: dict
) -> tuple[str, str | None]:
    """Resolve ``{{col}}`` (from ``row``) then ``{{$var}}`` (from ``var_values``).

    Columns are substituted first; surviving ``{{$var}}`` tokens are disjoint and
    replaced in a second pass whose output is not re-scanned, so a variable value
    containing ``{{...}}``-looking text is never re-expanded.
    """
    s, err = substitute(template, row)
    if err:
        return template, err
    return substitute_vars(s, var_values)
```

- [ ] **Step 4: Add re-exports to `src/bulk_post/__init__.py`**

After the `substitute as substitute` import block (after line 108) add:
```python
from .templating import (
    VAR_RE as VAR_RE,
)
from .templating import (
    render_template as render_template,
)
from .templating import (
    substitute_vars as substitute_vars,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_bulk_post.TestSubstituteVars tests.test_bulk_post.TestRenderTemplate -v`
Expected: PASS (all 6).

- [ ] **Step 6: Commit**

```bash
git add src/bulk_post/templating.py src/bulk_post/__init__.py tests/test_bulk_post.py
git commit -m "feat: add variable substitution pass (substitute_vars, render_template)"
```

---

## Task 4: Variable resolution + persistence (`variables.py`)

**Files:**
- Modify: `src/bulk_post/variables.py` (add `resolve_variables`, `persist_vars`)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bulk_post.py`:
```python
class TestResolveVariables(unittest.TestCase):
    def _step(self, *vardefs):
        from bulk_post import WorkflowStep

        s = WorkflowStep(
            path="g/b",
            url="",
            method="GET",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={v.name: v for v in vardefs},
        )
        return s

    def test_live_scalar(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        vals, err = resolve_variables(
            self._step(v), {"g/a": '{"id": 42}'}, {}
        )
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": "42"})

    def test_null_nullable_true_empty(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.missing", nullable=True)
        vals, err = resolve_variables(self._step(v), {"g/a": "{}"}, {})
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": ""})

    def test_null_nullable_false_errors(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.missing", nullable=False)
        vals, err = resolve_variables(self._step(v), {"g/a": "{}"}, {})
        self.assertIsNotNone(err)
        self.assertIn("$id", err)

    def test_nonscalar_errors(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.obj", nullable=True)
        vals, err = resolve_variables(
            self._step(v), {"g/a": '{"obj": {"k": 1}}'}, {}
        )
        self.assertIsNotNone(err)
        self.assertIn("non-scalar", err)

    def test_persisted_value_used_when_source_absent(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        row = {"_bulk_post_var/g/a/$id": "99"}
        vals, err = resolve_variables(self._step(v), {}, row)
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": "99"})

    def test_absent_source_no_persist_applies_nullable(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        vals, err = resolve_variables(self._step(v), {}, {})
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": ""})


class TestPersistVars(unittest.TestCase):
    def _step(self, *vardefs):
        from bulk_post import WorkflowStep

        return WorkflowStep(
            path="g/b",
            url="",
            method="GET",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={v.name: v for v in vardefs},
        )

    def test_persists_rendered_scalar(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        out = persist_vars([self._step(v)], {"g/a": '{"id": 42}'})
        self.assertEqual(out, {"_bulk_post_var/g/a/$id": "42"})

    def test_skips_when_source_not_run(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        out = persist_vars([self._step(v)], {})
        self.assertEqual(out, {})

    def test_skips_null_match(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.missing", nullable=True)
        out = persist_vars([self._step(v)], {"g/a": "{}"})
        self.assertEqual(out, {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_bulk_post.TestResolveVariables tests.test_bulk_post.TestPersistVars -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_variables'`.

- [ ] **Step 3: Add the `variables` field to `WorkflowStep` (prerequisite)**

The Task 4 tests construct `WorkflowStep(..., variables={...})`, so add the field now in `src/bulk_post/workflow.py`. After the `on_error: str` line (line 59) in the `WorkflowStep` dataclass add:
```python
    variables: dict = dataclasses.field(default_factory=dict)  # name -> VarDef
```
(No new import needed — the annotation is `dict`. The `VarDef` import into `workflow.py` is added in Task 5.)

- [ ] **Step 4: Add to `src/bulk_post/variables.py`**

Append:
```python
def resolve_variables(step: Any, responses: dict, row: dict) -> tuple[dict, str | None]:
    """Resolve every in-scope variable of ``step`` to a rendered string.

    Resolution order per variable: live response this run (``responses``) →
    persisted retry-CSV column (``row``) → null. Returns ``(values, None)`` or
    ``({}, error)`` if a non-nullable variable is null or any match is non-scalar.
    """
    values: dict = {}
    for name, var in step.variables.items():
        if var.source_path in responses:
            extracted = _extract(
                responses[var.source_path], _compile_jsonpath(var.json_path)
            )
        else:
            persisted = row.get(_var_col(var.source_path, var.name), "")
            extracted = persisted if persisted != "" else _NULL

        if extracted is _NONSCALAR:
            return {}, f"Variable {name} resolved to a non-scalar value"
        if extracted is _NULL:
            if var.nullable:
                values[name] = ""
            else:
                return {}, f"Variable {name} resolved to null (nullable=false)"
        else:
            # Live scalars render here; persisted values are already strings
            # (str(...) is a no-op for them).
            values[name] = _render_scalar(extracted)
    return values, None


def persist_vars(steps: list, responses: dict) -> dict:
    """Resolved values to persist into the retry CSV after a row fails.

    For each variable whose source step completed this run, capture its rendered
    scalar (skipping null / non-scalar / empty). Keyed by reserved column name;
    later steps win on duplicate (source, name) keys (same value).
    """
    out: dict = {}
    for step in steps:
        for var in step.variables.values():
            if var.source_path not in responses:
                continue
            extracted = _extract(
                responses[var.source_path], _compile_jsonpath(var.json_path)
            )
            if extracted is _NULL or extracted is _NONSCALAR:
                continue
            rendered = _render_scalar(extracted)
            if rendered != "":
                out[_var_col(var.source_path, var.name)] = rendered
    return out
```

- [ ] **Step 5: Add re-exports to `src/bulk_post/__init__.py`**

After the existing `from .templating import ... substitute` block, add a variables block:
```python
from .variables import (
    VarDef as VarDef,
)
from .variables import (
    _WORKFLOW_VAR_PREFIX as _WORKFLOW_VAR_PREFIX,
)
from .variables import (
    _var_col as _var_col,
)
from .variables import (
    persist_vars as persist_vars,
)
from .variables import (
    resolve_variables as resolve_variables,
)
from .variables import (
    validate_jsonpath as validate_jsonpath,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_bulk_post.TestResolveVariables tests.test_bulk_post.TestPersistVars -v`
Expected: PASS (all 9). (The `WorkflowStep.variables` field was added in Step 3 of this task.)

- [ ] **Step 7: Commit**

```bash
git add src/bulk_post/variables.py src/bulk_post/workflow.py src/bulk_post/__init__.py tests/test_bulk_post.py
git commit -m "feat: resolve and persist workflow variables"
```

---

## Task 5: Parse + validate variables in `parse_workflow`

**Files:**
- Modify: `src/bulk_post/workflow.py` (dataclass field, parse helpers, validation, `workflow_var_columns`)
- Modify: `src/bulk_post/__init__.py` (re-export `workflow_var_columns`)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bulk_post.py` (reuses the `_yaml` helper pattern from `TestParseWorkflow`):
```python
class TestParseWorkflowVariables(unittest.TestCase):
    def _yaml(self, content):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=".yaml", delete=False, prefix="wfvar"
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    _GOOD = """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/{{id}}
          method: POST
  groupB:
    variables:
      $newId:
        source: .workflow.groupA.create
        jsonPath: $.id
        nullable: false
    endpoints:
      - use:
          url: https://api/use/{{$newId}}
          method: POST
"""

    def test_valid_variable_attached_to_step(self):
        path = self._yaml(self._GOOD)
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertIn("$newId", use.variables)
        v = use.variables["$newId"]
        self.assertEqual(v.source_path, "groupA/create")
        self.assertEqual(v.json_path, "$.id")
        self.assertFalse(v.nullable)

    def test_nullable_defaults_true(self):
        path = self._yaml(
            self._GOOD.replace("        nullable: false\n", "")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertTrue(use.variables["$newId"].nullable)

    def test_endpoint_overrides_group_variable(self):
        path = self._yaml(
            """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/x
          method: POST
  groupB:
    variables:
      $v:
        source: .workflow.groupA.create
        jsonPath: $.a
    endpoints:
      - use:
          url: https://api/{{$v}}
          method: POST
          variables:
            $v:
              source: .workflow.groupA.create
              jsonPath: $.b
"""
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertEqual(use.variables["$v"].json_path, "$.b")

    def test_name_without_dollar_errors(self):
        path = self._yaml(
            self._GOOD.replace("$newId", "newId")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("$", err)

    def test_undefined_reference_errors(self):
        path = self._yaml(
            self._GOOD.replace("https://api/use/{{$newId}}", "https://api/use/{{$ghost}}")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("$ghost", err)

    def test_forward_reference_errors(self):
        # $newId in groupA references groupB/use which runs LATER
        path = self._yaml(
            """
workflow:
  groupA:
    variables:
      $x:
        source: .workflow.groupB.use
        jsonPath: $.id
    endpoints:
      - create:
          url: https://api/{{$x}}
          method: POST
  groupB:
    endpoints:
      - use:
          url: https://api/x
          method: POST
"""
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_unknown_source_errors(self):
        path = self._yaml(
            self._GOOD.replace(".workflow.groupA.create", ".workflow.groupA.nope")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_invalid_jsonpath_errors(self):
        path = self._yaml(
            self._GOOD.replace("jsonPath: $.id", "jsonPath: '$.['")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_missing_jsonpath_ng_reports_install_error(self):
        # Simulate jsonpath-ng not installed: a None entry in sys.modules makes
        # `import jsonpath_ng` raise ImportError, which validate_jsonpath converts
        # into a clean install message (mirrors the pyyaml-missing behavior).
        path = self._yaml(self._GOOD)
        with patch.dict(sys.modules, {"jsonpath_ng": None}):
            steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("jsonpath-ng", err)


class TestWorkflowVarColumns(unittest.TestCase):
    def test_dedup_and_order(self):
        from bulk_post import VarDef, WorkflowStep, workflow_var_columns

        def step(path, *vs):
            return WorkflowStep(
                path=path, url="", method="GET", body=None,
                content_type="application/json", headers={}, auth_type="none",
                auth_raw="", on_error="stop", variables={v.name: v for v in vs},
            )

        a = VarDef("$x", "g/a", "$.x", True)
        b = VarDef("$y", "g/a", "$.y", True)
        cols = workflow_var_columns([step("g/b", a), step("g/c", a, b)])
        self.assertEqual(
            cols, ["_bulk_post_var/g/a/$x", "_bulk_post_var/g/a/$y"]
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_bulk_post.TestParseWorkflowVariables tests.test_bulk_post.TestWorkflowVarColumns -v`
Expected: FAIL (variables not parsed; `workflow_var_columns` missing; `WorkflowStep` has no `variables`).

- [ ] **Step 3: Add the variables import to `workflow.py`**

The `WorkflowStep.variables` field was already added in Task 4 Step 3. Now add the `variables` import near the top of `src/bulk_post/workflow.py` (after line 16):
```python
from .variables import VarDef, _var_col, validate_jsonpath
```

- [ ] **Step 4: Add parse + normalize helpers**

In `src/bulk_post/workflow.py`, after `_extract_content_type` (after line 106) add:
```python
def _normalize_source(src: str) -> str:
    """Map a ``.workflow.group.endpoint`` source string to a ``group/endpoint`` path."""
    s = str(src).strip()
    if s.startswith("."):
        s = s[1:]
    if s.startswith("workflow."):
        s = s[len("workflow.") :]
    return s.replace(".", "/")


def _parse_variables(raw: dict | None) -> tuple[dict, str | None]:
    """Parse a ``variables:`` mapping into ``{name: VarDef}``. Returns (vars, err)."""
    if not raw:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "'variables' must be a mapping"
    out: dict = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.startswith("$"):
            return {}, f"Variable name '{name}' must start with '$'"
        if not isinstance(spec, dict):
            return {}, f"Variable '{name}' must be a mapping"
        source = spec.get("source")
        json_path = spec.get("jsonPath")
        if not source:
            return {}, f"Variable '{name}' is missing 'source'"
        if not json_path:
            return {}, f"Variable '{name}' is missing 'jsonPath'"
        nullable = spec.get("nullable", True)
        out[name] = VarDef(
            name=name,
            source_path=_normalize_source(source),
            json_path=str(json_path),
            nullable=bool(nullable),
        )
    return out, None
```

- [ ] **Step 5: Parse group + endpoint variables and merge onto each step**

In `parse_workflow`, inside the group loop, after `group_auth_type, group_auth_raw = _parse_auth_block(...)` (line 148) add:
```python
        group_vars, gv_err = _parse_variables(group_data.get("variables"))
        if gv_err:
            return [], f"Group '{group_name}': {gv_err}"
```

Inside the endpoint loop, before the `steps.append(...)` call (before line 197) add:
```python
            ep_vars, ev_err = _parse_variables(
                ep_data.get("variables") or entry.get("variables")
            )
            if ev_err:
                return [], f"Endpoint '{path}': {ev_err}"
            merged_vars = {**group_vars, **ep_vars}  # endpoint overrides group
```

Change the `steps.append(WorkflowStep(...))` to pass `variables=merged_vars`:
```python
            steps.append(
                WorkflowStep(
                    path=path,
                    url=url,
                    method=method,
                    body=body,
                    content_type=content_type,
                    headers=headers,
                    auth_type=auth_type,
                    auth_raw=auth_raw,
                    on_error=on_error,
                    variables=merged_vars,
                )
            )
```

- [ ] **Step 6: Add the post-parse variable validation**

In `parse_workflow`, replace the final `return steps, None` (line 213) with:
```python
    verr = _validate_variables(steps)
    if verr:
        return [], verr
    return steps, None
```

Add this helper after `parse_workflow` (and `from .templating import PLACEHOLDER_RE, substitute` already exists; also import `VAR_RE`):

Change the templating import (line 16) to:
```python
from .templating import PLACEHOLDER_RE, VAR_RE, substitute
```

Then add after `parse_workflow`:
```python
def _validate_variables(steps: list) -> str | None:
    """Validate variable definitions and references across all steps.

    Checks: every ``{{$name}}`` reference has an in-scope definition; each
    variable's source resolves to exactly one EARLIER step; each ``jsonPath``
    parses. Returns an error string, or ``None`` if everything checks out.
    """
    index = {step.path: i for i, step in enumerate(steps)}
    for i, step in enumerate(steps):
        # Every referenced {{$name}} must be defined in scope.
        referenced: list = VAR_RE.findall(step.url)
        if step.body:
            referenced += VAR_RE.findall(step.body)
        for val in step.headers.values():
            referenced += VAR_RE.findall(str(val))
        for name in referenced:
            if name not in step.variables:
                return f"Step '{step.path}': undefined variable {name}"
        # Each defined variable's source must be an earlier step; jsonPath valid.
        for var in step.variables.values():
            if var.source_path not in index:
                return (
                    f"Step '{step.path}': variable {var.name} source "
                    f"'{var.source_path}' does not match any endpoint"
                )
            if index[var.source_path] >= i:
                return (
                    f"Step '{step.path}': variable {var.name} source "
                    f"'{var.source_path}' must be an earlier step"
                )
            jerr = validate_jsonpath(var.json_path)
            if jerr:
                return f"Step '{step.path}': variable {var.name}: {jerr}"
    return None


def workflow_var_columns(steps: list) -> list:
    """Ordered, de-duplicated reserved retry-CSV column names for all variables."""
    cols: list = []
    seen: set = set()
    for step in steps:
        for var in step.variables.values():
            col = _var_col(var.source_path, var.name)
            if col not in seen:
                seen.add(col)
                cols.append(col)
    return cols
```

- [ ] **Step 7: Re-export `workflow_var_columns` from `__init__.py`**

In `src/bulk_post/__init__.py`, in the `from .workflow import ...` group (around line 181-196) add:
```python
from .workflow import (
    workflow_var_columns as workflow_var_columns,
)
```

- [ ] **Step 8: Run the new + existing workflow tests**

Run: `uv run python -m unittest tests.test_bulk_post.TestParseWorkflowVariables tests.test_bulk_post.TestWorkflowVarColumns tests.test_bulk_post.TestParseWorkflow -v`
Expected: PASS (new cases + the existing `TestParseWorkflow` still green).

- [ ] **Step 9: Commit**

```bash
git add src/bulk_post/workflow.py src/bulk_post/__init__.py tests/test_bulk_post.py
git commit -m "feat: parse and validate workflow variables; attach to steps"
```

---

## Task 6: Thread `responses` through `_fire_workflow_step`

**Files:**
- Modify: `src/bulk_post/workflow.py` (`_fire_workflow_step`)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bulk_post.py`:
```python
class TestFireWorkflowStepVariables(unittest.TestCase):
    def _step(self, url, variables):
        from bulk_post import WorkflowStep

        return WorkflowStep(
            path="g/b", url=url, method="GET", body=None,
            content_type="application/json", headers={}, auth_type="none",
            auth_raw="", on_error="stop", variables=variables,
        )

    def test_variable_substituted_into_url(self):
        from bulk_post import VarDef, _fire_workflow_step

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        step = self._step("https://api/use/{{$id}}", {"$id": v})
        responses = {"g/a": '{"id": 42}'}

        with patch("bulk_post.workflow.http_request") as mock_http:
            mock_http.return_value = (200, "ok", 0.01, {}, {})
            result = _fire_workflow_step(
                step, {}, None, 30, responses=responses
            )
        # final_url is index 3 of the returned tuple
        self.assertEqual(result[3], "https://api/use/42")
        mock_http.assert_called_once()
        self.assertEqual(mock_http.call_args[0][0], "https://api/use/42")

    def test_non_nullable_null_is_skip_error(self):
        from bulk_post import VarDef, _fire_workflow_step

        v = VarDef("$id", "g/a", "$.missing", nullable=False)
        step = self._step("https://api/use/{{$id}}", {"$id": v})

        with patch("bulk_post.workflow.http_request") as mock_http:
            result = _fire_workflow_step(step, {}, None, 30, responses={"g/a": "{}"})
        status, body, _, url = result[0], result[1], result[2], result[3]
        self.assertIsNone(status)
        self.assertEqual(url, "")  # routed as a substitution SKIP
        self.assertIn("$id", body)
        mock_http.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_bulk_post.TestFireWorkflowStepVariables -v`
Expected: FAIL — `_fire_workflow_step` has no `responses` parameter (TypeError) or variables not substituted.

- [ ] **Step 3: Update `_fire_workflow_step`**

In `src/bulk_post/workflow.py`, update the two import lines to their final state. `_fire_workflow_step` now uses `render_template` instead of `substitute`, so **drop `substitute`** (it becomes unused → ruff `F401`); `PLACEHOLDER_RE` stays (used by `_validate_workflow_placeholders`):
```python
from .templating import PLACEHOLDER_RE, VAR_RE, render_template
from .variables import VarDef, _var_col, resolve_variables, validate_jsonpath
```

Change the signature of `_fire_workflow_step` (line 262) to add a `responses` keyword param:
```python
def _fire_workflow_step(
    step: WorkflowStep,
    row: dict,
    auth_header: str | None,
    timeout: int,
    auth_refresh_fn: Callable | None = None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
    responses: dict | None = None,
) -> tuple[int | None, str, float, str, str | None, str | None, dict, dict]:
```

Replace the substitution block (lines 276-291) with variable resolution + `render_template`:
```python
    responses = responses or {}
    var_values, verr = resolve_variables(step, responses, row)
    if verr:
        return None, verr, 0.0, "", None, None, {}, {}

    url, err = render_template(step.url, row, var_values)
    if err:
        return None, err, 0.0, "", None, None, {}, {}

    req_body: str | None = None
    if step.body:
        req_body, err = render_template(step.body, row, var_values)
        if err:
            return None, err, 0.0, url, None, None, {}, {}

    extra_headers: dict = {}
    for k, v in step.headers.items():
        val, herr = render_template(str(v), row, var_values)
        if herr:
            return None, herr, 0.0, url, None, None, {}, {}
        extra_headers[k] = val
```

(The rest of `_fire_workflow_step` — the `http_request` call and 401 refresh — is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_bulk_post.TestFireWorkflowStepVariables -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add src/bulk_post/workflow.py tests/test_bulk_post.py
git commit -m "feat: resolve workflow variables inside _fire_workflow_step"
```

---

## Task 7: Wire `responses` + persistence into both runners

**Files:**
- Modify: `src/bulk_post/workflow_runner.py` (`_run_workflow_loop`, `_workflow_parallel_worker`)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing test (end-to-end sequential)**

First, ensure `import argparse` is at the top of `tests/test_bulk_post.py` (the runner, resume, and CLI tests construct `argparse.Namespace`); add it to the stdlib import group if missing.

Then append to `tests/test_bulk_post.py`. This drives `_run_workflow_loop` with a mocked `http_request` so step B's URL is built from step A's response:
```python
class TestWorkflowRunnerVariables(unittest.TestCase):
    def _args(self):
        ns = argparse.Namespace()
        ns.timeout = 30
        ns.verbose = False
        ns.delay = 0
        ns.debug = False
        ns.parallel = False
        return ns

    def _steps(self):
        from bulk_post import VarDef, WorkflowStep

        a = WorkflowStep(
            path="groupA/create", url="https://api/create", method="POST",
            body=None, content_type="application/json", headers={},
            auth_type="none", auth_raw="", on_error="stop", variables={},
        )
        b = WorkflowStep(
            path="groupB/use", url="https://api/use/{{$id}}", method="POST",
            body=None, content_type="application/json", headers={},
            auth_type="none", auth_raw="", on_error="stop",
            variables={"$id": VarDef("$id", "groupA/create", "$.id", False)},
        )
        return [a, b]

    def test_second_step_uses_first_response(self):
        import io

        from bulk_post import _run_workflow_loop

        reader = [{"x": "1"}]  # one row; DictReader-like iterable of dicts
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            if url == "https://api/create":
                return (200, '{"id": 555}', 0.01, {}, {})
            return (200, "done", 0.01, {}, {})

        log = io.StringIO()
        retry = MagicMock()
        with patch("bulk_post.workflow.http_request", side_effect=fake_http):
            ok, failed, processed = _run_workflow_loop(
                iter(reader), self._steps(), self._args(), {}, None, None, None,
                retry, log, 0, 1, ["x", "_bulk_post_step"],
            )
        self.assertEqual((ok, failed), (1, 0))
        self.assertIn("https://api/use/555", calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_bulk_post.TestWorkflowRunnerVariables -v`
Expected: FAIL — step B URL is `https://api/use/` (empty) or a SKIP, because `responses` isn't threaded yet, so `https://api/use/555` is never called.

- [ ] **Step 3: Update `_run_workflow_loop`**

In `src/bulk_post/workflow_runner.py`, add the persist import (after line 35):
```python
from .variables import persist_vars
```

Inside `_run_workflow_loop`'s per-row body, after `first_failed_step: str | None = None` (line 63) add:
```python
        responses: dict = {}
```

In the `_fire_workflow_step(...)` call (lines 75-91) add `responses=responses` as a keyword argument:
```python
            ) = _fire_workflow_step(
                step,
                row,
                step_auth,
                args.timeout,
                suspend=suspend,
                resume=resume,
                responses=responses,
            )
```

Immediately after the `if new_auth is not None: auth_headers[step.path] = new_auth` block (after line 94), capture the response:
```python
            if status is not None:
                responses[step.path] = body
```

In the `if row_failed:` block (lines 155-160), add the persisted vars before setting the step column:
```python
        if row_failed:
            failed_rows += 1
            retry_row = dict(row)
            retry_row.pop(_WORKFLOW_STEP_COL, None)
            retry_row.update(persist_vars(steps, responses))
            retry_row[_WORKFLOW_STEP_COL] = first_failed_step
            retry_writer.writerow(retry_row)
```

- [ ] **Step 4: Update `_workflow_parallel_worker` identically**

In `_workflow_parallel_worker`, after `first_failed_step: str | None = None` (line 252) add:
```python
            responses: dict = {}
```

In its `_fire_workflow_step(...)` call (lines 267-282) add `responses=responses`:
```python
                ) = _fire_workflow_step(
                    step,
                    row,
                    step_auth,
                    args.timeout,
                    auth_refresh_fn=auth_refresh_fns.get(step.path),
                    responses=responses,
                )
```

After the `if new_auth is not None: ... state.auth_headers[step.path] = new_auth` block (after line 286) add:
```python
                if status is not None:
                    responses[step.path] = body
```

In the failure-persist block (lines 361-367) add the persisted vars:
```python
        with state.lock:
            if row_failed:
                state.failed += 1
                retry_row = dict(row)
                retry_row.pop(_WORKFLOW_STEP_COL, None)
                retry_row.update(persist_vars(steps, responses))
                retry_row[_WORKFLOW_STEP_COL] = first_failed_step
                retry_writer.writerow(retry_row)
            else:
                state.ok += 1
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_bulk_post.TestWorkflowRunnerVariables -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bulk_post/workflow_runner.py tests/test_bulk_post.py
git commit -m "feat: thread per-row responses and persist variables in workflow runners"
```

---

## Task 8: Build retry fieldnames with variable columns (`cli.py`)

**Files:**
- Modify: `src/bulk_post/cli.py:18-24` (import) and `:231-234` (retry fieldnames)
- Test: `tests/test_bulk_post.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bulk_post.py`. Two CLI-level tests: a happy path (chained variable, exit 0) and a **failure path** that forces a retry-CSV write — the latter genuinely fails before the fix because the persisted-var column is not in `retry_fieldnames`, so `csv.DictWriter` raises `ValueError` on the extra key.
```python
class TestCliWorkflowVariablesEndToEnd(unittest.TestCase):
    _WF = """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/create
          method: POST
  groupB:
    variables:
      $id:
        source: .workflow.groupA.create
        jsonPath: $.id
        nullable: false
    endpoints:
      - use:
          url: https://api/use/{{$id}}
          method: POST
"""

    def _write(self, suffix, content, prefix="t"):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=suffix, delete=False, prefix=prefix
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_run_succeeds_with_chained_variable(self):
        csv_path = self._write(".csv", "x\n1\n", prefix="rows")
        wf_path = self._write(".yaml", self._WF, prefix="wf")

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            if url == "https://api/create":
                return (200, '{"id": 7}', 0.01, {}, {})
            return (200, "ok", 0.01, {}, {})

        with patch("sys.stdin") as stdin, patch(
            "bulk_post.workflow.http_request", side_effect=fake_http
        ):
            stdin.isatty.return_value = False
            code = bulk_post.main(["-w", wf_path, "-c", csv_path])
        self.assertEqual(code, 0)

    def test_failure_persists_variable_column(self):
        csv_path = self._write(".csv", "x\n1\n", prefix="rows")
        wf_path = self._write(".yaml", self._WF, prefix="wf")
        retry_path = Path(csv_path).with_name(Path(csv_path).stem + "_failed.csv")
        self.addCleanup(lambda: retry_path.unlink(missing_ok=True))
        self.addCleanup(
            lambda: retry_path.with_suffix(".log").unlink(missing_ok=True)
        )

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            if url == "https://api/create":
                return (200, '{"id": 7}', 0.01, {}, {})
            return (500, "boom", 0.01, {}, {})  # step B fails -> row written to retry

        with patch("sys.stdin") as stdin, patch(
            "bulk_post.workflow.http_request", side_effect=fake_http
        ):
            stdin.isatty.return_value = False
            code = bulk_post.main(["-w", wf_path, "-c", csv_path])

        self.assertEqual(code, 1)
        with open(retry_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["_bulk_post_var/groupA/create/$id"], "7")
        self.assertEqual(rows[0]["_bulk_post_step"], "groupB/use")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_bulk_post.TestCliWorkflowVariablesEndToEnd -v`
Expected: `test_failure_persists_variable_column` FAILS (errors with `ValueError: dict contains fields not in fieldnames: '_bulk_post_var/groupA/create/$id'` raised from `DictWriter.writerow`). `test_run_succeeds_with_chained_variable` may already pass — it's the success guard.

- [ ] **Step 3: Update the import in `cli.py`**

Change the `from .workflow import (...)` block (lines 18-23) to:
```python
from .workflow import (
    _WORKFLOW_STEP_COL,
    _resolve_workflow_auth_headers,
    _validate_workflow_placeholders,
    parse_workflow,
    workflow_var_columns,
)
from .variables import _WORKFLOW_VAR_PREFIX
```

- [ ] **Step 4: Update the retry fieldnames computation**

Replace lines 231-234 (the `base_fields`/`retry_fieldnames` assignment inside `if workflow_mode:`):
```python
        # fieldnames for retry CSV: original columns + persisted-variable columns
        # + _bulk_post_step at the end. Strip any pre-existing reserved columns
        # first (a resumed retry CSV already carries them) to avoid duplicates.
        base_fields = [
            f
            for f in fieldnames
            if f != _WORKFLOW_STEP_COL and not f.startswith(_WORKFLOW_VAR_PREFIX)
        ]
        retry_fieldnames = base_fields + workflow_var_columns(steps) + [
            _WORKFLOW_STEP_COL
        ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest tests.test_bulk_post.TestCliWorkflowVariablesEndToEnd -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bulk_post/cli.py tests/test_bulk_post.py
git commit -m "feat: include persisted-variable columns in workflow retry CSV"
```

---

## Task 9: Resume regression test (persisted variable survives a re-run)

**Files:**
- Test: `tests/test_bulk_post.py` (no production code change expected)

- [ ] **Step 1: Write the test**

Append to `tests/test_bulk_post.py`. Simulates a resumed row: step A is skipped (resume point is step B), and `$id` is read from the persisted column:
```python
class TestWorkflowResumeVariables(unittest.TestCase):
    def _args(self):
        ns = argparse.Namespace()
        ns.timeout = 30
        ns.verbose = False
        ns.delay = 0
        ns.debug = False
        ns.parallel = False
        return ns

    def test_resumed_step_reads_persisted_variable(self):
        import io

        from bulk_post import VarDef, WorkflowStep, _run_workflow_loop

        a = WorkflowStep(
            path="groupA/create", url="https://api/create", method="POST",
            body=None, content_type="application/json", headers={},
            auth_type="none", auth_raw="", on_error="stop", variables={},
        )
        b = WorkflowStep(
            path="groupB/use", url="https://api/use/{{$id}}", method="POST",
            body=None, content_type="application/json", headers={},
            auth_type="none", auth_raw="", on_error="stop",
            variables={"$id": VarDef("$id", "groupA/create", "$.id", False)},
        )
        # Resume row: _bulk_post_step points at groupB/use; $id is persisted.
        row = {
            "x": "1",
            "_bulk_post_var/groupA/create/$id": "321",
            "_bulk_post_step": "groupB/use",
        }
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            return (200, "ok", 0.01, {}, {})

        log = io.StringIO()
        with patch("bulk_post.workflow.http_request", side_effect=fake_http):
            ok, failed, processed = _run_workflow_loop(
                iter([row]), [a, b], self._args(), {}, None, None, None,
                MagicMock(), log, 0, 1,
                ["x", "_bulk_post_var/groupA/create/$id", "_bulk_post_step"],
            )
        self.assertEqual((ok, failed), (1, 0))
        # groupA/create is skipped on resume; only groupB/use fires, with $id=321
        self.assertEqual(calls, ["https://api/use/321"])
```

- [ ] **Step 2: Run the test**

Run: `uv run python -m unittest tests.test_bulk_post.TestWorkflowResumeVariables -v`
Expected: PASS (the mechanism from Tasks 4 & 7 already supports this; this locks it in).

- [ ] **Step 3: Commit**

```bash
git add tests/test_bulk_post.py
git commit -m "test: persisted variable survives mid-workflow resume"
```

---

## Task 10: Full suite, lint, type-check

**Files:** none (verification)

- [ ] **Step 1: Run the entire test suite**

Run: `uv run python -m unittest discover tests/`
Expected: OK, 0 failures/errors.

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/ tests/`
Expected: no errors. (If the broad `except Exception` in `validate_jsonpath` is flagged by `B`, add a targeted `# noqa: BLE001` with the comment that it's a validation boundary.)

- [ ] **Step 3: Format check**

Run: `uv run ruff format --check src/ tests/`
Expected: no changes needed (run `uv run ruff format src/ tests/` if it reports reformatting, then re-commit).

- [ ] **Step 4: Type-check**

Run: `uv run mypy src/bulk_post`
Expected: no new errors introduced by `variables.py`/the changes.

- [ ] **Step 5: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore: lint/format/type fixups for workflow variables"
```

---

## Task 11: Docs + example reconciliation

**Files:**
- Modify: `CLAUDE.md` (Workflow mode + Public API surface + dependency note)
- Modify: `README.md` (workflow schema + variables + dependency + retry-CSV security note)
- Modify: `workflow-example.yaml`

- [ ] **Step 1: Update `CLAUDE.md` — Workflow mode section**

Add a paragraph documenting variables: declaration at group/endpoint level, `{{$name}}` reference syntax, `source` (`.workflow.group.endpoint`) → JSONPath capture, `nullable` (default `true`) fail-vs-empty behavior, one-row lifetime, and resolution order (live → persisted retry column → null). Note the new lazy `jsonpath-ng` dependency and that **retry CSVs may contain response-derived data** (`_bulk_post_var/...` columns) and should not be shared/committed.

- [ ] **Step 2: Update `CLAUDE.md` — Public API surface**

Add the new exported names: `VarDef`, `resolve_variables(step, responses, row) -> (dict, err)`, `persist_vars(steps, responses) -> dict`, `substitute_vars(template, var_values) -> (str, err)`, `render_template(template, row, var_values) -> (str, err)`, `workflow_var_columns(steps) -> list`, `validate_jsonpath(expr) -> Optional[str]`.

- [ ] **Step 3: Update `README.md`**

Add a "Workflow variables" subsection mirroring `CLAUDE.md`, the `jsonpath-ng` dependency line, and the retry-CSV security note. Per the project rule, keep the flag/feature docs in sync across both files in this same change.

- [ ] **Step 4: Reconcile `workflow-example.yaml`**

Ensure the example matches the implemented schema: `source`, `jsonPath`, `nullable` keys; `{{$var}}` references; group-level and endpoint-level blocks; source uses `.workflow.group.endpoint`. Confirm it parses:
```bash
uv run python -c "import bulk_post; s,e=bulk_post.parse_workflow('workflow-example.yaml'); print(e or f'{len(s)} steps OK')"
```
Expected: `N steps OK` (no error).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md workflow-example.yaml
git commit -m "docs: document workflow response-chaining variables"
```

---

## Self-Review notes (for the implementer)

- **Import cycle guard:** `variables.py` must not import from `workflow.py` or `templating.py`. `workflow.py` imports from both. If you see a circular-import error, the `VarDef`/`_var_col`/`validate_jsonpath` definitions belong in `variables.py`, not `workflow.py`.
- **`substitute` is untouched:** single-URL mode keeps using `substitute(template, row)` directly. Only the workflow path calls `render_template`.
- **Column-vs-var disjointness:** `PLACEHOLDER_RE` (`\{\{(\w+)\}\}`) cannot match `{{$id}}`; `VAR_RE` (`\{\{(\$\w+)\}\}`) only matches `$`-prefixed. This is why `_validate_workflow_placeholders` needs no change and why the two passes don't collide.
- **Name consistency:** `VarDef.name` always includes the leading `$` (`"$id"`), and `var_values` / `step.variables` are keyed by that same `$`-name throughout.
- **Persisted-empty caveat:** `persist_vars` skips null/empty/non-scalar, so on resume a missing reserved column re-applies the `nullable` rule (a `nullable=false` variable whose source was skipped and produced no persisted value will correctly fail). This is the documented v1 behavior.
