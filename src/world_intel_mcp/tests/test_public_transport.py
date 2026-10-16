"""Offline tests: real httpcore pool, fake DNS/TCP/TLS only."""

import asyncio
import ipaddress
import socket
import ssl

import httpcore
import httpx
import pytest

from world_intel_mcp.public_transport import (
    PublicNetworkBackend,
    PublicSourcePolicyError,
    PublicSourceTransport,
    approved_public_ip,
    system_resolver,
)


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, response=None):
        self.response = response or b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        self.writes = []
        self.tls = []
        self.closed = False
        self.read_error = None
        self.tls_error = None
        self.tls_wait = None

    async def read(self, max_bytes, timeout=None):
        if self.read_error:
            raise self.read_error
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((ssl_context, server_hostname, timeout))
        if self.tls_wait is not None:
            self.tls_wait.set()
            await asyncio.Future()
        if self.tls_error:
            raise self.tls_error
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


class Connector(httpcore.AsyncNetworkBackend):
    def __init__(self, streams=None):
        self.streams = streams or [Stream()]
        self.calls = []
        self.block = set()
        self.fail = set()
        self.cancelled = []
        self.started = asyncio.Event()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # A hostname fallback is always a test failure, even if the fake DNS changes.
        assert str(ipaddress.ip_address(host)) == host
        self.calls.append((host, port, timeout, local_address, socket_options))
        self.started.set()
        if host in self.block:
            try:
                await asyncio.Future()
            finally:
                self.cancelled.append(host)
        if host in self.fail:
            raise httpcore.ConnectError("unreachable")
        return self.streams.pop(0)


def resolver_for(*answers):
    calls = []

    async def resolve(host, port):
        calls.append((host, port))
        return list(answers)

    return resolve, calls


@pytest.mark.parametrize("address", [
    "0.0.0.0", "0.1.2.3", "10.0.0.1", "100.64.0.1", "100.127.255.255",
    "127.0.0.1", "169.254.169.254", "169.254.170.2", "172.16.0.1", "192.168.1.1",
    "192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1", "224.0.0.1",
    "239.255.255.255", "240.0.0.1", "255.255.255.255", "168.63.129.16",
    "::", "::1", "fc00::1", "fe80::1", "fec0::1", "ff0e::1", "2001:db8::1",
    "::ffff:127.0.0.1", "::ffff:100.64.0.1", "::ffff:168.63.129.16",
    "64:ff9b::808:808", "64:ff9b:1::1", "2002:808:808::1", "2001:0:1234::1",
    "::8.8.8.8", "2606:4700::1111%eth0", "fe80::1%1",
    "192.0.0.8", "192.0.0.9", "192.0.0.170", "192.88.99.1", "2001:20::1",
    "2001:10::1", "2001:2::1", "3fff::1", "5f00::1",
    "2130706433", "0177.0.0.1", "0x7f000001", "127.1", "not-an-ip",
])
def test_special_address_policy(address):
    with pytest.raises(PublicSourcePolicyError):
        approved_public_ip(address)


@pytest.mark.parametrize(("value", "canonical"), [
    ("8.8.8.8", "8.8.8.8"), ("2606:4700:4700:0:0:0:0:1111", "2606:4700:4700::1111"),
    ("::ffff:8.8.8.8", "8.8.8.8"),
])
def test_public_address_normalization(value, canonical):
    assert approved_public_ip(value) == canonical


async def test_origin_tls_stream_and_no_second_lookup():
    stream = Stream()
    connector = Connector([stream])
    calls = []

    async def rebinding(host, port):
        calls.append((host, port))
        return ["8.8.8.8"] if len(calls) == 1 else ["127.0.0.1"]

    context = ssl.create_default_context()
    transport = PublicSourceTransport(resolver=rebinding, connector=connector, ssl_context=context)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        async with client.stream("GET", "https://public.example:8443/path?q=yes") as response:
            assert response.status_code == 200
            assert response.http_version == "HTTP/1.1"
            assert not response.is_closed
            assert await response.aread() == b"ok"
        assert response.is_closed
    assert stream.closed
    assert calls == [("public.example", 8443)]
    assert connector.calls[0][:2] == ("8.8.8.8", 8443)
    assert stream.tls[0][:2] == (context, "public.example")
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    wire = b"".join(stream.writes)
    assert b"GET /path?q=yes HTTP/1.1" in wire
    assert b"Host: public.example:8443\r\n" in wire


