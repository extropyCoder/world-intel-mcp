"""Real cache/Fetcher/adapters: sidecars attest one entry, not its reusable key."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest

from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.dashboard import focused, settings
from world_intel_mcp.dashboard.coverage import RequestWitness


PROFILE = settings.parse({"version": 1, "groups": ["military"], "sources": ["theater_posture"]})


@pytest.fixture(autouse=True)
def no_mock_transport_courtesy_delays(monkeypatch):
    # Keep real Fetcher/cache/adapters, but no public API is contacted here.
    monkeypatch.setattr("world_intel_mcp.fetcher._SOURCE_RATE_LIMITS", {})
    for key in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("replacement", ["different-value", "same-value", "different-ttl", "missing-sidecar", "bad-digest"])
async def test_real_processed_cache_cannot_inherit_old_witness(tmp_path, monkeypatch, replacement):
    # Same bytes AND identical timestamps must still be different generations.
    monkeypatch.setattr("time.time", lambda: 1700000000.0)
    cache = Cache(db_path=tmp_path / "binding.db")
    body = {"ac": [{"lat": 50, "lon": 0, "hex": "ae1234", "flight": "RCH123", "alt_baro": 10000}]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    try:
        first = (await focused.snapshot(PROFILE, fetcher))["theater_posture"]
        assert first["_coverage"]["state"] == "current"
        raw = cache.get("military:posture")
        assert "_coverage" not in raw
        original_generation = cache.get_stale_meta("military:posture")[1]
        reused = (await focused.snapshot(PROFILE, fetcher))["theater_posture"]
        assert reused["_coverage"]["requests"] == [{"source": "theater_posture", "operation": "cache", "outcome": "current"}]
        sidecar = RequestWitness(fetcher, "theater_posture").cache.key("military:posture")
        metadata = cache.get(sidecar)
        if replacement == "missing-sidecar":
            cache.delete(sidecar)
        elif replacement == "bad-digest":
            metadata["entry"]["digest"] = "wrong"
            cache.set(sidecar, metadata, 300)
        else:
            value = {"theaters": {"unwitnessed": {"count": 0}}} if replacement == "different-value" else raw
            cache.set("military:posture", value, 600 if replacement == "different-ttl" else 300)
            assert cache.get_stale_meta("military:posture")[1] == original_generation
        final = (await focused.snapshot(PROFILE, fetcher))["theater_posture"]
        assert final["_coverage"]["state"] == "unknown"
        assert final["degraded"]
        assert "entry" not in final["_coverage"] and "digest" not in json.dumps(final["_coverage"])
        assert "_coverage" not in cache.get("military:posture")
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


def test_competing_wrappers_publish_value_and_binding_atomically(tmp_path):
    path = tmp_path / "concurrent.db"
    reader_cache = Cache(db_path=path)
    barrier = Barrier(2)
    written = Event()
    def writer(label, outcome):
        cache = Cache(db_path=path)
        try:
            witness = RequestWitness(SimpleNamespace(cache=cache), "theater_posture")
            witness.record("get_json", outcome)
            barrier.wait(timeout=5)
            for index in range(30):
                witness.cache.set("military:posture", {"writer": label, "index": index}, 300)
                written.set()
        finally:
            cache.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer, "verified", "current"), pool.submit(writer, "unverified", "missing")]
            assert written.wait(timeout=10)
            observed = 0
            for _ in range(200):
                witness = RequestWitness(SimpleNamespace(cache=reader_cache), "theater_posture")
                value = witness.cache.get("military:posture")
                if value is not None:
                    expected = "current" if value["writer"] == "verified" else "unknown"
                    assert witness.finish(value)["_coverage"]["state"] == expected
                    observed += 1
            for future in futures:
                future.result(timeout=10)
            witness = RequestWitness(SimpleNamespace(cache=reader_cache), "theater_posture")
            value = witness.cache.get("military:posture")
            assert witness.finish(value)["_coverage"]["state"] == ("current" if value["writer"] == "verified" else "unknown")
            assert observed > 0
    finally:
        reader_cache.close()


def test_sidecar_write_failure_rolls_back_the_value_not_just_metadata(tmp_path):
    cache = Cache(db_path=tmp_path / "rollback.db")
    try:
        witness = RequestWitness(SimpleNamespace(cache=cache), "theater_posture")
        witness.record("get_json", "current")
        witness.cache.set("military:posture", {"old": True}, 300)
        key = witness.cache.key("military:posture")
        conn = cache._get_conn()
        conn.execute("CREATE TRIGGER reject_sidecar BEFORE INSERT ON cache WHEN NEW.key=" + "'" + key + "'" + " BEGIN SELECT RAISE(ABORT,'sidecar failure'); END")
        conn.commit()
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            witness.cache.set("military:posture", {"uncommitted": True}, 300)
        reader = RequestWitness(SimpleNamespace(cache=cache), "theater_posture")
        value = reader.cache.get("military:posture")
        assert value == {"old": True} and reader.finish(value)["_coverage"]["state"] == "current"
    finally:
        cache.close()
