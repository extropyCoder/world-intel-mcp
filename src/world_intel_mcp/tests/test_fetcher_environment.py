"""Owned HTTPX client proxy/CA wiring, using synthetic environment discovery.

No MockTransport is injected: HTTPX builds its real direct/proxy transports.
No network connections or real environment proxy/certificate values are used.
"""

import socket
import ssl
from pathlib import Path
from unittest.mock import Mock

import certifi
import httpcore
import httpx
import httpx._client
import pytest

from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.public_transport import PublicSourceTransport


@pytest.fixture
def breaker():
    return CircuitBreaker()


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    blocked = Mock(side_effect=AssertionError("environment-policy tests must remain offline"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def synthetic_environment(monkeypatch, tmp_path):
    bundle = Path(certifi.where()).read_bytes()
    start = bundle.index(b"-----BEGIN CERTIFICATE-----")
    end = bundle.index(b"-----END CERTIFICATE-----", start) + len(b"-----END CERTIFICATE-----")
    certificate = bundle[start:end] + b"\n"
    ca_file = tmp_path / "approved-ca.pem"
    ca_file.write_bytes(certificate)
    ca_dir = tmp_path / "approved-ca-dir"
    ca_dir.mkdir()
    (ca_dir / "ca.pem").write_bytes(certificate)
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-proxy.test:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://synthetic-proxy.test:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    discovery = Mock(return_value={
        "all://": "http://synthetic-proxy.test:3128", "all://internal.example": None,
    })
    monkeypatch.setattr(httpx._client, "get_environment_proxies", discovery)
    # Both HTTPX versions load real certificates through this SSLContext API,
    # but construct the context differently. Observe loading, not its factory.
    loads = []
    original_load = ssl.SSLContext.load_verify_locations
    def load(context, cafile=None, capath=None, cadata=None):
        loads.append((context, cafile, capath))
        return original_load(context, cafile=cafile, capath=capath, cadata=cadata)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", load)
    return discovery, loads, ca_file, ca_dir


@pytest.mark.parametrize("kwargs", [{}, {"trust_env": False}])
async def test_owned_default_and_explicit_false_skip_environment(cache, breaker, synthetic_environment, kwargs):
    discovery, loads, ca_file, _ = synthetic_environment
    fetcher = Fetcher(cache, breaker, **kwargs)
    try:
        client = await fetcher._get_client()
        assert client.trust_env is False
        assert isinstance(client._transport, PublicSourceTransport)
        assert fetcher.get_transport_mode == "strict-public"
        discovery.assert_not_called()
        context = client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.get_ca_certs()
        assert (context, certifi.where(), None) in loads
        assert all(cafile != str(ca_file) for _, cafile, _ in loads)
        assert client._mounts == {}
        assert type(client._transport._pool) is httpcore.AsyncConnectionPool
        assert client._transport._ssl_context is context
        assert await fetcher._get_client() is client
    finally:
        await fetcher.close()


@pytest.mark.parametrize("use_file", [False, True])
async def test_owned_opt_in_wires_proxy_no_proxy_and_ca_environment(
    cache, breaker, synthetic_environment, monkeypatch, use_file,
):
    discovery, loads, ca_file, ca_dir = synthetic_environment
    if not use_file:
        monkeypatch.delenv("SSL_CERT_FILE")
    fetcher = Fetcher(cache, breaker, trust_env=True)
    try:
        client = await fetcher._get_client()
        discovery.assert_called_once_with()
        assert client.trust_env is True
        assert not isinstance(client._transport, PublicSourceTransport)
        assert fetcher.get_transport_mode == "trusted-environment"
        proxy = client._transport_for_url(httpx.URL("https://public.example/resource"))
        assert type(proxy._pool) is httpcore.AsyncHTTPProxy
        assert proxy._pool._proxy_url.host == b"synthetic-proxy.test"
        assert proxy._pool._proxy_url.port == 3128
        contexts = [client._transport._pool._ssl_context, proxy._pool._ssl_context]
        for context in contexts:
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            expected = (context, str(ca_file), None) if use_file else (context, None, str(ca_dir))
            assert expected in loads
            if use_file:
                der = ssl.PEM_cert_to_DER_cert(ca_file.read_text())
                assert context.get_ca_certs(binary_form=True) == [der]
            # capath certificates are loaded lazily during verification; checking
            # its real load configuration is not a live certificate-chain test.
        # The discovered NO_PROXY exclusion really selects the direct transport.
        assert client._transport_for_url(httpx.URL("https://internal.example/resource")) is client._transport
    finally:
        await fetcher.close()


@pytest.mark.parametrize("trust_env", [False, True])
async def test_explicit_ca_context_takes_precedence(cache, breaker, synthetic_environment, trust_env):
    discovery, loads, _, _ = synthetic_environment
    explicit = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    fetcher = Fetcher(cache, breaker, verify=explicit, trust_env=trust_env)
    try:
        client = await fetcher._get_client()
        assert client.trust_env is trust_env
        assert client._transport._pool._ssl_context is explicit
        assert explicit.verify_mode == ssl.CERT_REQUIRED and explicit.check_hostname
        assert discovery.call_count == int(trust_env)
        if trust_env:
            proxy = client._transport_for_url(httpx.URL("https://public.example"))
            assert proxy._pool._ssl_context is explicit
        assert loads == []
    finally:
        await fetcher.close()