@pytest.mark.parametrize("answers", [
    ["8.8.8.8", "::1"], ["2606:4700:4700::1111", "10.0.0.1"],
    ["8.8.8.8", "168.63.129.16"], ["8.8.8.8", "nonsense"], [], ["8.8.8.8"] * 33,
])
async def test_mixed_or_invalid_answers_never_connect(answers):
    resolver, calls = resolver_for(*answers)
    connector = Connector()
    async with httpx.AsyncClient(transport=PublicSourceTransport(resolver=resolver, connector=connector),
                               trust_env=False) as client:
        with pytest.raises(PublicSourcePolicyError):
            await client.get("http://public.example")
    assert len(calls) == 1
    assert not connector.calls


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f000001", "127.1"])
async def test_legacy_numeric_dns_results_cannot_bypass(host):
    resolver, _ = resolver_for("127.0.0.1")
    connector = Connector()
    backend = PublicNetworkBackend(resolver=resolver, connector=connector)
    with pytest.raises(PublicSourcePolicyError):
        await backend.connect_tcp(host, 80)
    assert not connector.calls


async def test_numeric_literals_skip_dns_and_normalize_mapped_ipv4():
    async def forbidden(*args):
        pytest.fail("numeric IP unexpectedly resolved")

    connector = Connector()
    backend = PublicNetworkBackend(resolver=forbidden, connector=connector)
    stream = await backend.connect_tcp("::ffff:8.8.8.8", 443)
    assert connector.calls[0][0] == "8.8.8.8"
    await stream.aclose()
    with pytest.raises(PublicSourcePolicyError):
        await backend.connect_tcp("::ffff:127.0.0.1", 443)


async def test_dual_stack_timeout_fallback_is_bounded_and_cancelled():
    resolver, _ = resolver_for("2606:4700:4700::1111", "8.8.8.8")
    stream = Stream()
    connector = Connector([stream])
    connector.block.add("2606:4700:4700::1111")
    backend = PublicNetworkBackend(resolver=resolver, connector=connector, connect_timeout=0.1)
    async with asyncio.timeout(1):
        result = await backend.connect_tcp("public.example", 443)
    assert [call[0] for call in connector.calls] == ["2606:4700:4700::1111", "8.8.8.8"]
    assert connector.cancelled == ["2606:4700:4700::1111"]
    assert connector.calls[0][2] <= 0.05
    await result.aclose()
    assert stream.closed


async def test_connect_failure_fallback_never_uses_hostname():
    resolver, calls = resolver_for("8.8.8.8", "1.1.1.1")
    connector = Connector()
    connector.fail.add("8.8.8.8")
    backend = PublicNetworkBackend(resolver=resolver, connector=connector)
    result = await backend.connect_tcp("public.example", 80)
    assert len(calls) == 1
    assert [call[0] for call in connector.calls] == ["8.8.8.8", "1.1.1.1"]
    await result.aclose()


async def test_all_failed_no_hostname_fallback_and_httpx_mapping():
    resolver, _ = resolver_for("8.8.8.8", "1.1.1.1")
    connector = Connector()
    connector.fail.update({"8.8.8.8", "1.1.1.1"})
    async with httpx.AsyncClient(transport=PublicSourceTransport(resolver=resolver, connector=connector),
                               trust_env=False) as client:
        with pytest.raises(httpx.ConnectError) as error:
            await client.get("http://public.example")
        assert error.value.request.url.host == "public.example"
    assert len(connector.calls) == 2


async def test_dns_timeout_is_httpx_connect_timeout():
    cancelled = asyncio.Event()

    async def resolver(*args):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    connector = Connector()
    transport = PublicSourceTransport(resolver=resolver, connector=connector, resolve_timeout=0.02)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://public.example")
    assert cancelled.is_set()
    assert not connector.calls


async def test_dns_and_tcp_share_one_deadline():
    async def resolver(*args):
        await asyncio.sleep(0.03)
        return ["8.8.8.8"]

    connector = Connector()
    connector.block.add("8.8.8.8")
    backend = PublicNetworkBackend(resolver=resolver, connector=connector, connect_timeout=0.08)
    async with asyncio.timeout(1):
        with pytest.raises(httpcore.ConnectTimeout):
            await backend.connect_tcp("public.example", 80)
    assert connector.calls[0][2] < 0.06
    assert connector.cancelled == ["8.8.8.8"]


