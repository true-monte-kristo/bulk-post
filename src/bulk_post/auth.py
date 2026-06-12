"""Token / basic-auth resolution and 401 refresh callbacks."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from collections.abc import Callable

from .state import _ParallelState


def resolve_token(
    flag_value: str | None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if flag_value:
        return flag_value
    env = os.environ.get("BULK_TOKEN", "").strip()
    if env:
        return env
    if suspend:
        suspend()
    try:
        token = input("Paste your Bearer token: ").strip()
    except EOFError:
        token = ""
    if resume:
        resume()
    if not token:
        print("[ERROR] No token provided.", file=sys.stderr)
        sys.exit(1)
    return token


def prompt_new_token(
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if suspend:
        suspend()
    print("\n[AUTH]  Token expired (401). Grab a fresh token from browser DevTools.")
    try:
        token = input("Paste new Bearer token: ").strip()
    except EOFError:
        token = ""
    if resume:
        resume()
    if not token:
        print("[ERROR] No token provided — aborting.", file=sys.stderr)
        sys.exit(1)
    return token


def resolve_basic_creds(
    flag_value: str | None,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if flag_value:
        return flag_value
    env = os.environ.get("BULK_USER", "").strip()
    if env:
        return env
    if suspend:
        suspend()
    try:
        creds = input("Basic auth credentials (user:pass): ").strip()
    except EOFError:
        creds = ""
    if resume:
        resume()
    if not creds:
        print("[ERROR] No credentials provided.", file=sys.stderr)
        sys.exit(1)
    return creds


def prompt_new_basic_creds(
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str:
    if suspend:
        suspend()
    print("\n[AUTH]  Credentials rejected (401). Enter new credentials.")
    try:
        creds = input("Basic auth credentials (user:pass): ").strip()
    except EOFError:
        creds = ""
    if resume:
        resume()
    if not creds:
        print("[ERROR] No credentials provided — aborting.", file=sys.stderr)
        sys.exit(1)
    return creds


def resolve_auth_header(
    args: argparse.Namespace,
    suspend: Callable | None = None,
    resume: Callable | None = None,
) -> str | None:
    if args.auth_type == "none":
        return None
    if args.auth_type == "bearer":
        token = resolve_token(args.token, suspend=suspend, resume=resume)
        return f"Bearer {token}"
    creds = resolve_basic_creds(args.user, suspend=suspend, resume=resume)
    return f"Basic {base64.b64encode(creds.encode()).decode()}"


def _make_auth_refresh_fn(
    args,
    state: _ParallelState,
    suspend: Callable | None,
    resume: Callable | None,
) -> Callable:
    """Return a thread-safe 401-refresh closure for parallel workers."""

    def refresh(old_auth_header: str | None) -> str | None:
        with state.auth_lock:
            # Another thread already refreshed while we waited for the lock.
            if state.auth_header != old_auth_header:
                return state.auth_header
            with state.output_lock:
                if suspend:
                    suspend()
                try:
                    if args.auth_type == "bearer":
                        refreshed = prompt_new_token()
                        new = f"Bearer {refreshed}"
                    else:
                        refreshed = prompt_new_basic_creds()
                        new = f"Basic {base64.b64encode(refreshed.encode()).decode()}"
                finally:
                    if resume:
                        resume()
            state.auth_header = new
            return new

    return refresh
