"""Focused profile: real validation/ASGI, source spies only; no upstream I/O."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from world_intel_mcp.dashboard import app as dashboard, focused, settings


EXAMPLE = Path(__file__).resolve().parents[3] / "dashboard.example.toml"


def test_example_and_public_metadata_are_explicitly_small(monkeypatch):
    value = settings.load(EXAMPLE)
    assert set(value.groups) == {"markets", "cyber", "military"}
    assert set(value.sources) == set(focused.REGISTRY)
    monkeypatch.setenv("FRED_API_KEY", "DUMMY_PRIVATE_SECRET")
    public = json.dumps(value.public())
    assert "DUMMY_PRIVATE_SECRET" not in public and str(EXAMPLE) not in public
    assert not value.public()["vector"] and not value.aois


@pytest.mark.parametrize("data", [
    {"version": True}, {"version": 2}, {"version": 1, "port": True},
    {"version": 1, "port": 0}, {"version": 1, "refresh_seconds": 29},
    {"version": 1, "refresh_seconds": 3601}, {"version": 1, "groups": ["weather"]},
    {"version": 1, "sources": ["https://example.test/"]},
    {"version": 1, "sources": ["webcams"]}, {"version": 1, "trust_env": True},
    {"version": 1, "groups": ["cyber"], "sources": ["market_quotes"]},
    {"version": 1, "map": {"center": [True, 0]}},
    {"version": 1, "map": {"center": [0, float("nan")]}},
    {"version": 1, "map": {"zoom": float("inf")}},
    {"version": 1, "map": {"center": [90, 0]}},
    {"version": 1, "map": {"static": 1}},
    {"version": 1, "ui": {"sections": ["markets", "markets"]}},
    {"version": 1, "alerts": {"categories": ["surge"]}},
    {"version": 1, "alerts": {"cable_score": 4}},
    {"version": 1, "alerts": {"cyber_critical": False}},
    *[{"version": 1, "services": {key: True}} for key in ("vector", "ollama", "history")],
    {"version": 1, "credentials": {"key": "DUMMY"}},
])
def test_invalid_types_ranges_unknown_keys_and_capabilities(data):
    with pytest.raises(settings.ConfigError):
        settings.parse(data)


def test_parse_failures_do_not_include_values_or_paths(tmp_path):
    path = tmp_path / "private-secret.toml"
    path.write_text('version = "DUMMY_PRIVATE_SECRET"')
    with pytest.raises(settings.ConfigError) as error:
        settings.load(path)
    assert "DUMMY_PRIVATE_SECRET" not in str(error.value) and str(path) not in str(error.value)
    path.write_text("x" * 65537)
    with pytest.raises(settings.ConfigError):
        settings.load(path)


@pytest.fixture
def profile(monkeypatch):
    value = settings.load(EXAMPLE)
    monkeypatch.setattr(dashboard, "_settings", value)
    monkeypatch.setattr(dashboard, "_ensure_fetcher", lambda: object())
    return value


@pytest.fixture
def source_calls(monkeypatch):
    calls = []
    def imported(name):
        assert name.startswith("world_intel_mcp.sources.")
        def function(attr):
            async def spy(fetcher):
                calls.append((name.rsplit(".", 1)[-1], attr))
                return {"timestamp": "2026-10-18", "source": "offline-spy"}
            return spy
        class Source:
            __getattr__ = lambda self, attr: function(attr)
        return Source()
    monkeypatch.setattr(focused, "import_module", imported)
    for name in ("fetch_alert_digest", "fetch_weekly_trends", "fetch_strategic_posture", "fetch_population_exposure", "fetch_situation_brief", "fetch_fleet_report"):
        monkeypatch.setattr(dashboard, name, lambda *a, **k: pytest.fail("Forbidden dashboard composite"))
    return calls


async def test_overview_and_stream_never_call_unselected_composites(profile, source_calls):
    result = await dashboard._fetch_overview()
    assert set(result) == set(profile.sources) | {"alert_digest", "timestamp"}
    expected = set(focused.REGISTRY.values())
    assert set(source_calls) == expected
    source_calls.clear()
    response = await dashboard.api_stream(None)
    stream = response.body_iterator
    try:
        for _ in range(len(profile.sources) + 1):
            event = await anext(stream)
        assert "focused-selected-results" in event
        assert set(source_calls) == expected
    finally:
        await stream.aclose()


async def test_group_and_source_filtering_is_before_execution(monkeypatch, source_calls):
    profile = settings.parse({"version": 1, "groups": ["markets"], "sources": ["forex_rates"]})
    result = await focused.snapshot(profile, object())
    assert source_calls == [("forex", "fetch_forex_rates")]
    assert set(result) == {"forex_rates", "timestamp", "alert_digest"}


async def test_concurrency_is_bounded_and_cancellation_closes_all_tasks(monkeypatch):
    active = 0
    peak = 0
    async def wait():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(100)
        finally:
            active -= 1
    monkeypatch.setattr(focused, "factories", lambda *a: {str(n): wait for n in range(12)})
    task = asyncio.create_task(focused.snapshot(settings.load(EXAMPLE), object()))
    await asyncio.sleep(0.02)
    assert peak == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


async def test_progressive_disconnect_cancels_owned_snapshot(monkeypatch):
    profile = settings.parse({"version": 1, "groups": ["cyber"], "sources": ["cyber_threats", "cable_health"]})
    active = 0
    async def fast():
        return {"threats": []}
    async def slow():
        nonlocal active
        active += 1
        try:
            await asyncio.sleep(100)
        finally:
            active -= 1
    monkeypatch.setattr(dashboard, "_settings", profile)
    monkeypatch.setattr(dashboard, "_ensure_fetcher", lambda: object())
    monkeypatch.setattr(focused, "factories", lambda *a: {"cyber_threats": fast, "cable_health": slow})
    response = await dashboard.api_stream(None)
    stream = response.body_iterator
    first = await anext(stream)
    assert '"_progressive": true' in first and '"cyber_threats"' in first
    assert active == 1
    await stream.aclose()
    assert active == 0


def test_alerts_only_use_selected_results_and_do_not_confirm_stale_data():
    profile = settings.load(EXAMPLE)
    results = {"cable_health": {"corridors": {"a": {"status_score": 3}}},
               "cyber_threats": {"by_severity": {"critical": 2}, "threats": []}}
    for value in results.values():
        value["_coverage"] = {"state": "current"}
    digest = focused.compose(profile, results)
    assert digest["alert_count"] == 2
    assert "estimated" in digest["alerts"][0]["message"]
    results["cable_health"]["_stale"] = True
    assert focused.compose(profile, results)["degraded"]
    assert "cable_health" in focused.compose(profile, results)["unavailable_sources"]


def test_profile_root_health_and_browser_isolation(profile, monkeypatch):
    monkeypatch.setenv("ACLED_PASSWORD", "DUMMY_SECRET")
    client = TestClient(dashboard.app, base_url="http://127.0.0.1:8501")
    public = client.get("/api/profile?config=/etc/passwd")
    assert public.status_code == 200 and public.json()["focused"]
    assert "DUMMY_SECRET" not in public.text and "/etc/passwd" not in public.text
    root = client.get("/")
    assert root.status_code == 200 and '"focused": true' in root.text
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/profile", headers={"Host": "outside.example"}).status_code == 400
    assert client.get("/api/profile", headers={"Origin": "https://outside.example"}).status_code == 403
    assert "access-control-allow-origin" not in public.headers


async def test_vector_is_not_imported_even_when_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "_settings", settings.load(EXAMPLE))
    monkeypatch.setattr(dashboard, "_fetcher", None)
    monkeypatch.setattr(dashboard, "_vector_store", None)
    monkeypatch.setattr(dashboard, "_cache", None)
    monkeypatch.setattr(dashboard, "_breaker", None)
    monkeypatch.setenv("WORLD_INTEL_CACHE_DB", str(tmp_path / "dashboard.db"))
    monkeypatch.setitem(sys.modules, "world_intel_mcp.vector_store", SimpleNamespace(VectorStore=lambda **k: pytest.fail("Vector initialized")))
    fetcher = dashboard._ensure_fetcher()
    assert dashboard._vector_store is None
    await fetcher.close()
    fetcher.cache.close()


def test_profile_specific_browser_payload_filter_and_cache_namespaces():
    import runpy
    runtime_module = runpy.run_path(str(Path(__file__).with_name("test_dashboard_focus.py")))
    if not runtime_module["DUKTAPE"] and not runtime_module["NODE"]:
        pytest.skip("No local JS engine")
    html = (Path(__file__).resolve().parents[1] / "dashboard/index.html").read_text()
    functions = []
    for name in ("profileData", "_mergeStaticData"):
        start = html.index("function " + name + "(")
        functions.append(html[start:html.index("\n}\n", start) + 3])
    profile = settings.parse({"version": 1, "groups": ["cyber"], "sources": ["cyber_threats"], "map": {"static": False}, "alerts": {"categories": []}})
    code = "var dashboardProfile=" + json.dumps(profile.public()) + "; var latestStaticData=null; var INFRA_SNAPSHOT_KEYS=[];\n" + "\n".join(functions)
    code += "\nvar selected=_mergeStaticData({cyber_threats:{threats:[]},earthquakes:{},displacement:{},alert_digest:{alerts:[{}]},timestamp:'test'}); JSON.stringify(Object.keys(selected).sort());"
    output = runtime_module["JavaScriptRuntime"]().evaluate(code)
    assert json.loads(output) == ["cyber_threats", "timestamp"]
    assert "DASHBOARD_CACHE_KEY += '-focused-v2-' + dashboardProfile.cache_key" in html
    assert "if (!dashboardProfile || dashboardProfile.aois) fetch('/api/aois')" in html
    assert "if (dashboardProfile && !dashboardProfile.vector) return;" in html


def test_focused_browser_renderer_skips_disabled_map_callbacks_and_old_data():
    import runpy
    runtime = runpy.run_path(str(Path(__file__).with_name("test_dashboard_focus.py")))
    if not runtime["DUKTAPE"] and not runtime["NODE"]:
        pytest.skip("No local JS engine")
    html = (Path(__file__).resolve().parents[1] / "dashboard/index.html").read_text()
    functions = []
    for name in ("rememberControlFocus", "alertControlKey", "updateHudStats", "updateAlertBanner", "activateHudAction",
                 "updateDrawer", "updateFocusedDrawer", "profileData", "_mergeStaticData", "updateAll"):
        start = html.index("function " + name + "(")
        functions.append(html[start:html.index("\n}\n", start) + 3])
    harness = Path(__file__).with_name("dashboard_focus.js").read_text()
    profile = settings.parse({"version": 1, "groups": ["markets", "cyber"],
                              "sources": ["market_quotes", "forex_rates", "cyber_threats"],
                              "map": {"static": False}, "ui": {"counters": ["cyber"]}, "alerts": {"categories": []}})
    code = "\n".join(functions) + "\n" + harness + "\nvar dashboardProfile=" + json.dumps(profile.public()) + ";\n"
    code += """
