"""Daily intelligence digest: a cited markdown morning brief.

Composes a numbered-source markdown digest from current-event data across
several intelligence domains, plus (when the optional vector store is
installed) recent trend detection and a chronological timeline drawn from
accumulated history.

Follows the same citation discipline as the situation brief
(analysis/situation.py): every listed event carries a `[n]` reference into
a numbered `sources` list, and `cited` is only true when the markdown
actually contains a real one. When the vector store is unavailable, the
Trends and Timeline sections say so explicitly via `data_gaps` rather than
rendering empty sections that would read as a quiet day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .situation import _add_source, _cite, _has_valid_citation
from ..diagnostics import exception_summary, safe_error

logger = logging.getLogger("world-intel-mcp.analysis.daily_digest")

# Overview key -> (friendly label, domain used in _extract_metrics' sources)
_CURRENT_EVENT_DOMAINS = {
    "earthquakes": "Earthquakes",
    "military_flights": "Military aircraft",
    "wildfires": "Wildfires",
    "cyber_threats": "Cyber threats",
    "disease_outbreaks": "Disease outbreaks",
    "news_feed": "News",
    "space_weather": "Space weather",
    "domestic_flights": "Air traffic",
    "traffic_flow": "Traffic",
}


async def _safe_fetch(coro, label: str) -> dict:
    try:
        result = await coro
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        logger.warning("Daily digest: %s failed: %s", label, exception_summary(exc))
        return {"error": safe_error(exc)}


async def _fetch_current_events(fetcher) -> tuple[dict, list[str]]:
    """Fetch the same current-event domains the situation brief consumes.

    Returns a mini overview dict shaped like `_fetch_overview()`'s output
    (for reuse of `_extract_metrics`), plus a list of honest data_gaps for
    any domain that errored or returned nothing usable.
    """
    from ..sources import (
        seismology,
        military,
        conflict,
        wildfire,
        cyber,
        health,
        news,
        space_weather,
        aviation,
        traffic,
    )

    (
        earthquakes,
        military_flights,
        acled_events,
        ucdp_events,
        wildfires,
        cyber_threats,
        disease_outbreaks,
        news_feed,
        space_weather_data,
        domestic_flights,
        traffic_flow,
    ) = await asyncio.gather(
        _safe_fetch(seismology.fetch_earthquakes(fetcher), "earthquakes"),
        _safe_fetch(military.fetch_military_flights(fetcher), "military_flights"),
        _safe_fetch(conflict.fetch_acled_events(fetcher), "acled_events"),
        _safe_fetch(conflict.fetch_ucdp_events(fetcher), "ucdp_events"),
        _safe_fetch(wildfire.fetch_wildfires(fetcher), "wildfires"),
        _safe_fetch(cyber.fetch_cyber_threats(fetcher), "cyber_threats"),
        _safe_fetch(health.fetch_disease_outbreaks(fetcher), "disease_outbreaks"),
        _safe_fetch(news.fetch_news_feed(fetcher), "news_feed"),
        _safe_fetch(space_weather.fetch_space_weather(fetcher), "space_weather"),
        _safe_fetch(aviation.fetch_domestic_flights(fetcher), "domestic_flights"),
        _safe_fetch(traffic.fetch_traffic_flow(fetcher), "traffic_flow"),
    )

    mini_overview = {
        "earthquakes": earthquakes,
        "military_flights": military_flights,
        "acled_events": acled_events,
        "ucdp_events": ucdp_events,
        "wildfires": wildfires,
        "cyber_threats": cyber_threats,
        "disease_outbreaks": disease_outbreaks,
        "news_feed": news_feed,
        "space_weather": space_weather_data,
        "domestic_flights": domestic_flights,
        "traffic_flow": traffic_flow,
    }

    domain_gaps: list[str] = []
    for key, label in _CURRENT_EVENT_DOMAINS.items():
        payload = mini_overview.get(key)
        if not isinstance(payload, dict) or payload.get("error"):
            reason = (
                payload.get("error")
                if isinstance(payload, dict)
                else "no data returned"
            )
            domain_gaps.append(f"{label}: {reason or 'unavailable'}")

    # Conflict is a fallback pair (ACLED, then UCDP): only a gap if both failed.
    acled_ok = isinstance(acled_events, dict) and not acled_events.get("error")
    ucdp_ok = isinstance(ucdp_events, dict) and not ucdp_events.get("error")
    if not acled_ok and not ucdp_ok:
        acled_reason = (
            acled_events.get("error") if isinstance(acled_events, dict) else "no data"
        )
        domain_gaps.append(f"Conflict events: {acled_reason or 'unavailable'}")

    return mini_overview, domain_gaps


def _render_events_and_headlines(sources: list[dict]) -> tuple[list[str], list[str]]:
    """Split the sources produced so far into 'current event' bullets and
    'headline' bullets, each carrying its own [n]."""
    events_md = []
    headlines_md = []
    for s in sources:
        if s["domain"] == "news":
            url_suffix = f" ({s['url']})" if s.get("url") else ""
            headlines_md.append(f"- [{s['n']}] {s['description']}{url_suffix}")
        else:
            label = s["domain"].replace("_", " ").title()
            events_md.append(f"- **{label}** [{s['n']}]: {s['description']}")
    return events_md, headlines_md


async def fetch_daily_digest(fetcher, vector_store=None) -> dict:
    """Compose a cited markdown morning brief.

    Args:
        fetcher: Shared HTTP fetcher with caching and circuit breaking.
        vector_store: Optional VectorStore instance. When None (Qdrant /
            FastEmbed not installed, or the caller has none configured),
            the Trends and Timeline sections are honestly omitted via
            `data_gaps` instead of rendered empty.

    Returns:
        Dict with `markdown`, `sources`, `cited`, `data_gaps`,
        `vector_store_available`, `source`, and `timestamp`.
    """
    from .situation import _extract_metrics

    now = datetime.now(timezone.utc)
    data_gaps: list[str] = []

    mini_overview, domain_gaps = await _fetch_current_events(fetcher)
    data_gaps.extend(domain_gaps)

    _metrics, sources, _citations = _extract_metrics(mini_overview)
    events_md, headlines_md = _render_events_and_headlines(sources)

    vector_store_available = vector_store is not None
    trend_result: dict = {}
    timeline_result: dict = {}

    if vector_store_available:
        trend_result, timeline_result = await asyncio.gather(
            _safe_fetch(
                vector_store.trend_detection(recent_hours=6.0, baseline_hours=48.0),
                "trend_detection",
            ),
            _safe_fetch(vector_store.timeline(hours=24.0, limit=15), "timeline"),
        )
    else:
        data_gaps.append("vector store unavailable: trends and timeline omitted")

    # --- Trends section -----------------------------------------------
    if not vector_store_available:
        trends_section = "_Omitted: vector store unavailable._"
    elif trend_result.get("error"):
        data_gaps.append(f"trend detection unavailable: {trend_result['error']}")
        trends_section = f"_Unavailable: {trend_result['error']}._"
    elif not trend_result.get("trends"):
        trends_section = "_No trend data accumulated yet._"
    else:
        notable = [t for t in trend_result["trends"] if t.get("trend") != "NORMAL"]
        if not notable:
            trends_section = (
                "_No significant trend shifts in the last 6h vs. 48h baseline._"
            )
        else:
            lines = []
            for t in notable[:8]:
                n = _add_source(
                    sources,
                    "trends",
                    f"{t['category']}: {t['trend']} ({t['change_pct']:+.1f}% vs. 48h baseline, "
                    f"{t['recent_count']} recent / {t['baseline_count']} baseline)",
                )
                _cite(_citations, "trends", n)
                lines.append(
                    f"- **{t['category']}** [{n}]: {t['trend']} "
                    f"({t['change_pct']:+.1f}% vs. baseline)"
                )
            trends_section = "\n".join(lines)

    # --- Timeline section ------------------------------------------------
    if not vector_store_available:
        timeline_section = "_Omitted: vector store unavailable._"
    elif timeline_result.get("error"):
        data_gaps.append(f"timeline unavailable: {timeline_result['error']}")
        timeline_section = f"_Unavailable: {timeline_result['error']}._"
    elif not timeline_result.get("entries"):
        timeline_section = "_No timeline entries in the last 24h._"
    else:
        lines = []
        for entry in timeline_result["entries"][:10]:
            desc = (entry.get("text") or f"{entry.get('category', 'update')}").strip()[
                :200
            ]
            n = _add_source(
                sources,
                "timeline",
                desc,
                timestamp=entry.get("datetime"),
            )
            _cite(_citations, "timeline", n)
            lines.append(f"- [{n}] ({entry.get('category', 'unknown')}) {desc[:160]}")
        timeline_section = "\n".join(lines)

    # --- Compose markdown --------------------------------------------------
    md_parts = [f"# Daily Intelligence Digest ({now.strftime('%Y-%m-%d')})", ""]

    md_parts.append("## Top Events by Domain")
    md_parts.append(
        "\n".join(events_md) if events_md else "_No current-event data available._"
    )
    md_parts.append("")

    if headlines_md:
        md_parts.append("## Headlines")
        md_parts.append("\n".join(headlines_md))
        md_parts.append("")

    md_parts.append("## Trends (6h vs. 48h baseline)")
    md_parts.append(trends_section)
    md_parts.append("")

    md_parts.append("## Timeline (last 24h)")
    md_parts.append(timeline_section)

    if data_gaps:
        md_parts.append("")
        md_parts.append("## Data Gaps")
        md_parts.extend(f"- {gap}" for gap in data_gaps)

    markdown = "\n".join(md_parts)
    cited = _has_valid_citation(markdown, len(sources))

    return {
        "markdown": markdown,
        "sources": sources,
        "cited": cited,
        "data_gaps": data_gaps,
        "vector_store_available": vector_store_available,
        "source": "daily-digest",
        "timestamp": now.isoformat(),
    }
