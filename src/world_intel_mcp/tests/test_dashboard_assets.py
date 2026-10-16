"""Offline checks for the reviewed external browser asset policy.

The audit separately downloaded/hashed the public bytes. Tests deliberately
make no CDN, registry, advisory, font or map-tile requests.
"""

import base64
import json
import re
from html.parser import HTMLParser
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_HTML = _ROOT / "src/world_intel_mcp/dashboard/index.html"
_MANIFEST = _ROOT / "docs/dashboard-assets.json"


def test_map_uses_standard_keyless_osm_tiles_with_visible_attribution():
    html = _HTML.read_text()
    layers = re.findall(r"L\.tileLayer\('([^']+)',\s*\{(.*?)\}\)\.addTo\(map\)", html, re.S)
    assert len(layers) == 1
    url, options = layers[0]
    assert url == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert re.search(r"maxNativeZoom:\s*19\b", options)
    assert re.search(r"maxZoom:\s*19\b", options)
    # One static body-level copyright link replaces Leaflet's map-bound control.
    assert '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' in html
    assert html.count('href="https://www.openstreetmap.org/copyright"') == 1
    assert "attributionControl: false" in html
    assert "attribution:" not in options
    assert "carto" not in html.lower()
    assert "{r}" not in url and "{s}" not in url and "?" not in url
    # Ordinary Leaflet loading only: no offline/prefetch plugin, cache-busting
    # query or retina URL, and no CSS hiding the attribution control.
    assert not re.search(r"prefetch|tileLayer\.offline|serviceWorker\.register", html, re.I)
    for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", html):
        if "map-attribution" in selector:
            assert not re.search(r"display:\s*none|visibility:\s*hidden|opacity:\s*0\b|font-size:\s*0\b", declarations)


class _Structure(HTMLParser):
    """Record real HTML ancestry, not script strings mentioning element IDs."""
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            assert attrs["id"] not in self.elements
            self.elements[attrs["id"]] = (tuple(self.stack), attrs)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]


def test_attribution_is_outside_map_and_above_drawer_in_both_states():
    """Static layout contract only; not a rendered-browser overlap/hit-test."""
    html = _HTML.read_text()
    dom = _Structure()
    dom.feed(html)
    assert dom.elements["map-attribution"][0] == ("html", "body")
    assert "style" not in dom.elements["map-attribution"][1]
    css = re.sub(r"/\*.*?\*/", "", re.search(r"<style>(.*?)</style>", html, re.S).group(1), flags=re.S)
    rules = [(selector.strip(), dict(part.strip().split(":", 1) for part in body.split(";") if ":" in part))
             for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

    def declarations(selector):
        matches = [body for name, body in rules if name == selector]
        assert matches, f"Missing selector {selector}"
        return {key: value.strip() for body in matches for key, value in body.items()}

    attribution = {key: value.strip() for key, value in declarations("#map-attribution").items()}
    assert attribution["position"] == "fixed"
    assert attribution["pointer-events"] == "auto"
    assert attribution["max-width"] == "100%"
    assert attribution["overflow-wrap"] == "anywhere"
    assert attribution["left"] == "0"
    assert attribution["background"] == "#fff"
    assert not {"transform", "opacity", "visibility", "display", "filter"} & attribution.keys()
    assert "underline" in declarations("#map-attribution a")["text-decoration"]
    # No responsive/state selector may move, hide or restack the attribution.
    selectors = [selector for selector, _ in rules]
    assert [s for s in selectors if "#map-attribution" in s] == ["#map-attribution", "#map-attribution a"]
    for element in ("map", "drawer", "ticker", "detail"):
        assert dom.elements[element][0] == ("html", "body")
        assert int(attribution["z-index"]) > int(declarations("#" + element)["z-index"])
    ticker_height = int(declarations("#ticker")["height"].strip().removesuffix("px"))
    offset = int(attribution["bottom"].removesuffix("px"))
    assert offset > ticker_height
    # The mobile drawer spans the width from bottom:34px through 45vh. Prove
    # that it geometrically intersects the attribution band when open, but
    # cannot win painting/hit-testing by z-index. Closing it cannot lower the
    # separate body's attribution. This is not a browser layout engine.
    assert declarations("#drawer")["height"] == "45vh"
    assert declarations("#drawer")["bottom"] == "34px"
    assert declarations("#drawer.closed")["transform"] == "translateY(100%)"
    assert not any("z-index" in body for name, body in rules if name == "#drawer.closed")
    for width, height in ((320, 568), (375, 667), (768, 1024), (1440, 900)):
        assert 0 <= offset < height
        assert width > 2 * 6  # padding fits; text may wrap within max-width
        assert attribution["font"] == "12px/18px sans-serif"
        if width <= 900:
            for drawer_open in (False, True):
                drawer_bottom = 34 if drawer_open else 34 - 0.45 * height
                drawer_top = drawer_bottom + 0.45 * height
                intersects = drawer_bottom < offset + 22 and drawer_top > offset
                assert intersects == drawer_open
                assert int(attribution["z-index"]) > int(declarations("#drawer")["z-index"])


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.assets.append(("script", attrs["src"], attrs))
        if tag == "link" and "stylesheet" in attrs.get("rel", "").split():
            self.assets.append(("style", attrs["href"], attrs))


def test_external_assets_are_exact_reviewed_versions_with_sri():
    manifest = json.loads(_MANIFEST.read_text())
    approved = {item["url"]: item for item in manifest["assets"]}
    assert len(approved) == 5
    assert set(approved) == {
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
        "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
        "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css",
        "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js",
        "https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js",
    }
    exceptions = {item["url"] for item in manifest["unhashed_stylesheets"]}
    assert exceptions == {
        "https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@400;500;600;700&family=Share+Tech+Mono&display=swap"
    }
    parser = _Assets()
    parser.feed(_HTML.read_text())
    seen = set()
    for kind, url, attrs in parser.assets:
        assert url not in seen
        seen.add(url)
        assert attrs.get("crossorigin") == "anonymous"
        if url in exceptions:
            assert kind == "style" and "integrity" not in attrs
            continue
        assert url in approved, f"Unreviewed external asset: {url}"
        assert re.search(r"@\d+\.\d+\.\d+/", url), "Floating asset version"
        assert attrs.get("integrity") == approved[url]["integrity"]
        algorithm, digest = attrs["integrity"].split("-", 1)
        assert algorithm == "sha384"
        assert len(base64.b64decode(digest, validate=True)) == 48
        assert approved[url]["bytes"] > 0
    assert seen == set(approved) | exceptions


def test_renderer_still_uses_dompurify_without_an_unsafe_fallback():
    html = _HTML.read_text()
    assert "DOMPurify.sanitize(h," in html
    assert "dompurify@3/" not in html
    assert "dompurify@latest" not in html
