"""Real source adapters and Fetcher; only HTTP responses are mocked offline."""
import asyncio
import json

import httpx
import pytest

from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.dashboard import focused, settings
from world_intel_mcp.dashboard.coverage import RequestWitness


PROFILE = settings.parse({"version": 1, "groups": ["cyber"], "sources": ["cyber_threats", "cable_health"]})
HEALTHY = {
    "feodotracker.abuse.ch": [], "www.cisa.gov": {"vulnerabilities": []},
    "isc.sans.edu": [], "urlhaus-api.abuse.ch": {"urls": []},
    "msi.nga.mil": {"broadcast-warn": []},
}
KEYS = {"cyber:feodo": [], "cyber:cisa_kev": {"vulnerabilities": []},
        "cyber:sans_isc": [], "cyber:urlhaus": {"urls": []}, "infra:cables": {"broadcast-warn": []}}


@pytest.mark.parametrize("mode", ["missing", "stale", "partial", "healthy-empty", "malformed", "invalid-json"])
async def test_real_adapters_cannot_turn_missing_stale_or_malformed_reads_into_clear(tmp_path, mode):
    calls = []
    def response(request):
        calls.append(request.url.host)
        if mode in ("missing", "stale") or mode == "partial" and request.url.host == "www.cisa.gov":
            return httpx.Response(503)
        if mode == "invalid-json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={} if mode == "malformed" else HEALTHY[request.url.host])
    cache = Cache(db_path=tmp_path / "coverage.db")
    if mode == "stale":
        for key, value in KEYS.items():
            cache.set(key, value, 0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    try:
        result = await focused.snapshot(PROFILE, fetcher)
        cyber, cable = result["cyber_threats"], result["cable_health"]
        assert cyber["_coverage"]["attempted"] == 4
        assert cable["_coverage"]["attempted"] == 1
        assert len(calls) == 5 or mode == "missing"  # breaker may decline a request
        if mode == "healthy-empty":
            assert cyber["count"] == 0 and cyber["_coverage"]["state"] == "current"
            assert cable["_coverage"]["state"] == "current"
            assert all(c["status_label"] == "clear" for c in cable["corridors"].values())
            assert result["alert_digest"]["degraded"] is False
            assert result["alert_digest"]["unavailable_sources"] == []
            # Fresh HTTP/cache reads remain current; no source/MCP payload mutation.
            second = await focused.snapshot(PROFILE, fetcher)
            assert second["cyber_threats"]["_coverage"]["current"] == 4
            assert len(calls) == 5
            assert "_coverage" not in cache.get("cyber:cisa_kev")
        else:
            assert result["alert_digest"]["degraded"] is True
            assert "cyber_threats" in result["alert_digest"]["unavailable_sources"]
            if mode != "partial":
                assert "cable_health" in result["alert_digest"]["unavailable_sources"]
                assert all(c["status_label"] == "unknown" and c["status_score"] is None for c in cable["corridors"].values())
            if mode == "stale":
                assert cyber["_coverage"]["stale"] == 4  # includes stale lists, not just dicts
                assert cable["_coverage"]["stale"] == 1
            if mode == "partial":
                assert cyber["_coverage"]["state"] == "partial"
                assert cyber["_coverage"]["current"] == 3 and cyber["_coverage"]["failed"] == 1
        serialized = json.dumps([cyber["_coverage"], cable["_coverage"]])
        assert "https:" not in serialized and "cyber:cisa_kev" not in serialized
        for value in (cyber, cable):
            for record in value["_coverage"]["requests"]:
                assert set(record) == {"source", "operation", "outcome"}
                assert record["source"] in PROFILE.sources
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


async def test_xml_stale_hook_is_per_wrapper_and_nested_xml_text_is_one_read(tmp_path):
    rss = '<rss version="2.0"><channel><title>Quiet service</title></channel></rss>'
    cache = Cache(db_path=tmp_path / "xml.db")
    cache.set("expired-xml", rss, 0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    first, second = RequestWitness(fetcher, "service_status"), RequestWitness(fetcher, "usni_fleet")
    try:
        await asyncio.gather(first.get_xml("https://public.test/a", source="status:a", cache_key="expired-xml"),
                             second.get_text("https://public.test/b", source="status:a", cache_key="missing-xml"))
        a, b = first.finish({}), second.finish({})
        assert a["_coverage"]["stale"] == 1 and a["_coverage"]["attempted"] == 1
        assert a["_coverage"]["requests"][0]["operation"] == "get_xml"
        assert b["_coverage"]["stale"] == 0 and b["_coverage"]["failed"] == 1
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


@pytest.mark.parametrize("body,expected", [(b'<rss version="2.0"><channel><title>Quiet</title></channel></rss>', "current"), (b'not a feed', "malformed")])
async def test_real_service_status_adapter_empty_feed_vs_bad_body(tmp_path, body, expected):
    profile = settings.parse({"version": 1, "groups": ["cyber"], "sources": ["service_status"]})
    cache = Cache(db_path=tmp_path / "feeds.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    try:
        value = (await focused.snapshot(profile, fetcher))["service_status"]
        assert value["count"] == 0
        assert value["_coverage"]["attempted"] > 1
        if expected == "current":
            assert value["_coverage"]["state"] == "current"
        else:
            assert value["degraded"] and value["_coverage"]["failed"] > 0
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


async def test_processed_cache_without_witness_is_unknown_and_verified_cache_preserves_coverage(tmp_path):
    from world_intel_mcp.sources.military import fetch_theater_posture
    cache = Cache(db_path=tmp_path / "processed.db")
    cache.set("military:posture", {"theaters": {}}, 300)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ac": []})))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    try:
        witness = RequestWitness(fetcher, "theater_posture")
        first = witness.finish(await fetch_theater_posture(witness))
        assert first["_coverage"]["state"] == "unknown"
        # A new wrapper cannot attribute another wrapper's reads to itself.
        other = RequestWitness(fetcher, "military_flights")
        assert other.finish({})["_coverage"]["attempted"] == 0
        assert "_coverage" not in cache.get("military:posture")
        # Exercise the separate, focused-only sidecar while preserving the raw value.
        witness = RequestWitness(fetcher, "theater_posture")
        witness.record("get_json", "current")
        witness.cache.set("military:posture", {"theaters": {}}, 300)
        reused = RequestWitness(fetcher, "theater_posture")
        second = reused.finish(await fetch_theater_posture(reused))
        assert second["_coverage"]["state"] == "current"
        assert second["_coverage"]["requests"] == [{"source": "theater_posture", "operation": "cache", "outcome": "current"}]
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


async def test_timeout_preserves_failed_request_coverage_and_cancels_body(tmp_path, monkeypatch):
    profile = settings.parse({"version": 1, "groups": ["cyber"], "sources": ["cable_health"]})
    active = 0
    async def response(request):
        nonlocal active
        active += 1
        try:
            await asyncio.sleep(100)
        finally:
            active -= 1
    cache = Cache(db_path=tmp_path / "timeout.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    original = asyncio.wait_for
    monkeypatch.setattr(focused.asyncio, "wait_for", lambda future, timeout: original(future, 0.01))
    try:
        result = await focused.snapshot(profile, fetcher)
        assert result["cable_health"]["_coverage"]["failed"] == 1
        assert result["cable_health"]["_coverage"]["requests"][0]["outcome"] == "cancelled"
        assert result["alert_digest"]["degraded"] and active == 0
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


async def test_overridden_nested_text_method_cannot_claim_verified_xml_freshness(tmp_path):
    from unittest.mock import AsyncMock
    cache = Cache(db_path=tmp_path / "override.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP expected")))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    fetcher.get_text = AsyncMock(return_value='<rss version="2.0"><channel><title>Possibly stale</title></channel></rss>')
    witness = RequestWitness(fetcher, "service_status")
    try:
        await witness.get_xml("https://public.test/", source="status:test")
        result = witness.finish({})
        assert result["_coverage"]["state"] == "unknown"
        assert result["_coverage"]["attempted"] == 1
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()
