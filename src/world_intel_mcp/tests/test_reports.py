"""Tests for the PDF/HTML intelligence report generator."""

import sys
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from world_intel_mcp.reports import (
    _build_html,
    _esc,
    _fmt_num,
    _change_class,
    _render_markets,
    _render_earthquakes,
    _render_conflicts,
    _render_news_clusters,
    _render_alerts,
    _render_posture,
    _render_infrastructure,
    _render_cyber,
    _render_health,
    _render_maritime,
    _render_situation_brief,
    _render_key_value,
    _collect_report_data,
    _safe_fetch,
    _ReportResourceBlocker,
    pdf_dependencies_available,
    generate_report,
)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestEsc:
    def test_none(self):
        assert _esc(None) == ""

    def test_html_chars(self):
        assert (
            _esc('<script>"alert&"</script>')
            == "&lt;script&gt;&quot;alert&amp;&quot;&lt;/script&gt;"
        )

    def test_plain(self):
        assert _esc("hello world") == "hello world"

    def test_number(self):
        assert _esc(42) == "42"


class TestFmtNum:
    def test_none(self):
        assert _fmt_num(None) == "N/A"

    def test_integer(self):
        assert _fmt_num(1234567, 0) == "1,234,567"

    def test_float(self):
        assert _fmt_num(1234.567, 2) == "1,234.57"

    def test_string(self):
        assert _fmt_num("not a number") == "not a number"


class TestChangeClass:
    def test_positive(self):
        assert _change_class(1.5) == "green"

    def test_negative(self):
        assert _change_class(-0.5) == "red"

    def test_zero(self):
        assert _change_class(0) == ""

    def test_none(self):
        assert _change_class(None) == ""


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


class TestRenderMarkets:
    def test_no_data(self):
        assert "unavailable" in _render_markets({})

    def test_with_quotes(self):
        data = {
            "quotes": [
                {
                    "shortName": "S&P 500",
                    "regularMarketPrice": 4500.12,
                    "regularMarketChangePercent": 1.23,
                },
                {
                    "symbol": "DJI",
                    "regularMarketPrice": 35000.0,
                    "regularMarketChangePercent": -0.45,
                },
            ]
        }
        html = _render_markets(data)
        assert "S&amp;P 500" in html
        assert "4,500.12" in html
        assert "+1.23%" in html
        assert "green" in html
        assert "red" in html


class TestRenderEarthquakes:
    def test_no_data(self):
        assert "No significant" in _render_earthquakes({})

    def test_with_quakes(self):
        data = {
            "earthquakes": [
                {
                    "properties": {
                        "mag": 5.2,
                        "place": "Near Tokyo",
                        "time": 1700000000000,
                    }
                },
            ]
        }
        html = _render_earthquakes(data)
        assert "5.2" in html
        assert "Near Tokyo" in html


class TestRenderConflicts:
    def test_no_data(self):
        assert "No recent" in _render_conflicts({})

    def test_with_events(self):
        data = {
            "events": [
                {
                    "event_type": "Battle",
                    "country": "Ukraine",
                    "fatalities": 5,
                    "event_date": "2025-01-01",
                    "notes": "Test event",
                },
            ]
        }
        html = _render_conflicts(data)
        assert "Battle" in html
        assert "Ukraine" in html


class TestRenderNewsClusters:
    def test_no_data(self):
        assert "No news clusters" in _render_news_clusters({})

    def test_with_clusters(self):
        data = {"clusters": [{"label": "Climate Summit", "article_count": 42}]}
        html = _render_news_clusters(data)
        assert "Climate Summit" in html
        assert "42" in html


class TestRenderAlerts:
    def test_no_data(self):
        assert "No active alerts" in _render_alerts({})

    def test_with_alerts(self):
        data = {
            "alerts": [
                {
                    "severity": "critical",
                    "title": "Major Quake",
                    "detail": "M7.2 event detected",
                },
                {
                    "severity": "info",
                    "title": "Minor Issue",
                    "description": "Low impact",
                },
            ]
        }
        html = _render_alerts(data)
        assert "critical" in html
        assert "Major Quake" in html


class TestRenderPosture:
    def test_no_data(self):
        assert "unavailable" in _render_posture({})

    def test_with_data(self):
        data = {
            "overall_assessment": "Tensions elevated in multiple regions.",
            "threat_level": "HIGH",
            "regions": {"Europe": "NATO exercises ongoing"},
        }
        html = _render_posture(data)
        assert "HIGH" in html
        assert "red" in html
        assert "Europe" in html


