"""Tests for analysis/aoi.py and the intel_aoi_* tool family (issue #16).

Persistence tests exercise ``AOIStore`` directly against a ``tmp_path``
SQLite file (same pattern as ``test_cache.py``'s ``cache`` fixture).
Composition tests (``fetch_aoi_brief`` / ``fetch_aoi_escalation``) mock at
the source-function boundary via monkeypatch, matching the pattern
``test_daily_digest.py`` established for this exact class of function:
aoi.py composes existing ``sources/*.py`` fetch functions rather than
making HTTP calls itself, so there is no network edge for respx to sit at.
"""

import inspect
import json
from pathlib import Path

import httpx
import pytest
import respx

from world_intel_mcp.analysis import aoi
from world_intel_mcp.analysis.aoi import AOIAlreadyExists, AOIStore
from world_intel_mcp.sources import (
    aviation,
    conflict,
    geocode,
    military,
    news,
    seismology,
    wildfire,
)

_SERVER_PY = Path(__file__).resolve().parents[1] / "server.py"

# Pittsburgh, matching the README worked example (issue #16).
_PGH_LAT = 40.4406
_PGH_LON = -79.9959
_PGH_RADIUS_KM = 50.0

# ~7 km from Pittsburgh, inside a 50 km radius.
_NEAR_LAT, _NEAR_LON = 40.50, -80.00
# ~330 km from Pittsburgh (Detroit area), outside a 50 km radius.
_FAR_LAT, _FAR_LON = 42.33, -83.05


