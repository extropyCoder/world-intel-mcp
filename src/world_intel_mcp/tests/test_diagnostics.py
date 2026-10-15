"""Offline checks of rendered diagnostic sinks, not just sanitizer helpers."""

import ast
import copy
import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from world_intel_mcp.diagnostics import (
    exception_summary, redact_arguments, redact_text, safe_error, safe_url,
    sanitize_error_fields, suppress_transport_logs,
)

_PATH = "DUMMY_FIRMS_PATH_CREDENTIAL"
_QUERY = "DUMMY_QUERY_CREDENTIAL"
_USER = "DUMMY_USER_CREDENTIAL"
_PASSWORD = "DUMMY_PASSWORD_CREDENTIAL"
_HEADER = "DUMMY_AUTHORIZATION_CREDENTIAL"
_URL = f"https://{_USER}:{_PASSWORD}@service.test/api/area/csv/{_PATH}/VIIRS_SNPP_NRT/world/1?%41PI%5fKEY={_QUERY}#private"
_SECRETS = (_PATH, _QUERY, _USER, _PASSWORD, _HEADER)


def _assert_absent(text):
    for secret in _SECRETS:
        assert secret not in text


@pytest.mark.parametrize("key", ["api_key", "API_KEY", "%61pi%5fkey", "key", "ToKeN", "unrecognized"])
def test_url_policy_omits_all_paths_queries_fragments_and_userinfo(key):
    url = f"https://{_USER}:{_PASSWORD}@service.test:8443/api/{_PATH}?{key}={_QUERY}#private"
    assert safe_url(url) == "https://service.test:8443"
    result = redact_text(f"request failed for '{url}'")
    _assert_absent(result)
    assert "service.test" in result


@pytest.mark.parametrize("value", [
    f"Authorization: Bearer {_HEADER}", f"authorization='Bearer {_HEADER}'",
    f"Proxy-Authorization: Basic {_HEADER}", f"Bearer {_HEADER}",
    f"api_key={_QUERY}", f"NASA_FIRMS_API_KEY='{_PATH}'",
    f'{{"client_secret": "{_PASSWORD}", "access_token": "{_QUERY}"}}',
    f"password={_PASSWORD}", f"Cookie: session={_HEADER}",
    f"Cookie: first={_QUERY}; second={_HEADER}",
    f"password=prefix {_PASSWORD} suffix",
    f'password="prefix\\"{_PASSWORD}"',
])
def test_common_authentication_forms_are_scrubbed(value):
    result = redact_text(value)
    _assert_absent(result)
    assert "REDACTED" in result


def test_nested_arguments_are_copied_before_redaction():
    arguments = {"region": "europe", "api_key": _PATH, "url": _URL,
                 "nested": [{"API%5fKEY": _QUERY, "headers": {"Authorization": _HEADER},
                             "credentials": {"password": _PASSWORD}},
                            {"options": {"refresh_token": _QUERY, "X-API-Key": _PATH}}]}
    original = copy.deepcopy(arguments)
    redacted = redact_arguments(arguments)
    _assert_absent(json.dumps(redacted))
    assert redacted["region"] == "europe"
    assert arguments == original
    assert redact_text("ordinary unlabelled free text") == "ordinary unlabelled free text"


@pytest.mark.parametrize("key", ["API_KEY", "apiKey", "api-key", "X-API-Key", "NASA_FIRMS_API_KEY",
                                "%61pi%5Fkey", "%2561pi%255Fkey", "Authorization",
                                "proxy-authorization", "password", "client_secret", "access_token",
                                "session_token", "Cookie", "username", "credentials"])
def test_structured_credential_labels_are_case_and_encoding_insensitive(key):
    original = {"nested": [{key: _QUERY}]}
    assert _QUERY not in json.dumps(redact_arguments(original))
    assert original["nested"][0][key] == _QUERY


