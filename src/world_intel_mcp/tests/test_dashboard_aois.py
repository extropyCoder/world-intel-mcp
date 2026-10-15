"""Tests for the dashboard's /api/aois endpoint (Phase 23: dashboard AOI layer).

The map draws user-defined geofences from this endpoint and shows, per
area, what the last collector sweep counted inside it. Counts come from
the stored change snapshot, not a live multi-domain gather: the
dashboard boots instantly and never spends 60-90 s of upstream fetches
per AOI just to draw a shape.

First endpoint tests for the dashboard; the prior test_dashboard.py
covered only argument parsing. Uses Starlette's TestClient without the
context manager so lifespan (default-path Cache, vector store) never
runs.

Gaps / not covered: the Leaflet rendering itself (index.html JS) has no
automated test; it is verified by loading the dashboard live.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from world_intel_mcp.analysis import aoi
from world_intel_mcp.analysis.aoi import AOIStore
from world_intel_mcp.dashboard import app as dashboard_app

_PGH_LAT, _PGH_LON = 40.4406, -79.9959


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AOIStore:
    """The test's own handle for defining AOIs. The endpoint is patched
    to open a FRESH store on the same file per request, exactly as
    production does: SQLite connections are thread-bound, and the test
    client serves requests on a different thread from this fixture."""
    db_path = tmp_path / "dash_aoi.db"
    s = AOIStore(db_path)
    monkeypatch.setattr(dashboard_app, "_open_aoi_store", lambda: AOIStore(db_path))
    yield s
    s.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(dashboard_app.app, base_url="http://localhost:8501")


def test_api_aois_empty(client: TestClient, store: AOIStore) -> None:
    r = client.get("/api/aois")
    assert r.status_code == 200
    body = r.json()
    assert body["aois"] == []
    assert body["count"] == 0
    assert "error" not in body
    assert "access-control-allow-origin" not in r.headers


def test_api_aois_lists_geometry_and_last_sweep(
    client: TestClient, store: AOIStore
) -> None:
    aoi.define_aoi(store, "Home", _PGH_LAT, _PGH_LON, 50.0)
    aoi.define_polygon_aoi(
        store,
        "PGH Square",
        [(40.3, -80.1), (40.3, -79.9), (40.6, -79.9), (40.6, -80.1)],
    )
    aoi.define_corridor_aoi(
        store, "I-79", [(40.2, -80.2), (40.5, -80.0), (40.8, -79.9)], 20.0
    )
    store.save_snapshot(
        "Home",
        {"earthquakes": {"eq1": "M4.9", "eq2": "M5.1"}, "news": {}},
    )

    r = client.get("/api/aois")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    by_name = {a["name"]: a for a in body["aois"]}

    home = by_name["Home"]
    assert home["kind"] == "circle"
    assert home["lat"] == _PGH_LAT and home["lon"] == _PGH_LON
    assert home["radius_km"] == 50.0
    assert home["last_sweep"]["taken_at"] > 0
    assert home["last_sweep"]["counts"] == {"earthquakes": 2, "news": 0}
    assert home["last_sweep"]["total"] == 2

    square = by_name["PGH Square"]
    assert square["kind"] == "polygon"
    assert len(square["geometry"]["vertices"]) == 4
    assert square["last_sweep"] is None  # never swept: say so, don't fake zeros

    corridor = by_name["I-79"]
    assert corridor["kind"] == "corridor"
    assert corridor["geometry"]["width_km"] == 20.0
    assert len(corridor["geometry"]["waypoints"]) == 3


def test_api_aois_store_failure_is_honest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken store must not render as 'no AOIs defined'."""

    def _boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(dashboard_app, "_open_aoi_store", _boom)
    r = client.get("/api/aois")
    assert r.status_code == 503
    body = r.json()
    assert "disk on fire" in body["error"]
    assert body["aois"] == [] and body["count"] == 0
