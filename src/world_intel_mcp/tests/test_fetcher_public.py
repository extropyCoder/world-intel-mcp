"""Fetcher integration with real HTTPX/httpcore pools; fake DNS/TCP/TLS only.

The standalone public transport and its tests are deliberately not modified.
Fake TLS records identity/context propagation, not live certificate validation.
"""

import asyncio
import gzip
import ipaddress
import logging
import socket
import ssl
from unittest.mock import AsyncMock, Mock

import httpcore
import httpx
import httpx._client
import pytest

from world_intel_mcp import fetcher as fm
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.public_transport import PublicSourceTransport


def wire(body=b"ok", *, status="200 OK", headers=()):
    pairs = [("Content-Length", str(len(body))), *headers]
    return (f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in pairs) + "\r\n").encode() + body


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self, response=None):
        self.response = wire() if response is None else response
        self.writes = []
        self.tls = []
        self.closed = False
        self.tls_started = None

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, data, timeout=None):
        self.writes.append(data)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((ssl_context, server_hostname, timeout))
        if self.tls_started is not None:
            self.tls_started.set()
            await asyncio.Future()
        return self

    async def aclose(self):
        self.closed = True

    def get_extra_info(self, name):
        return False if name == "is_readable" else None


class NumericConnector(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.streams = []
        self.calls = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # This assertion catches a hostname fallback independently of DNS data.
        assert str(ipaddress.ip_address(host)) == host
        self.calls.append((host, port, timeout))
        return self.streams.pop(0)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    blocked = Mock(side_effect=AssertionError("real network is forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def strict_network(cache, monkeypatch):
    connector = NumericConnector()
    dns_calls = []
    answers = {"public.example": ["8.8.8.8"], "private.example": ["10.0.0.1"], "localhost": ["127.0.0.1"]}

    async def resolver(host, port):
        dns_calls.append((host, port))
        value = answers[host]
        return value() if callable(value) else list(value)

    transports = []
    def create_transport(**kwargs):
        transport = PublicSourceTransport(**kwargs, resolver=resolver, connector=connector)
        transports.append(transport)
        return transport

    # Only the external resolver/connector are replaced. Fetcher constructs its
    # real owned client, transport and httpcore pool, with real wire parsing.
    monkeypatch.setattr(fm, "PublicSourceTransport", create_transport)
    fetcher = fm.Fetcher(cache, CircuitBreaker())
    return fetcher, connector, answers, dns_calls, transports


@pytest.mark.parametrize("kind", ["json", "text", "xml", "bytes"])
@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "http://private.example/private"])
async def test_public_redirect_private_is_terminal_with_stale_no_writes(strict_network, monkeypatch, kind, target):
    fetcher, connector, _, dns, transports = strict_network
    stream = WireStream(wire(b"", status="302 Found", headers=[("Location", target)]))
    connector.streams.append(stream)
    fetcher.cache.set("old", {"previous": True} if kind == "json" else "previous", -1)
    fetcher.vector_store = Mock(store=AsyncMock())
    original_sleep = asyncio.sleep
    async def no_backoff(delay):
        assert delay == 0, "policy denial must not back off/retry"
        await original_sleep(0)
    monkeypatch.setattr(fm.asyncio, "sleep", no_backoff)
    try:
        result = await getattr(fetcher, f"get_{kind}")(
            "http://public.example/start", "test-public", **({} if kind == "bytes" else {"cache_key": "old"}),
        )
        if kind == "bytes":
            assert not result.ok and result.status is None and "PublicSourcePolicyError" in result.error
            assert target not in result.error  # Scheme/host only, no target path.
        elif kind == "json":
            assert result["previous"] is True and result["_stale"] is True
        else:
            assert result == "previous"
        assert len(transports) == 1 and isinstance(transports[0]._pool, httpcore.AsyncConnectionPool)
        assert len(connector.calls) == 1 and connector.calls[0][0] == "8.8.8.8"
        assert len(dns) == (2 if "private.example" in target else 1)
        assert fetcher.breaker.status()["test-public"]["total_failures"] == 1
        assert fetcher.cache.get("old") is None
        fetcher.vector_store.store.assert_not_called()
    finally:
        await fetcher.close()
    assert stream.closed


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/", "http://[::1]/"])
async def test_default_get_denies_local_even_with_local_source_label(strict_network, url):
    fetcher, connector, _, _, _ = strict_network
    try:
        result = await fetcher.get_bytes(url, source="local-operator")
        assert not result.ok and result.status is None
        assert not connector.calls
        assert fetcher.get_transport_mode == "strict-public"
        assert fetcher.breaker.status()["local-operator"]["total_failures"] == 1
    finally:
        await fetcher.close()


async def test_pin_reuse_and_new_connection_revalidates_rebinding(strict_network):
    fetcher, connector, answers, dns, transports = strict_network
    stream = WireStream()
    connector.streams.append(stream)
    answers["public.example"] = lambda: ["8.8.8.8"] if len(dns) == 1 else ["127.0.0.1"]
    try:
        assert (await fetcher.get_bytes("https://public.example:8443/path", "test-public", timeout=2)).content == b"ok"
        assert len(dns) == 1 and connector.calls[0][:2] == ("8.8.8.8", 8443)
        assert 0 < connector.calls[0][2] <= 2
        context = transports[0]._ssl_context
        assert stream.tls[0][:2] == (context, "public.example")
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert b"Host: public.example:8443\r\n" in b"".join(stream.writes)
        stream.response = wire(headers=[("Connection", "close")])
        assert (await fetcher.get_bytes("https://public.example:8443/again", "test-public")).content == b"ok"
        assert len(dns) == 1  # Reuse remains on the vetted socket.
        result = await fetcher.get_bytes("https://public.example:8443/new", "test-public")
        assert not result.ok and len(dns) == 2
        assert len(connector.calls) == 1  # No private or hostname fallback dial.
        assert transports[0]._pool._max_connections == 200
        assert transports[0]._pool._max_keepalive_connections == 40
    finally:
        await fetcher.close()
    assert stream.closed


async def test_post_uses_separate_operator_pool_and_does_not_follow(strict_network, monkeypatch):
    fetcher, connector, _, _, _ = strict_network
    stream = WireStream(wire(b"", status="302 Found", headers=[("Location", "http://127.0.0.1/elsewhere")]))
    calls = []
    async def operator_connect(backend, host, port, **kwargs):
        calls.append((host, port))
        return stream
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", operator_connect)
    response = await fetcher.post("http://127.0.0.1:9876/sink", json={"operator": "test"})
    assert response.status_code == 302 and calls == [("127.0.0.1", 9876)]
    assert fetcher._client is None  # POST does not construct a public GET pool.
    post = fetcher._post_client
    assert isinstance(post._transport, httpx.AsyncHTTPTransport)
    assert not post.follow_redirects and not post.trust_env
    assert not (await fetcher.get_bytes("http://127.0.0.1/", "test-public")).ok
    get = fetcher._client
    assert get is not post and isinstance(get._transport, PublicSourceTransport)
    assert not connector.calls
    await fetcher.close()
    assert get.is_closed and post.is_closed and stream.closed


async def test_trusted_environment_keeps_proxy_route_and_honest_mode(cache, monkeypatch, caplog):
    stream = WireStream()
    calls = []
    async def connect(backend, host, port, **kwargs):
        calls.append((host, port))
        return stream
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    discovery = Mock(return_value={"all://": "http://127.0.0.1:9999"})
    monkeypatch.setattr(httpx._client, "get_environment_proxies", discovery)
    monkeypatch.setattr(fm, "PublicSourceTransport", Mock(side_effect=AssertionError("must not bypass opt-in proxy")))
    fetcher = fm.Fetcher(cache, CircuitBreaker(), trust_env=True, verify=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    try:
        with caplog.at_level(logging.INFO):
            assert (await fetcher.get_bytes("http://127.0.0.1:8888/local", "test-public")).content == b"ok"
        assert calls == [("127.0.0.1", 9999)]
        assert b"GET http://127.0.0.1:8888/local HTTP/1.1" in b"".join(stream.writes)
        assert fetcher.get_transport_mode == "trusted-environment"
        assert "Fetcher GET transport mode: trusted-environment" in caplog.text
        discovery.assert_called_once_with()
        with pytest.raises(AttributeError):
            fetcher.trust_env = False
    finally:
        await fetcher.close()


async def test_injected_client_is_trusted_and_keeps_configuration(cache):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="local")), trust_env=False)
    fetcher = fm.Fetcher(cache, CircuitBreaker(), client=client)
    try:
        assert fetcher.get_transport_mode == "trusted-injected"
        assert (await fetcher.get_bytes("http://127.0.0.1/", "test-public")).content == b"local"
        assert (await fetcher.post("http://127.0.0.1/sink")).text == "local"
        assert await fetcher._get_client() is client and fetcher._post_client is None
        assert not client.trust_env
    finally:
        await fetcher.close()
    assert client.is_closed


