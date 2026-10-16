"""Tests for Fetcher — bounded response streaming and honest stale fallback.

fetcher.py's module docstring promises that a stale-cache fallback is
marked with ``_stale=True`` so dashboards/callers never mistake old data
for fresh data. Prior to the fix, ``_stale_fallback`` returned the cached
value completely unmodified — the marker was documented but never set.
"""

import asyncio
import gzip
import json
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.fetcher import _read_bounded


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(db_path=tmp_path / "test_cache.db")


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker()


@pytest.fixture
def fetcher(cache: Cache, breaker: CircuitBreaker) -> Fetcher:
    return Fetcher(cache=cache, breaker=breaker, default_timeout=5.0)


def test_stale_fallback_marks_dict_responses(fetcher: Fetcher) -> None:
    key = "test:stale:dict"
    fetcher.cache.set(key, {"value": 42}, ttl_seconds=-10)  # already expired

    result = fetcher._stale_fallback(key, "test-source")

    assert result is not None
    assert result["value"] == 42
    assert result["_stale"] is True
    assert isinstance(result["_stale_age_seconds"], (int, float))
    assert result["_stale_age_seconds"] >= 0


def test_stale_fallback_counts_non_dict_responses(
    fetcher: Fetcher, breaker: CircuitBreaker
) -> None:
    """Non-dict payloads (raw text/XML, lists) can't carry an inline
    marker, so the failure must surface via the breaker's stale-serve
    counter instead of vanishing silently."""
    key = "test:stale:list"
    fetcher.cache.set(key, [1, 2, 3], ttl_seconds=-10)

    result = fetcher._stale_fallback(key, "test-source")

    assert result == [1, 2, 3]
    assert breaker.status()["test-source"]["total_stale_serves"] == 1


def test_stale_fallback_returns_none_when_nothing_cached(fetcher: Fetcher) -> None:
    assert fetcher._stale_fallback("test:missing", "test-source") is None


class _Chunks(httpx.AsyncByteStream):
    """A real async response stream: reads and closure must be observable."""

    def __init__(self, chunks, error=None, pause=None):
        self.chunks = chunks
        self.error = error
        self.pause = pause
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.pause is not None:
            self.pause.set()
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error

    async def aclose(self):
        await asyncio.sleep(0)  # closure must also work when it awaits
        self.closed = True


@pytest.fixture
async def streamed(fetcher):
    """Install streaming respx responses, creating a fresh stream per attempt."""
    client = await fetcher._get_client()
    with respx.mock as router:
        def install(chunks, *, headers=None, status=200, error=None, pause=None):
            streams = []
            responses = []

            def respond(request):
                stream = _Chunks(chunks, error=error, pause=pause)
                streams.append(stream)
                response = httpx.Response(status, headers=headers, stream=stream)
                return response

            async def capture(response):
                # respx clones its return-value Response. Observe the actual
                # response exposed by HTTPX, not that unconsumed template.
                responses.append(response)

            client.event_hooks["response"] = [capture]
            route = router.get("https://upstream.test/feed").mock(side_effect=respond)
            return route, streams, responses

        yield install
    await fetcher.close()


async def _fetch(fetcher, kind, **kwargs):
    return await getattr(fetcher, f"get_{kind}")(
        "https://upstream.test/feed", "test-source", **kwargs
    )


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
@pytest.mark.parametrize("length", [None, "1", "999999999", "invalid"])
async def test_stream_caps_stop_early_without_trusting_length(fetcher, streamed, kind, length):
    headers = {} if length is None else {"content-length": length}
    route, streams, responses = streamed([b"1234", b"5678", b"9", b"unread"], headers=headers)
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    fetcher.vector_store = Mock(store=AsyncMock())
    result = await _fetch(fetcher, kind, max_bytes=8)
    if kind == "bytes":
        assert result.status == 200 and not result.ok and "exceeds cap 8" in result.error
    else:
        assert result is None
    assert route.call_count == 1  # no oversize retry or retry sleep
    assert streams[0].reads == 3  # the trailing body was never fetched
    assert streams[0].closed and responses[0].is_closed
    fetcher.cache.set.assert_not_called()
    fetcher.vector_store.store.assert_not_called()
    stats = fetcher.breaker.status()["test-source"]
    assert stats["total_failures"] == 1 and stats["total_successes"] == 0


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
async def test_compressed_body_cap_counts_decoded_bytes(fetcher, streamed, kind):
    # A tiny first gzip member expands well beyond the cap. The wire count
    # (and truthful compressed Content-Length) is much smaller than the cap.
    wire = gzip.compress(b" " * 16384)
    assert len(wire) < 1024
    route, streams, responses = streamed(
        [wire[:-8], wire[-8:], b"unread"],
        headers={"content-encoding": "gzip", "content-length": str(len(wire))},
    )
    result = await _fetch(fetcher, kind, max_bytes=1024)
    assert (not result.ok) if kind == "bytes" else result is None
    assert route.call_count == 1
    assert streams[0].reads == 1
    assert streams[0].closed and responses[0].is_closed


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
async def test_exact_cap_success_and_cache_behavior(fetcher, streamed, kind):
    body = b'{"value":42}' if kind == "json" else b"<body>ok</body>"
    route, streams, responses = streamed([body[:3], body[3:]])
    fetcher.vector_store = Mock(store=AsyncMock())
    result = await _fetch(fetcher, kind, max_bytes=len(body))
    if kind == "bytes":
        assert result.ok and result.content == body
        assert fetcher.cache.stats()["total_entries"] == 0
    else:
        assert result == ({"value": 42} if kind == "json" else body.decode())
        assert await _fetch(fetcher, kind, max_bytes=1) == result  # live cache stays unchanged
        assert route.call_count == 1
    assert streams[0].closed and responses[0].is_closed
    if kind == "json":
        fetcher.vector_store.store.assert_awaited_once_with("test-source", {"value": 42})
    else:
        fetcher.vector_store.store.assert_not_called()


