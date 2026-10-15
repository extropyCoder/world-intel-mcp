"""Explicit, credential-aware diagnostics; not a general-purpose secret scanner.

URL diagnostics intentionally omit *all* path/query/fragment/userinfo, covering
FIRMS path keys and encoded query-key variants without guessing their names.
Ordinary result data and actual request/tool argument objects are never edited.
"""

import logging
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

_REDACTED = "[REDACTED]"
_URL = re.compile(r"\b(?:https?|ftp)://[^\s<>]+", re.IGNORECASE)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/%=-]+", re.IGNORECASE)
_HEADER = re.compile(
    r"([\"']?(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\r\n]+)", re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"([\"']?(?:[a-z0-9_-]*api[-_]?key|[a-z0-9_-]*token|[a-z0-9_-]*password|"
    r"[a-z0-9_-]*secret|passwd|pwd|map[-_]?key|subscription[-_]?key|auth|key)"
    r"[\"']?\s*[:=]\s*)(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^,;}\]\r\n]+)",
    re.IGNORECASE,
)


def safe_url(value: Any) -> str:
    """Return only a validated scheme/hostname/port, never URL credentials."""
    try:
        url = urlsplit(str(value))
        host = url.hostname or ""
        if url.scheme.lower() not in ("http", "https", "ftp") or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
            return "[redacted-url]"
        port = url.port
        host = f"[{host}]" if ":" in host else host
        return f"{url.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")
    except (ValueError, TypeError):
        return "[redacted-url]"


def redact_text(value: Any) -> str:
    """Scrub URLs and common labelled credentials, not arbitrary free text."""
    text = _URL.sub(lambda match: safe_url(match[0]), str(value))
    text = _HEADER.sub(lambda match: match[1] + _REDACTED, text)
    text = _ASSIGNMENT.sub(lambda match: match[1] + _REDACTED, text)
    return _AUTH.sub(lambda match: match[1] + " " + _REDACTED, text)


def _sensitive_key(value: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", unquote(unquote(value)).casefold())
    return key in {"key", "auth", "credentials", "passwd", "pwd", "mapkey", "subscriptionkey",
                   "username", "user", "login", "accesskey", "accesskeyid", "sessionid"} or key.endswith(
        ("apikey", "token", "password", "secret", "authorization", "cookie")
    )


def redact_arguments(value: Any, _depth: int = 0) -> Any:
    """Copy structured arguments for logging, masking credential-labelled values."""
    if _depth >= 12:
        return "[omitted nested value]"
    if isinstance(value, dict):
        return {key: _REDACTED if _sensitive_key(str(key)) else redact_arguments(item, _depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_arguments(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def safe_error(exc: Any) -> str:
    """HTTP errors use metadata only; other diagnostic text is explicitly scrubbed.

    Never format traceback/chained exceptions: their rendered strings can contain
    complete authenticated URLs even when the log message itself is sanitized.
    """
    if isinstance(exc, httpx.HTTPError):
        parts = [type(exc).__name__]
        if isinstance(exc, httpx.HTTPStatusError):
            parts.append(f"status={exc.response.status_code}")
        try:
            parts.append(f"host={safe_url(exc.request.url)}")
        except RuntimeError:
            pass  # exceptions may have no attached request
        return " ".join(parts)
    return redact_text(exc)


def exception_summary(exc: BaseException) -> str:
    message = safe_error(exc)
    return message if isinstance(exc, httpx.HTTPError) else f"{type(exc).__name__}: {message}"


def sanitize_error_fields(value: Any, *, _error: bool = False) -> Any:
    """Copy only known error fields for output; normal result data stays intact."""
    if isinstance(value, dict):
        return {key: _REDACTED if _error and _sensitive_key(str(key)) else
                sanitize_error_fields(item, _error=_error or key in ("error", "_error", "errors"))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_error_fields(item, _error=_error) for item in value]
    return safe_error(value) if _error and isinstance(value, (str, BaseException)) else value


class _SuppressTransportDiagnostics(logging.Filter):
    def filter(self, record):
        return False


def suppress_transport_logs(*, enabled: bool = True) -> None:
    """Suppress HTTPX/core traces and the MCP SDK's raw request/exception logger.

    This deliberately touches only these third-party loggers, not root handlers,
    log-record factories or arbitrary application records. Custom trace callbacks
    and newly named third-party loggers are outside this policy. ``enabled=False``
    removes only our filters: operators must first supply their own safe logging
    policy, since raw transport logs can contain credentials.
    """
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                 "httpcore.http2", "httpcore.proxy", "httpcore.socks",
                 "mcp.server.lowlevel.server"):
        logger = logging.getLogger(name)
        if not enabled:
            for item in tuple(logger.filters):
                if isinstance(item, _SuppressTransportDiagnostics):
                    logger.removeFilter(item)
        elif not any(isinstance(item, _SuppressTransportDiagnostics) for item in logger.filters):
            logger.addFilter(_SuppressTransportDiagnostics())