var latestStaticData=null, latestData=null, INFRA_SNAPSHOT_KEYS=[], window={};
var DASHBOARD_CACHE_KEY='profile-test', savedPayload;
var _writeCacheEnvelope=function(key,value){savedPayload=value;};
var updateTicker=function(){};
var console={warn:function(message){throw new Error(message);}};
elements['#feedCount']=new Element(); elements['#lastUpdate']=new Element();
var current={state:'current',attempted:1,current:1,stale:0,failed:0,unknown:0};
updateAll({market_quotes:{quotes:[{symbol:'SPY',price:123}],_coverage:current},forex_rates:{rates:{EUR:0.9},_coverage:current},cyber_threats:{threats:[],_coverage:current},earthquakes:{},displacement:{},alert_digest:{alerts:[{}]}});
check($('#drawerBody').renderedHTML.indexOf('123')>=0,'focused price table must be rendered');
check($('#drawerBody').renderedHTML.indexOf('0.9')>=0,'focused FX table must be rendered');
check(!('earthquakes' in savedPayload) && !('displacement' in savedPayload),'old disabled sources must not be saved');
check(!('alert_digest' in savedPayload),'disabled alerts must not be restored');
check(!$('#alertBanner').classList.contains('show'),'disabled alerts must not display an unavailable warning');
check($('#feedCount').textContent==='3/3 feeds','count only selected source slots');
check(!hud.children.some(function(pill){return pill.textContent.indexOf('SPY')>=0;}),'counter selection must not leak market pills');
checks;
"""
    assert int(runtime["JavaScriptRuntime"]().evaluate(code)) >= 30


async def test_vector_endpoint_data_is_disabled(profile):
    assert await dashboard._fetch_vector_analytics() == {"available": False}
