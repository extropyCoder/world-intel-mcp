"""Tests for the dashboard's /api/migration endpoint (migration corridor map layer).

The map draws the largest UNHCR origin -> destination corridors as lines between
capital-city coordinates taken from the World Bank country list. The fetch
functions are faked at the ``sources.migration`` module boundary (their own
parsing is covered by test_migration.py); these tests pin what the endpoint adds:
coordinates attached from World Bank data, corridors without coordinates counted
and listed instead of dropped, and upstream failure surfaced as an error instead
of an empty (quiet-looking) corridor list.

Gaps / not covered: the Leaflet rendering (index.html JS) has no automated test;
it is verified by loading the dashboard in a headless browser by hand.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from world_intel_mcp.dashboard import app as dashboard_app
from world_intel_mcp.sources import migration


def _wb(iso, capital, lat, lon, aggregate=False):
    return {"name": iso, "iso2": None, "region": "", "region_id": "NA" if aggregate else "X",
            "income": "", "aggregate": aggregate, "capital": capital, "latitude": lat, "longitude": lon}


_WB = {
    "SDN": _wb("SDN", "Khartoum", 15.5932, 32.5363),
    "TCD": _wb("TCD", "N'Djamena", 12.1048, 15.0445),
    "EGY": _wb("EGY", "Cairo", 30.0982, 31.2461),
    "PSE": _wb("PSE", None, None, None),  # real World Bank row: no capital, empty coordinates
    "WLD": _wb("WLD", None, None, None, aggregate=True),
}


def _corridor(o, d, people, change=None, prior=None, **extra):
    return {"origin": o, "origin_iso3": o, "destination": d, "destination_iso3": d,
            "people": people, "refugees": people, "asylum_seekers": 0,
            "other_in_need_of_protection": 0, "prior_year_people": prior, "change": change, **extra}


_FLOWS = {
    "year": 2025, "corridor_count": 4, "prior_year_available": True, "measure": "stock",
    "timestamp": "2026-10-11T00:00:00Z", "source": "unhcr",
    "corridors": [
        _corridor("SDN", "TCD", 1_200_000, change=800_000, prior=400_000),
        _corridor("UNK", "EGY", 900_000, change=0, prior=900_000),  # UNHCR "Unknown" bucket
        _corridor("PSE", "EGY", 50_000),  # no capital coordinates, prior year unknown
        _corridor("SDN", "EGY", 700_000, change=-10, prior=700_010),
    ],
}

_SURGES = {
    "year": 2025, "compared_with": 2024, "source": "unhcr",
    "corridor_increases": [
        _corridor("SDN", "TCD", 1_200_000, change=800_000, prior=400_000, pct_change=200.0),
        _corridor("SDN", "XXA", 99_000, change=60_000, prior=39_000, pct_change=153.8),
    ],
}


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the three upstream calls; each test edits the returned dict."""
    state = {"flows": _FLOWS, "surges": _SURGES, "wb": _WB, "calls": []}

    async def flows(fetcher, year=None, limit=25, **_):
        state["calls"].append(("flows", year, limit))
        return state["flows"]

    async def surges(fetcher, year=None, limit=20, **_):
        state["calls"].append(("surges", year, limit))
        return state["surges"]

    async def wb(fetcher):
        return state["wb"]

    monkeypatch.setattr(migration, "fetch_migration_flows", flows)
    monkeypatch.setattr(migration, "fetch_migration_surges", surges)
    monkeypatch.setattr(migration, "wb_countries", wb)
    monkeypatch.setattr(dashboard_app, "_ensure_fetcher", lambda: object())
    return state


@pytest.fixture
def client() -> TestClient:
    return TestClient(dashboard_app.app, base_url="http://localhost:8501")


def test_coordinates_attached_from_world_bank_capitals(client, faked) -> None:
    r = client.get("/api/migration")
    assert r.status_code == 200
    body = r.json()
    assert "error" not in body
    assert "access-control-allow-origin" not in r.headers
    by_pair = {(c["origin_iso3"], c["destination_iso3"]): c for c in body["corridors"]}
    sdn_tcd = by_pair[("SDN", "TCD")]
    assert sdn_tcd["origin_point"] == {"lat": 15.5932, "lon": 32.5363, "capital": "Khartoum"}
    assert sdn_tcd["destination_point"] == {"lat": 12.1048, "lon": 15.0445, "capital": "N'Djamena"}
    assert sdn_tcd["people"] == 1_200_000 and sdn_tcd["change"] == 800_000
    assert set(by_pair) == {("SDN", "TCD"), ("SDN", "EGY")}
    assert body["plotted"] == 2
    assert body["year"] == 2025
    assert "capital" in body["anchor"]


def test_unplotted_corridors_are_counted_and_named_not_dropped(client, faked) -> None:
    body = client.get("/api/migration").json()
    assert body["unplotted"] == 2
    assert body["plotted"] + body["unplotted"] == len(_FLOWS["corridors"])
    missing = {(u["origin_iso3"], u["destination_iso3"]): u for u in body["unplotted_corridors"]}
    assert missing[("UNK", "EGY")]["no_coordinates"] == ["origin"]
    assert missing[("PSE", "EGY")]["no_coordinates"] == ["origin"]
    assert missing[("UNK", "EGY")]["people"] == 900_000


