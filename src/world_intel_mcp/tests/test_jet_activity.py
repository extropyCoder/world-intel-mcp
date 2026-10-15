"""Tests for the aircraft-cohort early-warning tools.

Heatmaps here are synthetic files written by a test-side encoder of
readsb's fixed heatEntry layout; network calls are mocked with respx.
"""

from __future__ import annotations

import gzip
import struct
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

import world_intel_mcp.fetcher as fetcher_mod
from world_intel_mcp.analysis import jet_activity as ja
from world_intel_mcp.sources import jet_cohort as jc

# ---------------------------------------------------------------------------
# Synthetic heatmap encoder
# ---------------------------------------------------------------------------

_E = struct.Struct("<iiihh")


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def encode_heatmap(start: datetime, slices: list[list[tuple]], gz: bool = True) -> bytes:
    """slices[i] = entries (addr, lat, lon, alt_units, gs_units) at start+10*i s.
    alt_units: feet/25, -123 ground, -124 unknown. gs_units: kt*10, -1 invalid.
    An entry ("info", addr) emits a callsign/squawk record."""
    base_ms = int(start.timestamp() * 1000)
    out = bytearray()
    for i in range(len(slices)):  # leading index records (ignored by readers)
        out += _E.pack(i, 0, 0, 0, 0)
    for i, entries in enumerate(slices):
        ms = base_ms + 10_000 * i
        out += _E.pack(jc.SLICE_MARKER, _s32(ms >> 32), _s32(ms & 0xFFFFFFFF), 10, 0)
        for e in entries:
            if e[0] == "info":
                out += _E.pack(e[1], (1 << 30) | 1200, _s32(int.from_bytes(b"TEST", "little")), 0, 0)
                continue
            addr, lat, lon, alt, gs = e
            out += _E.pack(addr, int(round(lat * 1e6)), int(round(lon * 1e6)), alt, gs)
    raw = bytes(out)
    return gzip.compress(raw) if gz else raw


def airborne(addr: int, lat=40.0, lon=-75.0, ft=30000, kt=450.0):
    return (addr, lat, lon, ft // 25, int(kt * 10))


def ground(addr: int, lat=40.0, lon=-75.0, kt=5.0):
    return (addr, lat, lon, -123, int(kt * 10))


def slot_file(start: datetime, airborne_addrs: list[int], others: list[int] = (), n_slices: int = 180):
    """Every listed aircraft airborne in every slice; ``others`` appear once."""
    slices = []
    for i in range(n_slices):
        entries = [airborne(a) for a in airborne_addrs]
        if i == 0:
            entries += [airborne(o, ft=35000) for o in others]
        slices.append(entries)
    return encode_heatmap(start, slices)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc)


def test_parse_heatmap_decodes_fields_and_skips_non_position_records():
    blob = encode_heatmap(
        T0,
        [
            [
                airborne(0xA00001, lat=33.5, lon=-112.25, ft=10000, kt=250.0),
                ground(0xA00002, lat=33.4, lon=-112.0, kt=12.5),
                ("info", 0xA00001),
                (0xA00003 | (1 << 24), 33.0, -112.0, 400, 2000),  # non-ICAO
                (0xA00004 | (5 << 27), 34.0, -111.0, -124, -1),  # MLAT, unknown alt/gs
            ]
        ],
    )
    slices, tracks, total = jc.parse_heatmap(blob, wanted=None)
    assert slices == [T0.timestamp()]
    assert total == 3  # non-ICAO address excluded
    ts, lat, lon, alt, on_ground, gs = tracks[0xA00001][0]
    assert (lat, lon, alt, on_ground, gs) == (33.5, -112.25, 10000, False, 250.0)
    assert tracks[0xA00002][0][3:] == (None, True, 12.5)
    assert tracks[0xA00004][0][3:] == (None, False, None)  # address-type bits masked
    assert 0xA00003 not in tracks and (0xA00003 | (1 << 24)) not in tracks


