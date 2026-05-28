"""
Httpx event hooks that print HTTP requests and responses (for local debugging).

Use with ``anthropic.DefaultHttpxClient`` or any ``httpx.Client``::

    from anthropic import Anthropic, DefaultHttpxClient
    from common.http_print import httpx_print_event_hooks

    event_hooks = httpx_print_event_hooks()  # stdout
    event_hooks = httpx_print_event_hooks(file_path="httpx.log")  # file

Response hooks call ``response.read()`` so the body is buffered before the
SDK consumes it.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TextIO

import httpx

_DEFAULT_SENSITIVE = frozenset(
    {
        "authorization",
        "x-api-key",
        "proxy-authorization",
        "cookie",
        "set-cookie",
    }
)


def _headers_for_print(
    headers: httpx.Headers,
    *,
    redact: bool,
    sensitive: frozenset[str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if redact and key.lower() in sensitive:
            out[key] = "***"
        else:
            out[key] = value
    return out


def _format_body_preview(raw: bytes, *, max_chars: int) -> str:
    if not raw:
        return "(empty body)"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary {len(raw)} bytes>"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... ({len(text) - max_chars} more chars)"
    return text


def _maybe_pretty_json(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return text


def _print_block(title: str, body: str, *, stream: TextIO) -> None:
    line = "=" * 72
    print(f"\n{line}\n{title}\n{line}", file=stream)
    print(body.rstrip() if body else "", file=stream)
    print(file=stream)
    stream.flush()


def _append_block_to_file(title: str, body: str, *, file_path: str | os.PathLike[str]) -> None:
    parent = os.path.dirname(os.fspath(file_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as stream:
        _print_block(title, body, stream=stream)


def build_httpx_event_hooks(destination: str | None) -> dict[str, list]:
    destination = (destination or "").strip()
    if not destination:
        return {}
    if destination.upper() == "STDIO":
        return httpx_print_event_hooks()
    return httpx_print_event_hooks(file_path=destination)


def httpx_print_event_hooks(
    *,
    stream: TextIO | None = None,
    file_path: str | os.PathLike[str] | None = None,
    max_body_chars: int = 48_000,
    redact_headers: bool = True,
    sensitive_headers: frozenset[str] | None = None,
    pretty_json: bool = True,
) -> dict[str, list]:
    """
    Build ``event_hooks`` for ``httpx.Client(..., event_hooks=...)``.

    Parameters
    ----------
    stream
        Where to print (default: ``sys.stdout``).
    file_path
        Append logs to this file instead of printing to ``stream``.
    max_body_chars
        Truncate request/response bodies after this many characters.
    redact_headers
        Mask values for common auth-related headers.
    sensitive_headers
        Lowercase header names to redact; merged with a built-in set.
    pretty_json
        If ``Content-Type`` is JSON, try to pretty-print the body.
    """
    if stream is not None and file_path is not None:
        raise ValueError("stream and file_path cannot both be set")

    out: TextIO = stream or sys.stdout
    sensitive = _DEFAULT_SENSITIVE | (sensitive_headers or frozenset())

    def write_block(title: str, body: str) -> None:
        if file_path is not None:
            _append_block_to_file(title, body, file_path=file_path)
        else:
            _print_block(title, body, stream=out)

    def on_request(request: httpx.Request) -> None:
        hdrs = _headers_for_print(request.headers, redact=redact_headers, sensitive=sensitive)
        lines = [
            f"{request.method} {request.url}",
            "",
            "Headers:",
        ]
        for k, v in hdrs.items():
            lines.append(f"  {k}: {v}")
        body = request.content
        lines.extend(["", "Body:"])
        if body:
            preview = _format_body_preview(body, max_chars=max_body_chars)
            ct = request.headers.get("content-type", "")
            if pretty_json and "json" in ct.lower():
                preview = _maybe_pretty_json(preview)
            lines.append(preview)
        else:
            lines.append("(empty body)")
        write_block("HTTP REQUEST", "\n".join(lines))

    def on_response(response: httpx.Response) -> None:
        response.read()
        hdrs = _headers_for_print(response.headers, redact=redact_headers, sensitive=sensitive)
        lines = [
            f"{response.status_code} {response.reason_phrase}",
            f"{response.request.method} {response.request.url}",
            "",
            "Headers:",
        ]
        for k, v in hdrs.items():
            lines.append(f"  {k}: {v}")
        raw = response.content or b""
        lines.extend(["", "Body:"])
        preview = _format_body_preview(raw, max_chars=max_body_chars)
        ct = response.headers.get("content-type", "")
        if pretty_json and "json" in ct.lower():
            preview = _maybe_pretty_json(preview)
        lines.append(preview)
        write_block("HTTP RESPONSE", "\n".join(lines))

    return {
        "request": [on_request],
        "response": [on_response],
    }