def test_error_fields_only_are_changed_in_output_copy():
    data = {"error": f"failed: {_URL}", "nested": [{"errors": [f"Bearer {_HEADER}"]}],
            "normal_data": {"url": _URL}, "has_error": False}
    original = copy.deepcopy(data)
    result = sanitize_error_fields(data)
    _assert_absent(result["error"] + result["nested"][0]["errors"][0])
    assert result["normal_data"]["url"] == _URL  # not a rewrite of normal data/cache
    assert result["has_error"] is False
    assert data == original


@pytest.mark.parametrize("field", ["error", "_error", "errors"])
def test_structured_error_credentials_keep_key_context_and_normal_data(field):
    error = {"message": "upstream failed", "headers": [{"Authorization": _HEADER}],
             "api_key": {"value": _QUERY}, "credentials": [{"password": _PASSWORD}],
             "nested": [{"API%5fKEY": _PATH, "Auth%6Frization": [_HEADER]}],
             "status": 403}
    data = {field: [error], "normal_data": copy.deepcopy(error)}
    original = copy.deepcopy(data)
    result = sanitize_error_fields(data)
    _assert_absent(json.dumps(result[field]))
    assert result[field][0]["message"] == "upstream failed"
    assert result[field][0]["status"] == 403
    assert result[field][0]["headers"][0]["Authorization"] == "[REDACTED]"
    assert result[field][0]["api_key"] == "[REDACTED]"
    assert result[field][0]["credentials"] == "[REDACTED]"
    assert result["normal_data"] == original["normal_data"]
    assert data == original


def test_http_error_uses_metadata_not_exception_message():
    request = httpx.Request("GET", _URL, headers={"Authorization": _HEADER})
    error = httpx.HTTPStatusError(f"raw body {_HEADER}", request=request,
                                  response=httpx.Response(403, request=request))
    result = exception_summary(error)
    _assert_absent(result)
    assert "HTTPStatusError" in result and "status=403" in result and "service.test" in result
    assert safe_error(httpx.ConnectError(_HEADER)) == "ConnectError"


def test_cache_write_failure_does_not_log_credential_bearing_keys(cache, monkeypatch, caplog):
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("readonly database")
    monkeypatch.setattr(cache, "_get_conn", lambda: connection)
    key = "source:" + _URL
    with caplog.at_level(logging.WARNING):
        cache.set(key, {"value": 42}, 60)
    _assert_absent(caplog.text)
    assert "Cache write failed" in caplog.text and "OperationalError" in caplog.text
    assert connection.execute.call_args.args[1][0] == key


def test_third_party_policy_is_logger_scoped_and_independent_of_root_levels(caplog):
    factory = logging.getLogRecordFactory()
    suppress_transport_logs()
    with caplog.at_level(logging.DEBUG):
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                     "httpcore.http2", "httpcore.proxy", "httpcore.socks",
                     "mcp.server.lowlevel.server"):
            logging.getLogger(name).warning("request=%s headers=%s", _URL, _HEADER)
        logging.getLogger("operator.custom").info("ordinary diagnostics still work")
    _assert_absent(caplog.text)
    assert "ordinary diagnostics still work" in caplog.text
    assert logging.getLogRecordFactory() is factory


def test_transport_suppression_can_be_removed_without_altering_other_configuration(caplog):
    logger = logging.getLogger("httpx")
    own_filter = logging.Filter()
    logger.addFilter(own_filter)
    factory = logging.getLogRecordFactory()
    handlers = tuple(logger.handlers)
    level = logger.level
    try:
        suppress_transport_logs(enabled=False)
        assert own_filter in logger.filters
        with caplog.at_level(logging.WARNING):
            logger.warning("operator-managed diagnostics enabled")
        assert "operator-managed diagnostics enabled" in caplog.text
        suppress_transport_logs()
        suppress_transport_logs()  # idempotent, no duplicate filters
        count = len(logger.filters)
        caplog.clear()
        logger.warning("suppressed diagnostics")
        assert "suppressed diagnostics" not in caplog.text
        suppress_transport_logs()
        assert len(logger.filters) == count
        assert tuple(logger.handlers) == handlers and logger.level == level
        assert logging.getLogRecordFactory() is factory
        assert own_filter in logger.filters
    finally:
        logger.removeFilter(own_filter)
        suppress_transport_logs()