def test_parse_heatmap_filters_by_wanted_and_bbox_and_accepts_raw():
    blob = encode_heatmap(
        T0,
        [[airborne(1, lat=40, lon=-75), airborne(2, lat=40, lon=-75), airborne(3, lat=10, lon=10)]],
        gz=False,
    )
    _, tracks, total = jc.parse_heatmap(blob, wanted={1, 3}, bbox=(24, -126, 50, -66))
    assert set(tracks) == {1}
    assert total == 3


def test_parse_heatmap_rejects_truncated_and_markerless_bodies():
    blob = encode_heatmap(T0, [[airborne(1)]], gz=False)
    with pytest.raises(ValueError, match="multiple of 16"):
        jc.parse_heatmap(blob[:-3], wanted=None)
    with pytest.raises(ValueError, match="no slice markers"):
        jc.parse_heatmap(_E.pack(1, 2, 3, 4, 5) * 4, wanted=None)


@pytest.mark.parametrize("compressed", [False, True])
def test_heatmap_input_and_expanded_limits_preserve_filtering(compressed):
    raw = encode_heatmap(T0, [[airborne(1), airborne(2, lat=10, lon=10)]], gz=False)
    body = gzip.compress(raw) if compressed else raw
    result = jc.parse_heatmap(body, {1, 2}, (24, -126, 50, -66),
                             max_compressed_bytes=len(body), max_expanded_bytes=len(raw))
    assert set(result[1]) == {1} and result[2] == 2
    with pytest.raises(ValueError, match="cap"):
        jc.parse_heatmap(body, None, max_compressed_bytes=len(body) - 1)
    with pytest.raises(ValueError, match="cap"):
        jc.parse_heatmap(body, None, max_expanded_bytes=len(raw) - 1)


@pytest.mark.parametrize("cached", [False, True])
@respx.mock
async def test_heatmap_bomb_download_and_cache_no_partial_write(fetcher, tmp_path, no_floor, cached):
    bomb = gzip.compress(b"\0" * 100000)
    disk = jc.HeatmapDiskCache(tmp_path / "hm", 10**9, max_expanded_bytes=100)
    if cached:
        path = disk.path_for(T0)
        path.parent.mkdir(parents=True)
        path.write_bytes(bomb)
    else:
        respx.get(jc.heatmap_url(T0)).mock(return_value=httpx.Response(200, content=bomb))
    result = await jc.fetch_slot(fetcher, T0, None, None, disk)
    assert result.status == "error" and "cap" in result.error
    assert disk.path_for(T0).exists() is cached


def test_heatmap_cache_read_uses_sentinel_not_read_bytes(tmp_path, monkeypatch):
    disk = jc.HeatmapDiskCache(tmp_path, 10000, max_compressed_bytes=8)
    path = disk.path_for(T0)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"12345678")
    monkeypatch.setattr(type(path), "read_bytes", lambda *_: pytest.fail("unbounded cache read"))
    assert disk.get(T0) == b"12345678"
    path.write_bytes(b"123456789")
    with pytest.raises(ValueError, match="cap"):
        disk.get(T0)


@respx.mock
async def test_heatmap_double_layer_http_and_application_gzip(fetcher, tmp_path, no_floor):
    body = encode_heatmap(T0, [[airborne(1), airborne(2, lat=10, lon=10)]])
    wire = gzip.compress(body)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(len(wire)):
                yield wire[i:i + 1]
    respx.get(jc.heatmap_url(T0)).mock(return_value=httpx.Response(
        200, stream=Stream(), headers={"content-encoding": "gzip"}))
    disk = jc.HeatmapDiskCache(tmp_path, 10000)
    result = await jc.fetch_slot(fetcher, T0, {1, 2}, (24, -126, 50, -66), disk)
    assert result.status == "ok" and set(result.data.tracks) == {1}
    assert disk.get(T0) == body


