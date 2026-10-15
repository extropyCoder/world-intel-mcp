"""Browser isolation through the full ASGI app, without live data or lifespan."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from world_intel_mcp.dashboard import app as dashboard


ROUTES = [
    ("GET", "/"),
    ("GET", "/api/health"),
    ("GET", "/api/static"),
    ("GET", "/api/overview"),
    ("GET", "/api/aois"),
    ("GET", "/api/migration"),
    ("GET", "/api/stream"),
    ("GET", "/sse/vector-analytics"),
    ("POST", "/api/vector-search"),
    ("OPTIONS", "/api/vector-search"),
    ("GET", "/api/report/pdf"),
    ("GET", "/not-found"),
]


def assert_no_cors(response):
    assert not any(name.startswith("access-control-") for name in response.headers)


@pytest.fixture
def client():
    # Deliberately no context manager: do not start application-wide workers.
    return TestClient(dashboard.app, base_url="http://localhost:8501")


BAD_HOSTS = [
    "evil.example", "localhost.evil.example", "localhost.", "127.0.0.2",
    "0.0.0.0", "[::1]", "127.1", "2130706433", "user@localhost",
    "localhost@evil.example", "http://localhost", "localhost/", "localhost?x",
    "localhost#x", "localhost:", "localhost:0", "localhost:65536",
    "localhost:-1", "localhost:+80", "localhost:8x", "localhost:80:90",
    " localhost", "localhost ", "localhost,127.0.0.1", "", "localhost:１２",
    "localhost:" + "9" * 5000,
]

BAD_ORIGINS = [
    "null", "", "http://evil.example", "http://localhost.evil.example:8501",
    "http://localhost:18501", "http://localhost", "http://127.0.0.1:8501",
    "https://localhost:8501", "ftp://localhost:8501", "//localhost:8501",
    "http://user@localhost:8501", "http://localhost:8501/",
    "http://localhost:8501/path", "http://localhost:8501?x",
    "http://localhost:8501#x", " http://localhost:8501",
    "http://localhost:8501 ", "http://localhost:0", "http://localhost:65536",
    "http://localhost:", "http://localhost:8501,http://localhost:8501",
    "http://localhost:8501 http://localhost:8501", "http://localhost.:8501",
    "http://localhost:8501\\evil",
]


@pytest.mark.parametrize("method,path", ROUTES)
async def test_rejections_precede_all_handlers(monkeypatch, method, path):
    """Even SSE, POST, preflight, 404 and 410 must reject before any work."""
    spies = []
    for name in ("_ensure_fetcher", "_open_aoi_store", "_fetch_overview",
                 "_fetch_vector_analytics", "_stream_source_defs", "StreamingResponse"):
        spy = Mock(side_effect=AssertionError(f"rejected request reached {name}"))
        monkeypatch.setattr(dashboard, name, spy)
        spies.append(spy)
    search = AsyncMock(side_effect=AssertionError("search invoked"))
    monkeypatch.setattr(dashboard, "_vector_store", Mock(semantic_search=search))

    cases = [([("host", host)], 400) for host in BAD_HOSTS]
    cases += [([], 400), ([("host", "localhost:8501")] * 2, 400)]
    cases += [([("host", "localhost:8501"), ("origin", origin)], 403)
              for origin in BAD_ORIGINS]
    cases += [([("host", "localhost:8501"), ("origin", "http://localhost:8501"),
                ("origin", "http://localhost:8501")], 403)]
    # ASGITransport preserves raw missing/duplicate headers; TestClient inserts
    # a Host when missing. This still exercises the complete application stack.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app),
                                 base_url="http://localhost:8501") as client:
        for headers, status in cases:
            request = client.build_request(method, path, json={"query": "test"})
            request.headers = httpx.Headers([(k.encode(), v.encode("utf-8")) for k, v in headers])
            response = await client.send(request)
            assert response.status_code == status, headers
            assert_no_cors(response)
    for spy in spies:
        spy.assert_not_called()
    search.assert_not_called()


@pytest.mark.parametrize("hostname", ["localhost", "LOCALHOST", "127.0.0.1"])
@pytest.mark.parametrize("port", ["", ":8501", ":18501", ":1", ":65535"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("with_origin", [False, True])
def test_allowed_authorities(hostname, port, scheme, with_origin):
    authority = hostname + port
    headers = {"host": authority}
    if with_origin:
        headers["origin"] = f"{scheme}://{authority}"
    client = TestClient(dashboard.app, base_url=f"{scheme}://localhost")
    response = client.get("/api/health", headers=headers)
    assert response.status_code == 200
    assert_no_cors(response)


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_effective_default_ports_and_scheme(scheme, port):
    client = TestClient(dashboard.app, base_url=f"{scheme}://localhost")
    for host, origin in [("localhost", f"{scheme}://localhost:{port}"),
                         (f"localhost:{port}", f"{scheme}://localhost")]:
        assert client.get("/", headers={"host": host, "origin": origin}).status_code == 200
    other_port = 443 if port == 80 else 80
    assert client.get("/", headers={"origin": f"{scheme}://localhost:{other_port}"}).status_code == 403
    other_scheme = "https" if scheme == "http" else "http"
    assert client.get("/", headers={"origin": f"{other_scheme}://localhost:{port}"}).status_code == 403


def test_forwarded_headers_are_not_authority(client):
    forwarded = {"forwarded": "host=localhost:8501;proto=https",
                 "x-forwarded-host": "localhost:8501", "x-forwarded-proto": "https"}
    assert client.get("/", headers={**forwarded, "host": "evil.example"}).status_code == 400
    assert client.get("/", headers={**forwarded, "origin": "https://localhost:8501"}).status_code == 403
    assert client.get("/", headers={**forwarded, "origin": "http://localhost:8501"}).status_code == 200


def test_normalized_host_and_port(client):
    response = client.get("/api/health", headers={
        "host": "LOCALHOST:08501", "origin": "HTTP://localhost:8501"})
    assert response.status_code == 200
    assert_no_cors(response)


@pytest.mark.parametrize("with_origin", [False, True])
def test_real_routes_success_errors_and_finite_sse(client, monkeypatch, with_origin):
    producer = AsyncMock(return_value={"test": True})
    monkeypatch.setattr(dashboard, "_fetch_overview", producer)
    monkeypatch.setattr(dashboard, "_fetch_vector_analytics", producer)
    monkeypatch.setattr(dashboard, "_ensure_fetcher", Mock(return_value=object()))
    monkeypatch.setattr(dashboard, "_stream_source_defs", lambda _: {"stub": producer})
    search = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(dashboard, "_vector_store", Mock(semantic_search=search))

    def finite_response(iterator, **kwargs):
        async def first_event():
            try:
                yield await anext(iterator)
            finally:
                await iterator.aclose()
        return StreamingResponse(first_event(), **kwargs)

    monkeypatch.setattr(dashboard, "StreamingResponse", finite_response)
    headers = {"origin": "http://localhost:8501"} if with_origin else {}
    for path in ("/", "/api/health", "/api/static", "/api/overview",
                 "/api/stream", "/sse/vector-analytics"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert_no_cors(response)
        if "stream" in path or path.startswith("/sse/"):
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.text.startswith("data: ")
    assert producer.await_count == 3
    response = client.post("/api/vector-search", headers=headers, json={"query": "test"})
    assert response.status_code == 200
    search.assert_awaited_once()
    assert_no_cors(response)
    for method, path, status in [("POST", "/api/vector-search", 400),
                                 ("GET", "/api/migration?limit=bad", 400),
                                 ("GET", "/api/report/pdf", 410),
                                 ("GET", "/missing", 404),
                                 ("OPTIONS", "/api/vector-search", 405)]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == status
        assert_no_cors(response)


def test_handler_failures_and_browser_preflight_have_no_cors(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_ensure_fetcher", Mock(return_value=object()))
    monkeypatch.setattr(dashboard, "_open_aoi_store", Mock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(dashboard, "_vector_store", Mock(
        semantic_search=AsyncMock(side_effect=RuntimeError("offline"))))
    response = client.get("/api/aois")
    assert response.status_code == 503
    assert_no_cors(response)
    response = client.post("/api/vector-search", json={"query": "test"})
    assert response.status_code == 500
    assert_no_cors(response)
    for origin, status in [("http://localhost:8501", 405), ("https://evil.example", 403)]:
        response = client.options("/api/vector-search", headers={
            "origin": origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        })
        assert response.status_code == status
        assert_no_cors(response)


def test_launcher_disables_proxy_scheme_rewriting(monkeypatch):
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    dashboard.run(host="127.0.0.1", port=8501)
    assert run.call_args.kwargs["proxy_headers"] is False
    assert run.call_args.kwargs["host"] == "127.0.0.1"
