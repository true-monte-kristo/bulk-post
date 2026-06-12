"""Workflow YAML parsing, validation, and single-step execution."""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Callable

from .auth import (
    prompt_new_basic_creds,
    prompt_new_token,
    resolve_basic_creds,
    resolve_token,
)
from .http import http_request
from .templating import PLACEHOLDER_RE, substitute

# ---------------------------------------------------------------------------
# Workflow constants
# ---------------------------------------------------------------------------

_WORKFLOW_STEP_COL = "_bulk_post_step"

# Auth types (values of a group/step `auth.type`)
_AUTH_BEARER = "bearer"
_AUTH_BASIC = "basic"
_AUTH_NONE = "none"

# Step on-error policies
_ON_ERROR_STOP = "stop"
_ON_ERROR_CONTINUE = "continue"

# Endpoint defaults
_DEFAULT_METHOD = "POST"
_DEFAULT_CONTENT_TYPE = "application/json"
_HEADER_CONTENT_TYPE = "content-type"  # lowercased, for case-insensitive match

# ---------------------------------------------------------------------------
# Workflow data structures
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WorkflowStep:
    """One normalized, ready-to-fire workflow step with resolved auth.

    Produced by ``parse_workflow``. ``path`` is the unique ``group/name`` id used
    for retry/resume (``_bulk_post_step``) and per-step auth lookup.
    """

    path: str  # "groupA/call-example-api"
    url: str
    method: str
    body: str | None
    content_type: str
    headers: dict  # without Content-Type
    auth_type: str  # "bearer", "basic", "none"
    auth_raw: str  # raw credential: token or user:pass
    on_error: str  # "stop" or "continue"


def _parse_auth_block(auth: dict | None) -> tuple[str, str]:
    """Normalize a raw ``auth:`` mapping into ``(auth_type, auth_raw)``.

    ``auth_raw`` is the bearer token, ``user:pass`` for basic, or ``""``.
    Shared by group-level and step-level auth.
    """
    auth = auth or {}
    auth_type = (auth.get("type") or _AUTH_NONE).lower()
    if auth_type == _AUTH_BEARER:
        return _AUTH_BEARER, auth.get("token") or ""
    if auth_type == _AUTH_BASIC:
        user = auth.get("user") or ""
        pw = auth.get("password") or ""
        return _AUTH_BASIC, f"{user}:{pw}" if (user or pw) else ""
    return _AUTH_NONE, ""


def _endpoint_name_and_data(entry: dict) -> tuple[str, dict]:
    """Resolve an endpoint entry into ``(name, ep_data)``.

    Two YAML styles are supported: a ``{name: {…fields}}`` mapping (name is the
    first key whose value is a mapping), or a flat mapping whose keys are the
    fields directly (name derived from the first key).
    """
    for k, v in entry.items():
        if isinstance(v, dict):
            return k, v
    # Flat style: the entry dict IS the endpoint data; use the first key as name.
    name = next(iter(entry))
    ep_data = {k: v for k, v in entry.items() if k != name or isinstance(v, dict)}
    return name, ep_data or entry


def _extract_content_type(raw_headers: dict) -> tuple[str, dict]:
    """Pop a case-insensitive ``Content-Type`` out of ``raw_headers``.

    Returns ``(content_type, headers_without_content_type)`` and never mutates
    the caller's dict.
    """
    headers = dict(raw_headers)
    content_type = _DEFAULT_CONTENT_TYPE
    ct_key = next((k for k in headers if k.lower() == _HEADER_CONTENT_TYPE), None)
    if ct_key:
        content_type = headers.pop(ct_key)
    return content_type, headers


def parse_workflow(yaml_path: str) -> tuple[list, str | None]:
    """
    Parse a workflow YAML file. Returns (steps, error_or_None).
    Steps are WorkflowStep objects in document order.
    """
    try:
        import yaml as _yaml  # noqa: PLC0415

        _HAS_YAML = True
    except ImportError:
        _HAS_YAML = False
        _yaml = None  # type: ignore[assignment]

    if not _HAS_YAML:
        return [], "PyYAML is required for --workflow. Install with: pip install pyyaml"
    try:
        with open(yaml_path, encoding="utf-8") as f:
            doc = _yaml.safe_load(f)
    except OSError as e:
        return [], f"Cannot open workflow file: {e}"
    except _yaml.YAMLError as e:
        return [], f"Invalid YAML in workflow file: {e}"

    if not isinstance(doc, dict) or "workflow" not in doc:
        return [], "Workflow file must have a top-level 'workflow' key"

    wf = doc["workflow"]
    if not isinstance(wf, dict):
        return [], "'workflow' must be a mapping"

    steps: list = []
    seen_paths: set = set()

    for group_name, group_data in wf.items():
        if group_name == "description":
            continue
        if not isinstance(group_data, dict):
            return [], f"Group '{group_name}' must be a mapping"

        group_auth_type, group_auth_raw = _parse_auth_block(group_data.get("auth"))

        endpoints = group_data.get("endpoints")
        if not endpoints:
            continue
        if not isinstance(endpoints, list):
            return [], f"Group '{group_name}'.endpoints must be a list"

        for entry in endpoints:
            if not isinstance(entry, dict):
                return [], f"Each endpoint in group '{group_name}' must be a mapping"

            name, ep_data = _endpoint_name_and_data(entry)
            name = name or "unnamed"
            path = f"{group_name}/{name}"

            # Ensure unique paths within the group
            base_path = path
            suffix = 1
            while path in seen_paths:
                path = f"{base_path}_{suffix}"
                suffix += 1
            seen_paths.add(path)

            url = ep_data.get("url") or entry.get("url") or ""
            if not url:
                return [], f"Endpoint '{path}' is missing 'url'"

            method = (
                ep_data.get("method") or entry.get("method") or _DEFAULT_METHOD
            ).upper()
            body = ep_data.get("body") or entry.get("body") or None

            raw_headers = ep_data.get("headers") or entry.get("headers") or {}
            content_type, headers = _extract_content_type(raw_headers)

            # Step-level auth overrides group auth
            step_auth = ep_data.get("auth") or entry.get("auth")
            if isinstance(step_auth, dict):
                auth_type, auth_raw = _parse_auth_block(step_auth)
            else:
                auth_type, auth_raw = group_auth_type, group_auth_raw

            on_error = (
                ep_data.get("on_error") or entry.get("on_error") or _ON_ERROR_STOP
            ).lower()
            if on_error not in (_ON_ERROR_STOP, _ON_ERROR_CONTINUE):
                on_error = _ON_ERROR_STOP

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
                )
            )

    if not steps:
        return [], "Workflow defines no endpoints"
    return steps, None