@pytest.mark.parametrize("kind,stale", [("json", {"known": "good"}), ("json", [1, 2]),
                                      ("text", "old text"), ("xml", "<old/>")])
async def test_oversize_uses_stale_without_overwriting_it(fetcher, streamed, kind, stale):
    key = "stale-feed"
    fetcher.cache.set(key, stale, ttl_seconds=-10)
    before = fetcher.cache.get_stale_meta(key)
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    fetcher.vector_store = Mock(store=AsyncMock())
    route, streams, _ = streamed([b"12345", b"unread"])
    result = await _fetch(fetcher, kind, cache_key=key, max_bytes=4)
    if isinstance(stale, dict):
        assert result["known"] == "good" and result["_stale"] is True
        assert result["_stale_age_seconds"] >= 0
    else:
        assert result == stale
        assert fetcher.breaker.status()["test-source"]["total_stale_serves"] == 1
    assert fetcher.cache.get_stale_meta(key) == before
    fetcher.cache.set.assert_not_called()
    fetcher.vector_store.store.assert_not_called()
    assert route.call_count == 1 and streams[0].closed


@pytest.mark.parametrize("kind", ["text", "xml"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "shift_jis"])
async def test_multibyte_text_preserves_httpx_decoding(fetcher, streamed, kind, encoding):
    text = "東京 café" if encoding != "shift_jis" else "東京"
    body = text.encode(encoding)
    streamed([body[i:i + 1] for i in range(len(body))],
             headers={"content-type": f"text/plain; charset={encoding}"})
    assert await _fetch(fetcher, kind, max_bytes=len(body)) == text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
async def test_json_encoding_and_compression_preserved(fetcher, streamed, encoding):
    data = {"city": "東京"}
    body = json.dumps(data, ensure_ascii=False).encode(encoding)
    compressed = gzip.compress(body)
    streamed([compressed[i:i + 1] for i in range(len(compressed))],
             headers={"content-encoding": "gzip", "content-type": "application/json"})
    assert await _fetch(fetcher, "json", max_bytes=len(body)) == data


async def test_text_default_autodetection_and_invalid_charset(fetcher):
    body = "café".encode("latin-1")
    detector = Mock(return_value="latin-1")
    stream = _Chunks([body])
    async with httpx.AsyncClient(default_encoding=detector, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/plain; charset=bogus"}, stream=stream)
    )) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text") == "café"
    detector.assert_called_once_with(body)
    assert stream.closed


@pytest.mark.parametrize("kind", ["text", "xml"])
async def test_compressed_multibyte_text_is_not_decoded_twice(fetcher, streamed, kind):
    text = "東京 café"
    body = text.encode("utf-16")
    wire = gzip.compress(body)
    streamed([wire[:5], wire[5:]], headers={
        "content-encoding": "gzip", "content-type": "text/xml; charset=utf-16"})
    assert await _fetch(fetcher, kind, max_bytes=len(body)) == text