class TestRenderInfrastructure:
    def test_no_data(self):
        assert "No infrastructure" in _render_infrastructure({})

    def test_with_outages(self):
        data = {"outages": [{"entity": "AS12345", "score": 85, "source": "IODA"}]}
        html = _render_infrastructure(data)
        assert "AS12345" in html


class TestRenderCyber:
    def test_no_data(self):
        assert "No recent cyber" in _render_cyber({})

    def test_with_threats(self):
        data = {
            "recent_threats": [
                {"name": "Emotet", "type": "malware", "url": "https://example.com"}
            ]
        }
        html = _render_cyber(data)
        assert "Emotet" in html


class TestRenderHealth:
    def test_no_data(self):
        assert "No active disease" in _render_health({})

    def test_with_outbreaks(self):
        data = {
            "outbreaks": [{"disease": "Mpox", "country": "DRC", "date": "2025-01-15"}]
        }
        html = _render_health(data)
        assert "Mpox" in html


class TestRenderMaritime:
    def test_no_data(self):
        assert "No maritime" in _render_maritime({})

    def test_with_vessels(self):
        data = {"vessels": [{"name": "USS Nimitz", "type": "CVN", "flag": "US"}]}
        html = _render_maritime(data)
        assert "USS Nimitz" in html


class TestRenderSituationBrief:
    def test_no_data(self):
        assert "unavailable" in _render_situation_brief({})

    def test_with_brief(self):
        data = {
            "brief": "Global tensions remain elevated with multiple hotspots active."
        }
        html = _render_situation_brief(data)
        assert "Global tensions" in html


class TestRenderKeyValue:
    def test_no_data(self):
        assert "unavailable" in _render_key_value({})

    def test_with_data(self):
        html = _render_key_value(
            {"metric1": 42, "metric2": "active", "source": "should_skip"}
        )
        assert "metric1" in html
        assert "42" in html
        assert "source" not in html  # filtered out


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------


class TestBuildHtml:
    def test_basic_structure(self):
        data = {"markets": {"quotes": []}, "world_brief": {"brief": "Test brief"}}
        html = _build_html(data)
        assert "<!DOCTYPE html>" in html
        assert "World Intelligence Report" in html
        assert "Executive Summary" in html
        assert "Financial Markets" in html

    def test_custom_title(self):
        html = _build_html({}, title="Custom Report")
        assert "Custom Report" in html

    def test_all_sections(self):
        data = {
            "world_brief": {"brief": "test"},
            "strategic_posture": {"overall_assessment": "test", "threat_level": "LOW"},
            "alerts": {"alerts": []},
            "markets": {"quotes": []},
            "economic": {"key": "val"},
            "conflicts": {"events": []},
            "military": {"data": "val"},
            "earthquakes": {"earthquakes": []},
            "infrastructure": {"outages": []},
            "cyber": {"recent_threats": []},
            "maritime": {"vessels": []},
            "health": {"outbreaks": []},
            "nuclear": {"sites": []},
            "climate": {"zones": {}},
            "news": {"clusters": []},
            "shipping": {"quotes": []},
            "service_status": {"services": []},
        }
        html = _build_html(data)
        assert "Executive Summary" in html
        assert "Strategic Posture" in html
        assert "Financial Markets" in html
        assert "Cyber Threats" in html


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


class TestSafeFetch:
    @pytest.mark.asyncio
    async def test_success(self):
        async def good_fn():
            return {"result": "ok"}

        result = await _safe_fetch("test", good_fn)
        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_failure(self):
        async def bad_fn():
            raise ValueError("boom")

        result = await _safe_fetch("test", bad_fn)
        assert "error" in result
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_non_dict_result(self):
        async def list_fn():
            return [1, 2, 3]

        result = await _safe_fetch("test", list_fn)
        assert result == {"data": [1, 2, 3]}


class TestCollectReportData:
    @pytest.mark.asyncio
    async def test_section_filter(self):
        with patch("world_intel_mcp.reports.markets") as mock_markets:
            mock_markets.fetch_market_quotes = AsyncMock(return_value={"quotes": []})

            data = await _collect_report_data(
                MagicMock(),
                sections=["markets"],
            )
            assert "markets" in data
            assert "earthquakes" not in data

    @pytest.mark.asyncio
    async def test_handles_failures(self):
        with patch("world_intel_mcp.reports.markets") as mock_markets:
            mock_markets.fetch_market_quotes = AsyncMock(
                side_effect=RuntimeError("api down")
            )

            data = await _collect_report_data(MagicMock(), sections=["markets"])
            assert "error" in data["markets"]


