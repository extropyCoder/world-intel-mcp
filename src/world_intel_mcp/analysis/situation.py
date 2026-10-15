"""AI-powered situational analysis for world-intel-mcp.

Generates a real-time intelligence brief from all dashboard data
using a local Ollama LLM.  Falls back to a structured metrics summary
when the LLM is unavailable.

Every brief carries a numbered ``sources`` list built from the same
overview data it summarizes, and a ``cited`` flag that is only true when
the returned text actually contains at least one ``[n]`` reference to a
real entry in that list. A metric with no traceable upstream item is
reported without a citation rather than pointing at something that
isn't really there.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from ..diagnostics import exception_summary, safe_error

logger = logging.getLogger("world-intel-mcp.analysis.situation")

_CITATION_RE = re.compile(r"\[(\d+)\]")

# Overview key -> short citable domain label. Kept distinct from the
# dict key so labels stay stable even when the underlying key varies
# (conflict data comes from whichever of acled/conflict_zones/ucdp is
# actually populated).
_CONFLICT_FEED_LABELS = {
    "acled_events": "ACLED",
    "conflict_zones": "hotspot fallback",
    "ucdp_events": "UCDP",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_source(
    sources: list[dict],
    domain: str,
    description: str,
    url: str | None = None,
    timestamp: str | None = None,
) -> int:
    """Append a numbered, traceable source and return its citation number."""
    n = len(sources) + 1
    entry: dict = {"n": n, "domain": domain, "description": description}
    if url:
        entry["url"] = url
    if timestamp:
        entry["timestamp"] = timestamp
    sources.append(entry)
    return n


def _cite(citations: dict[str, list[int]], key: str, n: int) -> None:
    citations.setdefault(key, []).append(n)


def _citation_suffix(citations: dict[str, list[int]], *keys: str) -> str:
    """Format the trailing ``[n][n]`` markers for a fallback-brief line
    from the metric keys it reports on. Empty when none of those keys
    produced a source: an uncited line is the honest outcome, not a
    bug, for a metric with nothing traceable behind it."""
    ns: list[int] = []
    for key in keys:
        for n in citations.get(key, []):
            if n not in ns:
                ns.append(n)
    ns.sort()
    return "".join(f"[{n}]" for n in ns)


def _has_valid_citation(text: str, source_count: int) -> bool:
    """True only if `text` contains at least one [n] whose n is a real
    source number. A hallucinated or out-of-range [n] does not count:
    that is the model ignoring the citation instruction, not honoring it."""
    if source_count <= 0:
        return False
    for match in _CITATION_RE.finditer(text):
        n = int(match.group(1))
        if 1 <= n <= source_count:
            return True
    return False


def _extract_metrics(data: dict) -> tuple[dict, list[dict], dict[str, list[int]]]:
    """Pull key numbers from the full overview data.

    Alongside the metrics dict, builds a numbered `sources` list and a
    `citations` map (metric key -> source numbers) covering every metric
    that has an identifiable upstream item behind it. Metrics computed
    from missing, errored, or empty domain data get no source entry and
    no citation.
    """
    sources: list[dict] = []
    citations: dict[str, list[int]] = {}

    eq = data.get("earthquakes", {})
    quakes = eq.get("count", 0) if isinstance(eq, dict) else 0
    # fetch_earthquakes() returns the event list under "earthquakes", not
    # "events": the latter key was never populated, so max_magnitude (and,
    # without this fix, every earthquake citation) was silently dead code.
    eq_events = eq.get("earthquakes", []) if isinstance(eq, dict) else []
    max_mag = (
        max((e.get("magnitude", 0) for e in eq_events), default=0) if eq_events else 0
    )
    if eq_events:
        top_quake = max(eq_events, key=lambda e: e.get("magnitude") or 0)
        n = _add_source(
            sources,
            "earthquakes",
            f"M{top_quake.get('magnitude')} earthquake, {top_quake.get('place') or 'unknown location'}",
            url=top_quake.get("url"),
            timestamp=top_quake.get("time"),
        )
        _cite(citations, "earthquakes", n)

    mil = data.get("military_flights", {})
    mil_count = mil.get("count", 0) if isinstance(mil, dict) else 0
    if isinstance(mil, dict) and mil and not mil.get("error"):
        n = _add_source(
            sources,
            "military",
            f"{mil_count} military aircraft currently tracked",
            timestamp=mil.get("timestamp"),
        )
        _cite(citations, "military_aircraft", n)

    conflict_domain = None
    for key in ("acled_events", "conflict_zones", "ucdp_events"):
        if data.get(key):
            conflict_domain = key
            break
    conflict_src = data.get(conflict_domain, {}) if conflict_domain else {}
    conflict_count = (
        conflict_src.get("count", 0) if isinstance(conflict_src, dict) else 0
    )
    if (
        conflict_domain
        and isinstance(conflict_src, dict)
        and not conflict_src.get("error")
    ):
        conflict_events = conflict_src.get("events", [])
        feed_label = _CONFLICT_FEED_LABELS.get(conflict_domain, conflict_domain)
        if conflict_events:
            top_event = conflict_events[0]
            loc = (
                top_event.get("location")
                or top_event.get("admin1")
                or top_event.get("country")
                or "unspecified location"
            )
            event_type = top_event.get("event_type") or "conflict event"
            n = _add_source(
                sources,
                "conflict",
                f"{event_type}, {loc} ({feed_label})",
                timestamp=top_event.get("event_date"),
            )
            _cite(citations, "conflicts", n)
        elif conflict_count:
            n = _add_source(
                sources,
                "conflict",
                f"{conflict_count} conflict events tracked ({feed_label})",
            )
            _cite(citations, "conflicts", n)

    fires = data.get("wildfires", {})
    fire_regions = fires.get("fires_by_region", {}) if isinstance(fires, dict) else {}
    fire_clusters = sum(
        len(r.get("top_clusters", []))
        for r in fire_regions.values()
        if isinstance(r, dict)
    )
    if fire_regions:
        top_region, top_count = max(
            (
                (name, len(r.get("top_clusters", [])) if isinstance(r, dict) else 0)
                for name, r in fire_regions.items()
            ),
            key=lambda pair: pair[1],
            default=(None, 0),
        )
        if top_region and top_count:
            n = _add_source(
                sources,
                "wildfires",
                f"{top_count} active fire clusters in {top_region}",
            )
            _cite(citations, "fire_clusters", n)

    cyber = data.get("cyber_threats", {})
    cyber_threats_list = cyber.get("threats", []) if isinstance(cyber, dict) else []
    cyber_count = len(cyber_threats_list)
    if cyber_threats_list:
        top_threat = cyber_threats_list[0]
        threat_label = (
            top_threat.get("threat") or top_threat.get("type") or "cyber threat"
        )
        indicator = top_threat.get("indicator") or ""
        severity = top_threat.get("severity") or "unknown"
        label = f"{threat_label}, {indicator}" if indicator else threat_label
        desc = f"{label} ({severity} severity)"
        n = _add_source(
            sources,
            "cyber",
            desc,
            timestamp=top_threat.get("first_seen") or None,
        )
        _cite(citations, "cyber_threats", n)

    posture = data.get("strategic_posture", {})
    posture_score = (
        posture.get("composite_score", 0) if isinstance(posture, dict) else 0
    )
    risk_level = (
        posture.get("risk_level", "unknown") if isinstance(posture, dict) else "unknown"
    )
    if isinstance(posture, dict) and posture and not posture.get("error"):
        n = _add_source(
            sources,
            "posture",
            f"Composite risk score {posture_score}/100 ({risk_level})",
            timestamp=posture.get("timestamp"),
        )
        _cite(citations, "posture", n)

    alerts = data.get("alert_digest", {})
    alert_count = alerts.get("alert_count", 0) if isinstance(alerts, dict) else 0
    if isinstance(alerts, dict) and alerts and not alerts.get("error"):
        alert_items = alerts.get("alerts") or []
        if alert_items:
            top_alert = alert_items[0]
            desc = top_alert.get("message") or f"{alert_count} active alerts"
            n = _add_source(sources, "alerts", desc, timestamp=alerts.get("timestamp"))
            _cite(citations, "alerts", n)
        elif alert_count:
            n = _add_source(
                sources,
                "alerts",
                f"{alert_count} active alerts across monitored domains",
                timestamp=alerts.get("timestamp"),
            )
            _cite(citations, "alerts", n)

    space = data.get("space_weather", {})
    kp = space.get("current_kp", 0) if isinstance(space, dict) else 0
    if isinstance(space, dict) and space.get("current_kp") is not None:
        n = _add_source(
            sources,
            "space_weather",
            f"Kp index {round(kp, 1)}",
            timestamp=space.get("timestamp"),
        )
        _cite(citations, "kp_index", n)

    health = data.get("disease_outbreaks", {})
    outbreaks = health.get("high_concern_count", 0) if isinstance(health, dict) else 0
    if isinstance(health, dict) and health and not health.get("error"):
        high_concern_items = [
            item
            for item in (health.get("items") or [])
            if isinstance(item, dict) and item.get("is_high_concern")
        ]
        if high_concern_items:
            top = high_concern_items[0]
            n = _add_source(
                sources,
                "health",
                top.get("title") or "disease outbreak alert",
                url=top.get("link"),
                timestamp=top.get("published"),
            )
            _cite(citations, "outbreaks", n)

    news = data.get("news_feed", {})
    headlines = []
    if isinstance(news, dict):
        for item in (news.get("items") or news.get("articles") or [])[:5]:
            if isinstance(item, dict):
                title = item.get("title", "")
                headlines.append(title)
                if title:
                    n = _add_source(
                        sources,
                        "news",
                        title,
                        url=item.get("link") or item.get("url"),
                        timestamp=item.get("published"),
                    )
                    _cite(citations, "headlines", n)

    domestic = data.get("domestic_flights", {})
    total_aircraft = (
        domestic.get("total_aircraft", 0) if isinstance(domestic, dict) else 0
    )
    if isinstance(domestic, dict) and domestic and not domestic.get("error"):
        n = _add_source(
            sources,
            "aviation",
            f"{total_aircraft} aircraft airborne globally",
            timestamp=domestic.get("timestamp"),
        )
        _cite(citations, "total_aircraft", n)

    traffic = data.get("traffic_flow", {})
    avg_congestion = (
        traffic.get("global_avg_congestion", 0) if isinstance(traffic, dict) else 0
    )
    if isinstance(traffic, dict) and traffic and not traffic.get("error"):
        n = _add_source(
            sources,
            "traffic",
            f"{avg_congestion}% average city congestion",
            timestamp=traffic.get("timestamp"),
        )
        _cite(citations, "avg_congestion", n)

    metrics = {
        "earthquakes": quakes,
        "max_magnitude": round(max_mag, 1),
        "military_aircraft": mil_count,
        "conflicts": conflict_count,
        "fire_clusters": fire_clusters,
        "cyber_threats": cyber_count,
        "posture_score": round(posture_score),
        "risk_level": risk_level,
        "alerts": alert_count,
        "kp_index": round(kp, 1),
        "outbreaks": outbreaks,
        "total_aircraft": total_aircraft,
        "avg_congestion": avg_congestion,
        "top_headlines": headlines,
    }
    return metrics, sources, citations


def _build_prompt(m: dict, sources: list[dict]) -> str:
    """Build an LLM prompt from extracted metrics and the source list."""
    headline_block = (
        "\n".join(f"  - {h}" for h in m["top_headlines"])
        if m["top_headlines"]
        else "  (no headlines available)"
    )
    source_block = (
        "\n".join(f"  [{s['n']}] ({s['domain']}) {s['description']}" for s in sources)
        if sources
        else "  (no numbered sources available, do not invent citation numbers)"
    )

    return f"""You are a senior intelligence analyst. Generate a concise 3-paragraph situational awareness brief based on these real-time metrics:

