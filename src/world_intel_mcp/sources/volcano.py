"""Smithsonian GVP weekly volcanic activity source for world-intel-mcp.

Parses the Smithsonian Global Volcanism Program / USGS Weekly Volcanic
Activity Report RSS feed (updated Thursdays). No API key required.

Feed facts, verified live 2026-09-01: RSS 2.0 with one item per
volcano; titles follow "Name (Country) - Report for <week> - <status>"
where status is "New Eruptive Activity" or "Continuing Eruptive
Activity"; each item carries a georss:point that feedparser exposes as
``entry.where`` with GeoJSON-ordered (lon, lat) coordinates; item
descriptions are HTML paragraphs.

Encoding honesty: the XML declares ISO-8859-1 but the server's
Content-Type header claims UTF-8, so httpx's decode can leave U+FFFD
replacement characters inside accented volcano names (e.g. Popocatépetl).
The feed structure survives; only some non-ASCII name/summary characters
may be mangled. Not fixable from here without bypassing the shared
fetcher, which this repo forbids.
"""

import logging
import re
from datetime import datetime, timezone
from html import unescape

from ..fetcher import Fetcher
from ..feed_parser import parse_feed_content

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

logger = logging.getLogger("world-intel-mcp.sources.volcano")

_GVP_WEEKLY_RSS = "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml"

# Report is published weekly (Thursdays); refetching hourly is plenty.
_CACHE_TTL = 3600

_TITLE_RE = re.compile(r"^(?P<name>.*?)\s+\((?P<country>[^)]*)\)$")
_TAG_RE = re.compile(r"<[^>]+>")


async def fetch_volcano_activity(fetcher: Fetcher) -> dict:
    """Fetch the GVP/USGS weekly volcanic activity report.

    Args:
        fetcher: Shared HTTP fetcher with caching and circuit breaking.

    Returns:
        Dict with volcanoes list (name, country, lat/lon, activity
        status, summary, report week), count, new/continuing activity
        counts, report_published, source, and timestamp.
    """
    now = datetime.now(timezone.utc)

    if feedparser is None:
        return {
            "error": "feedparser not installed — run: pip install feedparser",
            "degraded": True,
            "reason": "feedparser_missing",
            "volcanoes": [],
            "count": 0,
            "source": "gvp",
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    xml_text = await fetcher.get_xml(
        _GVP_WEEKLY_RSS,
        source="gvp",
        cache_key="volcano:gvp:weekly",
        cache_ttl=_CACHE_TTL,
    )

    if xml_text is None:
        logger.warning("GVP weekly volcano feed returned no data")
        return {
            "error": "GVP weekly volcano feed unavailable (no live or cached data)",
            "degraded": True,
            "reason": "gvp_fetch_failed",
            "volcanoes": [],
            "count": 0,
            "source": "gvp",
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    parsed = parse_feed_content(xml_text)

    volcanoes = []
    new_count = 0
    continuing_count = 0
    for entry in parsed.get("entries", []):
        name, country, week, activity_status = _parse_title(entry.get("title", ""))

        if activity_status and "new" in activity_status.lower():
            new_count += 1
        elif activity_status and "continuing" in activity_status.lower():
            continuing_count += 1

        lat: float | None = None
        lon: float | None = None
        where = entry.get("where")
        if where and where.get("type") == "Point":
            coords = where.get("coordinates") or ()
            if len(coords) >= 2:
                # feedparser normalizes georss:point to (lon, lat)
                lon, lat = coords[0], coords[1]

        summary = _strip_html(entry.get("summary") or "")
        if len(summary) > 500:
            summary = summary[:500]

        volcanoes.append(
            {
                "name": name,
                "country": country,
                "latitude": lat,
                "longitude": lon,
                "activity_status": activity_status,
                "summary": summary,
                "report_week": week,
                "published": entry.get("published"),
                "link": entry.get("id") or entry.get("link"),
            }
        )

    return {
        "volcanoes": volcanoes,
        "count": len(volcanoes),
        "new_activity_count": new_count,
        "continuing_activity_count": continuing_count,
        "report_published": parsed.get("feed", {}).get("published"),
        "source": "gvp",
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _parse_title(
    title: str,
) -> tuple[str, str | None, str | None, str | None]:
    """Split a GVP item title into (name, country, week, activity_status).

    Expected form: "Name (Country) - Report for <week>[ - <status>]".
    A title that doesn't match falls back to (title, None, None, None)
    rather than guessing.
    """
    parts = title.split(" - ")
    if len(parts) < 2 or not parts[1].startswith("Report for "):
        return (title, None, None, None)

    week = parts[1][len("Report for ") :]
    activity_status = parts[2] if len(parts) > 2 else None

    match = _TITLE_RE.match(parts[0])
    if match:
        return (match.group("name"), match.group("country"), week, activity_status)
    return (parts[0], None, week, activity_status)


def _strip_html(text: str) -> str:
    """Reduce an HTML fragment to whitespace-normalized plain text."""
    return " ".join(unescape(_TAG_RE.sub(" ", text)).split())