async def test_response_hook_encoding_and_replacement_behavior(fetcher):
    async def encoding_hook(response):
        response.encoding = "latin-1"

    stream = _Chunks([b"caf\xe9"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream)),
        event_hooks={"response": [encoding_hook]}
    ) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text", cache_key="hook") == "café"
    stream = _Chunks([b"bad\xff"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream)
    )) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text", cache_key="replacement") == "bad\ufffd"


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
async def test_cancellation_closes_response_without_failure_or_writes(fetcher, streamed, kind):
    entered = asyncio.Event()
    _, streams, responses = streamed([b"part"], pause=entered)
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    fetcher.vector_store = Mock(store=AsyncMock())
    task = asyncio.create_task(_fetch(fetcher, kind))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert streams[0].closed and responses[0].is_closed
    fetcher.cache.set.assert_not_called()
    fetcher.vector_store.store.assert_not_called()
    assert fetcher.breaker.status()["test-source"]["total_failures"] == 0


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
@pytest.mark.parametrize("error", [httpx.ReadError("broken stream"), httpx.DecodingError("bad compression")])
async def test_read_errors_close_each_attempt_and_do_not_write(fetcher, streamed, monkeypatch, kind, error):
    fetcher.max_retries = 1
    monkeypatch.setattr("world_intel_mcp.fetcher.asyncio.sleep", AsyncMock())
    route, streams, responses = streamed([b"partial"], error=error)
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    result = await _fetch(fetcher, kind)
    assert (not result.ok) if kind == "bytes" else result is None
    assert route.call_count == 2 and all(s.closed for s in streams)
    assert all(r.is_closed for r in responses)
    fetcher.cache.set.assert_not_called()
    assert fetcher.breaker.status()["test-source"]["total_failures"] == 1


@pytest.mark.parametrize("status", [404, 500])
@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
async def test_error_status_bodies_are_not_read(fetcher, streamed, kind, status):
    fetcher.max_retries = 0
    route, streams, responses = streamed([b"unread error body"], status=status)
    result = await _fetch(fetcher, kind)
    assert route.call_count == 1 and streams[0].reads == 0
    assert streams[0].closed and responses[0].is_closed
    if kind == "bytes" and status == 404:
        assert result.status == 404 and result.error is None and result.content is None
        assert fetcher.breaker.status()["test-source"]["total_failures"] == 0
    elif kind == "bytes":
        assert result.status == status and str(status) in result.error
    else:
        assert result is None


async def test_json_content_type_guard_and_invalid_json_close(fetcher, streamed):
    fetcher.max_retries = 0
    _, streams, _ = streamed([b"unread"], headers={"content-type": "text/html"})
    assert await _fetch(fetcher, "json") is None
    assert streams[0].reads == 0 and streams[0].closed
    _, streams, _ = streamed([b"not JSON"], headers={"content-type": "application/json"})
    assert await _fetch(fetcher, "json") is None
    assert streams[0].closed


@pytest.mark.parametrize("kind", ["bytes", "json", "text", "xml"])
async def test_bad_gzip_closes_without_caching(fetcher, streamed, kind):
    fetcher.max_retries = 0
    _, streams, responses = streamed([b"invalid gzip", b"unread"], headers={"content-encoding": "gzip"})
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    result = await _fetch(fetcher, kind)
    assert (not result.ok) if kind == "bytes" else result is None
    assert streams[0].reads == 1 and streams[0].closed and responses[0].is_closed
    fetcher.cache.set.assert_not_called()


async def test_binary_404_is_not_retried(fetcher, streamed):
    route, streams, _ = streamed([b"unread"], status=404)
    result = await _fetch(fetcher, "bytes")
    assert result.status == 404 and result.error is None
    assert route.call_count == 1 and streams[0].reads == 0 and streams[0].closed
    assert fetcher.breaker.status()["test-source"]["total_successes"] == 1


async def test_oversize_trips_breaker_without_retries(fetcher, streamed):
    fetcher.breaker.failure_threshold = 1
    route, streams, _ = streamed([b"too big", b"unread"])
    assert await _fetch(fetcher, "json", max_bytes=2) is None
    assert not fetcher.breaker.is_available("test-source")
    assert await _fetch(fetcher, "json", max_bytes=2) is None
    assert route.call_count == 1 and streams[0].closed


def test_default_limits_preserve_large_binary_history(fetcher):
    import inspect

    assert fetcher.max_json_bytes == 32 * 1024 * 1024
    assert fetcher.max_text_bytes == 32 * 1024 * 1024
    assert inspect.signature(Fetcher.get_bytes).parameters["max_bytes"].default == 256 * 1024 * 1024


