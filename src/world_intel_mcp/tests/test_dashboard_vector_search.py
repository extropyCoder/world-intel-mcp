"""Vector-search validation through the full guarded ASGI application, offline."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from world_intel_mcp.dashboard import app as dashboard
from world_intel_mcp.vector_store import DOMAIN_CATEGORIES


@pytest.fixture
def service(monkeypatch):
    initialize = Mock(return_value=object())
    search = AsyncMock(return_value={"results": [], "count": 0})
    monkeypatch.setattr(dashboard, "_ensure_fetcher", initialize)
    monkeypatch.setattr(dashboard, "_vector_store", SimpleNamespace(semantic_search=search))
    return initialize, search


@pytest.fixture
async def client():
    # No lifespan: do not initialize actual caches, embedding models or Qdrant.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dashboard.app),
        base_url="http://127.0.0.1:18501",
        headers={"origin": "http://127.0.0.1:18501"},
    ) as client:
        yield client


def _assert_error(response, status):
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    assert isinstance(response.json()["error"], str)
    assert not any(name.startswith("access-control-") for name in response.headers)


_INVALID = [None, [], "query", 1, 1.5, True, {}] + [
    {"query": value} for value in (None, True, 1, 1.5, [], {}, "", " \t\n", "x" * 2049,
                                  " " * 2048 + "x")
] + [
    {"query": "ok", "limit": value} for value in (
        None, True, False, 0, -1, 51, 1.0, 1.5, "20", [], {}, float("nan"),
        float("inf"), 10 ** 3000)
] + [
    {"query": "ok", "hours": value} for value in (
        True, False, 0, -1, 8760.001, "24", "", [], {}, float("nan"),
        float("inf"), -float("inf"), 10 ** 3000)
] + [
    {"query": "ok", "category": value} for value in (
        True, False, 0, 1.5, [], {}, "x" * 129)
]


@pytest.mark.parametrize("body", _INVALID, ids=[f"invalid-{i}" for i in range(len(_INVALID))])
async def test_invalid_inputs_precede_initialization(client, service, body):
    initialize, search = service
    # Explicit encoding permits nonstandard NaN/Infinity literals to reach the
    # ASGI app; httpx's json= encoder correctly refuses to emit them itself.
    response = await client.post("/api/vector-search", content=json.dumps(body))
    _assert_error(response, 400)
    initialize.assert_not_called()
    search.assert_not_called()


@pytest.mark.parametrize("body", [
    b"", b"{", b'{"query":"ok",}', b'{"query":"ok"} trailing', b"\xff",
    b'{"query":"unescaped\x01control"}',
    b'{"query":"ok","extra":' + b"[" * 2000 + b"0",  # incomplete nesting
    b'{"query":"ok","hours":1e9999}',
    b'{"query":"ok","ignored":NaN}',
    b'{"query":"ok","ignored":Infinity}',
    b'{"query":"ok","ignored":-Infinity}',
    b'{"query":"ok","ignored":1e9999}',
], ids=["empty", "incomplete", "trailing-comma", "trailing-text", "invalid-utf8",
        "control", "incomplete-nesting", "infinite-hours", "nan", "infinity",
        "negative-infinity", "infinite-extra"])
async def test_malformed_or_nonfinite_json_is_400(client, service, body):
    initialize, search = service
    _assert_error(await client.post("/api/vector-search", content=body), 400)
    initialize.assert_not_called()
    search.assert_not_called()


class _BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


@pytest.mark.parametrize("length", [None, "1", "999999999"])
async def test_body_limit_stops_stream_early_without_trusting_length(client, service, length):
    initialize, search = service
    prefix = b'{"query":"'
    stream = _BodyStream([
        prefix, b"x" * (dashboard._VECTOR_SEARCH_MAX_BODY_BYTES - len(prefix)),
        b"x", b'"}never consumed',
    ])
    headers = {} if length is None else {"content-length": length}
    response = await client.post("/api/vector-search", content=stream, headers=headers)
    _assert_error(response, 413)
    assert stream.reads == 3
    initialize.assert_not_called()
    search.assert_not_called()


@pytest.mark.parametrize("extra_byte", [False, True])
async def test_exact_body_boundary(client, service, extra_byte):
    body = b'{"query":"ok"}'
    body += b" " * (dashboard._VECTOR_SEARCH_MAX_BODY_BYTES - len(body) + extra_byte)
    stream = _BodyStream([body[:8000], body[8000:]])
    response = await client.post("/api/vector-search", content=stream)
    initialize, search = service
    if extra_byte:
        _assert_error(response, 413)
        initialize.assert_not_called()
        search.assert_not_called()
    else:
        assert response.status_code == 200
        search.assert_awaited_once()


@pytest.mark.parametrize("body,expected", [
    ({"query": "  test \n"}, {"query": "test", "limit": 20, "hours": None, "category": None}),
    ({"query": "x", "limit": 1, "hours": 0.5, "category": "Financial Markets"},
     {"query": "x", "limit": 1, "hours": 0.5, "category": "Financial Markets"}),
    ({"query": "x", "limit": 50, "hours": 8760, "category": "x" * 128},
     {"query": "x", "limit": 50, "hours": 8760.0, "category": "x" * 128}),
    ({"query": "x", "category": "", "hours": None},
     {"query": "x", "limit": 20, "hours": None, "category": ""}),
    ({"query": "x", "category": None, "hours": 1e-300, "ignored": "compatible"},
     {"query": "x", "limit": 20, "hours": 1e-300, "category": None}),
    ({"query": "x", "category": "Custom & Future Category"},
     {"query": "x", "limit": 20, "hours": None, "category": "Custom & Future Category"}),
])
async def test_valid_arguments_and_defaults(client, service, body, expected):
    response = await client.post("/api/vector-search", json=body)
    assert response.status_code == 200
    assert response.json() == {"results": [], "count": 0}
    initialize, search = service
    initialize.assert_called_once()
    search.assert_awaited_once_with(**expected)


@pytest.mark.parametrize("character", ["x", "🌍"])
@pytest.mark.parametrize("length", [2048, 2049])
async def test_query_character_boundary_not_utf8_byte_count(client, service, character, length):
    # ensure_ascii=False keeps the 2048-emoji request below the 16 KiB body cap.
    response = await client.post("/api/vector-search", content=json.dumps(
        {"query": character * length}, ensure_ascii=False).encode())
    initialize, search = service
    if length == 2048:
        assert response.status_code == 200
        assert search.await_args.kwargs["query"] == character * length
    else:
        _assert_error(response, 400)
        initialize.assert_not_called()
        search.assert_not_called()


@pytest.mark.parametrize("category", sorted(set(DOMAIN_CATEGORIES.values()) | {"Other"}))
async def test_all_current_vector_categories_remain_valid(client, service, category):
    response = await client.post("/api/vector-search", json={"query": "test", "category": category})
    assert response.status_code == 200
    assert service[1].await_args.kwargs["category"] == category


async def test_only_valid_input_can_return_vector_unavailable(client, service, monkeypatch):
    initialize, search = service
    monkeypatch.setattr(dashboard, "_vector_store", None)
    _assert_error(await client.post("/api/vector-search", json=[]), 400)
    initialize.assert_not_called()
    response = await client.post("/api/vector-search", json={"query": "test"})
    _assert_error(response, 503)
    initialize.assert_called_once()
    search.assert_not_called()


@pytest.mark.parametrize("failure", ["raise", "return-error", "initialize"])
async def test_internal_failures_are_generic_but_logged(client, service, caplog, failure):
    initialize, search = service
    secret = "dummy-internal-exception-detail"
    if failure == "raise":
        search.side_effect = RuntimeError(secret)
    elif failure == "return-error":
        search.return_value = {"error": secret, "results": [secret], "query": secret}
    else:
        initialize.side_effect = RuntimeError(secret)
    response = await client.post("/api/vector-search", json={"query": "test"})
    _assert_error(response, 500)
    assert response.json() == {"error": "Vector search failed"}
    assert secret not in response.text
    assert secret in caplog.text
    if failure == "initialize":
        search.assert_not_called()


@pytest.mark.parametrize("headers,status", [
    ({"host": "evil.example"}, 400),
    ({"origin": "https://evil.example"}, 403),
])
async def test_browser_isolation_still_precedes_body_reads(client, service, headers, status):
    stream = _BodyStream([b'{"query":"valid"}'])
    _assert_error(await client.post("/api/vector-search", headers=headers, content=stream), status)
    assert stream.reads == 0
    service[0].assert_not_called()
    service[1].assert_not_called()