async def test_cancellation_during_tcp_propagates_without_fallback():
    resolver, _ = resolver_for("8.8.8.8", "1.1.1.1")
    connector = Connector()
    connector.block.add("8.8.8.8")
    backend = PublicNetworkBackend(resolver=resolver, connector=connector)
    task = asyncio.create_task(backend.connect_tcp("public.example", 80))
    await connector.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connector.cancelled == ["8.8.8.8"]
    assert len(connector.calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_tls_failure_or_cancellation_closes_tcp(cancel):
    resolver, _ = resolver_for("8.8.8.8")
    stream = Stream()
    if cancel:
        stream.tls_wait = asyncio.Event()
    else:
        stream.tls_error = httpcore.ConnectError("certificate verification failed")
    transport = PublicSourceTransport(resolver=resolver, connector=Connector([stream]))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        task = asyncio.create_task(client.get("https://public.example"))
        if cancel:
            await stream.tls_wait.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else httpx.ConnectError):
            await task
    assert stream.closed


@pytest.mark.parametrize("mutation", ["host", "sni", "url", "duplicate_host"])
async def test_hooks_cannot_change_authority_identity(mutation):
    resolver, _ = resolver_for("8.8.8.8")
    connector = Connector()

    async def hook(request):
        if mutation == "host":
            request.headers["host"] = "169.254.169.254"
        elif mutation == "sni":
            request.extensions["sni_hostname"] = "other.example"
        elif mutation == "url":
            request.url = httpx.URL("http://127.0.0.1/")
        else:
            request.headers = httpx.Headers([("Host", "public.example"), ("Host", "other.example")])

    transport = PublicSourceTransport(resolver=resolver, connector=connector)
    async with httpx.AsyncClient(transport=transport, trust_env=False, event_hooks={"request": [hook]}) as client:
        with pytest.raises(PublicSourcePolicyError):
            await client.get("https://public.example")
    assert not connector.calls


async def test_consistent_hook_private_url_change_still_denied():
    connector = Connector()

    async def hook(request):
        request.url = httpx.URL("http://127.0.0.1/")
        request.headers["host"] = "127.0.0.1"

    async with httpx.AsyncClient(transport=PublicSourceTransport(connector=connector), trust_env=False,
                               event_hooks={"request": [hook]}) as client:
        with pytest.raises(PublicSourcePolicyError):
            await client.get("http://public.example")
    assert not connector.calls


async def test_pool_reuse_then_replacement_revalidates_dns():
    stream = Stream()
    connector = Connector([stream])
    calls = []

    async def resolver(*args):
        calls.append(args)
        return ["8.8.8.8"] if len(calls) == 1 else ["127.0.0.1"]

    transport = PublicSourceTransport(resolver=resolver, connector=connector)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        assert (await client.get("http://public.example")).text == "ok"
        stream.response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        assert (await client.get("http://public.example")).text == "ok"
        with pytest.raises(PublicSourcePolicyError):
            await client.get("http://public.example")
    assert len(calls) == 2
    assert len(connector.calls) == 1


async def test_read_error_mapping_and_close():
    resolver, _ = resolver_for("8.8.8.8")
    stream = Stream(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
    transport = PublicSourceTransport(resolver=resolver, connector=Connector([stream]))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        async with client.stream("GET", "http://public.example") as response:
            stream.read_error = httpcore.ReadTimeout("read deadline")
            with pytest.raises(httpx.ReadTimeout):
                await response.aread()
    assert stream.closed


async def test_system_resolver_uses_both_families_once(monkeypatch):
    calls = []

    async def getaddrinfo(*args, **kwargs):
        calls.append((args, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80)),
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1111", 80, 0, 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    assert await system_resolver("public.example", 80) == ["8.8.8.8", "2606:4700::1111"]
    assert len(calls) == 1
    assert calls[0][1] == dict(family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)


async def test_system_resolver_does_not_discard_ipv6_scope_id(monkeypatch):
    async def getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 ("2606:4700::1111", 80, 0, 2))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    connector = Connector()
    backend = PublicNetworkBackend(connector=connector)
    with pytest.raises(PublicSourcePolicyError):
        await backend.connect_tcp("public.example", 80)
    assert not connector.calls


