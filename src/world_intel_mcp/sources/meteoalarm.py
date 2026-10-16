"""Meteoalarm European severe-weather warning source for world-intel-mcp.

Parses the Meteoalarm legacy ATOM feeds at feeds.meteoalarm.org. No API
key required. Coverage: Europe only — 39 participating countries (the
EUMETNET members, including Israel), each with its own per-country feed.

Feed facts, verified live 2026-09-01 against the France feed: entries
carry CAP 1.2 extension elements (cap:event, cap:severity, cap:onset,
cap:expires, cap:areaDesc, cap:certainty, cap:urgency, cap:status,
cap:message_type, cap:identifier) that feedparser exposes as
``entry.cap_event`` etc.; the awareness color (yellow/orange/red) is
NOT a CAP field in this feed — it appears only as the leading word of
the entry title ("Yellow Thunderstorm Warning issued for France -
Hautes-Pyrénées"), so ``awareness_color`` here is derived from the
title. There is NO Europe-wide feed: the documented
``meteoalarm-legacy-atom-europe`` slug returns 404 and the feed index
page advertises per-country feeds only, so a country is required to
fetch warnings; calling without one returns the country roster.

Feed content is licensed by MeteoAlarm under terms equivalent to
CC BY 4.0 (per the feed's own <rights> element).
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

logger = logging.getLogger("world-intel-mcp.sources.meteoalarm")

_FEED_URL_TEMPLATE = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{slug}"

# Warnings feed; 10 minutes keeps us polite while staying current.
_CACHE_TTL = 600

# Complete per-country roster harvested from the feeds.meteoalarm.org
# index page 2026-09-01. These are URL slugs, not ISO codes.
_COUNTRIES: frozenset[str] = frozenset(
    {
        "andorra",
        "austria",
        "belgium",
        "bosnia-herzegovina",
        "bulgaria",
        "croatia",
        "cyprus",
        "czechia",
        "denmark",
        "estonia",
        "finland",
        "france",
        "germany",
        "greece",
        "hungary",
        "iceland",
        "ireland",
        "israel",
        "italy",
        "latvia",
        "lithuania",
        "luxembourg",
        "malta",
        "moldova",
        "montenegro",
        "netherlands",
        "norway",
        "poland",
        "portugal",
        "republic-of-north-macedonia",
        "romania",
        "serbia",
        "slovakia",
        "slovenia",
        "spain",
        "sweden",
        "switzerland",
        "ukraine",
        "united-kingdom",
    }
)

# Common names that differ from Meteoalarm's slugs.
_ALIASES: dict[str, str] = {
    "uk": "united-kingdom",
    "great-britain": "united-kingdom",
    "czech-republic": "czechia",
    "bosnia": "bosnia-herzegovina",
    "bosnia-and-herzegovina": "bosnia-herzegovina",
    "macedonia": "republic-of-north-macedonia",
    "north-macedonia": "republic-of-north-macedonia",
}

# Meteoalarm awareness colors, as they lead entry titles.
_COLOR_RE = re.compile(r"^(Green|Yellow|Orange|Red)\b", re.IGNORECASE)


async def fetch_meteoalarm_alerts(
    fetcher: Fetcher, country: str | None = None, limit: int = 50
) -> dict:
    """Fetch active severe-weather warnings for one European country.

    Args:
        fetcher: Shared HTTP fetcher with caching and circuit breaking.
        country: Country name or slug (e.g. "france", "united-kingdom",
            "uk"). Meteoalarm publishes per-country feeds only, so None
            returns the available-country roster instead of warnings.
        limit: Max alerts to return (each alert is one warning for one
            area; a single meteorological event spans several entries).

    Returns:
        Dict with alerts list (event, severity, awareness_color, area,
        onset/expires, certainty, urgency, status), count, by_severity
        and by_color counts, country, source, and timestamp. Zero
        alerts with no degraded marker means calm weather, not a
        failure.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if country is None:
        return {
            "note": (
                "Meteoalarm publishes per-country feeds only (no Europe-wide "
                "feed exists). Pass country=<name> to fetch warnings."
            ),
            "available_countries": sorted(_COUNTRIES),
            "alerts": [],
            "count": 0,
            "country": None,
            "source": "meteoalarm",
            "timestamp": timestamp,
        }

    slug = _normalize_country(country)
    if slug not in _COUNTRIES:
        return {
            "error": f"Unknown Meteoalarm country: {country!r}",
            "reason": "unknown_country",
            "available_countries": sorted(_COUNTRIES),
            "alerts": [],
            "count": 0,
            "country": country,
            "source": "meteoalarm",
            "timestamp": timestamp,
        }

    if feedparser is None:
        return {
            "error": "feedparser not installed — run: pip install feedparser",
            "degraded": True,
            "reason": "feedparser_missing",
            "alerts": [],
            "count": 0,
            "country": slug,
            "source": "meteoalarm",
            "timestamp": timestamp,
        }

    xml_text = await fetcher.get_xml(
        _FEED_URL_TEMPLATE.format(slug=slug),
        source="meteoalarm",
        cache_key=f"meteoalarm:atom:{slug}",
        cache_ttl=_CACHE_TTL,
    )

    if xml_text is None:
        logger.warning("Meteoalarm feed for %s returned no data", slug)
        return {
            "error": (
                f"Meteoalarm feed for {slug} unavailable (no live or cached data)"
            ),
            "degraded": True,
            "reason": "meteoalarm_fetch_failed",
            "alerts": [],
            "count": 0,
            "country": slug,
            "source": "meteoalarm",
            "timestamp": timestamp,
        }

    parsed = parse_feed_content(xml_text)

    alerts = []
    by_severity: dict[str, int] = {}
    by_color: dict[str, int] = {}
    total_entries = 0
    for entry in parsed.get("entries", []):
        total_entries += 1
        title = entry.get("title", "")
        color_match = _COLOR_RE.match(title)
        color = color_match.group(1).lower() if color_match else None

        severity = entry.get("cap_severity")
        if severity:
            by_severity[severity] = by_severity.get(severity, 0) + 1
        if color:
            by_color[color] = by_color.get(color, 0) + 1

        if len(alerts) >= limit:
            continue  # keep counting for by_severity/by_color totals

        alerts.append(
            {
                "event": entry.get("cap_event"),
                "severity": severity,
                "awareness_color": color,
                "area": entry.get("cap_areadesc"),
                "onset": entry.get("cap_onset"),
                "expires": entry.get("cap_expires"),
                "certainty": entry.get("cap_certainty"),
                "urgency": entry.get("cap_urgency"),
                "status": entry.get("cap_status"),
                "message_type": entry.get("cap_message_type"),
                "identifier": entry.get("cap_identifier"),
                "title": title,
                "published": entry.get("published"),
                "link": entry.get("link"),
            }
        )

    return {
        "alerts": alerts,
        "count": len(alerts),
        "total_entries": total_entries,
        "by_severity": by_severity,
        "by_color": by_color,
        "country": slug,
        "feed_updated": parsed.get("feed", {}).get("updated"),
        "source": "meteoalarm",
        "timestamp": timestamp,
    }


def _normalize_country(country: str) -> str:
    """Normalize a user-supplied country name to a Meteoalarm slug."""
    slug = re.sub(r"[\s_]+", "-", country.strip().lower())
    return _ALIASES.get(slug, slug)