def test_project_logger_calls_do_not_format_raw_caught_exceptions():
    """Guard the audited catch/logger pattern in addition to runtime sink tests.

    This is not an audit of arbitrary data logging, third-party code or secret
    formats; it prevents another generic exception wrapper bypassing helpers.
    """
    helpers = {"exception_summary", "safe_error", "redact_text", "sanitize_error_fields"}

    def contains_raw_exception(node, name):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
            return False
        return (isinstance(node, ast.Name) and node.id == name) or any(
            contains_raw_exception(child, name) for child in ast.iter_child_nodes(node)
        )

    unsafe = []
    for path in Path(__file__).resolve().parents[1].rglob("*.py"):
        if "tests" in path.parts:
            continue
        for handler in ast.walk(ast.parse(path.read_text())):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            for node in ast.walk(handler):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in {"logger", "logging"}
                    and any(contains_raw_exception(arg, handler.name) for arg in node.args)):
                    unsafe.append(f"{path.name}:{node.lineno}")
    assert unsafe == []


@pytest.mark.parametrize("kind", ["json", "text", "xml", "bytes"])
@pytest.mark.parametrize("failure", ["status", "read"])
@pytest.mark.parametrize("auth", ["header", "userinfo"])
async def test_fetcher_redirect_retry_stale_and_httpx_logs_are_secret_safe(
    fetcher, monkeypatch, caplog, kind, failure, auth
):
    from world_intel_mcp.fetcher import _source_last_call

    userinfo = f"{_USER}:{_PASSWORD}@" if auth == "userinfo" else ""
    url = f"https://{userinfo}service.test/api/area/csv/{_PATH}/start?%41pi%5Fkey={_QUERY}"
    target = f"https://service.test/api/{_PATH}/final?token={_QUERY}"
    headers = {"Authorization": f"Bearer {_HEADER}"} if auth == "header" else {}
    original_headers = dict(headers)
    seen = []
    fetcher.max_retries = 1
    fetcher.vector_store = Mock(store=AsyncMock())
    monkeypatch.setattr("world_intel_mcp.fetcher.asyncio.sleep", AsyncMock())

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError(f"{_URL} Authorization: Bearer {_HEADER}", request=seen[-1])
            yield b""  # async generator

    def respond(request):
        seen.append(request)
        # Model standard core DEBUG traces with headers, even though the offline
        # transport never connects a real httpcore socket.
        logging.getLogger("httpcore.http11").debug("headers=%r", request.headers)
        if request.url.path.endswith("/start"):
            return httpx.Response(302, headers={"location": target})
        if failure == "read":
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(503, text="upstream failed")

    # Default keys include the authenticated URL: serving stale data must not
    # log those keys, and must not change the stored cache value.
    key = f"nasa-firms:{url}:None" if kind == "json" else f"nasa-firms:text:{url}:None"
    if kind != "bytes":
        fetcher.cache.set(key, {"known": "good"} if kind == "json" else "old text", -10)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
            fetcher._client = client
            result = await getattr(fetcher, f"get_{kind}")(url, "nasa-firms", **(
                {"headers": headers} if kind != "xml" else {}
            ))
    _assert_absent(caplog.text)
    assert "service.test" in caplog.text and "nasa-firms" in caplog.text
    assert ("HTTPStatusError" if failure == "status" else "ReadError") in caplog.text
    if failure == "status":
        assert "503" in caplog.text
    assert len(seen) == 4  # redirect and final response on each attempt
    assert _PATH in seen[0].url.path and _QUERY in str(seen[0].url.query)
    if auth == "header" and kind != "xml":
        assert seen[0].headers["authorization"] == f"Bearer {_HEADER}"
    if auth == "userinfo":
        assert seen[0].url.username == _USER and seen[0].url.password == _PASSWORD
        assert seen[0].headers["authorization"].startswith("Basic ")
    if "authorization" in seen[0].headers:
        assert seen[0].headers["authorization"] not in caplog.text
    assert headers == original_headers
    if kind == "bytes":
        _assert_absent(result.error)
        assert not result.ok
    else:
        assert "Serving stale cache" in caplog.text
        if kind == "json":
            assert result["known"] == "good" and result["_stale"] is True
        else:
            assert result == "old text"
    fetcher.vector_store.store.assert_not_called()
    assert "nasa-firms" in _source_last_call