# ---------------------------------------------------------------------------
# PDF dependency check
# ---------------------------------------------------------------------------


class TestPdfDependencies:
    def test_check(self):
        # Just verify it returns a bool without crashing
        result = pdf_dependencies_available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Report generation (HTML mode — no weasyprint needed)
# ---------------------------------------------------------------------------


class TestGenerateReport:
    @pytest.mark.asyncio
    async def test_html_output(self, tmp_path):
        output = tmp_path / "test_report.html"

        with patch("world_intel_mcp.reports._collect_report_data") as mock_collect:
            mock_collect.return_value = {
                "markets": {
                    "quotes": [
                        {
                            "shortName": "SPX",
                            "regularMarketPrice": 4500,
                            "regularMarketChangePercent": 0.5,
                        }
                    ]
                },
            }

            fetcher = MagicMock()
            result = await generate_report(fetcher, output_path=output, fmt="html")

        assert result["format"] == "html"
        assert result["path"] == str(output)
        assert result["size_bytes"] > 0
        assert "markets" in result["sections_included"]
        assert output.exists()

        content = output.read_text()
        assert "<!DOCTYPE html>" in content
        assert "SPX" in content

    @pytest.mark.asyncio
    async def test_default_output_path(self, tmp_path):
        with patch("world_intel_mcp.reports._collect_report_data") as mock_collect:
            mock_collect.return_value = {"markets": {"quotes": []}}

            with patch("world_intel_mcp.reports.Path.home", return_value=tmp_path):
                fetcher = MagicMock()
                result = await generate_report(fetcher, fmt="html")

        assert result["format"] == "html"
        assert "report-" in result["path"]

    @pytest.mark.asyncio
    async def test_sections_tracking(self, tmp_path):
        output = tmp_path / "test.html"

        with patch("world_intel_mcp.reports._collect_report_data") as mock_collect:
            mock_collect.return_value = {
                "markets": {"quotes": []},
                "earthquakes": {"error": "api down"},
            }

            result = await generate_report(MagicMock(), output_path=output, fmt="html")

        assert "markets" in result["sections_included"]
        assert "earthquakes" in result["sections_failed"]

    @pytest.mark.asyncio
    async def test_pdf_without_weasyprint(self, tmp_path):
        output = tmp_path / "test.pdf"

        with patch(
            "world_intel_mcp.reports.pdf_dependencies_available", return_value=False
        ):
            with patch("world_intel_mcp.reports._collect_report_data") as mock_collect:
                mock_collect.return_value = {"markets": {"quotes": []}}

                result = await generate_report(
                    MagicMock(), output_path=output, fmt="pdf"
                )

        assert "error" in result
        assert "WeasyPrint" in result["error"]

    @pytest.mark.asyncio
    async def test_custom_title(self, tmp_path):
        output = tmp_path / "custom.html"

        with patch("world_intel_mcp.reports._collect_report_data") as mock_collect:
            mock_collect.return_value = {}

            result = await generate_report(
                MagicMock(), output_path=output, title="Daily Brief", fmt="html"
            )

        content = output.read_text()
        assert "Daily Brief" in content


# ---------------------------------------------------------------------------
# Untrusted feed values and the PDF resource boundary
# ---------------------------------------------------------------------------

_PAYLOAD = '<img src=x>&"\''


def _nested_field(path, value):
    for key in reversed(path):
        value = [value] if key == 0 else {key: value}
    return value


_DYNAMIC_FIELDS = [
    (_render_markets, ("quotes", 0, field)) for field in (
        "shortName", "symbol", "regularMarketPrice", "regularMarketChangePercent")
] + [
    (_render_earthquakes, ("earthquakes", 0, "properties", field)) for field in ("mag", "place")
] + [
    (_render_conflicts, ("events", 0, field)) for field in (
        "event_type", "country", "fatalities", "event_date", "notes")
] + [
    (_render_news_clusters, ("clusters", 0, field)) for field in (
        "label", "title", "article_count", "count")
] + [
    (_render_alerts, ("alerts", 0, field)) for field in ("title", "type", "detail", "description")
] + [
    (_render_posture, (field,)) for field in (
        "overall_assessment", "summary", "threat_level", "risk_level")
] + [
    (_render_posture, ("regions", "Region")),
    (_render_posture, ("regional_assessments", "Region", "summary")),
] + [
    (_render_infrastructure, ("outages", 0, field)) for field in (
        "entity", "name", "score", "severity", "source")
] + [
    (_render_cyber, ("threats", 0, field)) for field in ("name", "tag", "type", "url")
] + [
    (_render_health, ("outbreaks", 0, field)) for field in ("disease", "title", "country", "date")
] + [
    (_render_maritime, ("vessels", 0, field)) for field in (
        "name", "vessel_name", "type", "ship_type", "flag")
] + [
    (_render_situation_brief, (field,)) for field in ("brief", "summary")
] + [(_render_key_value, ("metric",))]


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