def _validate_workflow_placeholders(steps: list, fieldnames: list) -> str | None:
    """Return an error string if any step references a missing CSV column, else None."""
    for step in steps:
        placeholders = PLACEHOLDER_RE.findall(step.url)
        if step.body:
            placeholders += PLACEHOLDER_RE.findall(step.body)
        for val in step.headers.values():
            placeholders += PLACEHOLDER_RE.findall(str(val))
        missing = [p for p in placeholders if p not in fieldnames]
        if missing:
            return f"Step '{step.path}': CSV is missing columns for placeholders: {missing}"
    return None


def _resolve_workflow_auth_headers(
    steps: list,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> tuple[dict, str | None]:
    """
    Resolve auth for all workflow steps. Returns (auth_headers_by_path, error_or_None).
    Deduplicates: same (auth_type, auth_raw) prompts only once.
    """
    resolved: dict = {}  # (auth_type, auth_raw) -> auth_header string or None
    result: dict = {}  # step.path -> auth_header string or None

    for step in steps:
        key = (step.auth_type, step.auth_raw)
        if key not in resolved:
            if step.auth_type == _AUTH_NONE:
                resolved[key] = None
            elif step.auth_type == _AUTH_BEARER:
                token = resolve_token(
                    step.auth_raw or None, suspend=suspend, resume=resume
                )
                resolved[key] = f"Bearer {token}"
            else:
                creds = resolve_basic_creds(
                    step.auth_raw or None, suspend=suspend, resume=resume
                )
                resolved[key] = f"Basic {base64.b64encode(creds.encode()).decode()}"
        result[step.path] = resolved[key]

    return result, None


def _fire_workflow_step(
    step: WorkflowStep,
    row: dict,
    auth_header: str | None,
    timeout: int,
    auth_refresh_fn: Callable | None = None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> tuple[int | None, str, float, str, str | None, str | None, dict, dict]:
    """
    Fire a single workflow step for one CSV row.
    Returns (status, body, elapsed, final_url, new_auth_header_or_None, req_body, req_headers, resp_headers).
    Returns (None, err_message, 0, "", None, None, {}, {}) on substitution error.
    """
    url, err = substitute(step.url, row)
    if err:
        return None, err, 0.0, "", None, None, {}, {}

    req_body: str | None = None
    if step.body:
        req_body, err = substitute(step.body, row)
        if err:
            return None, err, 0.0, url, None, None, {}, {}

    extra_headers: dict = {}
    for k, v in step.headers.items():
        val, herr = substitute(str(v), row)
        if herr:
            return None, herr, 0.0, url, None, None, {}, {}
        extra_headers[k] = val

    status, body, elapsed, req_headers, resp_headers = http_request(
        url,
        auth_header,
        step.method,
        req_body,
        timeout,
        step.content_type,
        extra_headers,
    )

    new_auth_header: str | None = None
    if status == 401 and step.auth_type != _AUTH_NONE:
        if auth_refresh_fn is not None:
            new_auth_header = auth_refresh_fn(auth_header)
        elif step.auth_type == _AUTH_BEARER:
            refreshed = prompt_new_token(suspend=suspend, resume=resume)
            new_auth_header = f"Bearer {refreshed}"
        else:
            refreshed = prompt_new_basic_creds(suspend=suspend, resume=resume)
            new_auth_header = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
        if new_auth_header:
            status, body, elapsed, req_headers, resp_headers = http_request(
                url,
                new_auth_header,
                step.method,
                req_body,
                timeout,
                step.content_type,
                extra_headers,
            )

    return (
        status,
        body,
        elapsed,
        url,
        new_auth_header,
        req_body,
        req_headers,
        resp_headers,
    )