@pytest.mark.parametrize("mode", ["raise", "return-error", "return-structured-error"])
async def test_dashboard_vector_failure_has_no_raw_traceback_or_secret_error(monkeypatch, caplog, mode):
    from world_intel_mcp.dashboard import app as dashboard

    search = AsyncMock()
    if mode == "raise":
        search.side_effect = RuntimeError(f"failed {_URL} Authorization: Bearer {_HEADER}")
    elif mode == "return-error":
        search.return_value = {"error": f"HTTP failure {_URL} Authorization: Bearer {_HEADER}"}
    else:
        search.return_value = {"error": {"message": f"failed {_URL}", "api_key": {"value": _QUERY},
                                         "headers": [{"Auth%6Frization": _HEADER}],
                                         "credentials": [{"password": _PASSWORD}]}}
    original = copy.deepcopy(search.return_value) if mode != "raise" else None
    monkeypatch.setattr(dashboard, "_ensure_fetcher", Mock())
    monkeypatch.setattr(dashboard, "_vector_store", Mock(semantic_search=search))
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app),
                                    base_url="http://localhost:8501") as client:
            response = await client.post("/api/vector-search", json={"query": "test"})
    assert response.status_code == 500 and response.json() == {"error": "Vector search failed"}
    _assert_absent(caplog.text + response.text)
    assert "Vector search failed" in caplog.text and "service.test" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    if mode != "raise":
        assert search.return_value == original


async def test_webhook_failure_response_is_secret_safe(fetcher, monkeypatch, caplog):
    from world_intel_mcp.analysis.aoi import _notify_aoi_changes

    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _URL)
    seen = []

    def respond(request):
        seen.append(request)
        raise httpx.ConnectError(f"request {_URL} Authorization: Bearer {_HEADER}", request=request)

    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            fetcher._client = client
            result = await _notify_aoi_changes(fetcher, {"totals": {"new_items": 1}})
    _assert_absent(json.dumps(result) + caplog.text)
    assert result["sent"] is False and "ConnectError" in result["error"]
    assert seen[0].url.password == _PASSWORD


@pytest.mark.parametrize("path", ["/api/overview", "/api/stream", "/sse/vector-analytics"])
async def test_dashboard_source_error_fields_are_sanitized_without_mutating_data(monkeypatch, path):
    from starlette.responses import StreamingResponse
    from world_intel_mcp.dashboard import app as dashboard

    data = {"source": {"error": {
        "message": f"failed {_URL}", "headers": {"Authorization": _HEADER},
        "api_key": {"value": _QUERY}, "credentials": [{"password": _PASSWORD}],
        "nested": [{"Auth%6Frization": [_HEADER], "API%5fKEY": _PATH}],
        "status": 403,
    }}, "normal_data": {"api_key": "ordinary-data", "headers": {"Authorization": "unchanged"}}}
    original = copy.deepcopy(data)
    producer = AsyncMock(return_value=data)
    monkeypatch.setattr(dashboard, "_ensure_fetcher", Mock())
    monkeypatch.setattr(dashboard, "_fetch_overview", producer)
    monkeypatch.setattr(dashboard, "_fetch_vector_analytics", producer)
    monkeypatch.setattr(dashboard, "_stream_source_defs", lambda _: {"stub": producer})

    def finite_response(iterator, **kwargs):
        async def first_event():
            try:
                yield await anext(iterator)
            finally:
                await iterator.aclose()
        return StreamingResponse(first_event(), **kwargs)

    monkeypatch.setattr(dashboard, "StreamingResponse", finite_response)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app),
                                base_url="http://localhost:8501") as client:
        response = await client.get(path)
    assert response.status_code == 200
    _assert_absent(response.text)
    assert "service.test" in response.text
    assert "ordinary-data" in response.text and "unchanged" in response.text
    assert "403" in response.text
    assert data == original