async def test_closed_injected_replacement_obeys_constructor_policy(strict_network):
    fetcher, connector, _, _, _ = strict_network
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    fetcher._client = injected
    assert fetcher.get_transport_mode == "trusted-injected"
    await injected.aclose()
    try:
        assert fetcher.get_transport_mode == "strict-public"
        result = await fetcher.get_bytes("http://127.0.0.1/", "test-public")
        assert not result.ok and fetcher._client is not injected
        assert isinstance(fetcher._client._transport, PublicSourceTransport)
        assert not connector.calls
    finally:
        await fetcher.close()


@pytest.mark.parametrize("failure", [RuntimeError("close failed"), asyncio.CancelledError()])
async def test_close_attempts_both_owned_pools_even_on_error(strict_network, monkeypatch, failure):
    fetcher, _, _, _, _ = strict_network
    get = await fetcher._get_client()
    post = await fetcher._get_post_client()
    original = get.aclose
    async def close_then_fail():
        await original()
        raise failure
    monkeypatch.setattr(get, "aclose", close_then_fail)
    with pytest.raises(type(failure)):
        await fetcher.close()
    assert get.is_closed and post.is_closed
    assert fetcher._client is None and fetcher._post_client is None
    await fetcher.close()  # Idempotent after cleanup.


async def test_get_cancellation_during_tls_closes_tcp(strict_network):
    fetcher, connector, _, _, _ = strict_network
    stream = WireStream()
    stream.tls_started = asyncio.Event()
    connector.streams.append(stream)
    task = asyncio.create_task(fetcher.get_bytes("https://public.example/", "test-public"))
    await stream.tls_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await fetcher.close()
    assert stream.closed and len(connector.calls) == 1