@pytest.mark.parametrize("kind", ["json", "text", "xml"])
async def test_configured_defaults_and_per_call_override(cache, breaker, streamed, fetcher, kind):
    fetcher.max_json_bytes = 4
    fetcher.max_text_bytes = 4
    route, _, _ = streamed([b"[1,2]", b" "])
    assert await _fetch(fetcher, kind) is None
    assert route.call_count == 1
    result = await _fetch(fetcher, kind, max_bytes=6)
    assert result == ([1, 2] if kind == "json" else "[1,2] ")
    configured = Fetcher(cache, breaker, max_json_bytes=16, max_text_bytes=32)
    assert configured.max_json_bytes == 16 and configured.max_text_bytes == 32


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), "100"])
async def test_invalid_caps_are_configuration_errors(fetcher, streamed, value):
    route, _, _ = streamed([b"unread"])
    for kind in ("bytes", "json", "text", "xml"):
        with pytest.raises(ValueError, match="positive integer"):
            await _fetch(fetcher, kind, max_bytes=value)
    assert route.call_count == 0
    with pytest.raises(ValueError):
        Fetcher(fetcher.cache, fetcher.breaker, max_json_bytes=value)


async def test_redirects_close_without_buffering_bodies(fetcher):
    streams = [_Chunks([b"unread redirect"]), _Chunks([b'{"ok":true}'])]
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/final", "set-cookie": "a=b"}, stream=streams[0])
        assert streams[0].closed
        assert request.url.path == "/final" and request.headers["cookie"] == "a=b"
        return httpx.Response(200, stream=streams[1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "json") == {"ok": True}
    assert streams[0].reads == 0 and all(s.closed for s in streams)


async def test_redirect_limit_closes_without_reading(fetcher):
    streams = []

    def respond(request):
        stream = _Chunks([b"unread"])
        streams.append(stream)
        return httpx.Response(302, headers={"location": "/again"}, stream=stream)

    fetcher.max_retries = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True,
                                max_redirects=2) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text") is None
    assert len(streams) == 3
    assert all(s.closed and s.reads == 0 for s in streams)


class _CountingBasicAuth(httpx.BasicAuth):
    def __init__(self):
        super().__init__("dummy-user", "dummy-password")
        self.calls = 0

    def auth_flow(self, request):
        self.calls += 1
        yield from super().auth_flow(request)


@pytest.mark.parametrize("auth_mode", ["client-auth", "request-header", "client-header"])
@pytest.mark.parametrize("target,retained", [
    ("https://other.test/final", False),
    ("https://upstream.test:8443/final", False),
    ("https://upstream.test/final", True),
])
async def test_redirect_authorization_matches_httpx(fetcher, auth_mode, target, retained):
    """Dummy credentials must not be reapplied to prepared redirect requests."""
    seen = []
    streams = []
    auth = _CountingBasicAuth() if auth_mode == "client-auth" else None
    explicit = {"Authorization": "Bearer dummy-token"}

    def respond(request):
        seen.append(request)
        stream = _Chunks([b"unread redirect"] if len(seen) == 1 else [b'{"ok":true}'])
        streams.append(stream)
        return httpx.Response(302 if len(seen) == 1 else 200, stream=stream,
                              headers={"location": target} if len(seen) == 1 else {})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True, auth=auth,
        headers=explicit if auth_mode == "client-header" else None,
    ) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "json", headers=explicit if auth_mode == "request-header" else None) == {"ok": True}
    assert len(seen) == 2
    initial = seen[0].headers["authorization"]
    assert initial.startswith("Basic " if auth_mode == "client-auth" else "Bearer ")
    assert seen[1].headers.get("authorization") == (initial if retained else None)
    assert streams[0].reads == 0 and all(s.closed for s in streams)
    if auth is not None:
        assert auth.calls == 1


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("payload", ["json", "content"])
async def test_webhook_never_follows_redirects(fetcher, monkeypatch, owned, status, payload):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, headers={"location": "https://elsewhere.test/sink",
                                              "x-response": "original"}, content=b"original")

    original_client = httpx.AsyncClient
    if owned:
        def make_client(**kwargs):
            # This test exercises redirect/header handling, not destination policy.
            kwargs["transport"] = httpx.MockTransport(respond)
            return original_client(**kwargs)
        monkeypatch.setattr("world_intel_mcp.fetcher.httpx.AsyncClient", make_client)
    else:
        fetcher._client = original_client(transport=httpx.MockTransport(respond), follow_redirects=True)
    body = {"json": {"secret": "payload"}} if payload == "json" else {"content": b"raw payload"}
    try:
        response = await fetcher.post("http://localhost:9876/sink", **body,
                                      headers={"X-Sink-Key": "dummy"}, timeout=7.25)
        assert response.status_code == status
        assert response.content == b"original" and response.headers["x-response"] == "original"
        assert len(seen) == 1 and seen[0].method == "POST"
        assert seen[0].headers["x-sink-key"] == "dummy"
        assert seen[0].extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 7.25)
        assert json.loads(seen[0].content) == body["json"] if payload == "json" else seen[0].content == body["content"]
    finally:
        await fetcher.close()