THREAT POSTURE: Score {m["posture_score"]}/100 ({m["risk_level"]}), {m["alerts"]} active alerts
MILITARY: {m["military_aircraft"]} tracked aircraft
CONFLICT: {m["conflicts"]} active events
SEISMIC: {m["earthquakes"]} earthquakes (max M{m["max_magnitude"]})
FIRES: {m["fire_clusters"]} active fire clusters
CYBER: {m["cyber_threats"]} tracked IOCs
SPACE WEATHER: Kp {m["kp_index"]}
HEALTH: {m["outbreaks"]} high-concern outbreaks
AIR TRAFFIC: {m["total_aircraft"]} aircraft airborne
TRAFFIC: {m["avg_congestion"]}% avg city congestion

TOP HEADLINES:
{headline_block}

NUMBERED SOURCES (cite claims inline as [n], using ONLY the numbers below; never invent a source or cite a number that is not listed here):
{source_block}

Write exactly 3 paragraphs:
1. Overall threat assessment and most significant developments
2. Regional hotspots and emerging patterns
3. Recommended watch items for the next 12 hours

Be specific, cite numbers. Cite every factual claim inline as [n] referencing the source list above. No preamble."""


def _fallback_brief(m: dict, citations: dict[str, list[int]]) -> str:
    """Generate a structured summary without LLM, cited mechanically:
    each line is assembled per-metric, so it carries exactly the
    citations backing the numbers on that line, no more and no less."""
    lines = [
        f"THREAT POSTURE: {m['risk_level'].upper()} (score {m['posture_score']}/100) with {m['alerts']} active alerts."
        f"{_citation_suffix(citations, 'posture', 'alerts')}",
        f"MILITARY: {m['military_aircraft']} aircraft tracked. CONFLICT: {m['conflicts']} active events."
        f"{_citation_suffix(citations, 'military_aircraft', 'conflicts')}",
        f"SEISMIC: {m['earthquakes']} earthquakes (max M{m['max_magnitude']}). FIRES: {m['fire_clusters']} clusters."
        f"{_citation_suffix(citations, 'earthquakes', 'fire_clusters')}",
        f"CYBER: {m['cyber_threats']} IOCs. HEALTH: {m['outbreaks']} high-concern outbreaks."
        f"{_citation_suffix(citations, 'cyber_threats', 'outbreaks')}",
        f"SPACE: Kp {m['kp_index']}. AIR TRAFFIC: {m['total_aircraft']} airborne. CONGESTION: {m['avg_congestion']}%."
        f"{_citation_suffix(citations, 'kp_index', 'total_aircraft', 'avg_congestion')}",
    ]
    return "\n".join(lines)


async def fetch_situation_brief(overview_data: dict) -> dict:
    """Generate an AI situational analysis brief from dashboard data.

    Uses local Ollama LLM to synthesize all intelligence domains into
    an actionable 3-paragraph brief.  Falls back to structured metrics
    summary when Ollama is unavailable.

    Args:
        overview_data: Full dashboard overview dict from _fetch_overview().

    Returns:
        Dict with brief text, generation metadata, key metrics, a numbered
        `sources` list traceable back to overview_data, and a `cited` flag
        that is true only when the brief text actually references one of
        those sources.
    """
    metrics, sources, citations = _extract_metrics(overview_data)
    prompt = _build_prompt(metrics, sources)

    ollama_url = os.environ.get("OLLAMA_API_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")

    brief_text = ""
    ai_generated = False
    used_model = "fallback"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 500},
                },
            )
            resp.raise_for_status()
            result = resp.json()
            brief_text = result.get("response", "").strip()
            if brief_text:
                ai_generated = True
                used_model = model
    except Exception as exc:
        logger.debug("Ollama unavailable for situation brief: %s", exception_summary(exc))

    if not brief_text:
        brief_text = _fallback_brief(metrics, citations)

    cited = _has_valid_citation(brief_text, len(sources))

    return {
        "brief": brief_text,
        "ai_generated": ai_generated,
        "model": used_model,
        "metrics_snapshot": metrics,
        "sources": sources,
        "cited": cited,
        "source": "situation-brief",
        "timestamp": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# intel_situation_brief (MCP tool): bounded server-side overview
# ---------------------------------------------------------------------------

# The domains this tool gathers itself, matched against _extract_metrics'
# keys. A bounded subset of the dashboard's full 47-source fan-out (issue
# #18): the domains that carry the most weight in the brief (threat
# posture, military, conflict, seismic/fire/cyber/health current events,
# space weather, headlines), reusing existing analysis functions unchanged
# rather than reimplementing their fan-out.
_COMPACT_OVERVIEW_DOMAINS = {
    "earthquakes": "Earthquakes",
    "military_flights": "Military flights",
    "acled_events": "Conflict events",
    "wildfires": "Wildfires",
    "cyber_threats": "Cyber threats",
    "disease_outbreaks": "Disease outbreaks",
    "news_feed": "News",
    "space_weather": "Space weather",
    "strategic_posture": "Strategic posture",
    "alert_digest": "Alert digest",
}


async def _safe_fetch(coro, label: str) -> dict:
    try:
        result = await coro
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        logger.warning("Situation brief overview: %s failed: %s", label, exception_summary(exc))
        return {"error": safe_error(exc)}


async def _gather_compact_overview(fetcher) -> dict:
    """Fetch a bounded set of domains server-side, shaped like
    `_fetch_overview()`'s output (for `_extract_metrics()` reuse), without
    importing the dashboard's Starlette app or its full 47-source fan-out.
    """
    from ..sources import (
        cyber,
        health,
        military,
        news,
        seismology,
        space_weather,
        wildfire,
    )
    from ..sources import conflict as conflict_src
    from .alerts import fetch_alert_digest
    from .posture import fetch_strategic_posture

    (
        earthquakes,
        military_flights,
        acled_events,
        wildfires,
        cyber_threats,
        disease_outbreaks,
        news_feed,
        space_weather_data,
        strategic_posture,
        alert_digest,
    ) = await asyncio.gather(
        _safe_fetch(seismology.fetch_earthquakes(fetcher), "earthquakes"),
        _safe_fetch(military.fetch_military_flights(fetcher), "military_flights"),
        _safe_fetch(conflict_src.fetch_acled_events(fetcher), "acled_events"),
        _safe_fetch(wildfire.fetch_wildfires(fetcher), "wildfires"),
        _safe_fetch(cyber.fetch_cyber_threats(fetcher), "cyber_threats"),
        _safe_fetch(health.fetch_disease_outbreaks(fetcher), "disease_outbreaks"),
        _safe_fetch(news.fetch_news_feed(fetcher), "news_feed"),
        _safe_fetch(space_weather.fetch_space_weather(fetcher), "space_weather"),
        _safe_fetch(fetch_strategic_posture(fetcher), "strategic_posture"),
        _safe_fetch(fetch_alert_digest(fetcher), "alert_digest"),
    )

    return {
        "earthquakes": earthquakes,
        "military_flights": military_flights,
        "acled_events": acled_events,
        "wildfires": wildfires,
        "cyber_threats": cyber_threats,
        "disease_outbreaks": disease_outbreaks,
        "news_feed": news_feed,
        "space_weather": space_weather_data,
        "strategic_posture": strategic_posture,
        "alert_digest": alert_digest,
    }


async def fetch_live_situation_brief(fetcher) -> dict:
    """MCP-tool entry point for `intel_situation_brief` (issue #18).

    Gathers a compact overview server-side (`_gather_compact_overview`,
    ~8-10 domains rather than the dashboard's full 47-source fan-out) and
    delegates to `fetch_situation_brief()` unchanged for the AI-generated
    brief or its mechanically-cited fallback. A domain that fails to fetch
    simply carries no citation in the result (the same honesty guarantee
    `_extract_metrics` already provides for every caller of
    `fetch_situation_brief`); it does not fail the whole tool call.

    Args:
        fetcher: Shared HTTP fetcher with caching and circuit breaking.

    Returns:
        The unmodified `fetch_situation_brief()` result: `brief`,
        `ai_generated`, `model`, `metrics_snapshot`, `sources`, `cited`,
        `source`, `timestamp`.
    """
    overview = await _gather_compact_overview(fetcher)
    return await fetch_situation_brief(overview)
