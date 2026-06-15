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
            # Empty == absent: persist_vars never writes empty renders, so an
            # empty column can't lose a genuinely-empty value; fall to nullable.
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
