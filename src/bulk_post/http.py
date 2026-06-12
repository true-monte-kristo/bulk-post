"""HTTP request execution and header masking."""

from __future__ import annotations

import time
import urllib.error
import urllib.request


def http_request(
    url: str,
    auth_header: str | None,
    method: str,
    body: str | None,
    timeout: int = 30,
    content_type: str = "application/json",
    extra_headers: dict | None = None,
) -> tuple[int | None, str, float, dict, dict]:
    """Returns (status_or_None, response_body, elapsed_seconds, req_headers, resp_headers)."""
    encoded_body = body.encode("utf-8") if body else None
    req_headers: dict = {}
    if extra_headers:
        req_headers.update(extra_headers)
    if auth_header:
        req_headers["Authorization"] = auth_header
    if encoded_body:
        req_headers["Content-Type"] = content_type
    req = urllib.request.Request(
        url, data=encoded_body, method=method, headers=req_headers
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = dict(resp.headers.items())
            return (
                resp.status,
                response_body,
                time.monotonic() - t0,
                req_headers,
                resp_headers,
            )
    except urllib.error.HTTPError as e:
        resp_headers = dict(e.headers.items()) if e.headers else {}
        return (
            e.code,
            e.read().decode("utf-8", errors="replace"),
            time.monotonic() - t0,
            req_headers,
            resp_headers,
        )
    except urllib.error.URLError as e:
        return (
            None,
            f"Connection error: {e.reason}",
            time.monotonic() - t0,
            req_headers,
            {},
        )
    except TimeoutError:
        return (
            None,
            f"Request timed out ({timeout}s)",
            time.monotonic() - t0,
            req_headers,
            {},
        )


def _mask_headers(headers: dict) -> dict:
    return {
        k: "*****" if k.lower() == "authorization" else v for k, v in headers.items()
    }