@pytest.mark.parametrize("renderer,path", _DYNAMIC_FIELDS)
def test_dynamic_fields_are_escaped_once(renderer, path):
    data = _nested_field(path, _PAYLOAD)
    if renderer is _render_posture and not ({"overall_assessment", "summary"} & data.keys()):
        data["overall_assessment"] = "Assessment"
    html = renderer(data)
    assert _esc(_PAYLOAD) in html
    assert "&amp;lt;img" not in html
    assert "img" not in _Elements(html).tags


def test_dynamic_keys_and_severity_cannot_create_markup():
    for html in (
        _render_key_value({_PAYLOAD: _PAYLOAD}),
        _render_posture({"summary": "Summary", "regions": {_PAYLOAD: "Detail"}}),
    ):
        assert _esc(_PAYLOAD) in html
        assert "img" not in _Elements(html).tags
    html = _render_alerts({"alerts": [{"severity": _PAYLOAD, "title": "Alert"}]})
    assert _PAYLOAD not in html
    assert 'class="alert-box "' in html
    assert "img" not in _Elements(html).tags


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                  "NaN", "Infinity", "1e9999", 10 ** 1000])
def test_nonfinite_or_overflowing_numbers_are_unavailable(value):
    assert _fmt_num(value) == "N/A"
    assert _fmt_num(value, 0) == "N/A"
    assert _change_class(value) == ""
    html = _render_markets({"quotes": [{"regularMarketPrice": value,
                                       "regularMarketChangePercent": value}]})
    assert html.count("N/A") == 2


@pytest.mark.parametrize("value", [_PAYLOAD, "bad", {}, [], None,
                                  float("nan"), float("inf"), 10 ** 1000, -1e300])
def test_invalid_epoch_does_not_crash_or_interpolate(value):
    html = _render_earthquakes({"earthquakes": [{"properties": {"time": value}}]})
    assert "img" not in _Elements(html).tags
    assert "N/A" in html or value is None


@pytest.mark.parametrize("value", [_PAYLOAD, {"value": _PAYLOAD}, [_PAYLOAD]])
def test_invalid_numeric_fallback_is_html_safe(value):
    assert _fmt_num(value) == _esc(value)
    assert _change_class(value) == ""
    assert "img" not in _Elements(_fmt_num(value)).tags


def test_truncation_precedes_escaping():
    url = "x" * 59 + "<&"
    html = _render_cyber({"threats": [{"url": url}]})
    assert "x" * 59 + "&lt;</td>" in html


@pytest.mark.parametrize("payload", [
    '<img src="http://example.invalid/image">',
    '<link rel="stylesheet" href="file:///not-a-real-report-resource.css">',
    '<style>@import "https://example.invalid/style";</style>',
    '<a rel="attachment" href="data:text/plain,blocked">attachment</a>',
    '<script>alert("not markup")</script>',
])
async def test_generated_html_escapes_title_and_vulnerable_fields(tmp_path, payload):
    output = tmp_path / "safe.html"
    data = {
        "markets": {"quotes": [{"regularMarketPrice": payload, "regularMarketChangePercent": payload}]},
        "conflicts": {"events": [{"fatalities": payload}]},
        "news": {"clusters": [{"article_count": payload}, {"count": payload}]},
        "infrastructure": {"outages": [{"score": payload}, {"severity": payload}]},
    }
    with patch("world_intel_mcp.reports._collect_report_data", AsyncMock(return_value=data)):
        await generate_report(MagicMock(), output_path=output, title=payload, fmt="html")
    html = output.read_text()
    assert payload not in html
    assert html.count(_esc(payload)) == 8
    assert not {"img", "link", "a", "script"} & set(_Elements(html).tags)
    assert _Elements(html).tags.count("style") == 1  # trusted report CSS only


