"""JTWC tropical cyclone warning source for world-intel-mcp.

Parses the Joint Typhoon Warning Center's RSS feed at metoc.navy.mil.
No API key required. Complement to sources/cyclones.py (NHC): JTWC is
the US DoD warning agency for the Northwest Pacific, North Indian
Ocean, and Southern Hemisphere; its feed also carries a Central/Eastern
Pacific item that overlaps NHC coverage.

Feed facts, verified live 2026-09-01 against four active storms: RSS
2.0 with one item per basin group (guids NWPAC-NIO-WARNINGS,
EPAC-CPAC-WARNINGS, SH-WARNINGS) plus a TROPICAL-ADVISORIES item
listing the significant-tropical-weather advisories (ABPW10/ABIO10).
Storms live inside each item's CDATA HTML description as headers like
"Tropical Depression  17W (Saudel) Warning #42" followed by
"Issued at 01/2100Z" and product links; the HTML nests unbalanced
<p><b> tags, so parsing is regex-over-text, not DOM. A quiet basin's
item says "No Current Tropical Cyclone Warnings." — that is a VALID
empty result, not an outage. Item pubDates use a two-digit year
("Tue, 01 Sep 26 ..."); issued_at carries only day/time (DD/HHMMZ).

Positions, intensities, and forecast tracks are NOT in the RSS — they
live in the per-storm warning-text products this module links to but
does not fetch (one fetch per storm would fan out unboundedly).
"""

import logging
import re
from datetime import datetime, timezone

from ..fetcher import Fetcher
from ..feed_parser import parse_feed_content

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

logger = logging.getLogger("world-intel-mcp.sources.jtwc")

_JTWC_RSS_URL = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"

# Warnings are 6-hourly; the feed itself rebuilds more often.
_CACHE_TTL = 600

_BASINS_BY_GUID: dict[str, str] = {
    "NWPAC-NIO-WARNINGS": "northwest_pacific_north_indian",
    "EPAC-CPAC-WARNINGS": "central_eastern_pacific",
    "SH-WARNINGS": "southern_hemisphere",
}
_ADVISORIES_GUID = "TROPICAL-ADVISORIES"

# "Tropical Depression  17W (Saudel) Warning #42" — note the feed's
# double space after the classification; \s+ absorbs it.
_STORM_RE = re.compile(
    r"(?P<classification>(?:Super\s+)?(?:Tropical\s+(?:Depression|Storm|Cyclone)"
    r"|Typhoon|Hurricane|Subtropical\s+(?:Depression|Storm)))"
    r"\s+(?P<id>\d{2}[A-Z])\s+\((?P<name>[^)]+)\)\s+Warning\s+#?(?P<warning>\d+)"
)
_ISSUED_RE = re.compile(r"(?:Re)?issued at\s+(\d{2}/\d{4}Z)", re.IGNORECASE)
_WARNING_TEXT_RE = re.compile(r"href=['\"]([^'\"]*web\.txt)['\"]")
_GRAPHIC_RE = re.compile(r"href=['\"]([^'\"]*\.gif)['\"]")
_ADVISORY_LINK_RE = re.compile(r"<a\s+href=['\"]([^'\"]+\.txt)['\"][^>]*>([^<]+)</a>")
_QUIET_MARKER = "No Current Tropical Cyclone Warnings"


async def fetch_jtwc_cyclones(fetcher: Fetcher) -> dict:
    """Fetch active JTWC tropical cyclone warnings.

    Args:
        fetcher: Shared HTTP fetcher with caching and circuit breaking.

    Returns:
        Dict with storms list (id, name, classification, warning
        number, issued_at as DD/HHMMZ, basin, product links), count,
        by_basin counts (quiet basins present with 0), advisories list
        (significant tropical weather advisory products), source, and
        timestamp. Zero storms with no degraded marker means quiet
        basins, not a failure.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if feedparser is None:
        return {
            "error": "feedparser not installed — run: pip install feedparser",
            "degraded": True,
            "reason": "feedparser_missing",
            "storms": [],
            "count": 0,
            "by_basin": {},
            "advisories": [],
            "source": "jtwc",
            "timestamp": timestamp,
        }

    xml_text = await fetcher.get_xml(
        _JTWC_RSS_URL,
        source="jtwc",
        cache_key="jtwc:rss:current",
        cache_ttl=_CACHE_TTL,
    )

    if xml_text is None:
        logger.warning("JTWC RSS feed returned no data")
        return {
            "error": "JTWC RSS feed unavailable (no live or cached data)",
            "degraded": True,
            "reason": "jtwc_fetch_failed",
            "storms": [],
            "count": 0,
            "by_basin": {},
            "advisories": [],
            "source": "jtwc",
            "timestamp": timestamp,
        }

    parsed = parse_feed_content(xml_text)

    storms: list[dict] = []
    by_basin: dict[str, int] = {}
    advisories: list[dict] = []
    for entry in parsed.get("entries", []):
        guid = entry.get("id") or ""
        description = entry.get("summary") or ""

        if guid == _ADVISORIES_GUID:
            advisories = [
                {"title": " ".join(title.split()), "url": url}
                for url, title in _ADVISORY_LINK_RE.findall(description)
            ]
            continue

        basin = _BASINS_BY_GUID.get(guid)
        if basin is None:
            # Unknown item — derive a label from the category rather
            # than dropping storms silently.
            category = entry.get("category") or guid or "unknown"
            basin = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")

        basin_storms = _parse_storms(description, basin)
        by_basin[basin] = len(basin_storms)
        storms.extend(basin_storms)

    return {
        "storms": storms,
        "count": len(storms),
        "by_basin": by_basin,
        "advisories": advisories,
        "feed_published": parsed.get("feed", {}).get("published"),
        "source": "jtwc",
        "timestamp": timestamp,
    }


def _parse_storms(description: str, basin: str) -> list[dict]:
    """Extract storm warnings from one basin item's HTML description.

    Each storm's details (issued time, product links) are searched for
    only in the text span between its header and the next storm's
    header, so links can't bleed across storms.
    """
    matches = list(_STORM_RE.finditer(description))
    storms = []
    for i, match in enumerate(matches):
        segment_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(description)
        )
        segment = description[match.end() : segment_end]

        issued = _ISSUED_RE.search(segment)
        warning_text = _WARNING_TEXT_RE.search(segment)
        graphic = _GRAPHIC_RE.search(segment)

        storms.append(
            {
                "id": match.group("id"),
                "name": match.group("name").strip(),
                "classification": " ".join(match.group("classification").split()),
                "warning_number": int(match.group("warning")),
                "issued_at": issued.group(1) if issued else None,
                "basin": basin,
                "warning_text_url": warning_text.group(1) if warning_text else None,
                "warning_graphic_url": graphic.group(1) if graphic else None,
            }
        )
    if not storms and _QUIET_MARKER.lower() not in description.lower():
        logger.info(
            "JTWC %s item had no storm headers and no quiet marker "
            "— format may have drifted",
            basin,
        )
    return storms
