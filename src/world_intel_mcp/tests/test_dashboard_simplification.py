"""Offline structural/fetch-planning checks, plus optional Node interaction tests.

Run without project dependencies:
python3 -m unittest discover -s src/world_intel_mcp/tests -p test_dashboard_simplification.py -v
Static geometry checks are not browser rendering/hit-testing.
"""

import ast
import asyncio
from pathlib import Path
import re
import runpy
import shutil
import subprocess
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "src/world_intel_mcp/dashboard/index.html"
APP = ROOT / "src/world_intel_mcp/dashboard/app.py"
RETIRED = {"webcams", "migration", "signal_convergence"}


class DashboardSimplificationTests(unittest.TestCase):
    def test_automatic_overview_and_stream_factories_call_only_retained_feeds(self):
        tree = ast.parse(APP.read_text())
        for name in ("_fetch_overview", "_stream_source_defs"):
            fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
            if name == "_fetch_overview":
                expr = next(n.value for n in fn.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "coros" for t in n.targets))
            else:
                expr = next(n.value for n in fn.body if isinstance(n, ast.Return))
            calls = []
            def factory(label):
                async def spy(*args, **kwargs):
                    calls.append(label)
                    return {"source": label}
                return spy
            namespace = {"fetcher": object()}
            for call in ast.walk(expr):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute):
                    module = call.func.value.id
                    namespace.setdefault(module, SimpleNamespace())
                    setattr(namespace[module], call.func.attr, factory(call.func.attr))
                elif isinstance(call.func, ast.Name):
                    namespace[call.func.id] = factory(call.func.id)
            definitions = eval(compile(ast.Expression(expr), str(APP), "eval"), namespace)
            self.assertEqual(len(definitions), 45)  # README's automatic source slots
            self.assertFalse(RETIRED & definitions.keys())
            self.assertTrue({"population_exposure", "displacement", "cyber_threats"} <= definitions.keys())
            async def exercise():
                return await asyncio.gather(*(value() if callable(value) else value for value in definitions.values()))
            results = asyncio.run(exercise())
            self.assertEqual(len(results), len(definitions))
            self.assertFalse({"fetch_webcams", "fetch_migration_flows", "fetch_signal_convergence"} & set(calls))
            self.assertIn("fetch_population_exposure", calls)

    def test_retired_ui_and_render_paths_cannot_be_resurrected_by_old_cache(self):
        html = INDEX.read_text()
        for key in RETIRED:
            self.assertNotIn(f'data-layer="{key}"', html)
            self.assertNotIn("mapLayers." + key, html)
            self.assertNotIn("data." + key, html)
        self.assertNotIn("fetch('/api/migration')", html)
        self.assertNotRegex(html, r"updateMap(Webcams|Migration|Convergence)|_webcamRefresh|migLegend|cntConv")
        self.assertIn("['webcams', 'migration', 'signal_convergence'].forEach(function(key) { delete merged[key]; })", html)
        self.assertIn("if (layerState.hasOwnProperty(_k))", html)
        self.assertIn('data-layer="exposure"', html)

    def test_manual_migration_route_and_mcp_definitions_remain(self):
        tree = ast.parse(APP.read_text())
        self.assertTrue(any(isinstance(n, ast.AsyncFunctionDef) and n.name == "api_migration" for n in tree.body))
        routes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Route"]
        self.assertTrue(any(len(n.args) >= 2 and isinstance(n.args[0], ast.Constant) and n.args[0].value == "/api/migration" and n.args[1].id == "api_migration" for n in routes))
        tools = "\n".join(p.read_text() for p in (ROOT / "src/world_intel_mcp/tools").glob("*.py"))
        for function in ("fetch_webcams", "fetch_migration_flows", "fetch_signal_convergence"):
            self.assertIn(function, tools)

    def test_panel_width_and_scroll_contract_at_supported_viewports(self):
        html = INDEX.read_text()
        assets = runpy.run_path(str(Path(__file__).with_name("test_dashboard_assets.py")))
        dom = assets["_Structure"]()
        dom.feed(html)
        self.assertEqual(dom.elements["vectorPanel"][0], ("html", "body", "aside", "div"))
        self.assertEqual(dom.elements["drawerBody"][0], ("html", "body", "aside", "div"))
        self.assertEqual(dom.elements["map-attribution"][0], ("html", "body"))
        css = re.sub(r"/\*.*?\*/", "", re.search(r"<style>(.*?)</style>", html, re.S).group(1), flags=re.S)
        rules = [(s.strip(), dict(part.strip().split(":", 1) for part in b.split(";") if ":" in part)) for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
        def base(selector):
            return {k: v.strip() for s, body in rules if s == selector for k, v in body.items()}
        self.assertEqual(base("#vectorPanel")["position"], "relative")
        self.assertEqual(base("#vectorPanel")["width"], "100%")
        self.assertEqual(base(".drawer-inner")["overflow"], "auto")
        self.assertEqual(base(".dtable")["table-layout"], "fixed")
        self.assertEqual(base(".dtable")["overflow-wrap"], "anywhere")
        self.assertEqual(base(".hud-stats")["overflow-y"], "auto")
        self.assertFalse(any(s == ".hud-stats" and b.get("display", "").strip() == "none" for s, b in rules))
        self.assertIn("new ResizeObserver(fitHeader).observe($('#topBar'))", html)
        self.assertIn("getBoundingClientRect().bottom + 10", html)
        self.assertEqual(base("#layers.layers-open")["max-height"], "calc(100dvh - var(--layers-top, 66px) - 66px)")
        # Evaluate the authored min() constraints, not arbitrary test-only sizes.
        def bounded_width(selector, viewport):
            value = next(b["width"].strip() for s, b in rules if s == selector)
            match = re.fullmatch(r"min\((\d+)px, calc\(100vw - (\d+)px\)\)", value)
            self.assertIsNotNone(match)
            return min(int(match[1]), viewport - int(match[2]))
        for viewport in (375, 768, 1024, 1280, 1440):
            layers = bounded_width("#layers", viewport)
            drawer = viewport if viewport <= 900 else bounded_width("#drawer", viewport)
            self.assertLessEqual(layers + 20, viewport)
            self.assertLessEqual(drawer, viewport)
            if viewport > 900:
                self.assertGreater(viewport - layers - drawer - 30, 400)  # map still has room
        self.assertIn("width: min(400px, 100vw)", html)
        self.assertEqual(base("#detail")["width"], "100%")  # mobile override

    def test_hud_and_alert_actions_are_semantic_text_only_and_have_feedback(self):
        html = INDEX.read_text()
        self.assertNotIn("HUD_LAYER_MAP", html)
        self.assertIn("document.createElement(action ? 'button' : 'span')", html)
        self.assertIn("el.type = 'button'", html)
        self.assertIn("button[data-hud-action]", html)
        self.assertIn("'drawer', 'cyber'", html)
        self.assertIn('data-section="cyber"', html)
        self.assertIn("heading.scrollIntoView", html)
        self.assertIn("layer.eachLayer(include)", html)
        self.assertIn("No plotted data", html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn("'UNHCR '", html)
        self.assertIn("not a live displaced-person count", html)
        self.assertIn("not actual victims", html)
        self.assertIn("button.addEventListener('click', function() { showAlertDetail(index); })", html)
        detail = html[html.index("function showAlertDetail("):html.index("// ════════════ TOP BAR STATS")]
        self.assertNotIn("innerHTML", detail)
        self.assertNotIn("fetch(", detail)
        self.assertIn("alert.corridors.forEach", detail)
        self.assertIn("info.relevant_warnings", detail)
        self.assertIn("s.baseline", detail)
        self.assertIn("first 3 records", detail)
        self.assertIn("not confirmed cable damage", detail)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; JS runtime/syntax and DOM-stub checks not executed")
    def test_javascript_runtime_interactions_without_network(self):
        subprocess.run([shutil.which("node"), str(Path(__file__).with_name("dashboard_interactions.cjs")), str(INDEX)],
                       check=True, timeout=30, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
