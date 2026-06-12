"""Placeholder substitution and template/placeholder validation."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as _ET

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def substitute(template: str, row: dict) -> tuple[str, str | None]:
    """Replace every ``{{col}}`` in ``template`` with ``row[col]``.

    Returns ``(result, None)`` on success, or ``(template, error)`` if the row
    is missing any referenced column.
    """
    missing = [p for p in PLACEHOLDER_RE.findall(template) if p not in row]
    if missing:
        return template, f"Missing CSV columns for placeholders: {missing}"
    return PLACEHOLDER_RE.sub(lambda m: row[m.group(1)], template), None


def _validate_body_template(template: str, content_type: str) -> str | None:
    """Check that the body template is structurally valid for its content type.

    Substitutes placeholders with ``null`` and parses the result as JSON or XML
    (selected by ``content_type``). Returns an error string, or ``None`` if valid
    or the type isn't JSON/XML.
    """
    dummy = PLACEHOLDER_RE.sub("null", template)
    ct = content_type.lower()
    if "json" in ct:
        try:
            json.loads(dummy)
        except json.JSONDecodeError as e:
            return f"Invalid JSON body template: {e}"
    elif "xml" in ct:
        try:
            _ET.fromstring(dummy)
        except _ET.ParseError as e:
            return f"Invalid XML body template: {e}"
    return None


def _validate_placeholders(
    args: argparse.Namespace, fieldnames: list | None
) -> str | None:
    """Validate single-URL placeholders against the CSV header.

    Checks ``--header`` format, ensures every ``{{col}}`` in the URL, body, and
    header values has a matching CSV column, and validates the body template.
    Returns an error string, or ``None`` if everything checks out.
    """
    header_val_placeholders: list = []
    for raw in args.header or []:
        if ": " not in raw:
            return f"--header value must be in 'Name: value' format, got: {raw!r}"
        _, _, val_tmpl = raw.partition(": ")
        header_val_placeholders += PLACEHOLDER_RE.findall(val_tmpl)

    all_placeholders = (
        PLACEHOLDER_RE.findall(args.url)
        + (PLACEHOLDER_RE.findall(args.body) if args.body else [])
        + header_val_placeholders
    )
    if not all_placeholders:
        return None
    missing = [p for p in all_placeholders if p not in (fieldnames or [])]
    if missing:
        return f"CSV is missing columns required by placeholders: {missing}"
    if args.body:
        err = _validate_body_template(args.body, args.content_type)
        if err:
            return err
    return None
