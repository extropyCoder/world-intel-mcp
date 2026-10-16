"""Async HTTP fetcher with timeout, retry, rate limiting, and circuit breaker integration.

All external HTTP calls in world-intel-mcp go through this module.
Stale-data fallback: when an API call fails, the last-known-good cached
response is returned (marked with _stale=True) so dashboards never go blank.
"""

import asyncio
import json
import logging
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .cache import Cache
from .bounded_decode import (
    BoundedDecoder, BodyLimitError, ResponseTooLarge, INPUT_CHUNK,
    DEFAULT_PROCESSING_TIMEOUT, positive_bytes, processing_timeout as _validate_processing_timeout,
)
from .circuit_breaker import CircuitBreaker
from .diagnostics import exception_summary, redact_text, safe_error, safe_url
from .public_transport import PublicSourcePolicyError, PublicSourceTransport

logger = logging.getLogger("world-intel-mcp.fetcher")

_DEFAULT_JSON_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_TEXT_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_BINARY_MAX_BYTES = 256 * 1024 * 1024


_ResponseTooLarge = ResponseTooLarge


class RedirectPolicyError(Exception):
    """Terminal redirect refusal; messages intentionally contain no URLs."""


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Compare HTTPX-normalized names with effective HTTP(S) ports."""
    port = url.port if url.port is not None else {"http": 80, "https": 443}.get(url.scheme.lower())
    return url.scheme.lower(), url.host.lower(), port


def _validate_max_bytes(value: int) -> int:
    return positive_bytes(value)


async def _read_bounded(
    response: httpx.Response, max_bytes: int, *, max_encoded_bytes: int | None = None,
    processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
) -> bytes:
    """Read raw HTTP bytes with bounded package-owned decoding.

    Deadline starts at body-reader entry, includes stream waits, and excludes
    connection/header acquisition, hooks/auth, parsing and cache operations.
    Pre-buffered HTTPX content is already decoded; prior allocations/wire bytes
    cannot be recovered or bounded here. Content-Length is never trusted.
    """
    encoded_limit = positive_bytes(max_bytes + 1024 * 1024 if max_encoded_bytes is None else max_encoded_bytes)
    timeout = _validate_processing_timeout(processing_timeout)
    values = response.headers.get_list("content-encoding")
    encoding = "identity" if not values else ",".join(values).strip(" \t").lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise BodyLimitError("unsupported or malformed content encoding")
    try:
        async with asyncio.timeout(timeout):
            decoder = BoundedDecoder(encoding, max_bytes, deadline=time.monotonic() + timeout)
            if response.is_stream_consumed:
                # .content is public and already decoded by HTTPX. Never decode
                # it a second time or pretend we observed its original wire size.
                content = response.content
                if len(content) > max_bytes or response.num_bytes_downloaded > encoded_limit:
                    raise _ResponseTooLarge("pre-buffered body exceeds cap")
                if encoding == "identity" and len(content) > encoded_limit:
                    raise _ResponseTooLarge("encoded body exceeds cap")
                await asyncio.sleep(0)
                return content
            body = bytearray()
            encoded = 0
            async for chunk in response.aiter_raw():
                encoded += len(chunk)
                if encoded > encoded_limit:
                    raise _ResponseTooLarge("encoded body exceeds cap")
                for offset in range(0, len(chunk), INPUT_CHUNK):
                    for output in decoder.feed(chunk[offset:offset + INPUT_CHUNK]):
                        body.extend(output)
                        await asyncio.sleep(0)
                decoder.check_time()
                await asyncio.sleep(0)
            decoder.finish()
            return bytes(body)
    except TimeoutError as exc:
        raise BodyLimitError("body processing deadline exceeded") from exc


def _response_text(response: httpx.Response, body: bytes) -> str:
    # Feed already-decoded bytes to HTTPX's normal text decoding, including
    # charset validation, replacement errors and client default autodetection.
    # Do not copy Content-Encoding: that would decompress the body twice.
    if not body:
        return ""
    buffered = httpx.Response(
        200,
        content=body,
        headers={"content-type": response.headers.get("content-type", "")},
        default_encoding=response.default_encoding,
    )
    # Preserve any encoding selected explicitly by a response hook. Resolve
    # the usual content-based default on the buffered bytes first, since the
    # original streaming response has no .content for autodetection.
    response.default_encoding = buffered.encoding
    buffered.encoding = response.encoding
    return buffered.text


# Yahoo Finance requires serialized access (600ms gap)
_yahoo_lock = asyncio.Lock()
_yahoo_last_call: float = 0.0
_YAHOO_MIN_INTERVAL = 0.6  # seconds

# Per-source rate limits (min seconds between calls).
# Sources not listed here have no enforced limit.
_SOURCE_RATE_LIMITS: dict[str, float] = {
    "nominatim": 1.1,  # OSM usage policy: absolute max 1 req/sec
    "ripestat": 1.0,
    "yahoo-finance": 0.6,  # unofficial — ~100 req/min safe
    "opensky": 6.0,  # free tier: 10 req/min
    "coingecko": 2.0,  # free tier: 30 calls/min
    "cloudflare-radar": 3.0,  # 20 req/min
    "reddit": 1.5,  # ~60 req/min (be conservative)
    "nasa-firms": 2.0,  # API key: ~1000 req/day
    "adsblol": 5.0,  # community API — be very polite
    "polymarket": 1.0,  # be polite
    "faa": 1.0,  # govt API
    "usgs": 1.0,  # generous but be polite
    "acled": 2.0,  # API key based
    "nga": 2.0,  # govt API
    "sec-edgar": 1.0,  # SEC EDGAR — be polite
    # adsb.lol globe_history heatmaps are ~14 MB each; one at a time,
    # spaced, so a cold baseline never bursts the community server.
    "adsblol-history": 2.0,
}


@dataclass(frozen=True)
class BinaryResult:
    """Outcome of :meth:`Fetcher.get_bytes`.

    ``status`` is the final HTTP status (None when no response arrived),
    ``content`` the body on 2xx, ``error`` a short reason otherwise. A
    404 is reported as ``status=404`` with no error so callers can tell
    "not published (yet)" apart from "the fetch failed".
    """

    status: int | None
    content: bytes | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.content is not None
_source_locks: dict[str, asyncio.Lock] = {}
_source_last_call: dict[str, float] = {}


class Fetcher:
    """Centralized HTTP fetcher with caching, retries, and circuit breaking.

    Default owned GETs use strict-direct public-address transport. trust_env=True
    opts into trusted HTTPX proxy/CA behavior, outside that address guarantee.
    Open injected clients remain trusted/custom. Owned POSTs have a separate,
    unrestricted operator-sink pool. verify can supply an explicit SSLContext;
    strict GETs require certificate and hostname verification.
    """

    def __init__(
        self,
        cache: Cache,
        breaker: CircuitBreaker,
        default_timeout: float = 15.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        vector_store: Any = None,
        *,
        max_json_bytes: int = _DEFAULT_JSON_MAX_BYTES,
        max_text_bytes: int = _DEFAULT_TEXT_MAX_BYTES,
        max_encoded_bytes: int | None = None,
        processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
        trust_env: bool = False,
        verify: ssl.SSLContext | bool = True,
    ):
        if not isinstance(trust_env, bool):
            raise ValueError("trust_env must be a bool")
        if not isinstance(verify, (bool, ssl.SSLContext)):
            raise ValueError("verify must be a bool or SSLContext")
        self.cache = cache
        self.breaker = breaker
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = client
        self._owned_get_client: httpx.AsyncClient | None = None
        self._post_client: httpx.AsyncClient | None = None
        self._reported_get_mode: str | None = None
        self.vector_store = vector_store  # VectorStore instance (optional)
        self.max_json_bytes = _validate_max_bytes(max_json_bytes)
        self.max_text_bytes = _validate_max_bytes(max_text_bytes)
        self.max_encoded_bytes = None if max_encoded_bytes is None else positive_bytes(max_encoded_bytes)
        self.processing_timeout = _validate_processing_timeout(processing_timeout)
        # These apply only when we create a client, never to an injected client.
        self._trust_env = trust_env
        self.verify = verify

    @property
    def trust_env(self) -> bool:
        """Constructor-only environment/trusted transport opt-in."""
        return self._trust_env

    @property
    def get_transport_mode(self) -> str:
        """Safe diagnostic label, not a claim about a custom client's transport."""
        if (self._client is not None and not self._client.is_closed
                and self._client is not self._owned_get_client):
            return "trusted-injected"
        return "trusted-environment" if self.trust_env else "strict-public"

    async def _read_body(self, response, max_bytes, max_encoded_bytes, processing_timeout):
        return await _read_bounded(
            response, max_bytes,
            max_encoded_bytes=self.max_encoded_bytes if max_encoded_bytes is None else max_encoded_bytes,
            processing_timeout=self.processing_timeout if processing_timeout is None else processing_timeout,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if self._owned_get_client is not None and not self._owned_get_client.is_closed:
                await self._owned_get_client.aclose()
            limits = httpx.Limits(max_connections=200, max_keepalive_connections=40)
            options = {}
            if not self.trust_env:
                context = httpx.create_ssl_context(verify=self.verify, trust_env=False)
                options["transport"] = PublicSourceTransport(ssl_context=context, limits=limits)
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.default_timeout),
                follow_redirects=True,
                limits=limits,
                headers={"User-Agent": "PhoenixAGI-WorldIntel/0.1"},
                trust_env=self.trust_env,
                verify=self.verify,
                **options,
            )
            self._owned_get_client = self._client
        mode = self.get_transport_mode
        if mode != self._reported_get_mode:
            logger.info("Fetcher GET transport mode: %s", mode)
            self._reported_get_mode = mode
        return self._client

    async def _get_post_client(self) -> httpx.AsyncClient:
        if (self._client is not None and not self._client.is_closed
                and self._client is not self._owned_get_client):
            return self._client
        if self._post_client is None or self._post_client.is_closed:
            self._post_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.default_timeout),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
                headers={"User-Agent": "PhoenixAGI-WorldIntel/0.1"},
                trust_env=self.trust_env,
                verify=self.verify,
            )
        return self._post_client

    async def close(self) -> None:
        # Include a previously owned GET pool if a trusted integration replaced
        # _client. Close every distinct pool even if one close fails/cancels.
        clients = {id(client): client for client in (
            self._client, self._owned_get_client, self._post_client,
        ) if client is not None}
        self._client = self._owned_get_client = self._post_client = None
        error = None
        for client in clients.values():
            try:
                if not client.is_closed:
                    await client.aclose()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    @asynccontextmanager
    async def _stream_get(self, client: httpx.AsyncClient, url: str, **kwargs):
        """Stream GETs, closing even unread redirects and failed/cancelled reads.

        Automatic HTTPX redirects buffer intermediate bodies. Instead use its
        prepared next_request (URL resolution, cookies, header stripping) while
        discarding redirect bodies. Respect the client's redirect settings.
        Refuse HTTPS downgrades and redirect userinfo. Across origins, retain
        only negotiation/identity headers and HTTPX-prepared auth/cookies.
        Address enforcement belongs to the selected GET transport, independently
        of these redirect/header rules. Trusted/custom clients are not guarded.

        Client auth is applied only on the initial send. Injected clients must
        not use body-consuming auth flows/hooks for the streaming cap guarantee:
        HTTPX may buffer challenge responses internally before returning here.
        Trusted request hooks can modify prepared requests after our checks and
        are outside the redirect-header guarantee.
        """
        request = client.build_request("GET", url, **kwargs)
        request.headers["Accept-Encoding"] = "gzip, deflate"
        redirects = 0
        while True:
            # Reapplying client auth would restore Authorization after HTTPX
            # stripped it from a cross-origin/downgrade next_request. Same-origin
            # credentials are already present on that prepared request.
            auth_kwargs = {} if redirects == 0 else {"auth": None}
            response = await client.send(
                request, stream=True, follow_redirects=False, **auth_kwargs
            )
            try:
                if client.follow_redirects and response.next_request is not None:
                    if redirects >= client.max_redirects:
                        raise httpx.TooManyRedirects(
                            "Exceeded maximum allowed redirects.", request=request
                        )
                    next_request = response.next_request
                    if (next_request.url.userinfo or
                            "@" in urlsplit(response.headers["location"]).netloc):
                        # auth=None does not disable HTTPX's URL-derived Basic auth.
                        raise RedirectPolicyError("redirect URL userinfo is forbidden")
                    if request.url.scheme == "https" and next_request.url.scheme == "http":
                        raise RedirectPolicyError("HTTPS to HTTP redirect is forbidden")
                    if _origin(request.url) != _origin(next_request.url):
                        # Use ONLY HTTPX's prepared headers, never client defaults
                        # or original call headers. Preserve its auth exception
                        # for same-host HTTP->HTTPS and its cookie-jar rebuilding.
                        allowed = {b"accept", b"accept-encoding", b"user-agent",
                                   b"authorization", b"cookie"}
                        next_request.headers = httpx.Headers([
                            (key, value) for key, value in next_request.headers.raw
                            if key.lower() in allowed
                        ])
                        next_request.headers["Host"] = next_request.url.netloc.decode("ascii")
                    request = next_request
                    request.headers["Accept-Encoding"] = "gzip, deflate"
                    redirects += 1
                    continue
                yield response
                return
            finally:
                await response.aclose()

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        content: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> httpx.Response:
        """Outbound POST for operator-configured sinks (webhooks).

        Deliberately outside the get_json machinery: no caching, no
        retries, no circuit breaking, no rate floor — the target is the
        operator's own endpoint and delivery/failure semantics belong
        to the caller. Never follows redirects, even with an injected client;
        returns the original response. Raises whatever httpx raises.
        """
        client = await self._get_post_client()
        return await client.post(
            url, json=json, content=content, headers=headers, timeout=timeout,
            follow_redirects=False,
        )

    async def get_json(
        self,
        url: str,
        source: str,
        cache_key: str | None = None,
        cache_ttl: int = 300,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        yahoo_rate_limit: bool = False,
        *,
        max_bytes: int | None = None,
        max_encoded_bytes: int | None = None,
        processing_timeout: float | None = None,
    ) -> dict | list | None:
        """Fetch JSON with caching, circuit breaking, and retries.

        Args:
            url: Target URL.
            source: Source name for circuit breaker tracking.
            cache_key: Cache key. If None, uses url+params hash.
            cache_ttl: Cache TTL in seconds.
            headers: Extra HTTP headers.
            params: Query parameters.
            timeout: Per-request timeout override.
            yahoo_rate_limit: If True, enforce Yahoo Finance 600ms serialization.
            max_bytes: Decoded response cap; defaults to max_json_bytes (32 MiB).
            max_encoded_bytes: Wire-body cap; defaults to decoded cap + 1 MiB
                unless configured on the Fetcher.
            processing_timeout: Body-reader deadline, not a parsing timeout;
                defaults to the Fetcher's 120-second setting.

        Returns:
            Parsed JSON or None on failure.
        """
        max_bytes = _validate_max_bytes(self.max_json_bytes if max_bytes is None else max_bytes)
        self._validate_body_options(max_encoded_bytes, processing_timeout)
        # Check cache (live)
        effective_key = cache_key or f"{source}:{url}:{params}"
        cached = self.cache.get(effective_key)
        if cached is not None:
            return cached

        # Check circuit breaker — fall back to stale data if open
        if not self.breaker.is_available(source):
            logger.debug("Circuit open for %s, trying stale cache", redact_text(source))
            return self._stale_fallback(effective_key, source)

        # Per-source rate limiting
        if yahoo_rate_limit:
            await self._yahoo_throttle()
        await self._source_throttle(source)

        # Fetch with retries
        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with self._stream_get(
                    client,
                    url,
                    headers=headers,
                    params=params,
                    timeout=timeout or self.default_timeout,
                ) as resp:
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "html" in content_type and "json" not in content_type:
                        raise ValueError(
                            f"Expected JSON but got HTML from {source}"
                        )
                    data = json.loads(await self._read_body(resp, max_bytes, max_encoded_bytes, processing_timeout))
                if not isinstance(data, (dict, list)):
                    raise ValueError(
                        f"Expected dict/list from {source}, got {type(data).__name__}"
                    )
                self.breaker.record_success(source)
                self.cache.set(effective_key, data, cache_ttl)
                if self.vector_store:
                    await self.vector_store.store(source, data)
                return data
            except PublicSourcePolicyError as exc:
                last_error = exc
                break
            except (BodyLimitError, RedirectPolicyError) as exc:
                last_error = exc
                break
            except (httpx.HTTPStatusError, httpx.RequestError, Exception) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    wait = min(2**attempt, 30)
                    logger.debug(
                        "Retry %d/%d for %s (%s), waiting %.1fs",
                        attempt + 1,
                        self.max_retries,
                        redact_text(source),
                        exception_summary(exc),
                        wait,
                    )
                    await asyncio.sleep(wait)

        # All retries failed — try stale cache before giving up
        self.breaker.record_failure(source)
        logger.warning("Fetch failed for %s: %s (host=%s)", redact_text(source), safe_error(last_error), safe_url(url))
        return self._stale_fallback(effective_key, source)

    async def get_text(
        self,
        url: str,
        source: str,
        cache_key: str | None = None,
        cache_ttl: int = 300,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        *,
        max_bytes: int | None = None,
        max_encoded_bytes: int | None = None,
        processing_timeout: float | None = None,
    ) -> str | None:
        """Fetch text with caching and a decoded-byte cap (default 32 MiB)."""
        max_bytes = _validate_max_bytes(self.max_text_bytes if max_bytes is None else max_bytes)
        self._validate_body_options(max_encoded_bytes, processing_timeout)
        effective_key = cache_key or f"{source}:text:{url}:{params}"
        cached = self.cache.get(effective_key)
        if cached is not None:
            return cached

        if not self.breaker.is_available(source):
            return self._stale_fallback(effective_key, source)

        await self._source_throttle(source)

        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with self._stream_get(
                    client,
                    url,
                    headers=headers,
                    params=params,
                    timeout=timeout or self.default_timeout,
                ) as resp:
                    resp.raise_for_status()
                    text = _response_text(resp, await self._read_body(resp, max_bytes, max_encoded_bytes, processing_timeout))
                self.breaker.record_success(source)
                self.cache.set(effective_key, text, cache_ttl)
                return text
            except PublicSourcePolicyError as exc:
                last_error = exc
                break
            except (BodyLimitError, RedirectPolicyError) as exc:
                last_error = exc
                break
            except (httpx.HTTPStatusError, httpx.RequestError, Exception) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2**attempt, 30))

        self.breaker.record_failure(source)
        logger.warning("Text fetch failed for %s: %s (host=%s)", redact_text(source), safe_error(last_error), safe_url(url))
        return self._stale_fallback(effective_key, source)

    async def get_xml(
        self,
        url: str,
        source: str,
        cache_key: str | None = None,
        cache_ttl: int = 300,
        timeout: float | None = None,
        *,
        max_bytes: int | None = None,
        max_encoded_bytes: int | None = None,
        processing_timeout: float | None = None,
    ) -> str | None:
        """Fetch XML content (returns raw text for feedparser/ET parsing)."""
        return await self.get_text(
            url, source, cache_key, cache_ttl, timeout=timeout, max_bytes=max_bytes,
            max_encoded_bytes=max_encoded_bytes, processing_timeout=processing_timeout,
        )

    async def get_bytes(
        self,
        url: str,
        source: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int = _DEFAULT_BINARY_MAX_BYTES,
        *,
        max_encoded_bytes: int | None = None,
        processing_timeout: float | None = None,
    ) -> BinaryResult:
        """Fetch a binary body with rate limiting, retries and circuit breaking.

        Not cached here: binary payloads (multi-MB) do not belong in the
        SQLite TTL cache, so callers own any on-disk caching. A 404 is a
        definitive answer (not retried, not a breaker failure). A body
        larger than ``max_bytes`` decoded bytes is a failure, never retried.
        """
        max_bytes = _validate_max_bytes(max_bytes)
        self._validate_body_options(max_encoded_bytes, processing_timeout)
        if not self.breaker.is_available(source):
            return BinaryResult(None, None, f"circuit open for {redact_text(source)}")

        await self._source_throttle(source)
        client = await self._get_client()
        last_error = "unknown error"
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with self._stream_get(
                    client, url, headers=headers, timeout=timeout or self.default_timeout
                ) as resp:
                    last_status = resp.status_code
                    if resp.status_code == 404:
                        self.breaker.record_success(source)
                        return BinaryResult(404, None, None)
                    resp.raise_for_status()
                    body = await self._read_body(resp, max_bytes, max_encoded_bytes, processing_timeout)
                self.breaker.record_success(source)
                return BinaryResult(resp.status_code, body, None)
            except PublicSourcePolicyError as exc:
                last_status = None
                last_error = safe_error(exc)
                break
            except RedirectPolicyError as exc:
                last_status = None
                last_error = safe_error(exc)
                break
            except BodyLimitError as exc:
                last_error = safe_error(exc)
                break
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_error = exception_summary(exc)
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2**attempt, 30))

        self.breaker.record_failure(source)
        logger.warning("Binary fetch failed for %s: %s (host=%s)", redact_text(source), last_error, safe_url(url))
        return BinaryResult(last_status, None, last_error)

    @staticmethod
    def _validate_body_options(max_encoded_bytes, processing_timeout):
        if max_encoded_bytes is not None:
            positive_bytes(max_encoded_bytes)
        if processing_timeout is not None:
            _validate_processing_timeout(processing_timeout)

    def _stale_fallback(self, cache_key: str, source: str) -> Any | None:
        """Return stale (expired) cached data as last-known-good fallback.

        Per the module docstring's promise, dict responses are marked with
        ``_stale=True`` and ``_stale_age_seconds`` so a caller (dashboard,
        MCP tool consumer) can tell arbitrarily old data from a fresh
        response instead of receiving it silently. Non-dict responses
        (raw text/XML, lists) can't carry an inline marker the same way,
        so the circuit breaker's per-source stale-serve count is the
        signal there.
        """
        meta = self.cache.get_stale_meta(cache_key)
        if meta is None:
            return None
        value, created_at = meta
        age_seconds = max(0.0, time.time() - created_at)
        logger.info(
            "Serving stale cache for %s (age=%.0fs)",
            redact_text(source),
            age_seconds,
        )
        if isinstance(value, dict):
            value["_stale"] = True
            value["_stale_age_seconds"] = round(age_seconds, 1)
        else:
            self.breaker.record_stale_serve(source)
        return value

    async def _source_throttle(self, source: str) -> None:
        """Enforce per-source rate limit from _SOURCE_RATE_LIMITS."""
        min_interval = _SOURCE_RATE_LIMITS.get(source)
        if min_interval is None:
            return
        if source not in _source_locks:
            _source_locks[source] = asyncio.Lock()
        async with _source_locks[source]:
            now = time.time()
            last = _source_last_call.get(source, 0.0)
            elapsed = now - last
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            _source_last_call[source] = time.time()

    async def _yahoo_throttle(self) -> None:
        """Enforce Yahoo Finance rate limit (600ms between calls)."""
        global _yahoo_last_call
        async with _yahoo_lock:
            now = time.time()
            elapsed = now - _yahoo_last_call
            if elapsed < _YAHOO_MIN_INTERVAL:
                await asyncio.sleep(_YAHOO_MIN_INTERVAL - elapsed)
            _yahoo_last_call = time.time()
