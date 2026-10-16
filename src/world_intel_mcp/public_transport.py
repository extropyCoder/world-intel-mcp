"""Strict-direct HTTP transport for untrusted public-source URLs.

Used by Fetcher's strict owned GET profile; operator/SDK clients remain separate.
A transport owns one fixed, direct-only pool; do not mount a proxy alongside it.
Uses public httpcore 1.x and HTTPX >=0.27 APIs, and the asyncio runtime.

DNS answers are checked as a complete set, then only canonical numeric addresses
are passed to the connector. The original origin remains in HTTP Host and TLS
SNI/certificate verification. Existing connections remain pinned to the vetted
peer; every replacement connection resolves and validates again.

This is an address policy, not exhaustive cloud-provider metadata protection or
a substitute for egress firewalling. In particular, publicly routed services can
themselves proxy requests. The explicit Azure platform VIP denial supplements
non-global/link-local metadata denials. IPv6 transition/translation mechanisms
(NAT64, 6to4, Teredo, deprecated compatible IPv4) are conservatively excluded.

System getaddrinfo runs in asyncio's executor: timing out stops waiting, but
cannot stop a libc resolver thread already running. Answer count is bounded at
the adapter boundary, not inside libc. No third-party DNS library is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import contextmanager
import ipaddress
import math
import socket
import ssl
from typing import Any

import httpcore
import httpx


class PublicSourcePolicyError(httpx.RequestError):
    """Terminal policy denial; callers must not retry via an unguarded client."""


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

# Explicit IANA special-purpose exclusions also make the policy consistent on
# Python versions predating ipaddress classification corrections. Denying the
# whole IETF protocol-assignment blocks is deliberately conservative (including
# their few globally reachable exceptions). Mapped IPv4 is normalized instead.
_EXCLUDED_NETWORKS = tuple(
    ipaddress.ip_network(prefix)
    for prefix in (
        "192.0.0.0/24",  # IETF protocol assignments.
        "192.88.99.0/24",  # Deprecated 6to4 relay anycast.
        "64:ff9b::/96", "64:ff9b:1::/48",  # Well-known/local-use NAT64.
        "2002::/16",  # 6to4 embeds an IPv4 routing destination.
        "2001::/23",  # IETF assignments, including Teredo and ORCHID v1/v2.
        "::/96",  # Deprecated IPv4-compatible addresses.
        "3fff::/20",  # IPv6 documentation, allocated in 2024.
    )
)
_AZURE_PLATFORM_VIP = ipaddress.ip_address("168.63.129.16")


def approved_public_ip(value: str) -> str:
    """Return a canonical public IP, or raise a terminal policy error.

    ipaddress intentionally rejects legacy integer, octal, hex and shortened
    IPv4 spellings. Hostnames with those spellings still go through DNS, and
    *all* resulting addresses must pass this same policy.
    """
    if "%" in value:
        raise PublicSourcePolicyError("Scoped IP addresses are not public sources")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PublicSourcePolicyError("Resolver returned a non-IP address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or getattr(address, "is_site_local", False)
        or address == _AZURE_PLATFORM_VIP
        or any(address in network for network in _EXCLUDED_NETWORKS)
    ):
        raise PublicSourcePolicyError("Destination is not an approved public IP")
    return str(address)


async def system_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve both families once; the backend enforces duration/count bounds."""
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    # Do not silently discard an IPv6 sockaddr scope_id: surface it so the
    # address gate rejects scoped answers even when libc omits a '%' suffix.
    return [
        f"{sockaddr[0]}%{sockaddr[3]}"
        if family == socket.AF_INET6 and sockaddr[3] else sockaddr[0]
        for family, _, _, _, sockaddr in answers
    ]


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Per-connection public IP gate, with one DNS+TCP deadline.

    Resolver/connector injection is for deterministic tests, not a runtime
    profile escape hatch. The connector must honor cancellation and numeric IPs
    (the default public AnyIOBackend does). Sequential fallback reserves a fair
    share of remaining time for every address, avoiding orphaned racing sockets.
    """

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        connector: httpcore.AsyncNetworkBackend | None = None,
        resolve_timeout: float = 5.0,
        connect_timeout: float = 10.0,
        max_answers: int = 32,
    ) -> None:
        for value in (resolve_timeout, connect_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeout bounds must be finite and positive")
        if not isinstance(max_answers, int) or not 1 <= max_answers <= 256:
            raise ValueError("max_answers must be between 1 and 256")
        self._resolver = resolver
        self._connector = connector if connector is not None else httpcore.AnyIOBackend()
        self._resolve_timeout = resolve_timeout
        self._connect_timeout = connect_timeout
        self._max_answers = max_answers

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if local_address is not None:
            raise PublicSourcePolicyError("Local binding is not supported for public sources")
        budget = self._connect_timeout if timeout is None else min(timeout, self._connect_timeout)
        if not math.isfinite(budget) or budget <= 0:
            raise httpcore.ConnectTimeout("Public-source DNS/TCP deadline exceeded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        try:
            async with asyncio.timeout(budget):
                try:
                    literal = ipaddress.ip_address(host)
                except ValueError:
                    literal = None
                if literal is not None:
                    addresses = [approved_public_ip(host)]
                else:
                    if "%" in host:
                        raise PublicSourcePolicyError("Scoped destinations are not supported")
                    async with asyncio.timeout(min(self._resolve_timeout, budget)):
                        answers = await self._resolver(host, port)
                    if not answers or len(answers) > self._max_answers:
                        raise PublicSourcePolicyError("DNS answer count is outside the public-source limit")
                    # Validate the entire set BEFORE dialing any answer; reject mixed sets.
                    addresses = list(dict.fromkeys(approved_public_ip(item) for item in answers))
                options = tuple(socket_options) if socket_options is not None else None
                last_error: Exception | None = None
                for index, address in enumerate(addresses):
                    remaining = deadline - loop.time()
                    attempt_timeout = remaining / (len(addresses) - index)
                    try:
                        async with asyncio.timeout(attempt_timeout):
                            stream = await self._connector.connect_tcp(
                                host=address, port=port, timeout=attempt_timeout,
                                local_address=None, socket_options=options,
                            )
                        return _GuardedStream(stream)
                    except (TimeoutError, httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                        last_error = exc
                if isinstance(last_error, (TimeoutError, httpcore.ConnectTimeout)):
                    raise httpcore.ConnectTimeout("Public-source TCP connection timed out") from last_error
                raise httpcore.ConnectError("All approved public addresses failed") from last_error
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Public-source DNS/TCP deadline exceeded") from exc
        except OSError as exc:
            raise httpcore.ConnectError("Public-source DNS resolution failed") from exc

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise PublicSourcePolicyError("Unix sockets are not public sources")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _GuardedStream(httpcore.AsyncNetworkStream):
    """Ensure failed/cancelled TLS upgrades close the already-open TCP stream."""

    def __init__(self, stream: httpcore.AsyncNetworkStream) -> None:
        self._stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        try:
            stream = await self._stream.start_tls(ssl_context, server_hostname, timeout)
            return _GuardedStream(stream)
        except BaseException:
            # Shield cleanup from cancellation; bound waiting even for a broken peer.
            task = asyncio.create_task(self._stream.aclose())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                pass  # Preserve the original TLS/cancellation failure.
            raise

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


_EXCEPTIONS = {
    getattr(httpcore, name): getattr(httpx, name)
    for name in (
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
        "NetworkError", "ConnectError", "ReadError", "WriteError", "ProxyError",
        "UnsupportedProtocol", "ProtocolError", "LocalProtocolError", "RemoteProtocolError",
    )
}


@contextmanager
def _map_errors() -> Any:
    try:
        yield
    except Exception as exc:
        for cls in type(exc).__mro__:
            if cls in _EXCEPTIONS:
                raise _EXCEPTIONS[cls](str(exc)) from exc
        raise


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_errors():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self) -> None:
        with _map_errors():
            await self._stream.aclose()


class PublicSourceTransport(httpx.AsyncBaseTransport):
    """HTTPX adapter over a public httpcore direct-only pool.

    No proxy, UDS, custom Host/SNI, disabled certificate verification or mutable
    safety profiles. Supply a verifying SSLContext for custom trust roots. Client
    construction must also use trust_env=False and no proxy/mount overrides;
    client-level mounts could otherwise route around *any* custom transport.
    """

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        limits: httpx.Limits | None = None,
        http1: bool = True,
        http2: bool = False,
        resolver: Resolver = system_resolver,
        connector: httpcore.AsyncNetworkBackend | None = None,
        resolve_timeout: float = 5.0,
        connect_timeout: float = 10.0,
        max_answers: int = 32,
    ) -> None:
        context = ssl_context if ssl_context is not None else httpx.create_ssl_context(
            verify=True, trust_env=False,
        )
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("Public sources require certificate and hostname verification")
        if not http1 and not http2:
            raise ValueError("At least one HTTP version must be enabled")
        if http2:
            # Match HTTPX's optional dependency behavior rather than fail mid-request.
            import h2  # noqa: F401
        self._ssl_context = context
        limits = limits if limits is not None else httpx.Limits()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1, http2=http2, retries=0,
            network_backend=PublicNetworkBackend(
                resolver=resolver, connector=connector, resolve_timeout=resolve_timeout,
                connect_timeout=connect_timeout, max_answers=max_answers,
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https") or not request.url.raw_host:
            raise PublicSourcePolicyError("Only HTTP(S) public sources are supported", request=request)
        if self._ssl_context.verify_mode != ssl.CERT_REQUIRED or not self._ssl_context.check_hostname:
            raise PublicSourcePolicyError("TLS verification was disabled", request=request)
        # Executed after HTTPX request hooks, including for pooled connections.
        hosts = request.headers.get_list("host")
        expected_host = request.url.netloc.decode("ascii")
        if len(hosts) != 1 or hosts[0].lower() != expected_host.lower():
            raise PublicSourcePolicyError("Host must match the request URL authority", request=request)
        sni = request.extensions.get("sni_hostname")
        if sni is not None and sni != request.url.raw_host.decode("ascii"):
            raise PublicSourcePolicyError("SNI must match the request URL host", request=request)
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("PublicSourceTransport requires an async request stream")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        )
        with _map_errors():
            response = await self._pool.handle_async_request(core_request)
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self) -> None:
        with _map_errors():
            await self._pool.aclose()
