"""Duktape/Node executes the actual renderer and esc(), not a field-name grep."""
import json
from pathlib import Path
import runpy

import pytest


@pytest.mark.parametrize("altitude,fee,dominance", [(1234.5, 12, 48.75), (0, 0, 0), (None, None, None),
                                                   (float("inf"), float("nan"), -float("inf"))])
def test_controlled_fields_units_zero_and_untrusted_names(altitude, fee, dominance):
    here = Path(__file__).resolve().parent
    runtime = runpy.run_path(str(here / "test_dashboard_focus.py"))
    if not runtime["DUKTAPE"] and not runtime["NODE"]:
        pytest.skip("No local JS runtime")
    html = here.parent.joinpath("dashboard/index.html").read_text()
    names = ("rememberControlFocus", "alertControlKey", "updateHudStats", "updateAlertBanner", "activateHudAction", "updateDrawer", "updateFocusedDrawer", "profileData")
    functions = []
    for name in names:
        start = html.index("function " + name + "(")
        functions.append(html[start:html.index("\n}\n", start) + 3])
    # Use production esc after the shared legacy harness's simple test helper.
    start = html.index("function esc(")
    esc = html[start:html.index("\n", start)]
    coverage = {"state": "current", "attempted": 1, "current": 1, "failed": 0, "stale": 0, "unknown": 0}
    payload = {"macro_signals": {"_coverage": coverage, "signals": {
        "mempool_fees": {"fastest_fee": fee, "value": 777, "price": 888},
        "btc_dominance": {"btc_dominance_pct": dominance, "value": 999},
        '<img src=x onerror="bad()">': {"price": 456}}},
        "military_flights": {"_coverage": coverage, "aircraft": [
            {"callsign": '<script>bad()</script>', "origin_country": "XX", "altitude_m": altitude, "altitude": 99999}]}}
    script = "\n".join(functions) + "\n" + (here / "dashboard_focus.js").read_text() + "\nesc = (" + esc + ");"
    script += "\nvar dashboardProfile={sections:['markets','military']}; updateFocusedDrawer(" + json.dumps(payload) + "); $('#drawerBody').renderedHTML;"
    rendered = runtime["JavaScriptRuntime"]().evaluate(script)
    def display(value):
        import math
        return str(value) if value is not None and math.isfinite(value) else "—"
    assert f'<td>Mempool fastest fee</td><td>{display(fee)}</td><td>sat/vB</td>' in rendered
    assert f'<td>BTC dominance</td><td>{display(dominance)}</td><td>%</td>' in rendered
    assert '<th>Altitude (m)</th>' in rendered
    assert f'<td>&lt;script&gt;bad()&lt;/script&gt;</td><td>XX</td><td>{display(altitude)}</td>' in rendered
    assert '<img src=' not in rendered and '<script>' not in rendered
    assert 'Unsupported signal: &lt;img' in rendered
    for fallback in ('777', '888', '99999', '>999<'):
        assert fallback not in rendered


@pytest.mark.parametrize("state", ["current", "unavailable", "partial", "stale", "unknown"])
def test_critical_rows_and_counters_never_treat_unverified_empty_as_current(state):
    here = Path(__file__).resolve().parent
    runtime = runpy.run_path(str(here / "test_dashboard_focus.py"))
    if not runtime["DUKTAPE"] and not runtime["NODE"]:
        pytest.skip("No local JS engine")
    html = here.parent.joinpath("dashboard/index.html").read_text()
    functions = []
    for name in ("rememberControlFocus", "alertControlKey", "updateHudStats", "updateAlertBanner", "activateHudAction", "updateDrawer", "updateFocusedDrawer", "profileData", "_mergeStaticData", "updateAll"):
        start = html.index("function " + name + "(")
        functions.append(html[start:html.index("\n}\n", start) + 3])
    script = "\n".join(functions) + "\n" + (here / "dashboard_focus.js").read_text()
    script += """
var dashboardProfile={groups:['cyber'],sections:['cyber'],counters:['cyber'],group_sources:{cyber:['cyber_threats','cable_health']},sources:['cyber_threats','cable_health'],layers:['infra'],static_map:false,alerts:false};
var latestData=null, latestStaticData=null, INFRA_SNAPSHOT_KEYS=[], window={}, DASHBOARD_CACHE_KEY='test';
var _writeCacheEnvelope=function(){}, updateMapInfra=function(){}, updateTicker=function(){};
var console={warn:function(message){throw new Error(message);}};
elements['#feedCount']=new Element(); elements['#lastUpdate']=new Element();
"""
    coverage = {"state": state, "current": 1 if state == "current" else 0, "attempted": 1,
                "failed": int(state in ("unavailable", "partial")), "stale": int(state == "stale"), "unknown": int(state == "unknown")}
    data = {"cyber_threats": {"threats": [], "count": 0, "_coverage": coverage},
            "cable_health": {"corridors": {"example": {"status_label": "clear", "relevant_warnings": []}}, "_coverage": coverage}}
    script += "\nupdateAll(" + json.dumps(data) + "); JSON.stringify({body:$('#drawerBody').renderedHTML,hud:$('#hudStats').textContent,feeds:$('#feedCount').textContent});"
    rendered = json.loads(runtime["JavaScriptRuntime"]().evaluate(script))
    if state == "current":
        assert '>clear<' in rendered["body"] and '0 Threat indicators' in rendered["hud"]
        assert rendered["feeds"] == '2/2 feeds'
    else:
        assert '>clear<' not in rendered["body"]
        assert '— Threat indicators' in rendered["hud"]
        assert rendered["feeds"] == '0/2 feeds'
        assert 'request/cache coverage ' + state in rendered["body"]
    # The map's layer counter is also a metric: never show a verified zero for
    # failed/partial aircraft coverage, and clear any obsolete plotted markers.
    script += """
var dashboardProfile={groups:['military'],sections:['military'],counters:['military'],group_sources:{military:['military_flights']},sources:['military_flights'],layers:['military'],static_map:false,alerts:false};
elements['#cntMil']=new Element(); var cleared=false;
var mapLayers={military:{clearLayers:function(){cleared=true;}}};
var updateMapMilitary=function(value){elements['#cntMil'].textContent=String(value.aircraft.length);};
"""
    script += "updateAll(" + json.dumps({"military_flights": {"aircraft": [], "count": 0, "_coverage": coverage}}) + "); JSON.stringify({count:elements['#cntMil'].textContent,cleared:cleared});"
    result = json.loads(runtime["JavaScriptRuntime"]().evaluate(script))
    assert result == {"count": "0" if state == "current" else "?", "cleared": state != "current"}