@respx.mock
async def test_oversize_cached_heatmap_is_error_without_network(fetcher, tmp_path):
    disk = jc.HeatmapDiskCache(tmp_path, 10000, max_compressed_bytes=8)
    path = disk.path_for(T0)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 9)
    result = await jc.fetch_slot(fetcher, T0, None, None, disk)
    assert result.status == "error" and result.cached


def test_heatmap_record_processing_deadline(tmp_path, monkeypatch):
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(jc, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    raw = encode_heatmap(T0, [[airborne(1)]], gz=False)
    with pytest.raises(ValueError, match="deadline"):
        jc.parse_heatmap(raw, None, processing_timeout=1)


def test_parse_heatmap_thins_but_keeps_state_changes():
    slices = [[airborne(1, ft=30000)] for _ in range(12)]  # 120 s steady cruise
    slices.append([ground(1)])  # state change must survive thinning
    _, tracks, _ = jc.parse_heatmap(encode_heatmap(T0, slices), wanted=None)
    times = [f[0] - T0.timestamp() for f in tracks[1]]
    assert times == [0.0, 30.0, 60.0, 90.0, 120.0]
    assert tracks[1][-1][4] is True


# ---------------------------------------------------------------------------
# Metrics and scoring
# ---------------------------------------------------------------------------

def _slot(tracks, n=180):
    t = T0.timestamp()
    return jc.SlotData(T0, [t + 10 * i for i in range(n)], tracks, total_aircraft=len(tracks))


def test_concurrent_profile_counts_overlap_hold_and_ground():
    t = T0.timestamp()
    air = lambda s: (t + s, 40.0, -75.0, 30000, False, 450.0)  # noqa: E731
    gnd = lambda s: (t + s, 40.0, -75.0, None, True, 5.0)  # noqa: E731
    tracks = {
        1: [air(0), air(30), air(60)],  # present 0..180 (hold 120 after 60)
        2: [air(40), gnd(70)],  # present 40..70 only
        3: [gnd(0), gnd(30)],  # never airborne
        4: [air(1000)],  # present 1000..1120
    }
    prof = ja.concurrent_profile(_slot(tracks))
    assert prof["peak_concurrent"] == 2
    assert prof["unique_airborne"] == 3
    # One fix counts for the hold window (slices 0..110 s), not beyond it.
    held = ja.concurrent_profile(_slot({1: [air(0)]}), hold_seconds=120)
    assert held["peak_concurrent"] == 1
    assert ja.concurrent_profile(_slot({1: [air(0)]}), hold_seconds=0)["peak_concurrent"] == 0
    late = ja.concurrent_profile(_slot({1: [air(-200)]}), hold_seconds=120)
    assert late["peak_concurrent"] == 0


def test_robust_score_floor_and_levels():
    s = ja.robust_score(130, [100, 100, 100, 100])
    assert s["baseline_median"] == 100
    assert s["scale"] == 10.0  # MAD 0 -> sqrt(100) floor
    assert s["z"] == 3.0
    assert ja.alert_level(3.0, 5.0) == (3, "elevated")
    assert ja.alert_level(5.0, 5.0) == (5, "alarm")
    assert ja.alert_level(4.2, 6.0) == (4, "high")
    assert ja.alert_level(1.9, 5.0) == (1, "normal")
    wide = ja.robust_score(130, [60, 100, 140, 100])
    assert wide["scale"] == pytest.approx(1.4826 * 20, abs=1e-3)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def test_us_federal_holidays_2026():
    h = ja.us_federal_holidays(2026)
    assert h[date(2026, 1, 19)] == "Martin Luther King Jr. Day"
    assert h[date(2026, 5, 25)] == "Memorial Day"
    assert h[date(2026, 7, 3)] == "Independence Day (observed)"
    assert h[date(2026, 9, 7)] == "Labor Day"
    assert h[date(2026, 10, 12)] == "Columbus Day"
    assert h[date(2026, 11, 26)] == "Thanksgiving Day"
    assert ja.holiday_near(date(2026, 11, 27)) == "Thanksgiving Day"
    assert ja.holiday_near(date(2026, 11, 20)) is None


def test_baseline_starts_skips_holiday_weeks():
    target = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
    starts, skipped = ja.baseline_starts(target, 4)
    assert [s.date().isoformat() for s in starts] == [
        "2026-09-28", "2026-09-21", "2026-09-14", "2026-08-31",
    ]
    assert skipped == [{"date": "2026-09-07", "reason": "near Labor Day"}]


# ---------------------------------------------------------------------------
# Endpoint inference and clustering
# ---------------------------------------------------------------------------

def _fx(s, ft, on_ground=False, kt=300.0, lat=40.0, lon=-75.0):
    return (T0.timestamp() + s, lat, lon, ft, on_ground, kt)


W0, W1 = T0.timestamp(), T0.timestamp() + 3600


def test_infer_takeoff_and_landing_from_ground_transitions():
    track = [_fx(300, None, True, 10.0), _fx(330, 1500), _fx(360, 4000)]
    assert [(k, r) for k, _, r in ja.infer_endpoints(track, W0, W1)] == [("takeoff", "ground_to_air")]
    track = [_fx(300, 2000), _fx(330, None, True, 60.0), _fx(360, None, True, 10.0)]
    events = ja.infer_endpoints(track, W0, W1)
    assert [(k, r) for k, _, r in events] == [("landing", "air_to_ground")]
    assert events[0][1][0] == W0 + 330


def test_infer_coverage_edge_events_respect_window_edges():
    climb = [_fx(600, 3000), _fx(660, 4500), _fx(720, 6000)]
    assert [r for _, _, r in ja.infer_endpoints(climb, W0, W1)] == ["appeared_climbing"]
    at_edge = [_fx(30, 3000), _fx(90, 4500), _fx(150, 6000)]
    assert ja.infer_endpoints(at_edge, W0, W1) == []
    cruise = [_fx(600, 35000), _fx(660, 37000)]
    assert ja.infer_endpoints(cruise, W0, W1) == []
    descent = [_fx(1000, 8000), _fx(1060, 6000), _fx(1120, 4000)]
    assert [r for _, _, r in ja.infer_endpoints(descent, W0, W1)] == ["vanished_descending"]


def test_infer_dedupes_repeat_events_within_ten_minutes():
    track = [
        _fx(100, None, True, 5.0), _fx(130, 1000),
        _fx(160, None, True, 5.0), _fx(190, 1000),  # jitter, same takeoff
    ]
    kinds = [k for k, _, _ in ja.infer_endpoints(track, W0, W1)]
    assert kinds.count("takeoff") == 1


def _pts(lat, lon, n, window, spread=0.02):
    return [
        {"lat": lat + spread * (i % 3), "lon": lon + spread * (i // 3 % 3), "window": window, "hex": 1000 + i, "reason": "ground_to_air"}
        for i in range(n)
    ]


def test_density_clusters_groups_and_noise():
    pts = _pts(40.0, -75.0, 5, 0) + _pts(41.5, -73.0, 4, 0) + [{"lat": 10.0, "lon": 10.0}]
    labels = ja.density_clusters(pts, radius_mi=12, min_points=3)
    assert len(set(labels[:5])) == 1 and labels[0] >= 0
    assert len(set(labels[5:9])) == 1 and labels[5] not in (labels[0], -1)
    assert labels[9] == -1


def test_density_clusters_work_across_antimeridian():
    pts = [{"lat": 0.0, "lon": 179.95}, {"lat": 0.0, "lon": -179.95}, {"lat": 0.05, "lon": 179.99}]
    assert len(set(ja.density_clusters(pts, radius_mi=12, min_points=3))) == 1


def test_summarize_and_classify_departure_anchor():
    take = (
        _pts(33.37, -81.96, 30, 0)  # big current departure cluster
        + _pts(33.37, -81.96, 2, 1) + _pts(33.37, -81.96, 1, 2)
        + _pts(26.1, -80.2, 6, 0) + _pts(26.1, -80.2, 5, 1) + _pts(26.1, -80.2, 6, 2)
    )
    summary = ja.summarize_clusters(take, n_baseline=2, radius_mi=12, min_points=3, ref=(33.47, -82.02))
    top = summary["top_excess_clusters"][0]
    assert top["current"] == 30 and top["baseline_median"] == 1.5 and top["significant"]
    assert top["distance_to_reference_mi"] < 10
    land = ja.summarize_clusters([], 2, 12, 3)
    cls = ja.classify_excess(summary, land)
    assert cls["pattern"] == "departure_anchor"


def test_classify_diffuse_and_no_excess():
    diffuse = []
    for k in range(10):
        diffuse += _pts(30 + 2 * k, -100 + 3 * k, 4, 0) + _pts(30 + 2 * k, -100 + 3 * k, 3, 1)
    s = ja.summarize_clusters(diffuse, 1, 12, 3)
    empty = ja.summarize_clusters([], 1, 12, 3)
    assert ja.classify_excess(s, empty)["pattern"] == "diffuse"
    assert ja.classify_excess(empty, empty)["pattern"] == "no_excess"


# ---------------------------------------------------------------------------
# Cohort store, disk cache, fetcher binary path
# ---------------------------------------------------------------------------

def test_cohort_store_upsert_and_learning_gate(tmp_path):
    store = jc.CohortStore(tmp_path / "c.db")
    assert store.upsert("bizjet", [{"hex": "a1b2c3", "type": "GLF6"}], 100.0) == 1
    assert store.upsert("bizjet", [{"hex": "a1b2c3", "type": None}, {"hex": "000001"}], 200.0) == 1
    assert set(store.members("bizjet")) == {0xA1B2C3, 1}
    assert set(store.members("bizjet", learned_before=150.0)) == {0xA1B2C3}
    assert store.members("bizjet")[0xA1B2C3]["type"] == "GLF6"  # not erased by a null
    assert store.members("military") == {}
    stats = store.stats("bizjet")
    assert stats["members"] == 2 and stats["learning_started"] == "1970-01-01T00:01:40Z"
    store.close()


def test_heatmap_disk_cache_evicts_oldest(tmp_path):
    disk = jc.HeatmapDiskCache(tmp_path, cap_bytes=2500)
    a, b = T0, T0 + timedelta(minutes=30)
    disk.put(a, b"\x1f\x8b" + b"x" * 1200)
    disk.put(b, b"\x1f\x8b" + b"y" * 1200)
    assert disk.get(a) is not None and disk.get(b) is not None
    disk.put(b + timedelta(minutes=30), b"\x1f\x8b" + b"z" * 1200)
    remaining = sorted(p.name for p in tmp_path.rglob("*.bin.gz"))
    assert len(remaining) == 2


def test_resolve_region():
    assert jc.resolve_region(None) == ("global", None)
    assert jc.resolve_region("CONUS")[1] == (24.0, -126.0, 50.0, -66.0)
    assert jc.resolve_region("10,20,30,40") == ("bbox", (10.0, 20.0, 30.0, 40.0))
    for bad in ("mars", "1,2,3", "30,20,10,40"):
        with pytest.raises(ValueError):
            jc.resolve_region(bad)


@respx.mock
async def test_get_bytes_404_error_and_cap(fetcher):
    respx.get("https://x.test/missing").mock(return_value=httpx.Response(404))
    respx.get("https://x.test/boom").mock(return_value=httpx.Response(500))
    respx.get("https://x.test/big").mock(return_value=httpx.Response(200, content=b"0" * 50))
    r = await fetcher.get_bytes("https://x.test/missing", source="t1")
    assert r.status == 404 and r.content is None and r.error is None
    assert fetcher.breaker.is_available("t1")
    fetcher.max_retries = 0
    r = await fetcher.get_bytes("https://x.test/boom", source="t2")
    assert not r.ok and "500" in r.error
    r = await fetcher.get_bytes("https://x.test/big", source="t3", max_bytes=10)
    assert not r.ok and "exceeds cap" in r.error


# ---------------------------------------------------------------------------
# Live-snapshot source (mocked)
# ---------------------------------------------------------------------------

_LIVE = {
    "ac": [
        {"hex": "a18d83", "t": "GLF6", "r": "N2C", "dbFlags": 8, "lat": 40.0, "lon": -100.0, "alt_baro": 43000, "gs": 480, "flight": "N2C     "},
        {"hex": "a00002", "t": "C68A", "r": "N2", "dbFlags": 0, "lat": 48.0, "lon": 2.0, "alt_baro": 30000, "gs": 420},
        {"hex": "a00003", "t": "E55P", "dbFlags": 4, "lat": 41.0, "lon": -90.0, "alt_baro": "ground", "gs": 3},
        {"hex": "~2a0001", "t": "E55P", "lat": 41.0, "lon": -90.0, "alt_baro": 20000},
    ]
}


@respx.mock
async def test_cohort_snapshot_learns_globally_and_summarizes_region(fetcher, tmp_path):
    route = respx.get(url__regex=r"https://api\.adsb\.lol/v2/type/GLF2,.*").mock(
        return_value=httpx.Response(200, json=_LIVE)
    )
    store = jc.CohortStore(tmp_path / "s.db")
    r = await jc.fetch_cohort_snapshot(fetcher, "bizjet", region="conus", store=store)
    assert route.called
    assert r["airborne"] == 1 and r["on_ground"] == 1
    assert r["by_type"] == {"GLF6": 1}
    assert r["privacy_flags"] == {"pia": 0, "ladd": 1}
    assert r["cohort_store"]["members"] == 3  # Paris jet learned, non-ICAO not
    assert set(store.members("bizjet")) == {0xA18D83, 0xA00002, 0xA00003}
    store.close()


@respx.mock
async def test_cohort_snapshot_upstream_failure_is_an_error_not_zero(fetcher, tmp_path):
    fetcher.max_retries = 0
    respx.get(url__regex=r"https://api\.adsb\.lol/v2/mil.*").mock(return_value=httpx.Response(503))
    store = jc.CohortStore(tmp_path / "m.db")
    r = await jc.fetch_cohort_snapshot(fetcher, "military", store=store)
    store.close()
    assert "error" in r and "airborne" not in r
    assert (await jc.fetch_cohort_snapshot(fetcher, "boats"))["error"].startswith("cohort must be")


# ---------------------------------------------------------------------------
# End-to-end analyses over mocked heatmaps
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 14, 12, 10, tzinfo=timezone.utc)  # Wednesday
CUR = datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc)
COHORT = list(range(0xA00000, 0xA00000 + 100))
OTHERS = list(range(0x400000, 0x400000 + 300))


@pytest.fixture
def no_floor(monkeypatch):
    monkeypatch.setitem(fetcher_mod._SOURCE_RATE_LIMITS, jc.HEATMAP_SOURCE, 0.0)


@pytest.fixture
def learned_store(tmp_path):
    store = jc.CohortStore(tmp_path / "cohort.db")
    store.upsert("bizjet", [{"hex": f"{a:06x}"} for a in COHORT], NOW.timestamp() - 10 * 86400)
    yield store
    store.close()


def _mock_slot(start, body=None, status=200):
    return respx.get(jc.heatmap_url(start)).mock(
        return_value=httpx.Response(status, content=body or b"")
    )


@respx.mock
async def test_activity_alarm_on_synthetic_surge_then_disk_cache(fetcher, learned_store, no_floor, tmp_path):
    routes = [_mock_slot(CUR, slot_file(CUR, COHORT[:100], OTHERS))]
    for k, n in zip(range(1, 5), (50, 52, 48, 51)):
        s = CUR - timedelta(weeks=k)
        routes.append(_mock_slot(s, slot_file(s, COHORT[:n], OTHERS)))
    disk = jc.HeatmapDiskCache(tmp_path / "hm", 10**9)
    r = await ja.fetch_cohort_activity(fetcher, store=learned_store, disk=disk, now=NOW)
    assert r["status"] == "ok", r
    assert r["window"]["slot"] == "2026-10-14T11:30Z"
    assert r["window"]["peak_concurrent"] == 100
    assert [w["peak_concurrent"] for w in r["baseline_windows"]] == [50, 52, 48, 51]
    assert r["score"]["count"]["baseline_median"] == 50.5
    assert r["alert_level"] == 5 and r["alert"] == "alarm" and r["deficit"] is False
    assert all(route.call_count == 1 for route in routes)
    again = await ja.fetch_cohort_activity(fetcher, store=learned_store, disk=disk, now=NOW)
    assert all(route.call_count == 1 for route in routes)  # served from disk
    assert again["window"]["cached"] is True and again["alert_level"] == 5


@respx.mock
async def test_activity_normal_and_deficit(fetcher, learned_store, no_floor, tmp_path):
    for k, n in zip(range(0, 5), (12, 50, 52, 48, 51)):
        s = CUR - timedelta(weeks=k)
        _mock_slot(s, slot_file(s, COHORT[:n], OTHERS))
    r = await ja.fetch_cohort_activity(
        fetcher, store=learned_store, disk=jc.HeatmapDiskCache(tmp_path / "hm", 10**9), now=NOW
    )
    assert r["alert_level"] == 1 and r["deficit"] is True


@respx.mock
async def test_activity_degrades_honestly(fetcher, learned_store, no_floor, tmp_path):
    fetcher.max_retries = 0
    disk = jc.HeatmapDiskCache(tmp_path / "hm", 10**9)
    _mock_slot(CUR, slot_file(CUR, COHORT[:60], OTHERS))
    _mock_slot(CUR - timedelta(weeks=1), slot_file(CUR - timedelta(weeks=1), COHORT[:50], OTHERS))
    _mock_slot(CUR - timedelta(weeks=2), status=404)
    _mock_slot(CUR - timedelta(weeks=3), status=500)
    s4 = CUR - timedelta(weeks=4)
    _mock_slot(s4, slot_file(s4, COHORT[:50], OTHERS, n_slices=60))  # 1/3 coverage
    r = await ja.fetch_cohort_activity(fetcher, store=learned_store, disk=disk, now=NOW)
    assert r["status"] == "insufficient_baseline"
    assert "alert_level" not in r
    assert len(r["data_gaps"]) == 3 and any("coverage" in g for g in r["data_gaps"])


@respx.mock
async def test_activity_current_slot_missing_is_error(fetcher, learned_store, no_floor, tmp_path):
    for k in range(0, 3):
        _mock_slot(CUR - timedelta(minutes=30 * k), status=404)
    r = await ja.fetch_cohort_activity(
        fetcher, store=learned_store, disk=jc.HeatmapDiskCache(tmp_path / "hm", 10**9), now=NOW
    )
    assert r["status"] == "error" and "unavailable" in r["error"]


async def test_activity_warming_up_and_validation(fetcher, tmp_path):
    store = jc.CohortStore(tmp_path / "fresh.db")
    store.upsert("bizjet", [{"hex": f"{a:06x}"} for a in COHORT], NOW.timestamp() - 3600)
    r = await ja.fetch_cohort_activity(fetcher, store=store, now=NOW)
    assert r["status"] == "warming_up" and r["cohort_store"]["eligible_members"] == 0
    assert "error" in await ja.fetch_cohort_activity(fetcher, baseline_weeks=1, store=store, now=NOW)
    assert "error" in await ja.fetch_cohort_activity(fetcher, slot="2026-10-14T12:00Z", store=store, now=NOW)
    assert "error" in await ja.fetch_cohort_activity(fetcher, region="nowhere", store=store, now=NOW)
    store.close()


@respx.mock
async def test_activity_biased_cohort_suppresses_alert(fetcher, no_floor, tmp_path):
    store = jc.CohortStore(tmp_path / "fresh.db")
    store.upsert("bizjet", [{"hex": f"{a:06x}"} for a in COHORT], NOW.timestamp() - 60)
    for k, n in zip(range(0, 5), (90, 50, 52, 48, 51)):
        s = CUR - timedelta(weeks=k)
        _mock_slot(s, slot_file(s, COHORT[:n], OTHERS))
    r = await ja.fetch_cohort_activity(
        fetcher, store=store, min_learning_lead_hours=-24,
        disk=jc.HeatmapDiskCache(tmp_path / "hm", 10**9), now=NOW,
    )
    assert r["status"] == "degraded"
    assert r["alert_level"] is None and r["alert"].startswith("suppressed")
    assert any("selection bias" in g for g in r["data_gaps"])
    store.close()


def _departure_slot(start, n_departing, base_lat=33.37, base_lon=-81.96):
    """n aircraft sit on the ground then take off mid-slot near one airport."""
    slices = []
    for i in range(180):
        entries = []
        for j in range(n_departing):
            a = COHORT[j]
            lat, lon = base_lat + 0.01 * (j % 5), base_lon + 0.01 * (j // 5 % 5)
            if i < 60:
                entries.append(ground(a, lat, lon, kt=8.0))
            else:
                entries.append(airborne(a, lat, lon, ft=2000 + 100 * (i - 60), kt=180.0))
        slices.append(entries)
    return encode_heatmap(start, slices)


@respx.mock
async def test_excess_origins_finds_departure_anchor(fetcher, learned_store, no_floor, tmp_path):
    _mock_slot(CUR, _departure_slot(CUR, 25))
    for k in (1, 2):
        s = CUR - timedelta(weeks=k)
        _mock_slot(s, _departure_slot(s, 2))
    r = await ja.fetch_excess_origins(
        fetcher, slots=1, baseline_weeks=2, ref_lat=33.47, ref_lon=-82.02,
        store=learned_store, disk=jc.HeatmapDiskCache(tmp_path / "hm", 10**9), now=NOW,
    )
    assert r["status"] == "ok", r
    assert r["classification"]["pattern"] == "departure_anchor"
    top = r["takeoffs"]["top_excess_clusters"][0]
    assert top["current"] == 25 and top["baseline_median"] == 2 and top["delta"] == 23
    assert top["evidence"] == {"ground_to_air": 25}
    assert top["distance_to_reference_mi"] < 15
    assert [w["takeoffs"] for w in r["windows"]] == [25, 2, 2]


@respx.mock
async def test_excess_origins_bounds_and_incomplete_current(fetcher, learned_store, no_floor, tmp_path):
    disk = jc.HeatmapDiskCache(tmp_path / "hm", 10**9)
    r = await ja.fetch_excess_origins(fetcher, slots=4, baseline_weeks=3, store=learned_store, now=NOW)
    assert "<= 12" in r["error"]
    r = await ja.fetch_excess_origins(fetcher, ref_lat=1.0, store=learned_store, now=NOW)
    assert "together" in r["error"]
    fetcher.max_retries = 0
    start = CUR - timedelta(minutes=30)
    _mock_slot(start, _departure_slot(start, 3))
    _mock_slot(CUR, status=500)
    r = await ja.fetch_excess_origins(
        fetcher, start=start.isoformat(), slots=2, store=learned_store, disk=disk, now=NOW
    )
    assert r["status"] == "error" and "incomplete" in r["error"]
