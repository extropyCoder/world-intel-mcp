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