@pytest.mark.parametrize("kind", ["json", "text", "xml", "bytes"])
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("boundary", ["direct", "after-upgrade", "userinfo", "userinfo-relative", "empty-userinfo"])
async def test_redirect_policy_terminal_unread_no_writes(fetcher, monkeypatch, caplog, kind, boundary, owned):
    seen, streams, responses = [], [], []
    start = "http://upstream.test/start" if boundary == "after-upgrade" else "https://upstream.test/start"
    targets = {
        "direct": ["http://upstream.test/private?token=hidden"],
        "after-upgrade": ["https://upstream.test/second", "http://other.test/private?token=hidden"],
        "userinfo": ["https://dummy-user:dummy-password@other.test/private?token=hidden"],
        "userinfo-relative": ["//dummy-user:dummy-password@upstream.test/private"],
        "empty-userinfo": ["https://@other.test/private"],
    }[boundary]
    stale = {"old": True} if kind == "json" else "old text"
    fetcher.cache.set("redirect-stale", stale, ttl_seconds=-10)
    before = fetcher.cache.get_stale_meta("redirect-stale")
    fetcher.cache.set = Mock(wraps=fetcher.cache.set)
    fetcher.vector_store = Mock(store=AsyncMock())
    # _Chunks needs real cooperative sleeps for close. Record only backoff waits.
    real_sleep = asyncio.sleep
    waits = []
    async def sleep(delay):
        if delay:
            waits.append(delay)
        await real_sleep(0)
    monkeypatch.setattr("world_intel_mcp.fetcher.asyncio.sleep", sleep)

    def respond(request):
        seen.append(request)
        stream = _Chunks([b"must remain unread"])
        streams.append(stream)
        response = httpx.Response(302, headers={"location": targets[len(seen) - 1]}, stream=stream)
        responses.append(response)
        return response

    original_client = httpx.AsyncClient
    if owned:
        def make_client(**kwargs):
            # Destination pinning is covered with the real pool in integration tests.
            kwargs["transport"] = httpx.MockTransport(respond)
            return original_client(**kwargs)
        monkeypatch.setattr("world_intel_mcp.fetcher.httpx.AsyncClient", make_client)
    else:
        fetcher._client = original_client(transport=httpx.MockTransport(respond), follow_redirects=True)
    try:
        kwargs = {} if kind == "bytes" else {"cache_key": "redirect-stale"}
        result = await getattr(fetcher, f"get_{kind}")(start, "test-source", **kwargs)
    finally:
        await fetcher.close()
    if kind == "bytes":
        assert result.status is None and result.content is None and "forbidden" in result.error
    elif kind == "json":
        assert result["old"] and result["_stale"]
    else:
        assert result == stale
    assert len(seen) == len(targets)
    assert all(s.closed and s.reads == 0 for s in streams)
    assert all(r.is_closed for r in responses)
    assert not waits
    assert fetcher.breaker.status()["test-source"]["total_failures"] == 1
    assert fetcher.cache.get_stale_meta("redirect-stale") == before
    fetcher.cache.set.assert_not_called()
    fetcher.vector_store.store.assert_not_called()
    assert "dummy-password" not in caplog.text and "hidden" not in caplog.text
    assert "/start" not in caplog.text and "/private" not in caplog.text


@pytest.mark.parametrize("target", ["https://other.test:8443/second", "https://upstream.test:8443/second",
                                    "https://upstream.test:0/second"])
