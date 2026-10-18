"""Provider-aware focused copies; real adapters and HTTP/cache fixtures only."""
import copy
import json
from pathlib import Path
import runpy

import httpx
import pytest

from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.dashboard import focused, settings
from world_intel_mcp.dashboard.aircraft import display_aircraft
from world_intel_mcp.sources import military


PROFILE = settings.parse({"version": 1, "groups": ["military"], "sources": ["military_flights"]})


@pytest.fixture(autouse=True)
def no_mock_transport_courtesy_delays(monkeypatch):
    monkeypatch.setattr("world_intel_mcp.fetcher._SOURCE_RATE_LIMITS", {})
    for key in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)


def rendered(value):
    here = Path(__file__).resolve().parent
    runtime = runpy.run_path(str(here / "test_dashboard_focus.py"))
    if not runtime["DUKTAPE"] and not runtime["NODE"]:
        pytest.skip("No local JS runtime")
    html = here.parent.joinpath("dashboard/index.html").read_text()
    functions = []
    for name in ("rememberControlFocus", "alertControlKey", "updateHudStats", "updateAlertBanner", "activateHudAction", "updateDrawer", "updateFocusedDrawer", "profileData"):
        start = html.index("function " + name + "(")
        functions.append(html[start:html.index("\n}\n", start) + 3])
    code = "\n".join(functions) + "\n" + here.joinpath("dashboard_focus.js").read_text()
    code += "\nvar dashboardProfile={sections:['military']};updateFocusedDrawer(" + json.dumps({"military_flights": value}) + ");$('#drawerBody').renderedHTML;"
    return runtime["JavaScriptRuntime"]().evaluate(code)


@pytest.mark.parametrize("provider", ["adsb.lol", "opensky"])
@pytest.mark.parametrize("mode", ["live", "fresh-cache", "stale-cache"])
async def test_real_adapter_units_are_correct_only_on_focused_copy(tmp_path, provider, mode):
    adsb = {"ac": [{"lat": 50, "lon": 0, "hex": "ae1234", "flight": "RCH123", "alt_baro": 10000}]}
    states = {"states": [["ae1234", "RCH123", "United States", 0, 0, 0, 50, 10000, False, 100, 90, 0, None, 10000, "1234", False, 0]]}
    def response(request):
        if mode == "stale-cache":
            return httpx.Response(503)
        return httpx.Response(200, json=adsb if provider == "adsb.lol" else {"ac": []}) if "adsb.lol" in request.url.host else httpx.Response(200, json=states)
    cache = Cache(db_path=tmp_path / "aircraft.db")
    if mode != "live":
        ttl = 0 if mode == "stale-cache" else 300
        cache.set("military:adsblol:mil", adsb if provider == "adsb.lol" else {"ac": []}, ttl)
        if provider == "opensky":
            cache.set("military:flights:global", states, ttl)
    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    fetcher = Fetcher(cache, CircuitBreaker(), client=client, max_retries=0)
    try:
        raw = await military.fetch_military_flights(fetcher)
        original = copy.deepcopy(raw)
        assert raw["source"] == provider and raw["aircraft"][0]["altitude_m"] == 10000
        value = (await focused.snapshot(PROFILE, fetcher))["military_flights"]
        expected = 3048 if provider == "adsb.lol" else 10000
        assert value["aircraft"][0]["altitude_m"] == expected
        assert value["aircraft"][0]["altitude_unit"] == "m"
        assert raw == original and "altitude_unit" not in raw["aircraft"][0]
        assert display_aircraft(value) == value  # idempotent focused projection
        assert (await military.fetch_military_flights(fetcher))["aircraft"][0]["altitude_m"] == 10000
        if mode == "stale-cache":
            assert value["_coverage"]["state"] == "stale"
        body = rendered(value)
        assert '<th>Altitude (m)</th>' in body and '<td>' + str(expected) + '</td>' in body
    finally:
        await fetcher.close()
        await client.aclose()
        cache.close()


@pytest.mark.parametrize("provider,altitude,expected", [
    ("adsb.lol", 0, 0), ("opensky", 0, 0), ("adsb.lol", "ground", None),
    ("adsb.lol", None, None), ("adsb.lol", float("inf"), None),
    ("opensky", float("nan"), None), ("unknown", 10000, None), ("adsblol", 10000, None),
])
def test_ground_zero_nonfinite_and_unknown_provider_do_not_guess_metres(provider, altitude, expected):
    raw = {"source": provider, "aircraft": [{"altitude_m": altitude, "on_ground": altitude == "ground"}]}
    result = display_aircraft(raw)
    assert result is not raw and result["aircraft"][0] is not raw["aircraft"][0]
    assert result["aircraft"][0]["altitude_m"] == expected
    assert result["aircraft"][0]["altitude_unit"] == ("m" if expected is not None else "unknown")
    assert result["aircraft"][0]["on_ground"] == raw["aircraft"][0]["on_ground"]