async def test_environment_proxy_not_used_by_transport(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    resolver, calls = resolver_for("8.8.8.8")
    connector = Connector()
    async with httpx.AsyncClient(transport=PublicSourceTransport(resolver=resolver, connector=connector),
                               trust_env=False) as client:
        assert (await client.get("http://public.example")).status_code == 200
    assert calls == [("public.example", 80)]
    assert connector.calls[0][0] == "8.8.8.8"
    with pytest.raises(TypeError):
        PublicSourceTransport(proxy="http://127.0.0.1:1")


def test_insecure_context_and_invalid_bounds_rejected():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        PublicSourceTransport(ssl_context=context)
    for kwargs in ({"resolve_timeout": 0}, {"connect_timeout": float("inf")}, {"max_answers": 0}):
        with pytest.raises(ValueError):
            PublicNetworkBackend(**kwargs)


async def test_uds_and_local_binding_are_terminal():
    backend = PublicNetworkBackend()
    with pytest.raises(PublicSourcePolicyError):
        await backend.connect_unix_socket("/tmp/service.sock")
    with pytest.raises(PublicSourcePolicyError):
        await backend.connect_tcp("8.8.8.8", 80, local_address="127.0.0.1")


async def test_request_connect_timeout_caps_backend_default():
    resolver, _ = resolver_for("8.8.8.8")
    connector = Connector()
    connector.block.add("8.8.8.8")
    transport = PublicSourceTransport(resolver=resolver, connector=connector, connect_timeout=10)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("http://public.example", timeout=httpx.Timeout(1, connect=0.02))
    assert 0 < connector.calls[0][2] <= 0.02
    assert connector.cancelled == ["8.8.8.8"]


async def test_zero_timeout_cannot_dial():
    connector = Connector()
    backend = PublicNetworkBackend(connector=connector)
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("8.8.8.8", 80, timeout=0)
    assert not connector.calls


async def test_cancel_during_dns_propagates():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def resolver(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    connector = Connector()
    backend = PublicNetworkBackend(resolver=resolver, connector=connector)
    task = asyncio.create_task(backend.connect_tcp("public.example", 80))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not connector.calls


async def test_pool_limits_timeout_and_early_response_close():
    resolver, calls = resolver_for("8.8.8.8")
    first = Stream(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
    connector = Connector([first, Stream()])
    transport = PublicSourceTransport(resolver=resolver, connector=connector,
                                      limits=httpx.Limits(max_connections=1))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        async with client.stream("GET", "http://public.example"):
            with pytest.raises(httpx.PoolTimeout):
                await client.get("http://other.example", timeout=httpx.Timeout(1, pool=0.02))
        assert first.closed
        assert (await client.get("http://other.example")).text == "ok"
    assert len(calls) == 2


async def test_redirect_to_private_denied_by_new_connection():
    resolver, calls = resolver_for("8.8.8.8")
    stream = Stream(b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: http://169.254.169.254/metadata\r\n\r\n")
    connector = Connector([stream])
    transport = PublicSourceTransport(resolver=resolver, connector=connector)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=True) as client:
        with pytest.raises(PublicSourcePolicyError):
            await client.get("http://public.example")
    assert len(connector.calls) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("url,authority,sni,ip", [
    ("https://8.8.8.8/", "8.8.8.8", "8.8.8.8", "8.8.8.8"),
    ("https://[2606:4700:4700::1111]:8443/", "[2606:4700:4700::1111]:8443",
     "2606:4700:4700::1111", "2606:4700:4700::1111"),
])
async def test_ip_literal_host_and_tls_identity(url, authority, sni, ip):
    stream = Stream()
    connector = Connector([stream])
    async with httpx.AsyncClient(transport=PublicSourceTransport(connector=connector), trust_env=False) as client:
        assert (await client.get(url)).status_code == 200
    assert connector.calls[0][0] == ip
    assert stream.tls[0][1] == sni
    assert f"Host: {authority}\r\n".encode() in b"".join(stream.writes)


async def test_context_mutation_after_construction_is_denied():
    context = ssl.create_default_context()
    connector = Connector()
    transport = PublicSourceTransport(ssl_context=context, connector=connector)
    context.check_hostname = False
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(PublicSourcePolicyError):
            await client.get("https://8.8.8.8/")
    assert not connector.calls


async def test_dns_oserror_is_mapped_without_retry():
    calls = []

    async def resolver(*args):
        calls.append(args)
        raise socket.gaierror("no address")

    connector = Connector()
    transport = PublicSourceTransport(resolver=resolver, connector=connector)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://public.example")
    assert len(calls) == 1
    assert not connector.calls