async def test_cross_origin_strips_all_non_allowlisted_headers_permanently(fetcher, target):
    seen = []
    auth = _CountingBasicAuth()
    client_headers = {"X-Client-Credential": "client-secret", "Proxy-Authorization": "proxy-secret",
                      "Forwarded": "for=internal", "X-Forwarded-For": "internal", "Referer": "secret-referrer"}
    call_headers = {"X-Arbitrary-Credential": "call-secret", "X-Api-Key": "api-secret",
                    "Accept": "application/json", "User-Agent": "test-agent", "Host": "override.invalid",
                    "Cookie": "manual=secret", "Accept-Encoding": "br"}

    def respond(request):
        seen.append(request)
        locations = [target, "https://upstream.test/final"]
        return httpx.Response(302, headers={"location": locations[len(seen) - 1]}, stream=_Chunks([])) if len(seen) <= 2 else httpx.Response(200, stream=_Chunks([b"ok"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True,
                                auth=auth, headers=client_headers) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text", headers=call_headers) == "ok"
    assert all(key in seen[0].headers for key in client_headers | call_headers)
    for request in seen[1:]:
        assert set(request.headers) == {"accept", "accept-encoding", "user-agent", "host"}
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "test-agent"
        assert request.headers["host"] == request.url.netloc.decode("ascii")
    assert all(r.headers["accept-encoding"] == "gzip, deflate" for r in seen)
    assert auth.calls == 1


@pytest.mark.parametrize("upgrade", [False, True])
async def test_same_origin_headers_and_upgrade_auth_exception_with_cookie_jar(fetcher, upgrade):
    seen = []
    start = "http://upstream.test/start" if upgrade else "https://UPSTREAM.test:443/start"
    target = "https://upstream.test/final"
    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": target, "set-cookie": "fresh=jar; Path=/"}, stream=_Chunks([b"unread"]))
        return httpx.Response(200, stream=_Chunks([b"ok"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True,
                                headers={"X-Client": "client", "Authorization": "Bearer dummy"}) as client:
        client.cookies.set("stored", "cookie", domain="upstream.test", path="/")
        fetcher._client = client
        assert await fetcher.get_text(start, "test-source", headers={"X-Call": "call", "Cookie": "manual=secret", "Host": "override.invalid"}) == "ok"
    assert seen[1].headers["authorization"] == "Bearer dummy"
    assert seen[1].headers["cookie"] == "stored=cookie; fresh=jar"
    assert seen[1].headers["host"] == ("upstream.test" if upgrade else "override.invalid")
    assert seen[1].headers.get("x-client") == (None if upgrade else "client")
    assert seen[1].headers.get("x-call") == (None if upgrade else "call")


async def test_cross_origin_uses_only_httpx_destination_cookies_and_host(fetcher):
    seen = []
    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://destination.test:8443/final",
                                              "set-cookie": "source=fresh; Path=/"}, stream=_Chunks([]))
        return httpx.Response(200, stream=_Chunks([b"ok"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        client.cookies.set("destination", "jar", domain="destination.test", path="/")
        client.cookies.set("source", "jar", domain="upstream.test", path="/")
        fetcher._client = client
        assert await _fetch(fetcher, "text", headers={"Cookie": "manual=secret", "Host": "override.invalid"}) == "ok"
    assert seen[0].headers["cookie"] == "manual=secret"
    assert seen[1].headers["cookie"] == "destination=jar"
    assert seen[1].headers["host"] == "destination.test:8443"


@pytest.mark.parametrize("kind", ["json", "text", "xml", "bytes"])
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_allowed_cross_origin_redirects_keep_bounded_decoding(fetcher, kind, encoding, overflow):
    seen, streams = [], []
    body = b'{"ok":true}'
    wire = gzip.compress(body) if encoding == "gzip" else zlib.compress(body)
    def respond(request):
        seen.append(request)
        if len(seen) <= 2:
            stream = _Chunks([b"unread redirect body"])
            streams.append(stream)
            return httpx.Response(302, headers={"location": "https://other.test/final"}, stream=stream)
        stream = _Chunks([wire])
        streams.append(stream)
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True,
                                headers={"Accept-Encoding": "br, zstd"}) as client:
        fetcher._client = client
        result = await _fetch(fetcher, kind, max_bytes=len(body) - int(overflow))
    if overflow:
        assert (not result.ok) if kind == "bytes" else result is None
        assert fetcher.breaker.status()["test-source"]["total_failures"] == 1
        assert fetcher.cache.stats()["total_entries"] == 0
    else:
        assert result.content == body if kind == "bytes" else result == ({"ok": True} if kind == "json" else body.decode())
    assert len(seen) == 3
    assert all(s.closed for s in streams)
    assert all(s.reads == 0 for s in streams[:2])
    assert all(r.headers["accept-encoding"] == "gzip, deflate" for r in seen)


async def test_credentials_do_not_reappear_later_in_redirect_chain(fetcher):
    auth = _CountingBasicAuth()
    seen = []

    def respond(request):
        seen.append(request)
        locations = ["https://other.test/second", "https://upstream.test/final"]
        if len(seen) <= len(locations):
            return httpx.Response(302, headers={"location": locations[len(seen) - 1]},
                                  stream=_Chunks([b"unread"]))
        return httpx.Response(200, stream=_Chunks([b"ok"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), auth=auth,
                                follow_redirects=True) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text") == "ok"
    assert "authorization" in seen[0].headers
    assert all("authorization" not in request.headers for request in seen[1:])
    assert auth.calls == 1


async def test_injected_challenge_auth_buffering_is_outside_reader_cap(fetcher):
    """Document HTTPX's internal read boundary using only small offline bodies.

    The challenge is deliberately larger than max_bytes. An injected multi-step
    auth flow lets HTTPX consume it before _stream_get receives the final body;
    callers needing the cap on every response must not inject such a flow.
    """
    class ChallengeAuth(httpx.Auth):
        def auth_flow(self, request):
            response = yield request
            if response.status_code == 401:
                request.headers["authorization"] = "Bearer dummy-challenge"
                yield request

    challenge = _Chunks([b"challenge", b"body", b"read by HTTPX"])
    final = _Chunks([b"ok"])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401 if len(requests) == 1 else 200,
                              stream=challenge if len(requests) == 1 else final)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                auth=ChallengeAuth()) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text", max_bytes=2) == "ok"
    assert len(requests) == 2
    assert challenge.reads == 3 and challenge.closed and final.closed


@pytest.mark.parametrize("kind", ["json", "text", "xml", "bytes"])
@pytest.mark.parametrize("encoding", ["br", "zstd", "unknown", "gzip, deflate", "", "gzip,", "gzip gzip", "\vgzip"])
async def test_encoding_policy_rejects_unread_terminal(fetcher, streamed, kind, encoding):
    route, streams, responses = streamed([b"unread"], headers={"Content-Encoding": encoding})
    result = await _fetch(fetcher, kind)
    if kind == "bytes":
        assert not result.ok
    else:
        assert result is None
    assert route.call_count == 1
    assert streams[0].reads == 0 and streams[0].closed and responses[0].is_closed
    assert fetcher.breaker.status()["test-source"]["total_failures"] == 1


async def test_duplicate_encoding_headers_rejected_unread(fetcher, streamed):
    _, streams, _ = streamed([b"unread"], headers=[("content-encoding", "gzip"), ("content-encoding", "gzip")])
    assert await _fetch(fetcher, "text") is None
    assert streams[0].reads == 0


@pytest.mark.parametrize("raw", [False, True])
async def test_http_deflate_one_byte_stream(fetcher, streamed, raw):
    compressor = zlib.compressobj(wbits=-15 if raw else 15)
    body = b"hello" * 20000
    wire = compressor.compress(body) + compressor.flush()
    streamed([b""] + [wire[i:i + 1] for i in range(len(wire))] + [b""], headers={"content-encoding": "DeFlAtE"})
    assert (await _fetch(fetcher, "bytes", max_bytes=len(body))).content == body


@pytest.mark.parametrize("length", [None, "1", "invalid", "9999999"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_encoded_budget_is_actual_wire_count(fetcher, streamed, length, overflow):
    wire = gzip.compress(b"a")
    headers = {"content-encoding": "gzip"}
    if length is not None:
        headers["content-length"] = length
    route, streams, _ = streamed([wire, b"unread"], headers=headers)
    # Stop after wire: the second chunk is only present on the rejection path.
    if not overflow:
        route, streams, _ = streamed([wire], headers=headers)
    result = await _fetch(fetcher, "bytes", max_bytes=10, max_encoded_bytes=len(wire) - int(overflow))
    assert result.ok is not overflow
    if not overflow:
        assert result.content == b"a"
    else:
        assert "encoded" in result.error and streams[0].reads == 1
    assert route.call_count == 1 and streams[0].closed


@pytest.mark.parametrize("compressed", [False, True])
async def test_raw_huge_chunk_and_no_httpx_decoding(fetcher, streamed, monkeypatch, compressed):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTPX must not decode/chunk the response")
    monkeypatch.setattr(httpx.Response, "aiter_bytes", forbidden)
    body = bytes(range(256)) * 1024
    streamed([gzip.compress(body) if compressed else body],
             headers={"content-encoding": "gzip"} if compressed else {})
    assert (await _fetch(fetcher, "bytes", max_bytes=len(body))).content == body


async def test_application_gzip_is_opaque_to_binary_fetch(fetcher, streamed):
    body = gzip.compress(b"application payload")
    streamed([body], headers={"content-type": "application/gzip"})
    assert (await _fetch(fetcher, "bytes")).content == body


async def test_negotiation_overrides_injected_and_caller_headers(fetcher):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/final"}, stream=_Chunks([])) if len(requests) == 1 else httpx.Response(200, stream=_Chunks([b"ok"]))
    async with httpx.AsyncClient(
        headers={"Accept-Encoding": "br, zstd"}, follow_redirects=True,
        transport=httpx.MockTransport(respond),
    ) as client:
        fetcher._client = client
        assert await _fetch(fetcher, "text", headers={"accept-encoding": "identity"}) == "ok"
    assert [r.headers["accept-encoding"] for r in requests] == ["gzip, deflate"] * 2


async def test_processing_deadline_includes_stream_wait_and_closes(fetcher, streamed):
    route, streams, responses = streamed([b""], pause=asyncio.Event())
    result = await _fetch(fetcher, "bytes", processing_timeout=0.01)
    assert not result.ok and "deadline" in result.error
    assert route.call_count == 1 and streams[0].closed and responses[0].is_closed


async def test_cancel_during_in_memory_expansion_is_cooperative(fetcher, streamed, monkeypatch):
    from world_intel_mcp.bounded_decode import BoundedDecoder
    entered = asyncio.Event()
    original = BoundedDecoder.feed
    def observe(self, data):
        for output in original(self, data):
            entered.set()
            yield output
    monkeypatch.setattr(BoundedDecoder, "feed", observe)
    _, streams, _ = streamed([gzip.compress(b"x" * (4 * 1024 * 1024))], headers={"content-encoding": "gzip"})
    task = asyncio.create_task(_fetch(fetcher, "bytes"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert streams[0].closed


async def test_prebuffered_content_is_not_decoded_twice():
    response = httpx.Response(200, content=gzip.compress(b"hello"), headers={"content-encoding": "gzip"})
    assert response.is_stream_consumed and response.content == b"hello"
    assert await _read_bounded(response, 5) == b"hello"
    with pytest.raises(ValueError, match="cap"):
        await _read_bounded(response, 4)


async def test_terminal_encoding_failure_preserves_stale_and_no_writes(fetcher, streamed):
    fetcher.cache.set("stale", {"old": True}, ttl_seconds=-1)
    fetcher.vector_store = Mock(store=AsyncMock())
    route, streams, _ = streamed([b"unread"], headers={"content-encoding": "br"})
    result = await _fetch(fetcher, "json", cache_key="stale")
    assert result["old"] and result["_stale"]
    assert fetcher.cache.get("stale") is None
    fetcher.vector_store.store.assert_not_called()
    assert route.call_count == 1 and streams[0].reads == 0


async def test_constructor_body_options_and_per_call_override(cache, breaker):
    fetcher = Fetcher(cache, breaker, max_encoded_bytes=1, processing_timeout=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=_Chunks([b"ok"]))
    )) as client:
        fetcher._client = client
        assert not (await _fetch(fetcher, "bytes")).ok
        assert (await _fetch(fetcher, "bytes", max_encoded_bytes=2)).content == b"ok"


@pytest.mark.parametrize("option,value", [
    ("max_encoded_bytes", 0), ("max_encoded_bytes", True),
    ("processing_timeout", 0), ("processing_timeout", float("inf")),
    ("processing_timeout", float("nan")), ("processing_timeout", True),
])
async def test_body_options_validate_before_request_or_cache(fetcher, cache, breaker, option, value):
    with pytest.raises(ValueError):
        Fetcher(cache, breaker, **{option: value})
    for kind in ("json", "text", "xml", "bytes"):
        with pytest.raises(ValueError):
            await _fetch(fetcher, kind, **{option: value})


async def test_deadline_during_decoding_is_terminal(fetcher, streamed, monkeypatch):
    from world_intel_mcp.bounded_decode import BoundedDecoder, BodyLimitError
    original = BoundedDecoder.check_time
    calls = 0
    def expires(self):
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise BodyLimitError("body processing deadline exceeded")
        original(self)
    monkeypatch.setattr(BoundedDecoder, "check_time", expires)
    route, streams, _ = streamed([gzip.compress(b"x" * 1000000)], headers={"content-encoding": "gzip"})
    result = await _fetch(fetcher, "bytes")
    assert not result.ok and "deadline" in result.error
    assert route.call_count == 1 and streams[0].closed


@pytest.mark.parametrize("encoding,wire", [
    ("gzip", gzip.compress(b"x")[:-1]),
    ("gzip", gzip.compress(b"x") + b"padding"),
    ("deflate", zlib.compress(b"x")[:-1]),
])
async def test_integrity_failure_is_terminal_no_cache(fetcher, streamed, encoding, wire):
    route, streams, _ = streamed([wire], headers={"content-encoding": encoding})
    assert await _fetch(fetcher, "text") is None
    assert route.call_count == 1 and streams[0].closed
    assert fetcher.breaker.status()["test-source"]["total_failures"] == 1