@pytest.mark.parametrize("injected_trust", [False, True])
async def test_injected_client_retains_its_proxy_ca_and_trust_settings(
    cache, breaker, synthetic_environment, injected_trust,
):
    discovery, loads, _, _ = synthetic_environment
    explicit = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    other = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with httpx.AsyncClient(
        proxy="http://deliberate-proxy.test:8443", verify=explicit, trust_env=injected_trust,
    ) as injected:
        mounts = dict(injected._mounts)
        transport = injected._transport
        proxy = injected._transport_for_url(httpx.URL("https://public.example"))
        fetcher = Fetcher(cache, breaker, client=injected, trust_env=not injected_trust, verify=other)
        assert await fetcher._get_client() is injected
        assert fetcher.get_transport_mode == "trusted-injected"
        assert injected.trust_env is injected_trust
        assert injected._mounts == mounts and injected._transport is transport
        assert proxy._pool._proxy_url.host == b"deliberate-proxy.test"
        assert proxy._pool._ssl_context is explicit
        assert injected._transport._pool._ssl_context is explicit
        discovery.assert_not_called()
        assert loads == []


@pytest.mark.parametrize("failure", ["proxy", "ca"])
async def test_opt_in_configuration_errors_do_not_silently_fall_back(
    cache, breaker, synthetic_environment, failure,
):
    discovery, _, ca_file, _ = synthetic_environment
    if failure == "proxy":
        discovery.return_value = {"all://": "gopher://synthetic-proxy.test:3128"}
        error = ValueError
    else:
        # It must exist: HTTPX 0.27.1 ignores nonexistent env CA paths.
        ca_file.write_text("not a CA certificate\n")
        error = ssl.SSLError
    fetcher = Fetcher(cache, breaker, trust_env=True)
    with pytest.raises(error):
        await fetcher._get_client()
    assert fetcher._client is None


async def test_nonexistent_env_ca_paths_keep_httpx_version_behavior(
    cache, breaker, synthetic_environment, monkeypatch,
):
    _, loads, ca_file, ca_dir = synthetic_environment
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file.with_name("missing.pem")))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir.with_name("missing-directory")))
    fetcher = Fetcher(cache, breaker, trust_env=True)
    try:
        if tuple(map(int, httpx.__version__.split(".")[:2])) < (0, 28):
            # 0.27.1 checks existence before selecting environment CA paths.
            client = await fetcher._get_client()
            context = client._transport._pool._ssl_context
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert context.get_ca_certs()
            assert (context, certifi.where(), None) in loads
        else:
            # 0.28.1 tries the configured CA file and propagates its file error.
            with pytest.raises(FileNotFoundError):
                await fetcher._get_client()
            assert fetcher._client is None
    finally:
        await fetcher.close()


async def test_recreated_owned_client_keeps_explicit_policy(cache, breaker, synthetic_environment):
    discovery, loads, _, _ = synthetic_environment
    explicit = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    fetcher = Fetcher(cache, breaker, trust_env=False, verify=explicit)
    try:
        first = await fetcher._get_client()
        await first.aclose()
        second = await fetcher._get_client()
        assert second is not first and not second.is_closed
        assert second.trust_env is False
        assert second._transport._pool._ssl_context is explicit
        discovery.assert_not_called()
        assert loads == []
    finally:
        await fetcher.close()


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", {}, object()])
def test_trust_env_requires_actual_bool(cache, breaker, value):
    with pytest.raises(ValueError, match="trust_env must be a bool"):
        Fetcher(cache, breaker, trust_env=value)


@pytest.mark.parametrize("value", [None, 0, 1, "/synthetic/ca.pem", Path("ca.pem"), object()])
def test_verify_rejects_ambiguous_or_deprecated_config(cache, breaker, value):
    with pytest.raises(ValueError, match="verify must be a bool or SSLContext"):
        Fetcher(cache, breaker, verify=value)


async def test_explicit_verify_false_is_not_an_implicit_default(cache, breaker, synthetic_environment):
    fetcher = Fetcher(cache, breaker, verify=False)
    try:
        with pytest.raises(ValueError, match="verification"):
            await fetcher._get_client()
        # Operator POST configuration remains separate; strict GET cannot be
        # silently downgraded to an unguarded pool by an insecure TLS setting.
        client = await fetcher._get_post_client()
        context = client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_NONE and not context.check_hostname
        assert client.trust_env is False
    finally:
        await fetcher.close()


async def test_real_default_tls_and_owned_requests_still_work(cache, breaker, monkeypatch):
    # Invalid synthetic env CA paths must not be opened. Unlike the selection
    # tests above, this uses the actual HTTPX/ssl default certificate context.
    monkeypatch.setenv("SSL_CERT_FILE", "/synthetic/nonexistent-file.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/synthetic/nonexistent-directory")
    discovery = Mock(side_effect=AssertionError("default must not discover env proxies"))
    monkeypatch.setattr(httpx._client, "get_environment_proxies", discovery)
    seen = []

    async def reply(transport, request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    # This is a CA/owned-client test, not destination-policy proof. The latter
    # uses the real httpcore pool with fake DNS/TCP in test_fetcher_public.py.
    monkeypatch.setattr(PublicSourceTransport, "handle_async_request", reply)
    fetcher = Fetcher(cache, breaker)
    try:
        client = await fetcher._get_client()
        context = client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.get_ca_certs()
        assert await fetcher.get_json("https://provider.example/data", "synthetic") == {"ok": True}
        assert len(seen) == 1 and seen[0].headers["accept-encoding"] == "gzip, deflate"
        discovery.assert_not_called()
    finally:
        await fetcher.close()