async def test_real_pool_preserves_bounded_gzip_and_auth_headers(strict_network):
    fetcher, connector, _, _, _ = strict_network
    body = gzip.compress(b'{"ok":true}')
    stream = WireStream(wire(body, headers=[("Content-Encoding", "gzip")]))
    connector.streams.append(stream)
    try:
        assert await fetcher.get_json("https://public.example/data", "test-public", headers={"Authorization": "Bearer synthetic"}) == {"ok": True}
        request = b"".join(stream.writes)
        assert b"Authorization: Bearer synthetic\r\n" in request
        assert b"Accept-Encoding: gzip, deflate\r\n" in request
    finally:
        await fetcher.close()


async def test_context_mutation_is_terminal_in_fetcher(strict_network):
    fetcher, connector, _, dns, transports = strict_network
    connector.streams.append(WireStream())
    try:
        assert (await fetcher.get_bytes("https://public.example/", "test-public")).ok
        transports[0]._ssl_context.check_hostname = False
        result = await fetcher.get_bytes("https://public.example/", "test-public")
        assert not result.ok and "PublicSourcePolicyError" in result.error
        assert len(connector.calls) == len(dns) == 1
        assert fetcher.breaker.status()["test-public"]["total_failures"] == 1
    finally:
        await fetcher.close()