async def test_real_vector_search_error_wrapper_is_secret_safe(monkeypatch):
    from world_intel_mcp.vector_store import VectorStore

    request = httpx.Request("GET", _URL)
    error = httpx.HTTPStatusError(_HEADER, request=request,
                                  response=httpx.Response(403, request=request))
    store = VectorStore(enabled=False)
    monkeypatch.setattr(store, "_search_sync", Mock(side_effect=error))
    result = await store.semantic_search("safe query")
    _assert_absent(json.dumps(result))
    assert "HTTPStatusError" in result["error"] and "403" in result["error"]


@pytest.mark.parametrize("wrapper", ["report", "brief", "digest", "dossier", "fleet", "aoi",
                                   "alerts", "posture", "exposure", "company", "macro", "world-brief"])
async def test_source_error_wrappers_do_not_log_or_return_authenticated_urls(caplog, wrapper):
    from world_intel_mcp.reports import _safe_fetch
    from world_intel_mcp.analysis import situation, daily_digest, dossier
    from world_intel_mcp.analysis import aoi, alerts, posture, exposure, company, macro_composite, world_brief
    from world_intel_mcp.sources import fleet

    request = httpx.Request("GET", _URL)
    async def fail():
        raise httpx.HTTPStatusError(_HEADER, request=request,
                                    response=httpx.Response(403, request=request))

    with caplog.at_level(logging.WARNING):
        if wrapper == "report":
            result = await _safe_fetch("wildfire", fail)
        else:
            fn = {"brief": situation._safe_fetch, "digest": daily_digest._safe_fetch,
                  "dossier": dossier._safe, "fleet": fleet._safe, "aoi": aoi._safe_fetch,
                  "alerts": alerts._safe_fetch, "posture": posture._safe, "exposure": exposure._safe,
                  "company": company._safe, "macro": macro_composite._safe,
                  "world-brief": world_brief._safe}[wrapper]
            result = await fn(fail(), "wildfire")
    _assert_absent(json.dumps(result) + caplog.text)
    assert "HTTPStatusError" in caplog.text and "403" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_direct_oauth_logs_are_safe_and_original_credentials_are_sent(monkeypatch, caplog):
    import asyncio
    import respx
    from world_intel_mcp.sources import conflict

    monkeypatch.setenv("ACLED_EMAIL", _USER)
    monkeypatch.setenv("ACLED_PASSWORD", _PASSWORD)
    monkeypatch.setattr(conflict, "_ACLED_TOKEN_URL", _URL)
    monkeypatch.setattr(conflict, "_acled_token", None)
    monkeypatch.setattr(conflict, "_acled_refresh_token", None)
    monkeypatch.setattr(conflict, "_acled_token_lock", asyncio.Lock())
    with caplog.at_level(logging.DEBUG), respx.mock as router:
        route = router.post(_URL).respond(403)
        assert await conflict._acled_get_token() is None
    _assert_absent(caplog.text)
    assert "ACLED" in caplog.text and "HTTPStatusError" in caplog.text and "403" in caplog.text
    request = route.calls.last.request
    assert f"username={_USER}" in request.content.decode()
    assert f"password={_PASSWORD}" in request.content.decode()
