"""Placeholder substitution and template/placeholder validation."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as _ET

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def substitute(template: str, row: dict) -> tuple[str, str | None]:
    missing = [p for p in PLACEHOLDER_RE.findall(template) if p not in row]
    if missing:
        return template, f"Missing CSV columns for placeholders: {missing}"
    return PLACEHOLDER_RE.sub(lambda m: row[m.group(1)], template), None


def _validate_body_template(template: str, content_type: str) -> str | None:
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


def _validate_placeholders(args: argparse.Namespace, fieldnames: list | None) -> None:
    header_val_placeholders: list = []
    for raw in args.header or []:
        if ": " not in raw:
            print(
                f"[ERROR] --header value must be in 'Name: value' format, got: {raw!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        _, _, val_tmpl = raw.partition(": ")
        header_val_placeholders += PLACEHOLDER_RE.findall(val_tmpl)

    all_placeholders = (
        PLACEHOLDER_RE.findall(args.url)
        + (PLACEHOLDER_RE.findall(args.body) if args.body else [])
        + header_val_placeholders
    )
    if not all_placeholders:
        return
    missing = [p for p in all_placeholders if p not in (fieldnames or [])]
    if missing:
        print(
            f"[ERROR] CSV is missing columns required by placeholders: {missing}",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.body:
        err = _validate_body_template(args.body, args.content_type)
        if err:
            print(f"[ERROR] {err}", file=sys.stderr)
            sys.exit(1)