@pytest.mark.parametrize("url", ["file:///not-a-real-resource", "http://example.invalid/",
                                "https://example.invalid/", "data:text/plain,blocked",
                                "ftp://example.invalid/", "relative.png", ""])
def test_pdf_resource_blocker_denies_every_url(url):
    with pytest.raises(ValueError, match="resource loading is disabled"):
        _ReportResourceBlocker()(url)


async def test_pdf_generation_passes_restrictive_loader(tmp_path, monkeypatch):
    """Assert the actual production constructor boundary, even without WeasyPrint."""
    constructor = MagicMock()
    constructor.return_value.write_pdf.return_value = b"%PDF-stub"
    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=constructor))
    with patch("world_intel_mcp.reports.pdf_dependencies_available", return_value=True), patch(
        "world_intel_mcp.reports._collect_report_data", AsyncMock(return_value={})
    ):
        output = tmp_path / "report.pdf"
        await generate_report(MagicMock(), output_path=output)
    kwargs = constructor.call_args.kwargs
    assert set(kwargs) == {"string", "url_fetcher"}
    assert "<!DOCTYPE html>" in kwargs["string"]
    assert isinstance(kwargs["url_fetcher"], _ReportResourceBlocker)
    with pytest.raises(ValueError):
        kwargs["url_fetcher"]("https://example.invalid/")
    assert output.read_bytes() == b"%PDF-stub"


def _weasyprint_or_skip():
    try:
        import weasyprint
    except (ImportError, OSError) as exc:
        pytest.skip(f"Optional WeasyPrint/native libraries unavailable: {exc}")
    return weasyprint


@pytest.mark.parametrize("kind", ["image", "css-link", "css-import", "css-url", "font-face",
                                 "attachment", "attachment-link"])
@pytest.mark.parametrize("scheme", ["file", "http", "https", "data"])
async def test_real_pdf_resource_rejection(tmp_path, monkeypatch, kind, scheme):
    """Exercise actual WeasyPrint via generate_report; never use its real loader.

    Inject raw markup at the assembly boundary to test defense in depth even if
    a future renderer accidentally omits escaping. All file paths are disposable
    test files and all network names are .invalid; fallback IO is trapped too.
    """
    weasyprint = _weasyprint_or_skip()
    from weasyprint import urls
    from urllib import request
    from world_intel_mcp import reports

    resource = tmp_path / "resource.txt"
    resource.write_text("test resource, not sensitive")
    url = {"file": resource.as_uri(), "http": "http://example.invalid/resource",
           "https": "https://example.invalid/resource", "data": "data:text/plain,blocked"}[scheme]
    markup = {
        "image": f'<img src="{url}">',
        "css-link": f'<link rel="stylesheet" href="{url}">',
        "css-import": f'<style>@import url("{url}");</style>',
        "css-url": f'<div style="background-image:url({url});width:10px;height:10px">test</div>',
        "font-face": f'<style>@font-face {{font-family:Blocked;src:url("{url}")}} '
                     'body {font-family:Blocked}</style><p>test font</p>',
        "attachment": f'<link rel="attachment" href="{url}" title="blocked">',
        "attachment-link": f'<a rel="attachment" href="{url}">blocked</a>',
    }[kind]
    blocked = []
    original = _ReportResourceBlocker.__call__

    def record_rejection(self, target):
        blocked.append(target)
        return original(self, target)

    # Trap default fetching across both the older function API and newer class
    # API, so a regression cannot perform network or file-resource IO in tests.
    fallback = MagicMock(side_effect=AssertionError("default resource loader invoked"))
    monkeypatch.setattr(request.OpenerDirector, "open", fallback)
    if hasattr(urls, "URLFetcher"):
        monkeypatch.setattr(urls.URLFetcher, "fetch", fallback)
    if hasattr(urls, "default_url_fetcher"):
        monkeypatch.setattr(urls, "default_url_fetcher", fallback)
        monkeypatch.setattr(weasyprint, "default_url_fetcher", fallback)
    monkeypatch.setattr(_ReportResourceBlocker, "__call__", record_rejection)
    monkeypatch.setattr(reports, "_collect_report_data", AsyncMock(return_value={}))
    monkeypatch.setattr(reports, "_build_html", lambda *args: f"<html><body>{markup}</body></html>")
    output = tmp_path / "blocked.pdf"
    await generate_report(MagicMock(), output_path=output)
    assert blocked == [url]
    fallback.assert_not_called()
    assert output.read_bytes().startswith(b"%PDF-")