def test_aggregate_and_unknown_iso_have_no_anchor(client, faked) -> None:
    faked["flows"] = {**_FLOWS, "corridors": [_corridor("SDN", "WLD", 5), _corridor("SDN", "ZZZ", 6)]}
    body = client.get("/api/migration").json()
    assert body["plotted"] == 0
    assert body["unplotted"] == 2
    assert all(u["no_coordinates"] == ["destination"] for u in body["unplotted_corridors"])


def test_rising_corridors_placed_and_unplotted_counted(client, faked) -> None:
    body = client.get("/api/migration").json()
    assert [(c["origin_iso3"], c["destination_iso3"]) for c in body["rising"]] == [("SDN", "TCD")]
    assert body["rising"][0]["destination_point"]["capital"] == "N'Djamena"
    assert body["rising_unplotted"] == 1  # SDN -> XXA (stateless bucket)
    assert body["compared_with"] == 2024
    assert "rising_error" not in body


def test_flows_error_passes_through_as_503_not_an_empty_map(client, faked) -> None:
    faked["flows"] = {"error": "UNHCR population API unavailable", "degraded": True, "source": "unhcr"}
    r = client.get("/api/migration")
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "UNHCR population API unavailable"
    assert body["corridors"] == [] and body["rising"] == []
    assert "plotted" not in body  # no zero count that could read as "quiet"


def test_missing_world_bank_coordinates_is_an_error(client, faked) -> None:
    faked["wb"] = None
    r = client.get("/api/migration")
    assert r.status_code == 503
    assert "World Bank" in r.json()["error"]


def test_upstream_exception_is_an_error(client, faked, monkeypatch) -> None:
    async def boom(fetcher, **_):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(migration, "fetch_migration_flows", boom)
    r = client.get("/api/migration")
    assert r.status_code == 503
    assert "socket closed" in r.json()["error"]


def test_surge_failure_keeps_corridors_and_reports_rising_error(client, faked) -> None:
    faked["surges"] = {"error": "UNHCR has no data for 2025 and 2024 to compare", "degraded": True}
    r = client.get("/api/migration")
    assert r.status_code == 200
    body = r.json()
    assert body["plotted"] == 2
    assert body["rising"] == []
    assert body["rising_error"] == "UNHCR has no data for 2025 and 2024 to compare"


def test_limit_and_year_are_passed_to_the_tools_and_validated(client, faked) -> None:
    assert client.get("/api/migration?limit=12&year=2024").status_code == 200
    assert ("flows", 2024, 12) in faked["calls"]
    assert ("surges", 2024, dashboard_app._MIGRATION_RISING_LIMIT) in faked["calls"]
    for bad in ("limit=0", "limit=151", "limit=abc", "year=x"):
        r = client.get(f"/api/migration?{bad}")
        assert r.status_code == 400, bad
        assert "error" in r.json()


@pytest.mark.asyncio
async def test_wb_countries_reads_capital_coordinates() -> None:
    """Coordinates come off the World Bank response; an empty string (West Bank
    and Gaza, aggregates) becomes None rather than 0.0."""
    rows = [
        {"id": "SDN", "iso2Code": "SD", "name": "Sudan", "region": {"id": "SSF", "value": "Sub-Saharan Africa "},
         "incomeLevel": {"value": "Low income"}, "capitalCity": "Khartoum",
         "longitude": "32.5363", "latitude": "15.5932"},
        {"id": "PSE", "iso2Code": "PS", "name": "West Bank and Gaza", "region": {"id": "MEA", "value": "MENA"},
         "incomeLevel": {"value": "Lower middle income"}, "capitalCity": "", "longitude": "", "latitude": ""},
    ]

    class Stub:
        async def get_json(self, **kwargs):
            return [{"page": 1}, rows]

    out = await migration.wb_countries(Stub())
    assert (out["SDN"]["latitude"], out["SDN"]["longitude"], out["SDN"]["capital"]) == (15.5932, 32.5363, "Khartoum")
    assert out["PSE"]["latitude"] is None and out["PSE"]["longitude"] is None and out["PSE"]["capital"] is None


@pytest.mark.parametrize("part", ["flows", "surges"])
def test_structured_upstream_errors_are_sanitized_copies(client, faked, part):
    import copy
    import json

    error = {"message": "upstream failed", "headers": [{"Authorization": "DUMMY_MIGRATION_AUTH"}],
             "api_key": [{"value": "DUMMY_MIGRATION_KEY"}]}
    original = copy.deepcopy(error)
    faked[part] = {"error": error}
    response = client.get("/api/migration")
    assert response.status_code == (503 if part == "flows" else 200)
    output = response.json()["error" if part == "flows" else "rising_error"]
    assert "DUMMY_MIGRATION" not in json.dumps(output)
    assert output["message"] == "upstream failed"
    assert output["api_key"] == "[REDACTED]"
    assert error == original