@pytest.fixture
def store(tmp_path: Path) -> AOIStore:
    s = AOIStore(tmp_path / "test_aoi_cache.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------


def test_haversine_km_zero_distance() -> None:
    assert aoi.haversine_km(40.44, -79.99, 40.44, -79.99) == pytest.approx(
        0.0, abs=1e-6
    )


def test_haversine_km_known_pair() -> None:
    # NYC (40.7128, -74.0060) to LA (34.0522, -118.2437) is ~3936 km.
    dist = aoi.haversine_km(40.7128, -74.0060, 34.0522, -118.2437)
    assert dist == pytest.approx(3936, rel=0.01)


def test_bboxes_widen_at_high_latitude() -> None:
    equator_box = aoi.bboxes_from_radius_km(0.0, 0.0, 100.0)[0]
    polar_box = aoi.bboxes_from_radius_km(85.0, 0.0, 100.0)[0]
    eq_lomin, eq_lomax = (
        float(equator_box.split(",")[1]),
        float(equator_box.split(",")[3]),
    )
    pl_lomin, pl_lomax = float(polar_box.split(",")[1]), float(polar_box.split(",")[3])
    # Same radius spans far more longitude near the pole than at the equator.
    assert (pl_lomax - pl_lomin) > (eq_lomax - eq_lomin)


def test_bboxes_stay_within_valid_ranges() -> None:
    for box in aoi.bboxes_from_radius_km(89.5, 179.5, 2000.0):
        lamin, lomin, lamax, lomax = (float(x) for x in box.split(","))
        assert -90.0 <= lamin <= lamax <= 90.0
        assert -180.0 <= lomin <= lomax <= 180.0


def test_filter_by_radius_includes_near_excludes_far() -> None:
    """The load-bearing pair: a fence that does not exclude is not a fence."""
    items = [
        {"latitude": _NEAR_LAT, "longitude": _NEAR_LON, "id": "near"},
        {"latitude": _FAR_LAT, "longitude": _FAR_LON, "id": "far"},
    ]
    result = aoi.filter_by_radius(items, _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    ids = [r["id"] for r in result]
    assert "near" in ids
    assert "far" not in ids
    assert result[0]["distance_km"] < _PGH_RADIUS_KM


def test_filter_by_radius_drops_items_missing_coordinates() -> None:
    items = [{"latitude": None, "longitude": None, "id": "no-coords"}]
    result = aoi.filter_by_radius(items, _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert result == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_aoi_params_accepts_valid_input() -> None:
    assert (
        aoi.validate_aoi_params("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
        is None
    )


@pytest.mark.parametrize(
    "name,lat,lon,radius_km",
    [
        ("", 40.0, -80.0, 50.0),
        ("   ", 40.0, -80.0, 50.0),
        ("X", 91.0, -80.0, 50.0),
        ("X", -91.0, -80.0, 50.0),
        ("X", 40.0, 181.0, 50.0),
        ("X", 40.0, -181.0, 50.0),
        ("X", 40.0, -80.0, 0.5),
        ("X", 40.0, -80.0, 2001.0),
        ("X", "not-a-number", -80.0, 50.0),
        ("X", 40.0, None, 50.0),
        (123, 40.0, -80.0, 50.0),
    ],
)
def test_validate_aoi_params_rejects_invalid_input(name, lat, lon, radius_km) -> None:
    assert aoi.validate_aoi_params(name, lat, lon, radius_km) is not None


# ---------------------------------------------------------------------------
# AOIStore persistence
# ---------------------------------------------------------------------------


def test_define_and_get_round_trip(store: AOIStore) -> None:
    defined = store.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert defined["name"] == "Pittsburgh"
    fetched = store.get("Pittsburgh")
    assert fetched["lat"] == _PGH_LAT
    assert fetched["lon"] == _PGH_LON
    assert fetched["radius_km"] == _PGH_RADIUS_KM


def test_get_is_case_insensitive(store: AOIStore) -> None:
    store.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert store.get("pittsburgh") is not None
    assert store.get("PITTSBURGH") is not None


def test_get_missing_returns_none(store: AOIStore) -> None:
    assert store.get("Nowhere") is None


def test_list_all_sorted_by_name(store: AOIStore) -> None:
    store.define("Zurich", 47.37, 8.54, 25.0)
    store.define("Austin", 30.27, -97.74, 40.0)
    names = [a["name"] for a in store.list_all()]
    assert names == ["Austin", "Zurich"]


def test_delete_round_trip(store: AOIStore) -> None:
    store.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert store.delete("Pittsburgh") is True
    assert store.get("Pittsburgh") is None


def test_delete_nonexistent_returns_false(store: AOIStore) -> None:
    assert store.delete("Nowhere") is False


def test_define_duplicate_raises_with_existing_echoed(store: AOIStore) -> None:
    store.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    with pytest.raises(AOIAlreadyExists) as exc_info:
        store.define("pittsburgh", 0.0, 0.0, 10.0)
    assert exc_info.value.existing["name"] == "Pittsburgh"
    assert exc_info.value.existing["lat"] == _PGH_LAT


# ---------------------------------------------------------------------------
# Tool-facing define / list / delete
# ---------------------------------------------------------------------------


def test_define_aoi_success(store: AOIStore) -> None:
    result = aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert "error" not in result
    assert result["aoi"]["name"] == "Pittsburgh"


def test_define_aoi_validation_rejection_does_not_touch_store(store: AOIStore) -> None:
    result = aoi.define_aoi(store, "Bad", 999.0, -80.0, 50.0)
    assert "error" in result
    assert store.get("Bad") is None


def test_define_aoi_duplicate_echoes_existing_politely(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.define_aoi(store, "Pittsburgh", 0.0, 0.0, 10.0)
    assert "error" in result
    assert result["existing"]["lat"] == _PGH_LAT
    assert result["existing"]["radius_km"] == _PGH_RADIUS_KM


def test_list_aois_reports_count(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    aoi.define_aoi(store, "Austin", 30.27, -97.74, 40.0)
    result = aoi.list_aois(store)
    assert result["count"] == 2
    assert {a["name"] for a in result["aois"]} == {"Pittsburgh", "Austin"}


def test_delete_aoi_not_found(store: AOIStore) -> None:
    result = aoi.delete_aoi(store, "Nowhere")
    assert "error" in result


def test_delete_aoi_success(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.delete_aoi(store, "Pittsburgh")
    assert result["deleted"] == "Pittsburgh"


# ---------------------------------------------------------------------------
# Nearby static infrastructure (pure, real config datasets)
# ---------------------------------------------------------------------------


def test_nearby_bases_finds_norfolk_at_its_own_coordinates() -> None:
    # Norfolk Naval Station: lat 36.95, lon -76.33 (config/geospatial.py).
    result = aoi.nearby_bases(36.95, -76.33, 5.0)
    names = [b["name"] for b in result]
    assert "Norfolk Naval Station" in names


def test_nearby_bases_excludes_far_away_base() -> None:
    result = aoi.nearby_bases(_PGH_LAT, _PGH_LON, 5.0)
    assert result == []


def test_nearby_pipelines_matches_on_either_endpoint() -> None:
    # TAPS pipeline ends at Valdez (61.13, -146.35); a tight radius there
    # should match via the endpoint-proximity approximation.
    result = aoi.nearby_pipelines(61.13, -146.35, 5.0)
    names = [p["name"] for p in result]
    assert "Trans-Alaska (TAPS)" in names


def test_nearby_cables_matches_closest_landing_point() -> None:
    # MAREA lands at Bilbao, Spain and Virginia Beach, USA.
    result = aoi.nearby_cables(36.95, -76.0, 60.0)
    assert any(c["name"] == "MAREA" for c in result)


def test_nearby_infrastructure_has_all_seven_categories() -> None:
    result = aoi.nearby_infrastructure(_PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert set(result.keys()) == {
        "military_bases",
        "ports",
        "pipelines",
        "nuclear_facilities",
        "undersea_cables",
        "datacenters",
        "spaceports",
    }


# ---------------------------------------------------------------------------
# Wildfire region mapping
# ---------------------------------------------------------------------------


def test_overlapping_wildfire_regions_finds_north_america_for_pittsburgh() -> None:
    regions = aoi._overlapping_wildfire_regions(_PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert "north_america" in regions


# ---------------------------------------------------------------------------
# fetch_aoi_brief: composition, citations, data_gaps
# ---------------------------------------------------------------------------


async def _fake_earthquakes(fetcher, **kwargs):
    return {
        "earthquakes": [
            {
                "id": "near1",
                "magnitude": 3.2,
                "place": "near Pittsburgh",
                "time": "2026-08-16T01:00:00Z",
                "latitude": _NEAR_LAT,
                "longitude": _NEAR_LON,
                "url": "https://earthquake.usgs.gov/near1",
            },
            {
                "id": "far1",
                "magnitude": 5.0,
                "place": "near Detroit",
                "time": "2026-08-16T01:00:00Z",
                "latitude": _FAR_LAT,
                "longitude": _FAR_LON,
                "url": "https://earthquake.usgs.gov/far1",
            },
        ],
        "count": 2,
        "source": "usgs",
    }


async def _fake_military(fetcher, bbox=None):
    return {
        "aircraft": [
            {
                "icao24": "aaa111",
                "callsign": "RCH123",
                "origin_country": "United States",
                "latitude": _NEAR_LAT,
                "longitude": _NEAR_LON,
            },
            {
                "icao24": "bbb222",
                "callsign": "RCH999",
                "origin_country": "United States",
                "latitude": _FAR_LAT,
                "longitude": _FAR_LON,
            },
        ],
        "count": 2,
        "source": "adsb.lol",
    }


async def _fake_acled(fetcher, **kwargs):
    return {
        "events": [
            {
                "event_type": "Protests",
                "location": "Near Pittsburgh",
                "admin1": "Pennsylvania",
                "country": "United States",
                "event_date": "2026-08-15",
                "latitude": _NEAR_LAT,
                "longitude": _NEAR_LON,
                "fatalities": 0,
            },
            {
                "event_type": "Battles",
                "location": "Near Detroit",
                "admin1": "Michigan",
                "country": "United States",
                "event_date": "2026-08-15",
                "latitude": _FAR_LAT,
                "longitude": _FAR_LON,
                "fatalities": 2,
            },
        ],
        "count": 2,
        "source": "acled",
    }


async def _fake_wildfires(fetcher, region=None):
    return {
        "fires_by_region": {
            region or "north_america": {
                "count": 2,
                "top_clusters": [
                    {
                        "lat": _NEAR_LAT,
                        "lon": _NEAR_LON,
                        "fire_count": 4,
                        "max_frp": 12.5,
                    },
                    {
                        "lat": _FAR_LAT,
                        "lon": _FAR_LON,
                        "fire_count": 9,
                        "max_frp": 40.0,
                    },
                ],
            }
        },
        "total_fires": 2,
        "source": "nasa-firms",
    }


async def _fake_domestic_flights(fetcher):
    return {
        "total_aircraft": 2,
        "sampled": [
            {"lat": _NEAR_LAT, "lon": _NEAR_LON, "callsign": "UAL1", "origin": "USA"},
            {"lat": _FAR_LAT, "lon": _FAR_LON, "callsign": "UAL2", "origin": "USA"},
        ],
        "source": "opensky-domestic",
    }


async def _fake_gdelt(fetcher, **kwargs):
    return {
        "articles": [
            {
                "title": "Pittsburgh flooding warning issued",
                "url": "https://news.example/pgh",
                "seendate": "20260816T010000Z",
                "domain": "news.example",
            }
        ],
        "count": 1,
        "source": "gdelt",
    }


@pytest.fixture
def aoi_store_with_pittsburgh(tmp_path: Path) -> AOIStore:
    s = AOIStore(tmp_path / "test_aoi_brief_cache.db")
    s.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    yield s
    s.close()


async def _fake_place_context(fetcher, lat, lon):
    """Default reverse-geocode stub. Without this every AOI test would
    make a live Nominatim request: slow, flaky, and rude to a free
    service that rate-limits to one request per second."""
    return {
        "place": "Pittsburgh",
        "county": "Allegheny County",
        "state": "Pennsylvania",
        "country_code": "us",
        "display_name": "Pittsburgh, Allegheny County, Pennsylvania, United States",
        "terms": ["Pittsburgh", "Allegheny County"],
        "source": "nominatim-reverse",
    }


def _patch_all_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocode, "fetch_place_context", _fake_place_context)
    monkeypatch.setattr(seismology, "fetch_earthquakes", _fake_earthquakes)
    monkeypatch.setattr(military, "fetch_military_flights", _fake_military)
    monkeypatch.setattr(conflict, "fetch_acled_events", _fake_acled)
    monkeypatch.setattr(wildfire, "fetch_wildfires", _fake_wildfires)
    monkeypatch.setattr(aviation, "fetch_domestic_flights", _fake_domestic_flights)
    monkeypatch.setattr(news, "fetch_gdelt_search", _fake_gdelt)


@pytest.mark.asyncio
async def test_fetch_aoi_brief_not_found(fetcher, store: AOIStore) -> None:
    result = await aoi.fetch_aoi_brief(fetcher, store, "Nowhere")
    assert "error" in result


@pytest.mark.asyncio
async def test_fetch_aoi_brief_includes_in_radius_excludes_out_of_radius(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing pair, at the tool level: every synthetic domain
    carries one event inside the 50 km radius and one ~330 km outside it.
    Only the in-radius ones may appear."""
    _patch_all_domains(monkeypatch)

    result = await aoi.fetch_aoi_brief(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")

    assert result["counts"]["earthquakes"] == 1
    assert result["counts"]["military_flights"] == 1
    assert result["counts"]["conflict_events"] == 1
    assert result["counts"]["wildfires"] == 1
    assert result["counts"]["aviation"] == 1

    assert "near Pittsburgh" in result["markdown"]
    assert "near Detroit" not in result["markdown"]
    assert "Near Pittsburgh" in result["markdown"]
    assert "Near Detroit" not in result["markdown"]


@pytest.mark.asyncio
async def test_fetch_aoi_brief_is_cited(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    result = await aoi.fetch_aoi_brief(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")
    assert result["cited"] is True
    assert len(result["sources"]) > 0
    # Every source has a real, resolvable citation number.
    max_n = len(result["sources"])
    assert all(1 <= s["n"] <= max_n for s in result["sources"])


@pytest.mark.asyncio
async def test_fetch_aoi_brief_reports_data_gap_for_unscopable_domain(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)

    async def _broken_acled(fetcher, **kwargs):
        return {
            "error": "ACLED credentials not configured (ACLED_EMAIL + ACLED_PASSWORD)"
        }

    monkeypatch.setattr(conflict, "fetch_acled_events", _broken_acled)

    result = await aoi.fetch_aoi_brief(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")

    assert any("Conflict events" in gap for gap in result["data_gaps"])
    assert not any(s["domain"] == "conflict_events" for s in result["sources"])


@pytest.mark.asyncio
async def test_fetch_aoi_brief_reports_data_gap_when_gdelt_fails(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #17: a GDELT fetch failure (rate limit, API error) must
    surface as a data_gap here, the same as every other domain in this
    brief, not read as "no news mentions" the way it silently did
    before fetch_gdelt_search() carried an honest error/degraded shape."""
    _patch_all_domains(monkeypatch)

    async def _broken_gdelt(fetcher, **kwargs):
        return {
            "error": "GDELT fetch failed (rate limited, API error, or malformed response)",
            "degraded": True,
            "reason": "gdelt_fetch_failed",
            "articles": [],
            "count": 0,
        }

    monkeypatch.setattr(news, "fetch_gdelt_search", _broken_gdelt)

    result = await aoi.fetch_aoi_brief(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")

    assert any("News" in gap for gap in result["data_gaps"])
    assert not any(s["domain"] == "news" for s in result["sources"])


@pytest.mark.asyncio
async def test_fetch_aoi_brief_reports_data_gap_when_no_wildfire_region_overlaps(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    s = AOIStore(tmp_path / "test_open_ocean.db")
    # Deep South Pacific, far from all 9 REGIONS bboxes in wildfire.py.
    s.define("Open Ocean", -55.0, -140.0, 50.0)

    result = await aoi.fetch_aoi_brief(fetcher, s, "Open Ocean")

    assert any("Wildfires" in gap for gap in result["data_gaps"])
    s.close()


@pytest.mark.asyncio
async def test_fetch_aoi_brief_includes_nearby_infrastructure(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    s = AOIStore(tmp_path / "test_norfolk.db")
    s.define("Norfolk", 36.95, -76.33, 5.0)

    result = await aoi.fetch_aoi_brief(fetcher, s, "Norfolk")

    assert result["counts"]["military_bases"] >= 1
    assert "Norfolk Naval Station" in result["markdown"]
    s.close()


# ---------------------------------------------------------------------------
# fetch_aoi_escalation: reuses analysis/escalation.py unmodified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_aoi_escalation_not_found(fetcher, store: AOIStore) -> None:
    result = await aoi.fetch_aoi_escalation(fetcher, store, "Nowhere")
    assert "error" in result


@pytest.mark.asyncio
async def test_fetch_aoi_escalation_scores_only_in_radius_signals(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conflict, "fetch_acled_events", _fake_acled)
    monkeypatch.setattr(military, "fetch_military_flights", _fake_military)

    result = await aoi.fetch_aoi_escalation(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )

    assert "error" not in result
    # 1 in-radius military aircraft -> military component = min(20, 1*1.0) = 1.0
    assert result["components"]["military"] == pytest.approx(1.0)
    # 1 in-radius protest event, 0 conflict events, 0 fatalities in radius
    # -> conflict component = min(20, 0*0.5 + 0*0.1) = 0.0
    assert result["components"]["conflict"] == pytest.approx(0.0)
    assert result["components"]["news"] is None
    assert result["components"]["convergence"] is None
    assert set(result["unavailable_components"]) == {"news", "convergence"}


@pytest.mark.asyncio
async def test_fetch_aoi_escalation_reports_data_gap_on_fetch_failure(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _broken_military(fetcher, bbox=None):
        return {"error": "adsb.lol and OpenSky both unavailable"}

    monkeypatch.setattr(conflict, "fetch_acled_events", _fake_acled)
    monkeypatch.setattr(military, "fetch_military_flights", _broken_military)

    result = await aoi.fetch_aoi_escalation(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )

    assert any("Military flights" in gap for gap in result["data_gaps"])


# ---------------------------------------------------------------------------
# Server registration / dispatch parity. Since the Phase 26 split the AOI
# tools live in tools/aoi.py; these tests import the aggregated registry
# for real (under the suite's sanctioned temp cache path) instead of
# scanning server.py as text, and verify both registration and that each
# handler still dispatches to the intended analysis/aoi.py function.
# ---------------------------------------------------------------------------


def _tools_registry():
    """Import world_intel_mcp.tools under the suite's temp cache path.

    Importing the tools package imports runtime, which opens a live
    ``Cache()``/``AOIStore()``; pointing WORLD_INTEL_CACHE_DB at
    test_server_registry's temp path (shared, so the override holds no
    matter which test file triggers the first import) keeps that off the
    developer's real cache database."""
    import importlib
    import os

    from .test_server_registry import _TMP_CACHE

    prior = os.environ.get("WORLD_INTEL_CACHE_DB")
    os.environ["WORLD_INTEL_CACHE_DB"] = str(_TMP_CACHE)
    try:
        return importlib.import_module("world_intel_mcp.tools")
    finally:
        if prior is None:
            os.environ.pop("WORLD_INTEL_CACHE_DB", None)
        else:
            os.environ["WORLD_INTEL_CACHE_DB"] = prior


@pytest.mark.parametrize(
    "tool_name,fn_name",
    [
        ("intel_aoi_define", "aoi.define_aoi"),
        ("intel_aoi_list", "aoi.list_aois"),
        ("intel_aoi_delete", "aoi.delete_aoi"),
        ("intel_aoi_brief", "aoi.fetch_aoi_brief"),
        ("intel_aoi_escalation", "aoi.fetch_aoi_escalation"),
        ("intel_aoi_digest", "aoi.fetch_aoi_digest"),
    ],
)
def test_aoi_tools_registered_and_dispatched(tool_name: str, fn_name: str) -> None:
    """Structural parity check: the TOOLS/handler 1:1 invariant this repo
    maintains (see ROADMAP.md 'MCP tool parity') must hold for every AOI
    tool. Membership in ALL_TOOLS/ALL_HANDLERS proves tools/aoi.py is
    wired into _MODULES (not merely present on disk); the handler-source
    check proves the tool still routes to the intended analysis/aoi.py
    function."""
    tools_pkg = _tools_registry()

    assert tool_name in {t.name for t in tools_pkg.aoi.TOOLS}
    assert tool_name in {t.name for t in tools_pkg.ALL_TOOLS}

    handler = tools_pkg.aoi.HANDLERS[tool_name]
    assert tools_pkg.ALL_HANDLERS[tool_name] is handler
    assert fn_name in inspect.getsource(handler)


def test_aoi_store_instantiated_from_cache_db_path() -> None:
    """The AOIStore must share the Cache's resolved db_path, not compute
    its own default independently (which could diverge under
    WORLD_INTEL_CACHE_DB or the tempdir fallback). Since the Phase 26
    split the construction lives in runtime.py."""
    text = (_SERVER_PY.parent / "runtime.py").read_text()
    assert "aoi.AOIStore(cache.db_path)" in text


# ---------------------------------------------------------------------------
# Antimeridian-aware bounding boxes (v0.4 geofence hardening)
# ---------------------------------------------------------------------------


def test_bboxes_single_box_away_from_dateline() -> None:
    boxes = aoi.bboxes_from_radius_km(_PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert len(boxes) == 1
    lamin, lomin, lamax, lomax = (float(x) for x in boxes[0].split(","))
    assert lamin < _PGH_LAT < lamax
    assert lomin < _PGH_LON < lomax


def test_bboxes_split_when_circle_crosses_dateline_eastward() -> None:
    """An AOI at lon 179.5 with a 200 km radius extends past +180; the
    old single-box clamp silently cut off everything on the far side of
    the dateline. Two boxes must now cover both sides."""
    boxes = aoi.bboxes_from_radius_km(52.0, 179.5, 200.0)
    assert len(boxes) == 2
    parsed = [tuple(float(x) for x in b.split(",")) for b in boxes]
    for lamin, lomin, lamax, lomax in parsed:
        assert -180.0 <= lomin <= lomax <= 180.0
        assert -90.0 <= lamin <= lamax <= 90.0
    # One box ends at +180, the other starts at -180.
    assert any(lomax == 180.0 for _, _, _, lomax in parsed)
    assert any(lomin == -180.0 for _, lomin, _, _ in parsed)
    # A point just across the dateline (~35 km away) is inside some box.
    assert any(
        lomin <= -179.9 <= lomax and lamin <= 52.0 <= lamax
        for lamin, lomin, lamax, lomax in parsed
    )


def test_bboxes_split_when_circle_crosses_dateline_westward() -> None:
    boxes = aoi.bboxes_from_radius_km(-17.7, -179.5, 300.0)
    assert len(boxes) == 2
    parsed = [tuple(float(x) for x in b.split(",")) for b in boxes]
    # A point just west of the dateline (Fiji side) is inside some box.
    assert any(
        lomin <= 179.9 <= lomax and lamin <= -17.7 <= lamax
        for lamin, lomin, lamax, lomax in parsed
    )


def test_bboxes_full_longitude_band_near_pole() -> None:
    """When the longitude half-width reaches 180 degrees the circle rings
    the pole; one full-longitude box, not two overlapping ones."""
    boxes = aoi.bboxes_from_radius_km(89.0, 10.0, 1500.0)
    assert len(boxes) == 1
    _, lomin, _, lomax = (float(x) for x in boxes[0].split(","))
    assert lomin == -180.0
    assert lomax == 180.0


def test_overlapping_wildfire_regions_across_dateline() -> None:
    """An AOI just EAST of the dateline near Fiji reaches across into the
    oceania FIRMS box (which ends at lon 180). The old clamped math found
    no overlap and reported a false 'no FIRMS coverage' gap."""
    regions = aoi._overlapping_wildfire_regions(-17.7, -179.5, 300.0)
    assert "oceania" in regions


# ---------------------------------------------------------------------------
# Great-circle segment distance (pipelines and cables as lines, not points)
# ---------------------------------------------------------------------------


def test_segment_distance_abeam_midpoint() -> None:
    # 1 degree of latitude abeam an equatorial segment: ~111.19 km.
    d = aoi.segment_distance_km(1.0, 0.0, 0.0, -1.0, 0.0, 1.0)
    assert d == pytest.approx(111.19, rel=0.01)


def test_segment_distance_clamps_beyond_end() -> None:
    """A point past the far endpoint on the same great circle is NOT at
    distance 0 (the infinite-great-circle answer); it is at the distance
    to the endpoint."""
    d = aoi.segment_distance_km(0.0, 20.0, 0.0, 0.0, 0.0, 10.0)
    assert d == pytest.approx(aoi.haversine_km(0.0, 20.0, 0.0, 10.0), rel=1e-6)
    assert d > 1000


def test_segment_distance_clamps_before_start() -> None:
    d = aoi.segment_distance_km(0.0, -10.0, 0.0, 0.0, 0.0, 10.0)
    assert d == pytest.approx(aoi.haversine_km(0.0, -10.0, 0.0, 0.0), rel=1e-6)


def test_segment_distance_degenerate_segment() -> None:
    d = aoi.segment_distance_km(1.0, 1.0, 40.0, -80.0, 40.0, -80.0)
    assert d == pytest.approx(aoi.haversine_km(1.0, 1.0, 40.0, -80.0), rel=1e-6)


def test_segment_distance_across_dateline() -> None:
    d = aoi.segment_distance_km(1.0, 180.0, 0.0, 179.0, 0.0, -179.0)
    assert d == pytest.approx(111.19, rel=0.01)


def test_nearby_pipelines_detects_midspan_crossing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing improvement: a pipeline whose midspan passes
    through the AOI but whose endpoints are both far away was invisible
    to the old endpoint-only proximity check."""
    from world_intel_mcp.config import geospatial as geo_cfg

    monkeypatch.setattr(
        geo_cfg,
        "PIPELINES",
        [
            {
                "name": "Test Midspan Line",
                "route": "A-B",
                "type": "oil",
                "status": "operational",
                "lat_start": 0.0,
                "lon_start": -5.0,
                "lat_end": 0.0,
                "lon_end": 5.0,
            }
        ],
    )
    # AOI center 0.5 deg (~55.6 km) abeam the midspan; endpoints ~558 km away.
    result = aoi.nearby_pipelines(0.5, 0.0, 100.0)
    names = [p["name"] for p in result]
    assert "Test Midspan Line" in names
    assert result[0]["distance_km"] == pytest.approx(55.6, rel=0.02)


def test_nearby_cables_detects_midspan_crossing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from world_intel_mcp.config import cables as cables_cfg

    monkeypatch.setattr(
        cables_cfg,
        "UNDERSEA_CABLES",
        [
            {
                "name": "Test Midspan Cable",
                "status": "active",
                "landing_points": [
                    {"name": "West End", "lat": 0.0, "lon": -5.0},
                    {"name": "East End", "lat": 0.0, "lon": 5.0},
                ],
            }
        ],
    )
    result = aoi.nearby_cables(0.5, 0.0, 100.0)
    assert any(c["name"] == "Test Midspan Cable" for c in result)
    assert result[0]["distance_km"] == pytest.approx(55.6, rel=0.02)
    # The nearest published landing point is still reported for context.
    assert result[0]["nearest_landing_point"] in {"West End", "East End"}


def test_nearby_pipelines_still_finds_endpoint_proximity() -> None:
    # TAPS endpoint at Valdez must keep matching (regression guard).
    result = aoi.nearby_pipelines(61.13, -146.35, 5.0)
    assert "Trans-Alaska (TAPS)" in [p["name"] for p in result]


# ---------------------------------------------------------------------------
# AOIStore.update / update_aoi tool
# ---------------------------------------------------------------------------


def test_update_aoi_radius_only(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.update_aoi(store, "Pittsburgh", radius_km=120.0)
    assert "error" not in result
    assert result["aoi"]["radius_km"] == 120.0
    assert result["aoi"]["lat"] == _PGH_LAT
    assert result["previous"]["radius_km"] == _PGH_RADIUS_KM
    assert store.get("Pittsburgh")["radius_km"] == 120.0


def test_update_aoi_rename(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.update_aoi(store, "Pittsburgh", new_name="Steel City")
    assert "error" not in result
    assert store.get("Pittsburgh") is None
    assert store.get("Steel City")["lat"] == _PGH_LAT


def test_update_aoi_rename_collision_rejected(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    aoi.define_aoi(store, "Austin", 30.27, -97.74, 40.0)
    result = aoi.update_aoi(store, "Pittsburgh", new_name="austin")
    assert "error" in result
    assert store.get("Pittsburgh") is not None


def test_update_aoi_case_change_rename_of_itself_allowed(store: AOIStore) -> None:
    aoi.define_aoi(store, "pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.update_aoi(store, "pittsburgh", new_name="Pittsburgh")
    assert "error" not in result
    assert store.get("Pittsburgh")["name"] == "Pittsburgh"


def test_update_aoi_not_found(store: AOIStore) -> None:
    assert "error" in aoi.update_aoi(store, "Nowhere", radius_km=10.0)


def test_update_aoi_no_fields_is_an_error(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert "error" in aoi.update_aoi(store, "Pittsburgh")


def test_update_aoi_invalid_merged_params_rejected(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    result = aoi.update_aoi(store, "Pittsburgh", lat=95.0)
    assert "error" in result
    assert store.get("Pittsburgh")["lat"] == _PGH_LAT


# ---------------------------------------------------------------------------
# fetch_aoi_changes: geofence change detection (enter/leave since last check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aoi_changes_first_run_is_baseline(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )
    assert result["baseline"] is True
    # A baseline run must not claim anything entered or left.
    for domain_changes in result["changes"].values():
        assert domain_changes["new"] == []
        assert domain_changes["departed"] == []


@pytest.mark.asyncio
async def test_aoi_changes_detects_new_and_departed(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    await aoi.fetch_aoi_changes(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")

    # Second sweep: quake near1 is gone, near2 appeared; military unchanged.
    async def _second_earthquakes(fetcher, **kwargs):
        return {
            "earthquakes": [
                {
                    "id": "near2",
                    "magnitude": 4.1,
                    "place": "near Pittsburgh again",
                    "time": "2026-08-16T02:00:00Z",
                    "latitude": _NEAR_LAT,
                    "longitude": _NEAR_LON,
                    "url": "https://earthquake.usgs.gov/near2",
                }
            ],
            "count": 1,
            "source": "usgs",
        }

    monkeypatch.setattr(seismology, "fetch_earthquakes", _second_earthquakes)
    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )

    assert result["baseline"] is False
    eq = result["changes"]["earthquakes"]
    assert [i["key"] for i in eq["new"]] == ["near2"]
    assert [i["key"] for i in eq["departed"]] == ["near1"]
    mil = result["changes"]["military_flights"]
    assert mil["new"] == []
    assert mil["departed"] == []
    assert mil["unchanged"] == 1


@pytest.mark.asyncio
async def test_aoi_changes_error_domain_reports_gap_not_departures(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty invariant: a failed fetch must never read as 'everything
    left the area'. The domain goes to data_gaps, its diff is skipped, and
    its baseline slice survives for the next successful run."""
    _patch_all_domains(monkeypatch)
    await aoi.fetch_aoi_changes(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")

    async def _broken_earthquakes(fetcher, **kwargs):
        return {"error": "USGS unavailable"}

    monkeypatch.setattr(seismology, "fetch_earthquakes", _broken_earthquakes)
    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )
    assert any("Earthquakes" in gap for gap in result["data_gaps"])
    assert "earthquakes" not in result["changes"]

    # Recovery run: the original quake is still there; nothing new or
    # departed versus the PRE-ERROR baseline.
    monkeypatch.setattr(seismology, "fetch_earthquakes", _fake_earthquakes)
    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )
    eq = result["changes"]["earthquakes"]
    assert eq["new"] == []
    assert eq["departed"] == []
    assert eq["unchanged"] == 1


@pytest.mark.asyncio
async def test_aoi_changes_aviation_excluded_from_diff(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 1-in-10 aviation sample is pure churn between sweeps; diffing
    it would manufacture fake enter/leave events every run."""
    _patch_all_domains(monkeypatch)
    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )
    assert "aviation" not in result["changes"]


@pytest.mark.asyncio
async def test_aoi_changes_not_found(fetcher, store: AOIStore) -> None:
    result = await aoi.fetch_aoi_changes(fetcher, store, "Nowhere")
    assert "error" in result


def test_geometry_update_drops_snapshot(store: AOIStore) -> None:
    """Moving or resizing an AOI invalidates its change baseline: the old
    snapshot described a different piece of the planet."""
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    store.save_snapshot("Pittsburgh", {"earthquakes": {"near1": "M3.2"}})
    aoi.update_aoi(store, "Pittsburgh", radius_km=500.0)
    assert store.get_snapshot("Pittsburgh") is None


def test_rename_migrates_snapshot(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    store.save_snapshot("Pittsburgh", {"earthquakes": {"near1": "M3.2"}})
    aoi.update_aoi(store, "Pittsburgh", new_name="Steel City")
    snap = store.get_snapshot("Steel City")
    assert snap is not None
    assert snap["domains"]["earthquakes"] == {"near1": "M3.2"}


def test_delete_aoi_also_deletes_snapshot(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    store.save_snapshot("Pittsburgh", {"earthquakes": {"near1": "M3.2"}})
    store.delete("Pittsburgh")
    assert store.get_snapshot("Pittsburgh") is None


# ---------------------------------------------------------------------------
# New tool registration parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,fn_name",
    [
        ("intel_aoi_update", "aoi.update_aoi"),
        ("intel_aoi_changes", "aoi.fetch_aoi_changes"),
        ("intel_aoi_define_polygon", "aoi.define_polygon_aoi"),
        ("intel_aoi_define_corridor", "aoi.define_corridor_aoi"),
    ],
)
def test_new_aoi_tools_registered_and_dispatched(tool_name: str, fn_name: str) -> None:
    tools_pkg = _tools_registry()

    assert tool_name in {t.name for t in tools_pkg.aoi.TOOLS}
    assert tool_name in {t.name for t in tools_pkg.ALL_TOOLS}

    handler = tools_pkg.aoi.HANDLERS[tool_name]
    assert tools_pkg.ALL_HANDLERS[tool_name] is handler
    assert fn_name in inspect.getsource(handler)


# ---------------------------------------------------------------------------
# Coverage completion for the v0.4 geofence edges. Deliberately NOT hit,
# as defensive guards unreachable through the public paths that wrap them:
# the non-dict continue in _fetch_military_merged (its inputs pass through
# _safe_fetch, which always yields a dict).
# ---------------------------------------------------------------------------


def test_segment_distance_perpendicular_pole() -> None:
    # P at the pole, equatorial segment: cross-track is exactly 90 deg,
    # cos(xt) ~ 0, exercising the polar guard; distance is a quarter
    # circumference (~10,007 km).
    d = aoi.segment_distance_km(90.0, 0.0, 0.0, -10.0, 0.0, 10.0)
    assert d == pytest.approx(10007, rel=0.01)


def test_store_update_missing_aoi_raises() -> None:
    import tempfile as _tf

    s = AOIStore(Path(_tf.mkdtemp()) / "kx.db")
    try:
        with pytest.raises(KeyError):
            s.update("Nowhere", radius_km=10.0)
    finally:
        s.close()


def test_get_snapshot_with_corrupt_json_returns_none(store: AOIStore) -> None:
    store.define("Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    store._conn.execute(
        "INSERT OR REPLACE INTO aoi_snapshots (name_key, taken_at, snapshot) "
        "VALUES ('pittsburgh', 0, 'not json')"
    )
    store._conn.commit()
    assert store.get_snapshot("Pittsburgh") is None


@pytest.mark.parametrize("bad_name", [123, "", "   ", None])
def test_name_validation_across_tools(bad_name, store: AOIStore, fetcher) -> None:
    assert "error" in aoi.delete_aoi(store, bad_name)
    assert "error" in aoi.update_aoi(store, bad_name, radius_km=10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [123, "   "])
async def test_name_validation_async_tools(bad_name, store: AOIStore, fetcher) -> None:
    assert "error" in await aoi.fetch_aoi_brief(fetcher, store, bad_name)
    assert "error" in await aoi.fetch_aoi_escalation(fetcher, store, bad_name)
    assert "error" in await aoi.fetch_aoi_changes(fetcher, store, bad_name)


def test_nearby_pipelines_endpoint_fallback_on_partial_coords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline missing one endpoint's coordinates can't be treated as
    a segment; it falls back to whichever endpoint is placeable."""
    from world_intel_mcp.config import geospatial as geo_cfg

    monkeypatch.setattr(
        geo_cfg,
        "PIPELINES",
        [
            {
                "name": "Half-Mapped Line",
                "lat_start": None,
                "lon_start": None,
                "lat_end": 0.0,
                "lon_end": 0.0,
            },
            {"name": "Unmappable Line"},
        ],
    )
    result = aoi.nearby_pipelines(0.2, 0.0, 100.0)
    names = [p["name"] for p in result]
    assert "Half-Mapped Line" in names
    assert "Unmappable Line" not in names


def test_nearby_cables_skips_unplaceable_landing_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from world_intel_mcp.config import cables as cables_cfg

    monkeypatch.setattr(
        cables_cfg,
        "UNDERSEA_CABLES",
        [
            {
                "name": "Patchy Cable",
                "status": "active",
                "landing_points": [
                    {"name": "Ghost", "lat": None, "lon": None},
                    {"name": "Bad", "lat": "not-a-number", "lon": "x"},
                    {"name": "Real", "lat": 0.0, "lon": 0.0},
                ],
            }
        ],
    )
    result = aoi.nearby_cables(0.2, 0.0, 100.0)
    assert result[0]["name"] == "Patchy Cable"
    assert result[0]["nearest_landing_point"] == "Real"


def test_wildfire_region_dedup_when_both_boxes_overlap_one_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No real FIRMS region spans the dateline today; patch one that does
    so the both-boxes-same-region dedup guard is exercised."""
    from world_intel_mcp.sources import wildfire

    monkeypatch.setattr(wildfire, "REGIONS", {"global": "-180,-90,180,90"})
    regions = aoi._overlapping_wildfire_regions(52.0, 179.9, 300.0)
    assert regions == ["global"]


@pytest.mark.asyncio
async def test_wildfires_for_aoi_all_regions_error_and_non_dict(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    async def _flaky_wildfires(fetcher, region=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-a-dict"
        return {"error": f"FIRMS down for {region}"}

    monkeypatch.setattr(wildfire, "fetch_wildfires", _flaky_wildfires)
    result = await aoi._fetch_wildfires_for_aoi(
        fetcher, ["north_america", "europe", "africa"]
    )
    assert "FIRMS down" in result["error"]


@pytest.mark.asyncio
async def test_safe_fetch_catches_raising_coroutine(fetcher) -> None:
    async def _boom():
        raise RuntimeError("upstream exploded")

    result = await aoi._safe_fetch(_boom(), "boom")
    assert result == {"error": "upstream exploded"}


@pytest.mark.asyncio
async def test_brief_data_gaps_for_eq_military_aviation_and_titleless_news(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)

    async def _err(fetcher, **kwargs):
        return {"error": "backend down"}

    async def _titleless_gdelt(fetcher, **kwargs):
        return {"articles": [{"url": "https://x.example/1", "title": ""}], "count": 1}

    monkeypatch.setattr(seismology, "fetch_earthquakes", _err)
    monkeypatch.setattr(military, "fetch_military_flights", _err)
    monkeypatch.setattr(aviation, "fetch_domestic_flights", _err)
    monkeypatch.setattr(news, "fetch_gdelt_search", _titleless_gdelt)

    result = await aoi.fetch_aoi_brief(fetcher, aoi_store_with_pittsburgh, "Pittsburgh")
    gaps = " | ".join(result["data_gaps"])
    assert "Earthquakes" in gaps
    assert "Military flights" in gaps
    assert "Aviation" in gaps
    # The titleless article is counted but produces no citation line.
    assert result["counts"]["news"] == 1
    assert not any(s["domain"] == "news" for s in result["sources"])


@pytest.fixture
def dateline_store(tmp_path: Path) -> AOIStore:
    s = AOIStore(tmp_path / "dateline.db")
    # Two bounding boxes (crosses lon 180).
    s.define("Bering", 52.0, 179.8, 200.0)
    yield s
    s.close()


def _per_bbox_military(fail_boxes: set[int]):
    """Fake military fetch that errors for chosen box indexes and
    returns one duplicated + one unique aircraft otherwise."""
    seen: list[str] = []

    async def _fake(fetcher, bbox=None):
        seen.append(bbox)
        idx = len(seen) - 1
        if idx in fail_boxes:
            return {"error": f"box {idx} upstream down"}
        return {
            "aircraft": [
                {
                    "icao24": "dup001",
                    "callsign": "RCH777",
                    "origin_country": "United States",
                    "latitude": 52.0,
                    "longitude": 179.9,
                },
                {
                    "icao24": f"uniq{idx}",
                    "callsign": f"RCH{idx}",
                    "origin_country": "United States",
                    "latitude": 52.0,
                    "longitude": -179.95,
                },
            ]
        }

    _fake.seen = seen
    return _fake


@pytest.mark.asyncio
async def test_dateline_brief_merges_boxes_and_dedupes(
    fetcher, dateline_store: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    fake = _per_bbox_military(fail_boxes=set())
    monkeypatch.setattr(military, "fetch_military_flights", fake)

    result = await aoi.fetch_aoi_brief(fetcher, dateline_store, "Bering")

    assert len(fake.seen) == 2, "a dateline AOI must query both boxes"
    # dup001 counted once, uniq0 and uniq1 both in range: 3 aircraft.
    assert result["counts"]["military_flights"] == 3


@pytest.mark.asyncio
async def test_dateline_brief_partial_coverage_gap(
    fetcher, dateline_store: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    monkeypatch.setattr(
        military, "fetch_military_flights", _per_bbox_military(fail_boxes={1})
    )

    result = await aoi.fetch_aoi_brief(fetcher, dateline_store, "Bering")

    assert any("partial coverage" in g for g in result["data_gaps"])
    # The surviving box still contributes aircraft.
    assert result["counts"]["military_flights"] == 2


@pytest.mark.asyncio
async def test_dateline_escalation_partial_coverage_and_battle_count(
    fetcher, dateline_store: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _battle_acled(fetcher, **kwargs):
        return {
            "events": [
                {
                    "event_type": "Battles",
                    "location": "Near Bering",
                    "event_date": "2026-08-31",
                    "latitude": 52.0,
                    "longitude": 179.9,
                    "fatalities": 3,
                }
            ]
        }

    monkeypatch.setattr(conflict, "fetch_acled_events", _battle_acled)
    monkeypatch.setattr(
        military, "fetch_military_flights", _per_bbox_military(fail_boxes={0})
    )

    result = await aoi.fetch_aoi_escalation(fetcher, dateline_store, "Bering")

    assert any("partial coverage" in g for g in result["data_gaps"])
    # One battle with 3 fatalities: conflict component = 1*0.5 + 3*0.1.
    assert result["components"]["conflict"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_dateline_changes_partial_coverage(
    fetcher, dateline_store: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    monkeypatch.setattr(
        military, "fetch_military_flights", _per_bbox_military(fail_boxes={1})
    )

    result = await aoi.fetch_aoi_changes(fetcher, dateline_store, "Bering")

    assert any("partial coverage" in g for g in result["data_gaps"])
    # No FIRMS region reaches this stretch of the Bering; the honest gap.
    assert any("Wildfires" in g for g in result["data_gaps"])
    assert "wildfires" not in result["changes"]


@pytest.mark.asyncio
async def test_changes_drops_unkeyable_item_keeps_placeable(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item whose key function raises (unparseable coordinates in a
    wildfire cluster that slipped past the radius filter's own guard)
    is dropped from the diff, not allowed to poison the sweep."""
    _patch_all_domains(monkeypatch)

    real_filter = aoi.filter_by_radius

    def _leaky_filter(
        items, lat, lon, radius_km, lat_key="latitude", lon_key="longitude"
    ):
        kept = real_filter(items, lat, lon, radius_km, lat_key, lon_key)
        # Simulate a malformed item surviving into the scoped list.
        leak = [i for i in items if i.get("fire_count") == 99]
        return kept + leak

    async def _wildfires_with_bad_cluster(fetcher, region=None):
        return {
            "fires_by_region": {
                region: {
                    "top_clusters": [
                        {"lat": _NEAR_LAT, "lon": _NEAR_LON, "fire_count": 2},
                        {"lat": "bad", "lon": None, "fire_count": 99},
                    ]
                }
            }
        }

    monkeypatch.setattr(wildfire, "fetch_wildfires", _wildfires_with_bad_cluster)
    monkeypatch.setattr(aoi, "filter_by_radius", _leaky_filter)

    result = await aoi.fetch_aoi_changes(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )

    # The malformed cluster is dropped by the key function guard; only
    # the placeable one is counted.
    assert result["counts"]["wildfires"] == 1


@pytest.mark.asyncio
async def test_escalation_reports_gap_when_acled_fails(
    fetcher, aoi_store_with_pittsburgh: AOIStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _broken_acled(fetcher, **kwargs):
        return {"error": "ACLED down"}

    monkeypatch.setattr(conflict, "fetch_acled_events", _broken_acled)
    monkeypatch.setattr(military, "fetch_military_flights", _fake_military)

    result = await aoi.fetch_aoi_escalation(
        fetcher, aoi_store_with_pittsburgh, "Pittsburgh"
    )
    assert any("Conflict events" in g for g in result["data_gaps"])


# ---------------------------------------------------------------------------
# Polygon + corridor AOIs (Phase 23)
# ---------------------------------------------------------------------------

# A square around Pittsburgh, ~±0.9 deg (~100 km), listed counterclockwise.
_PGH_SQUARE = [
    [39.5, -81.0],
    [39.5, -79.0],
    [41.3, -79.0],
    [41.3, -81.0],
]

# Inside the square's bounding CIRCLE but outside the square itself:
# the bounding circle around the centroid reaches the corners, so a point
# due east beyond the square's edge still falls inside the circle.
_IN_CIRCLE_NOT_SQUARE = (40.4, -78.7)


def test_polygon_centroid_of_square() -> None:
    lat, lon = aoi.polygon_centroid(_PGH_SQUARE)
    assert lat == pytest.approx(40.4, abs=0.01)
    assert lon == pytest.approx(-80.0, abs=0.01)


def test_point_in_polygon_inside_and_outside() -> None:
    assert aoi.point_in_polygon(40.4, -80.0, _PGH_SQUARE) is True
    assert aoi.point_in_polygon(42.0, -80.0, _PGH_SQUARE) is False
    assert aoi.point_in_polygon(*_IN_CIRCLE_NOT_SQUARE, _PGH_SQUARE) is False


def test_point_in_polygon_across_dateline() -> None:
    # A box straddling lon 180 near Fiji latitudes.
    box = [[-20.0, 178.0], [-20.0, -178.0], [-15.0, -178.0], [-15.0, 178.0]]
    assert aoi.point_in_polygon(-17.0, 179.5, box) is True
    assert aoi.point_in_polygon(-17.0, -179.5, box) is True
    assert aoi.point_in_polygon(-17.0, 170.0, box) is False


def test_point_in_polygon_concave() -> None:
    # A "U" shape: the notch at the top middle is outside.
    u = [
        [0.0, 0.0],
        [0.0, 3.0],
        [2.0, 3.0],
        [2.0, 2.0],
        [1.0, 2.0],
        [1.0, 1.0],
        [2.0, 1.0],
        [2.0, 0.0],
    ]
    assert aoi.point_in_polygon(1.5, 1.5, u) is False  # in the notch
    assert aoi.point_in_polygon(0.5, 1.5, u) is True  # in the base


@pytest.mark.parametrize(
    "vertices",
    [
        [[0, 0], [1, 1]],  # fewer than 3
        [[0, 0], [95, 1], [1, 1]],  # bad latitude
        "not-a-list",
        [[0, 0], [1, "x"], [1, 1]],  # non-numeric
    ],
)
def test_define_polygon_aoi_rejects_invalid(store: AOIStore, vertices) -> None:
    result = aoi.define_polygon_aoi(store, "Bad Poly", vertices)
    assert "error" in result
    assert store.get("Bad Poly") is None


def test_define_polygon_aoi_round_trip(store: AOIStore) -> None:
    result = aoi.define_polygon_aoi(store, "PGH Square", _PGH_SQUARE)
    assert "error" not in result
    row = store.get("PGH Square")
    assert row["kind"] == "polygon"
    assert row["geometry"]["vertices"] == _PGH_SQUARE
    # Derived bounding circle: centroid near Pittsburgh, radius reaching
    # the corners (roughly 125-135 km for this square).
    assert row["lat"] == pytest.approx(40.4, abs=0.01)
    assert row["radius_km"] > 100
    assert row["radius_km"] < 200


def test_define_corridor_aoi_round_trip(store: AOIStore) -> None:
    waypoints = [[40.0, -80.0], [41.0, -78.0], [42.0, -76.0]]
    result = aoi.define_corridor_aoi(store, "Route A", waypoints, width_km=40.0)
    assert "error" not in result
    row = store.get("Route A")
    assert row["kind"] == "corridor"
    assert row["geometry"]["waypoints"] == waypoints
    assert row["geometry"]["width_km"] == 40.0


@pytest.mark.parametrize(
    "waypoints,width",
    [
        ([[40.0, -80.0]], 40.0),  # one waypoint is a point, not a route
        ([[40.0, -80.0], [41.0, -78.0]], 0.2),  # width below minimum
        ([[40.0, -80.0], [41.0, -78.0]], 800.0),  # width above maximum
        ([[40.0, -80.0], [200.0, -78.0]], 40.0),  # bad latitude
    ],
)
def test_define_corridor_aoi_rejects_invalid(store: AOIStore, waypoints, width) -> None:
    result = aoi.define_corridor_aoi(store, "Bad Route", waypoints, width_km=width)
    assert "error" in result
    assert store.get("Bad Route") is None


def test_filter_by_aoi_polygon_excludes_in_circle_out_of_polygon(
    store: AOIStore,
) -> None:
    """THE load-bearing polygon test: a point inside the bounding circle
    but outside the polygon must be excluded - otherwise a polygon is
    just a circle with extra steps."""
    aoi.define_polygon_aoi(store, "PGH Square", _PGH_SQUARE)
    row = store.get("PGH Square")
    items = [
        {"latitude": 40.4, "longitude": -80.0, "id": "inside"},
        {
            "latitude": _IN_CIRCLE_NOT_SQUARE[0],
            "longitude": _IN_CIRCLE_NOT_SQUARE[1],
            "id": "circle-only",
        },
        {"latitude": 45.0, "longitude": -80.0, "id": "far"},
    ]
    kept = aoi.filter_by_aoi(items, row)
    ids = [i["id"] for i in kept]
    assert ids == ["inside"]
    assert kept[0]["distance_km"] >= 0


def test_filter_by_aoi_corridor_keeps_along_route_drops_off_route(
    store: AOIStore,
) -> None:
    """THE load-bearing corridor test: a point far from the centroid but
    within the width of a distant segment is kept; a point near the
    route's latitude band but off to the side is dropped."""
    waypoints = [[0.0, -5.0], [0.0, 0.0], [0.0, 5.0]]
    aoi.define_corridor_aoi(store, "Equator Lane", waypoints, width_km=100.0)
    row = store.get("Equator Lane")
    items = [
        # ~30 km abeam the far-east end of the route: inside the corridor.
        {"latitude": 0.3, "longitude": 4.8, "id": "along-route"},
        # ~330 km abeam the middle: outside the 50 km half-width.
        {"latitude": 3.0, "longitude": 0.0, "id": "off-route"},
    ]
    kept = aoi.filter_by_aoi(items, row)
    assert [i["id"] for i in kept] == ["along-route"]


def test_filter_by_aoi_circle_matches_filter_by_radius(store: AOIStore) -> None:
    """Back-compat: for a circle AOI the shape filter must agree exactly
    with the original radius filter."""
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    row = store.get("Pittsburgh")
    items = [
        {"latitude": _NEAR_LAT, "longitude": _NEAR_LON, "id": "near"},
        {"latitude": _FAR_LAT, "longitude": _FAR_LON, "id": "far"},
    ]
    via_shape = aoi.filter_by_aoi(items, row)
    via_radius = aoi.filter_by_radius(items, _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    assert [i["id"] for i in via_shape] == [i["id"] for i in via_radius]
    assert via_shape[0]["distance_km"] == via_radius[0]["distance_km"]


@pytest.mark.asyncio
async def test_brief_with_polygon_aoi_scopes_by_shape(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)

    async def _eq_in_and_out(fetcher_, **kwargs):
        return {
            "earthquakes": [
                {
                    "id": "in-poly",
                    "magnitude": 3.0,
                    "place": "inside square",
                    "latitude": 40.4,
                    "longitude": -80.0,
                },
                {
                    "id": "circle-only",
                    "magnitude": 4.0,
                    "place": "circle only",
                    "latitude": _IN_CIRCLE_NOT_SQUARE[0],
                    "longitude": _IN_CIRCLE_NOT_SQUARE[1],
                },
            ]
        }

    monkeypatch.setattr(seismology, "fetch_earthquakes", _eq_in_and_out)
    s = AOIStore(tmp_path / "poly.db")
    aoi.define_polygon_aoi(s, "PGH Square", _PGH_SQUARE)

    result = await aoi.fetch_aoi_brief(fetcher, s, "PGH Square")
    assert result["counts"]["earthquakes"] == 1
    assert "inside square" in result["markdown"]
    assert "circle only" not in result["markdown"]
    assert result["aoi"]["kind"] == "polygon"
    s.close()


@pytest.mark.asyncio
async def test_changes_with_corridor_aoi_baselines_and_diffs(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)

    async def _eq_on_route(fetcher_, **kwargs):
        return {
            "earthquakes": [
                {
                    "id": "route-eq",
                    "magnitude": 3.0,
                    "place": "on route",
                    "latitude": 0.2,
                    "longitude": 4.8,
                }
            ]
        }

    monkeypatch.setattr(seismology, "fetch_earthquakes", _eq_on_route)
    s = AOIStore(tmp_path / "corr.db")
    aoi.define_corridor_aoi(
        s, "Equator Lane", [[0.0, -5.0], [0.0, 0.0], [0.0, 5.0]], width_km=100.0
    )

    first = await aoi.fetch_aoi_changes(fetcher, s, "Equator Lane")
    assert first["baseline"] is True
    second = await aoi.fetch_aoi_changes(fetcher, s, "Equator Lane")
    assert second["baseline"] is False
    assert second["changes"]["earthquakes"]["unchanged"] == 1
    s.close()


def test_update_geometry_rejected_for_non_circle(store: AOIStore) -> None:
    aoi.define_polygon_aoi(store, "PGH Square", _PGH_SQUARE)
    result = aoi.update_aoi(store, "PGH Square", radius_km=500.0)
    assert "error" in result
    # Renames still work for any shape.
    renamed = aoi.update_aoi(store, "PGH Square", new_name="Square v2")
    assert "error" not in renamed
    assert store.get("Square v2")["kind"] == "polygon"


def test_list_aois_reports_kind(store: AOIStore) -> None:
    aoi.define_aoi(store, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    aoi.define_polygon_aoi(store, "PGH Square", _PGH_SQUARE)
    kinds = {a["name"]: a["kind"] for a in aoi.list_aois(store)["aois"]}
    assert kinds == {"Pittsburgh": "circle", "PGH Square": "polygon"}


def test_store_migrates_pre_shape_schema(tmp_path: Path) -> None:
    """A database created before the kind/geometry columns existed must
    open cleanly, with old rows readable as circles."""
    import sqlite3 as _sq

    db = tmp_path / "old.db"
    conn = _sq.connect(str(db))
    conn.execute(
        "CREATE TABLE aois (name_key TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "lat REAL NOT NULL, lon REAL NOT NULL, radius_km REAL NOT NULL, "
        "created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO aois VALUES ('pittsburgh', 'Pittsburgh', 40.4406, "
        "-79.9959, 50.0, 0)"
    )
    conn.commit()
    conn.close()

    s = AOIStore(db)
    row = s.get("Pittsburgh")
    assert row["kind"] == "circle"
    assert row["geometry"] is None
    assert row["radius_km"] == 50.0
    # And new shapes write fine into the migrated table.
    assert "error" not in aoi.define_polygon_aoi(s, "PGH Square", _PGH_SQUARE)
    s.close()


# ---------------------------------------------------------------------------
# intel_aoi_digest: one-call change sweep across all AOIs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aoi_digest_no_aois_is_honest_empty(fetcher, store: AOIStore) -> None:
    result = await aoi.fetch_aoi_digest(fetcher, store)
    assert "error" not in result
    assert result["aois"] == []
    assert result["count"] == 0
    assert "No AOIs defined" in result["note"]


@pytest.mark.asyncio
async def test_aoi_digest_sweeps_all_aois_and_totals(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    s = AOIStore(tmp_path / "digest.db")
    aoi.define_aoi(s, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    aoi.define_polygon_aoi(s, "PGH Square", _PGH_SQUARE)

    first = await aoi.fetch_aoi_digest(fetcher, s)
    assert first["count"] == 2
    assert all(entry["baseline"] for entry in first["aois"])
    assert first["totals"]["new_items"] == 0
    assert first["totals"]["departed_items"] == 0
    # Per-AOI sections in the digest markdown.
    assert "Pittsburgh" in first["markdown"]
    assert "PGH Square" in first["markdown"]

    # Second sweep: quake near1 replaced by near2 -> 1 new + 1 departed
    # per AOI that contains both points.
    async def _second_earthquakes(fetcher_, **kwargs):
        return {
            "earthquakes": [
                {
                    "id": "near2",
                    "magnitude": 4.1,
                    "place": "near Pittsburgh again",
                    "latitude": _NEAR_LAT,
                    "longitude": _NEAR_LON,
                }
            ]
        }

    monkeypatch.setattr(seismology, "fetch_earthquakes", _second_earthquakes)
    second = await aoi.fetch_aoi_digest(fetcher, s)
    assert not any(entry["baseline"] for entry in second["aois"])
    assert second["totals"]["new_items"] == 2  # near2 entered both AOIs
    assert second["totals"]["departed_items"] == 2  # near1 left both
    pgh = next(e for e in second["aois"] if e["name"] == "Pittsburgh")
    assert pgh["changes"]["earthquakes"]["new"][0]["key"] == "near2"
    s.close()


@pytest.mark.asyncio
async def test_aoi_digest_names_filter_and_unknown_name(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_domains(monkeypatch)
    s = AOIStore(tmp_path / "digest2.db")
    aoi.define_aoi(s, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    aoi.define_aoi(s, "Austin", 30.27, -97.74, 40.0)

    only = await aoi.fetch_aoi_digest(fetcher, s, names=["pittsburgh"])
    assert [e["name"] for e in only["aois"]] == ["Pittsburgh"]

    missing = await aoi.fetch_aoi_digest(fetcher, s, names=["Nowhere"])
    assert "error" in missing
    assert "Nowhere" in missing["error"]
    s.close()


@pytest.mark.asyncio
async def test_aoi_digest_domain_error_stays_per_aoi(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing domain shows up in that AOI's data_gaps without
    contaminating the other AOIs' results."""
    _patch_all_domains(monkeypatch)

    async def _broken_earthquakes(fetcher_, **kwargs):
        return {"error": "USGS down"}

    monkeypatch.setattr(seismology, "fetch_earthquakes", _broken_earthquakes)
    s = AOIStore(tmp_path / "digest3.db")
    aoi.define_aoi(s, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)

    result = await aoi.fetch_aoi_digest(fetcher, s)
    entry = result["aois"][0]
    assert any("Earthquakes" in g for g in entry["data_gaps"])
    assert "error" not in result  # the digest itself succeeded
    s.close()


# ---------------------------------------------------------------------------
# fetch_aoi_sweep — the collector-daemon entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aoi_sweep_derives_store_from_fetcher_cache(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collector calls every source as ``fn(fetcher)``; the sweep
    wrapper must open the AOI store on the same SQLite file the
    fetcher's cache uses — the store the MCP server and CLI write to,
    not a fresh default-path database."""
    seen: dict = {}

    async def _fake_digest(fetcher_, store, names=None):
        seen["db"] = Path(store.db_path)
        return {"marker": "digest"}

    monkeypatch.setattr(aoi, "fetch_aoi_digest", _fake_digest)
    result = await aoi.fetch_aoi_sweep(fetcher)
    assert result["marker"] == "digest"
    assert result["notification"] == {"configured": False}
    assert seen["db"] == Path(fetcher.cache.db_path)


@pytest.mark.asyncio
async def test_aoi_sweep_real_path_no_aois(fetcher) -> None:
    """Real (unmocked-digest) path: no AOIs defined is the honest note,
    not an error, and the wrapper leaves no store handle behind."""
    result = await aoi.fetch_aoi_sweep(fetcher)
    assert "error" not in result
    assert result["count"] == 0
    assert "No AOIs defined" in result["note"]


@pytest.mark.asyncio
async def test_aoi_sweep_sees_aois_defined_via_cache_db(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AOIs defined through another store handle on the same cache DB
    (as the MCP server or CLI would) are picked up by the sweep."""
    _patch_all_domains(monkeypatch)
    s = AOIStore(fetcher.cache.db_path)
    aoi.define_aoi(s, "Pittsburgh", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)
    s.close()

    result = await aoi.fetch_aoi_sweep(fetcher)
    assert result["count"] == 1
    assert result["aois"][0]["name"] == "Pittsburgh"
    assert result["aois"][0]["baseline"] is True


# ---------------------------------------------------------------------------
# Sweep webhook notifications (WORLD_INTEL_AOI_WEBHOOK)
# ---------------------------------------------------------------------------

_HOOK = "https://hooks.example.test/aoi"


def _digest_result(new: int, departed: int) -> dict:
    return {
        "aois": [{"name": "Pittsburgh"}],
        "count": 1,
        "totals": {"new_items": new, "departed_items": departed},
        "markdown": "# AOI digest\n- M4.9 earthquake entered Pittsburgh",
        "source": "aoi-digest",
        "timestamp": "2026-09-01T00:00:00+00:00",
    }


def _patch_digest(monkeypatch: pytest.MonkeyPatch, digest: dict) -> None:
    async def _fake(fetcher_, store, names=None):
        return dict(digest)

    monkeypatch.setattr(aoi, "fetch_aoi_digest", _fake)


@pytest.mark.asyncio
async def test_sweep_notification_unconfigured(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WORLD_INTEL_AOI_WEBHOOK", raising=False)
    _patch_digest(monkeypatch, _digest_result(2, 1))
    result = await aoi.fetch_aoi_sweep(fetcher)
    assert result["notification"] == {"configured": False}


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_skips_quiet_sweep(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep with zero changes must not POST — the sink is for
    departures/arrivals, not a heartbeat."""
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    route = respx.post(_HOOK).mock(return_value=httpx.Response(200))
    _patch_digest(monkeypatch, _digest_result(0, 0))
    result = await aoi.fetch_aoi_sweep(fetcher)
    note = result["notification"]
    assert note["configured"] is True
    assert note["sent"] is False
    assert "no changes" in note["reason"]
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_posts_json(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    monkeypatch.delenv("WORLD_INTEL_AOI_WEBHOOK_FORMAT", raising=False)
    route = respx.post(_HOOK).mock(return_value=httpx.Response(200))
    _patch_digest(monkeypatch, _digest_result(2, 1))

    result = await aoi.fetch_aoi_sweep(fetcher)

    note = result["notification"]
    assert note["sent"] is True
    assert note["status_code"] == 200
    body = json.loads(route.calls.last.request.content)
    assert "2 new" in body["title"] and "1 departed" in body["title"]
    assert body["totals"] == {"new_items": 2, "departed_items": 1}
    assert "earthquake entered Pittsburgh" in body["markdown"]


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_text_format(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """format=text posts the raw markdown body with a Title header —
    the shape ntfy-style sinks consume directly."""
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK_FORMAT", "text")
    route = respx.post(_HOOK).mock(return_value=httpx.Response(200))
    _patch_digest(monkeypatch, _digest_result(1, 0))

    result = await aoi.fetch_aoi_sweep(fetcher)

    assert result["notification"]["sent"] is True
    req = route.calls.last.request
    assert b"earthquake entered Pittsburgh" in req.content
    assert "Title" in req.headers


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_failure_is_honest(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead sink must not fail the sweep (snapshots already advanced)
    but must not be silent either."""
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    respx.post(_HOOK).mock(side_effect=httpx.ConnectError("refused"))
    _patch_digest(monkeypatch, _digest_result(1, 0))

    result = await aoi.fetch_aoi_sweep(fetcher)

    assert result["count"] == 1  # sweep itself succeeded
    note = result["notification"]
    assert note["sent"] is False
    assert "ConnectError" in note["error"]


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_non_2xx_is_not_sent(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    respx.post(_HOOK).mock(return_value=httpx.Response(500))
    _patch_digest(monkeypatch, _digest_result(1, 0))

    result = await aoi.fetch_aoi_sweep(fetcher)

    note = result["notification"]
    assert note["sent"] is False
    assert note["status_code"] == 500


@pytest.mark.asyncio
@respx.mock
async def test_sweep_notification_unknown_format_rejected(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK", _HOOK)
    monkeypatch.setenv("WORLD_INTEL_AOI_WEBHOOK_FORMAT", "xml")
    route = respx.post(_HOOK).mock(return_value=httpx.Response(200))
    _patch_digest(monkeypatch, _digest_result(1, 0))

    result = await aoi.fetch_aoi_sweep(fetcher)

    note = result["notification"]
    assert note["sent"] is False
    assert "xml" in note["error"] and "json" in note["error"]
    assert not route.called


# ---------------------------------------------------------------------------
# Geo-scoped AOI news (place names, not AOI-name mention)
# ---------------------------------------------------------------------------


def _patch_geocode(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    async def _fake(fetcher_, lat, lon):
        return dict(result)

    monkeypatch.setattr(geocode, "fetch_place_context", _fake)


@pytest.mark.asyncio
async def test_news_query_uses_geocoded_place_not_aoi_name(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this fixes, measured live 2026-09-02: an AOI named
    "Home" queried GDELT for "Home" and got a Chinese IPO article. The
    query must come from where the AOI IS."""
    _patch_geocode(
        monkeypatch,
        {
            "place": "Pittsburgh",
            "county": "Allegheny County",
            "terms": ["Pittsburgh", "Allegheny County"],
        },
    )
    row = {
        "name": "Home",
        "lat": _PGH_LAT,
        "lon": _PGH_LON,
        "radius_km": _PGH_RADIUS_KM,
    }

    query, basis = await aoi._aoi_news_query(fetcher, row)

    assert "Pittsburgh" in query
    assert "Home" not in query
    assert basis["basis"] == "geocoded"
    assert basis["place"] == "Pittsburgh"


@pytest.mark.asyncio
async def test_news_query_falls_back_to_name_and_says_so(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When geocoding fails the old name-mention behavior is the only
    option left, but the caller must be told, because that is exactly
    when results are unreliable."""
    _patch_geocode(monkeypatch, {"error": "Nominatim HTTP 503", "place": None})
    row = {
        "name": "Pittsburgh",
        "lat": _PGH_LAT,
        "lon": _PGH_LON,
        "radius_km": _PGH_RADIUS_KM,
    }

    query, basis = await aoi._aoi_news_query(fetcher, row)

    assert query == "Pittsburgh"
    assert basis["basis"] == "aoi_name"
    assert "503" in basis["reason"]


@pytest.mark.asyncio
async def test_news_query_unnamed_place_falls_back_to_name(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-ocean AOI: the lookup succeeded but named nothing."""
    _patch_geocode(monkeypatch, {"place": None, "terms": [], "note": "open water"})
    row = {"name": "Bering Watch", "lat": 60.0, "lon": -179.0, "radius_km": 200.0}

    query, basis = await aoi._aoi_news_query(fetcher, row)

    assert query == "Bering Watch"
    assert basis["basis"] == "aoi_name"


@pytest.mark.asyncio
async def test_brief_reports_news_scoping_basis(
    fetcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief must disclose how its news was scoped; a name-mention
    fallback is a data gap, since those results may be unrelated."""
    _patch_all_domains(monkeypatch)
    _patch_geocode(monkeypatch, {"error": "Nominatim HTTP 503", "place": None})
    s = AOIStore(tmp_path / "newsbasis.db")
    aoi.define_aoi(s, "Home", _PGH_LAT, _PGH_LON, _PGH_RADIUS_KM)

    result = await aoi.fetch_aoi_brief(fetcher, s, "Home")

    assert result["news_scoping"]["basis"] == "aoi_name"
    assert any("name" in g.lower() for g in result["data_gaps"])
    s.close()


@pytest.mark.asyncio
async def test_news_query_parenthesizes_multi_term_or(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured live 2026-09-02: GDELT's DOC API returns NOTHING for a
    bare `"a" OR "b"` and works for `("a" OR "b")`. Unparenthesized
    would have been worse than the defect being fixed - silent empty
    news instead of wrong news."""
    _patch_geocode(
        monkeypatch,
        {
            "place": "Pittsburgh",
            "county": "Allegheny County",
            "terms": ["Pittsburgh", "Allegheny County"],
        },
    )
    row = {"name": "Home", "lat": _PGH_LAT, "lon": _PGH_LON, "radius_km": _PGH_RADIUS_KM}

    query, _ = await aoi._aoi_news_query(fetcher, row)

    assert query.startswith("(") and query.endswith(")")
    assert query == '("Pittsburgh" OR "Allegheny County")'


@pytest.mark.asyncio
async def test_news_query_single_term_needs_no_parens(
    fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_geocode(monkeypatch, {"place": "Reykjavik", "county": None, "terms": ["Reykjavik"]})
    row = {"name": "North Watch", "lat": 64.15, "lon": -21.94, "radius_km": 30.0}

    query, _ = await aoi._aoi_news_query(fetcher, row)

    assert query == '"Reykjavik"'
