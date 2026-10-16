"""Real feedparser content-only regressions; no parser result is mocked."""

import ast
import builtins
import io
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import feedparser
import pytest

from world_intel_mcp.feed_parser import parse_feed_content
from world_intel_mcp.sources import (
    ai_watch, health, jtwc, meteoalarm, news, service_status, usni_fleet, volcano,
)


_LOCATORS = [
    "http://127.0.0.1:6333/private",
    "https://internal.example/private",
    "file:///etc/passwd",
    "ftp://internal.example/private",
    "//internal.example/private",
    "/etc/passwd",
    "relative/feed.xml",
    "feed:https://internal.example/private",
]
_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title><item>
<title>東京 café</title><link>https://example.test/item</link>
<description>Résumé &amp; weather</description>
</item></channel></rss>"""
_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
<entry><id>urn:test:1</id><title>東京 café</title>
<link href="https://example.test/item"/><summary>Résumé &amp; weather</summary>
</entry></feed>"""


@pytest.fixture
def no_parser_io(monkeypatch):
    # Warm up the actual parser's lazy XML imports before blocking file opens.
    feedparser.parse(io.BytesIO(_RSS.encode("utf-8")))
    blocked = Mock(side_effect=AssertionError("feed parsing must not perform resource I/O"))
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", blocked)
        guard.setattr(io, "open", blocked)
        guard.setattr(urllib.request, "urlopen", blocked)
        guard.setattr(urllib.request.OpenerDirector, "open", blocked)
        guard.setattr(socket, "getaddrinfo", blocked)
        guard.setattr(socket.socket, "connect", blocked)
        yield blocked
        blocked.assert_not_called()


@pytest.mark.parametrize("body", _LOCATORS)
@pytest.mark.parametrize("as_bytes", [False, True])
def test_locator_shaped_content_never_opens_resources(no_parser_io, body, as_bytes):
    parsed = parse_feed_content(body.encode("utf-8") if as_bytes else body)
    assert parsed.entries == []
    no_parser_io.assert_not_called()


def test_existing_local_feed_is_not_read(tmp_path, no_parser_io):
    # File creation uses an already-defined descriptor API, not parser I/O.
    # A real existing path guards against treating failed open attempts as safe.
    import os
    path = tmp_path / "feed.xml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, _RSS.encode("utf-8"))
    finally:
        os.close(fd)
    assert parse_feed_content(str(path)).entries == []
    assert parse_feed_content(bytes(path)).entries == []
    no_parser_io.assert_not_called()


@pytest.mark.parametrize("xml", [_RSS, _ATOM], ids=["rss", "atom"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_valid_unicode_feeds_keep_entries(no_parser_io, xml, as_bytes):
    parsed = parse_feed_content(xml.encode("utf-8") if as_bytes else xml)
    assert len(parsed.entries) == 1
    entry = parsed.entries[0]
    assert entry.title == "東京 café"
    assert entry.link == "https://example.test/item"
    assert entry.summary == "Résumé & weather"


@pytest.mark.parametrize("encoding", ["iso-8859-1", "utf-16", "utf-32"])
@pytest.mark.parametrize("xml", [_RSS, _ATOM], ids=["rss", "atom"])
def test_byte_feeds_preserve_declared_encoding_and_bom(no_parser_io, xml, encoding):
    xml = xml.replace("東京 café", "café").replace("UTF-8", encoding)
    parsed = parse_feed_content(xml.encode(encoding))
    assert len(parsed.entries) == 1
    assert parsed.entries[0].title == "café"
    assert parsed.entries[0].summary == "Résumé & weather"


@pytest.mark.parametrize("content", [None, Path("feed.xml"), io.BytesIO(b"data")])
def test_only_content_types_are_accepted(no_parser_io, content):
    with pytest.raises(TypeError, match="str or bytes"):
        parse_feed_content(content)


_HANDLERS = [
    (news.fetch_news_feed, {}, "items"),
    (service_status.fetch_service_status, {}, "incidents"),
    (health.fetch_disease_outbreaks, {}, "items"),
    (ai_watch.fetch_ai_watch, {}, "items"),
    (meteoalarm.fetch_meteoalarm_alerts, {"country": "germany"}, "alerts"),
    (jtwc.fetch_jtwc_cyclones, {}, "storms"),
    (volcano.fetch_volcano_activity, {}, "volcanoes"),
    (usni_fleet.fetch_usni_fleet, {}, "ships"),
]


@pytest.fixture
def single_feed(monkeypatch):
    url = "https://provider.example/feed"
    monkeypatch.setattr(news, "_RSS_FEEDS", {"test": [("Test", url)]})
    monkeypatch.setattr(service_status, "_STATUS_FEEDS", [{"provider": "Test", "url": url}])
    monkeypatch.setattr(health, "_HEALTH_FEEDS", [("Test", url)])
    monkeypatch.setattr(ai_watch, "_AI_FEEDS", [("Test", url, "test")])


@pytest.mark.parametrize("handler,kwargs,result_key", _HANDLERS, ids=[h[0].__name__ for h in _HANDLERS])
@pytest.mark.parametrize("body", _LOCATORS)
async def test_all_eight_handlers_parse_downloads_as_content(
    single_feed, no_parser_io, handler, kwargs, result_key, body,
):
    fetcher = SimpleNamespace(get_xml=AsyncMock(return_value=body), get_text=AsyncMock(return_value=body))
    result = await handler(fetcher, **kwargs)
    assert result[result_key] == []
    assert fetcher.get_xml.await_count + fetcher.get_text.await_count == 1
    no_parser_io.assert_not_called()


async def test_actual_service_status_handler_keeps_valid_incidents(single_feed, no_parser_io):
    fetcher = SimpleNamespace(get_xml=AsyncMock(return_value=_RSS))
    result = await service_status.fetch_service_status(fetcher)
    fetcher.get_xml.assert_awaited_once()
    assert result["count"] == 1 and result["incidents"][0]["title"] == "東京 café"
    assert result["incidents"][0]["link"] == "https://example.test/item"
    assert result["providers_checked"] == ["Test"]


def test_feedparser_entrypoint_is_only_used_in_content_helper():
    root = Path(__file__).resolve().parents[1]
    calls = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        modules, direct = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.asname or a.name for a in node.names if a.name == "feedparser")
            elif isinstance(node, ast.ImportFrom) and node.module == "feedparser":
                direct.update(a.asname or a.name for a in node.names if a.name == "parse")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if ((isinstance(fn, ast.Attribute) and fn.attr == "parse"
                 and isinstance(fn.value, ast.Name) and fn.value.id in modules)
                or (isinstance(fn, ast.Name) and fn.id in direct)):
                calls.append(path.relative_to(root).as_posix())
    assert calls == ["feed_parser.py"]
