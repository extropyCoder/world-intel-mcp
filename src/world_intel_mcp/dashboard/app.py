"""World Intelligence Dashboard — live real-time intelligence overview.

Starlette app serving a self-contained HTML dashboard with SSE streaming.
All data pulled from the same source modules used by the MCP server.
"""

import asyncio
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager, aclosing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

if not os.environ.get("WORLD_INTEL_CONFIG"):
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher
from world_intel_mcp.dashboard.settings import from_environment
from world_intel_mcp.dashboard import focused

_settings = from_environment()  # operator-selected once; never selected by a request
from world_intel_mcp.diagnostics import exception_summary, safe_error, sanitize_error_fields
if _settings is None:
    # Legacy composites can initialize temporal storage during import. A focused
    # process must not import them, even if it never calls their fetch functions.
    from world_intel_mcp.analysis.aoi import AOIStore
    from world_intel_mcp.sources import (
        markets, seismology, military, infrastructure, maritime, economic,
        wildfire, cyber, news, prediction, displacement, aviation, climate,
        conflict, intelligence, space_weather, ai_watch, health, elections,
        shipping, social, nuclear, service_status, traffic, migration,
    )
    from world_intel_mcp.analysis.alerts import fetch_alert_digest, fetch_weekly_trends
    from world_intel_mcp.analysis.posture import fetch_strategic_posture
    from world_intel_mcp.analysis.exposure import fetch_population_exposure
    from world_intel_mcp.analysis.situation import fetch_situation_brief
    from world_intel_mcp.sources.fleet import fetch_fleet_report
    from world_intel_mcp.sources.usni_fleet import fetch_usni_fleet
    from world_intel_mcp.sources.central_banks import fetch_central_bank_rates
from world_intel_mcp.config.countries import (
    INTEL_HOTSPOTS,
    STRATEGIC_WATERWAYS,
    TIER1_COUNTRIES,
)
from world_intel_mcp.config.geospatial import (
    MILITARY_BASES,
    STRATEGIC_PORTS,
    PIPELINES,
    NUCLEAR_FACILITIES,
)
from world_intel_mcp.sources.infrastructure import CABLE_CORRIDORS
from world_intel_mcp.config.trade_routes import (
    TRADE_ROUTES,
    CLOUD_REGIONS,
    FINANCIAL_CENTERS,
)

logger = logging.getLogger(__name__)


def _loopback_authority(value: str) -> tuple[str, int | None] | None:
    """Parse only the two supported browser hostnames, not general URLs."""
    match = re.fullmatch(r"(localhost|127\.0\.0\.1)(?::([0-9]+))?", value, re.IGNORECASE)
    if match is None:
        return None
    hostname, port = match.groups()
    if port is not None:
        # Bound conversion even for adversarially long digit strings.
        port = port.lstrip("0")
        if not port or len(port) > 5 or not 1 <= int(port) <= 65535:
            return None
        return hostname.lower(), int(port)
    return hostname.lower(), None


class BrowserIsolationMiddleware:
    """Reject DNS rebinding and cross-origin browser requests before routing.

    This is browser isolation, not authentication of local processes. Only raw
    Host/Origin and the transport scheme are authoritative; never forwarded
    headers. Run the ASGI server without proxy-header rewriting as well.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == b"host"]
        host = _loopback_authority(hosts[0]) if len(hosts) == 1 else None
        if host is None:
            await JSONResponse({"error": "Invalid Host"}, status_code=400)(scope, receive, send)
            return
        origins = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == b"origin"]
        if origins:
            origin = re.fullmatch(r"(https?)://([^/\s?#]+)", origins[0], re.IGNORECASE)
            authority = _loopback_authority(origin[2]) if origin else None
            scheme = scope["scheme"]
            default_port = {"http": 80, "https": 443}.get(scheme)
            if (
                len(origins) != 1
                or origin is None
                or authority is None
                or origin[1].lower() != scheme
                or (authority[0], authority[1] or default_port)
                != (host[0], host[1] or default_port)
            ):
                await JSONResponse({"error": "Invalid Origin"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

# ---------------------------------------------------------------------------
# Shared infrastructure — lazily initialised
# ---------------------------------------------------------------------------
_fetcher: Fetcher | None = None
_cache: Cache | None = None
_breaker: CircuitBreaker | None = None
_vector_store = None


def _ensure_fetcher() -> Fetcher:
    global _fetcher, _cache, _breaker, _vector_store
    if _fetcher is None:
        _cache = Cache()
        _breaker = CircuitBreaker()
        # Vector store — optional, dashboard populates it alongside the MCP server
        try:
            if _settings is not None:
                raise ImportError("Focused dashboard does not use vector storage")
            from world_intel_mcp.vector_store import VectorStore

            _vector_store = VectorStore(enabled=True)
        except Exception:
            logger.info("Vector store unavailable in dashboard mode")
        _fetcher = Fetcher(cache=_cache, breaker=_breaker, vector_store=_vector_store)
    return _fetcher


def _open_aoi_store() -> "AOIStore":
    """Open the AOI store on the same cache database the MCP server and
    CLI write geofences to, so the map shows exactly the areas a user
    defined over MCP.

    Opened per request and closed by the caller, never cached: SQLite
    connections are bound to the thread that created them, and a
    module-level store built on one thread and read from another (the
    test client's portal thread; any ``to_thread`` path) raises. A
    fresh connection per read is cheap and thread-safe by construction,
    the same pattern ``analysis.aoi.fetch_aoi_sweep`` uses."""
    from world_intel_mcp.analysis.aoi import AOIStore
    return AOIStore(_ensure_fetcher().cache.db_path)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _conflict_zones_fallback(acled: dict | None, ucdp: dict | None) -> dict | None:
    """Static hotspot events for the conflict layer when neither ACLED nor
    UCDP has data (error or zero events), else ``None``.

    Shared by ``/api/overview`` and ``/api/stream`` so both deliver the
    same fallback; before it was shared, the stream never sent one and the
    page's conflict layer and drawer section stayed empty."""
    acled = acled or {}
    ucdp = ucdp or {}
    acled_ok = not acled.get("error") and (acled.get("count") or 0) > 0
    ucdp_ok = not ucdp.get("error") and (ucdp.get("count") or 0) > 0
    if acled_ok or ucdp_ok:
        return None
    escalation_labels = {
        1: "low",
        2: "low",
        3: "moderate",
        4: "high",
        5: "critical",
    }
    # Map ISO codes to country names for richer display
    _iso_names = {c: info["name"] for c, info in TIER1_COUNTRIES.items()}
    hotspot_events = []
    for name, h in INTEL_HOTSPOTS.items():
        assoc = h.get("associated_countries", [])
        country_name = _iso_names.get(assoc[0], "") if assoc else ""
        actors = [_iso_names.get(c, c) for c in assoc]
        esc = h["baseline_escalation"]
        hotspot_events.append(
            {
                "latitude": h["lat"],
                "longitude": h["lon"],
                "country": country_name or name.replace("_", " ").title(),
                "location": name.replace("_", " ").title(),
                "event_type": "Active Hotspot",
                "type_of_violence_label": "active hotspot",
                "fatalities": 0,
                "best": 0,
                "escalation": esc,
                "severity": escalation_labels.get(esc, "unknown"),
                "event_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "actor1": actors[0] if len(actors) > 0 else "\u2014",
                "actor2": actors[1] if len(actors) > 1 else "\u2014",
                "source": "Intel Hotspot Database",
                "notes": f"Escalation level {esc}/5 ({escalation_labels.get(esc, 'unknown')}). "
                f"Monitored hotspot involving {', '.join(actors)}.",
                "associated_countries": assoc,
            }
        )
    return {
        "events": hotspot_events,
        "count": len(hotspot_events),
        "source": "intel-hotspots",
    }


async def _fetch_overview() -> dict:
    """Fetch all dashboard domains in parallel, return unified dict."""
    if _settings is not None:
        return sanitize_error_fields(await focused.snapshot(_settings, _ensure_fetcher()))
    fetcher = _ensure_fetcher()

    coros = {
        "market_quotes": markets.fetch_market_quotes(fetcher),
        "crypto_quotes": markets.fetch_crypto_quotes(fetcher),
        "macro_signals": markets.fetch_macro_signals(fetcher),
        "sector_heatmap": markets.fetch_sector_heatmap(fetcher),
        "earthquakes": seismology.fetch_earthquakes(fetcher),
        "military_flights": military.fetch_military_flights(fetcher),
        "cyber_threats": cyber.fetch_cyber_threats(fetcher),
        "news_feed": news.fetch_news_feed(fetcher),
        "trending_keywords": news.fetch_trending_keywords(fetcher),
        "nav_warnings": maritime.fetch_nav_warnings(fetcher),
        "internet_outages": infrastructure.fetch_internet_outages(fetcher),
        "cable_health": infrastructure.fetch_cable_health(fetcher),
        "wildfires": wildfire.fetch_wildfires(fetcher),
        "prediction_markets": prediction.fetch_prediction_markets(fetcher),
        "airport_delays": aviation.fetch_airport_delays(fetcher),
        "climate_anomalies": climate.fetch_climate_anomalies(fetcher),
        "energy_prices": economic.fetch_energy_prices(fetcher),
        "gas_prices": economic.fetch_gas_prices(fetcher),
        "residential_natgas": economic.fetch_residential_natgas_prices(fetcher),
        "electricity_rates": economic.fetch_electricity_rates(fetcher),
        "stablecoin_status": markets.fetch_stablecoin_status(fetcher),
        "etf_flows": markets.fetch_etf_flows(fetcher),
        "acled_events": conflict.fetch_acled_events(fetcher),
        "ucdp_events": conflict.fetch_ucdp_events(fetcher),
        "displacement": displacement.fetch_displacement_summary(fetcher),
        "risk_scores": intelligence.fetch_risk_scores(fetcher),
        "space_weather": space_weather.fetch_space_weather(fetcher),
        "ai_watch": ai_watch.fetch_ai_watch(fetcher),
        "disease_outbreaks": health.fetch_disease_outbreaks(fetcher),
        "election_calendar": elections.fetch_election_calendar(fetcher),
        "shipping_index": shipping.fetch_shipping_index(fetcher),
        "social_signals": social.fetch_social_signals(fetcher),
        "nuclear_monitor": nuclear.fetch_nuclear_monitor(fetcher),
        "alert_digest": fetch_alert_digest(fetcher),
        "weekly_trends": fetch_weekly_trends(fetcher),
        "service_status": service_status.fetch_service_status(fetcher),
        "strategic_posture": fetch_strategic_posture(fetcher),
        "fleet_report": fetch_fleet_report(fetcher),
        "usni_fleet": fetch_usni_fleet(fetcher),
        "population_exposure": fetch_population_exposure(fetcher),
        "domestic_flights": aviation.fetch_domestic_flights(fetcher),
        "traffic_flow": traffic.fetch_traffic_flow(fetcher),
        "traffic_incidents": traffic.fetch_traffic_incidents(fetcher),
        "btc_technicals": markets.fetch_btc_technicals(fetcher),
        "central_bank_rates": fetch_central_bank_rates(fetcher),
    }

    # Per-coro timeout so no single slow source blocks the entire dashboard.
    # news_feed and trending_keywords get extra time (119 RSS feeds with semaphore).
    _SLOW_SOURCES = {
        "news_feed",
        "trending_keywords",
        "alert_digest",
        "weekly_trends",
        "strategic_posture",
    }

    async def _with_timeout(name: str, coro, timeout: float = 45.0):
        effective = 90.0 if name in _SLOW_SOURCES else timeout
        try:
            return await asyncio.wait_for(coro, timeout=effective)
        except asyncio.TimeoutError:
            logger.warning("Dashboard fetch %s timed out after %.0fs", name, effective)
            return {"error": f"timeout after {effective}s", "_timeout": True}

    gathered = await asyncio.gather(
        *[_with_timeout(name, c) for name, c in zip(coros.keys(), coros.values())],
        return_exceptions=True,
    )

    result: dict = {}
    for name, data in zip(coros.keys(), gathered):
        if isinstance(data, Exception):
            logger.warning("Dashboard fetch %s failed: %s", name, exception_summary(data))
            result[name] = {"error": exception_summary(data)[:120]}
        else:
            result[name] = data

    # Conflict zone fallback: when both ACLED and UCDP fail, provide
    # static hotspot data so the conflict layer is never empty.
    zones = _conflict_zones_fallback(
        result.get("acled_events", {}), result.get("ucdp_events", {})
    )
    if zones is not None:
        result["conflict_zones"] = zones

    # Static geospatial datasets (no API calls)
    result["military_bases"] = {"bases": MILITARY_BASES, "count": len(MILITARY_BASES)}
    result["strategic_ports"] = {
        "ports": STRATEGIC_PORTS,
        "count": len(STRATEGIC_PORTS),
    }
    result["pipelines"] = {"pipelines": PIPELINES, "count": len(PIPELINES)}
    result["nuclear_facilities"] = {
        "facilities": NUCLEAR_FACILITIES,
        "count": len(NUCLEAR_FACILITIES),
    }
    result["waterways"] = {
        "waterways": STRATEGIC_WATERWAYS,
        "count": len(STRATEGIC_WATERWAYS),
    }
    result["trade_routes"] = {"routes": TRADE_ROUTES, "count": len(TRADE_ROUTES)}
    result["cloud_regions"] = {"regions": CLOUD_REGIONS, "count": len(CLOUD_REGIONS)}
    result["financial_centers"] = {
        "centers": FINANCIAL_CENTERS,
        "count": len(FINANCIAL_CENTERS),
    }
    result["cable_corridors"] = {
        "corridors": [
            {
                "name": n,
                "lat_range": c["lat_range"],
                "lon_range": c["lon_range"],
                "cables": c["cables"],
            }
            for n, c in CABLE_CORRIDORS.items()
        ],
        "count": len(CABLE_CORRIDORS),
    }

    # AI situational brief (runs after main gather so it has all data)
    try:
        result["situation_brief"] = await asyncio.wait_for(
            fetch_situation_brief(result),
            timeout=35.0,
        )
    except Exception as exc:
        logger.warning("Situation brief failed: %s", exception_summary(exc))
        result["situation_brief"] = {"error": safe_error(exc)}

    # Attach source health + timestamp
    result["source_health"] = _breaker.status() if _breaker else {}
    result["cache_stats"] = _cache.stats() if _cache else {}
    result["cache_freshness"] = _cache.freshness() if _cache else {}
    result["timestamp"] = datetime.now(timezone.utc).isoformat()

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def index(request):
    """Serve the dashboard HTML page (reloads on each request during dev)."""
    html_path = Path(__file__).parent / "index.html"
    html = html_path.read_text()
    metadata = json.dumps(_settings.public()).replace("<", "\\u003c") if _settings else "null"
    return HTMLResponse(html.replace("var dashboardProfile = null;", "var dashboardProfile = " + metadata + ";"))


async def api_profile(request):
    return JSONResponse(_settings.public() if _settings else {"focused": False})


async def api_overview(request):
    """REST endpoint — full snapshot of all intelligence domains."""
    data = await _fetch_overview()
    return JSONResponse(sanitize_error_fields(data))


_CONFLICT_FEEDS = ("acled_events", "ucdp_events")


def _stream_source_defs(fetcher: Fetcher) -> dict:
    """Source name -> zero-arg coroutine factory for one /api/stream tick."""
    if _settings is not None:
        return focused.factories(_settings, fetcher)
    return {
        "market_quotes": lambda: markets.fetch_market_quotes(fetcher),
        "crypto_quotes": lambda: markets.fetch_crypto_quotes(fetcher),
        "macro_signals": lambda: markets.fetch_macro_signals(fetcher),
        "sector_heatmap": lambda: markets.fetch_sector_heatmap(fetcher),
        "earthquakes": lambda: seismology.fetch_earthquakes(fetcher),
        "military_flights": lambda: military.fetch_military_flights(
            fetcher
        ),
        "cyber_threats": lambda: cyber.fetch_cyber_threats(fetcher),
        "news_feed": lambda: news.fetch_news_feed(fetcher),
        "trending_keywords": lambda: news.fetch_trending_keywords(fetcher),
        "nav_warnings": lambda: maritime.fetch_nav_warnings(fetcher),
        "internet_outages": lambda: infrastructure.fetch_internet_outages(
            fetcher
        ),
        "cable_health": lambda: infrastructure.fetch_cable_health(fetcher),
        "wildfires": lambda: wildfire.fetch_wildfires(fetcher),
        "prediction_markets": lambda: prediction.fetch_prediction_markets(
            fetcher
        ),
        "airport_delays": lambda: aviation.fetch_airport_delays(fetcher),
        "climate_anomalies": lambda: climate.fetch_climate_anomalies(
            fetcher
        ),
        "energy_prices": lambda: economic.fetch_energy_prices(fetcher),
        "gas_prices": lambda: economic.fetch_gas_prices(fetcher),
        "residential_natgas": lambda: economic.fetch_residential_natgas_prices(
            fetcher
        ),
        "electricity_rates": lambda: economic.fetch_electricity_rates(
            fetcher
        ),
        "stablecoin_status": lambda: markets.fetch_stablecoin_status(
            fetcher
        ),
        "etf_flows": lambda: markets.fetch_etf_flows(fetcher),
        "acled_events": lambda: conflict.fetch_acled_events(fetcher),
        "ucdp_events": lambda: conflict.fetch_ucdp_events(fetcher),
        "displacement": lambda: displacement.fetch_displacement_summary(
            fetcher
        ),
        "risk_scores": lambda: intelligence.fetch_risk_scores(fetcher),
        "space_weather": lambda: space_weather.fetch_space_weather(fetcher),
        "ai_watch": lambda: ai_watch.fetch_ai_watch(fetcher),
        "disease_outbreaks": lambda: health.fetch_disease_outbreaks(
            fetcher
        ),
        "election_calendar": lambda: elections.fetch_election_calendar(
            fetcher
        ),
        "shipping_index": lambda: shipping.fetch_shipping_index(fetcher),
        "social_signals": lambda: social.fetch_social_signals(fetcher),
        "nuclear_monitor": lambda: nuclear.fetch_nuclear_monitor(fetcher),
        "alert_digest": lambda: fetch_alert_digest(fetcher),
        "weekly_trends": lambda: fetch_weekly_trends(fetcher),
        "service_status": lambda: service_status.fetch_service_status(
            fetcher
        ),
        "strategic_posture": lambda: fetch_strategic_posture(fetcher),
        "fleet_report": lambda: fetch_fleet_report(fetcher),
        "usni_fleet": lambda: fetch_usni_fleet(fetcher),
        "population_exposure": lambda: fetch_population_exposure(fetcher),
        "domestic_flights": lambda: aviation.fetch_domestic_flights(
            fetcher
        ),
        "traffic_flow": lambda: traffic.fetch_traffic_flow(fetcher),
        "traffic_incidents": lambda: traffic.fetch_traffic_incidents(
            fetcher
        ),
        "btc_technicals": lambda: markets.fetch_btc_technicals(fetcher),
        "central_bank_rates": lambda: fetch_central_bank_rates(fetcher),
    }


async def api_stream(request):
    """SSE endpoint — progressive: emits each source as it completes."""

    async def event_generator():
        while True:
            try:
                fetcher = _ensure_fetcher()

                if _settings is not None:
                    # One shared bounded snapshot; no global composites, no
                    # orphan per-source tasks on disconnect/cancellation.
                    async with aclosing(focused.events(_settings, fetcher)) as events:
                        async for result in events:
                            result = sanitize_error_fields(result)
                            yield f"data: {json.dumps(result, default=str)}\n\n"
                    await asyncio.sleep(_settings.refresh_seconds)
                    continue

                # Sources to fetch, grouped so fast ones arrive first
                source_defs = _stream_source_defs(fetcher)

                _SLOW = {
                    "news_feed",
                    "trending_keywords",
                    "alert_digest",
                    "weekly_trends",
                    "strategic_posture",
                }
                results_q: asyncio.Queue = asyncio.Queue()

                async def _run_source(name: str, factory):
                    timeout = 90.0 if name in _SLOW else 45.0
                    try:
                        data = await asyncio.wait_for(factory(), timeout=timeout)
                    except asyncio.TimeoutError:
                        data = {"error": f"timeout after {timeout}s", "_timeout": True}
                    except Exception as exc:
                        data = {"error": exception_summary(exc)[:120]}
                    await results_q.put((name, sanitize_error_fields(data)))

                # Fire all sources concurrently
                tasks = [
                    asyncio.create_task(_run_source(name, factory))
                    for name, factory in source_defs.items()
                ]
                total = len(tasks)
                completed = 0
                conflict_seen: dict = {}

                # Yield each result as it arrives
                while completed < total:
                    try:
                        name, data = await asyncio.wait_for(
                            results_q.get(), timeout=120
                        )
                    except asyncio.TimeoutError:
                        break
                    completed += 1
                    payload = json.dumps(
                        {
                            "_progressive": True,
                            "_done": completed,
                            "_total": total,
                            name: data,
                        },
                        default=str,
                    )
                    yield f"data: {payload}\n\n"
                    # Once both conflict feeds are in, send the same fallback
                    # /api/overview builds (or null, so a recovered feed clears
                    # a stale fallback in the page's accumulated state).
                    if name in _CONFLICT_FEEDS:
                        conflict_seen[name] = data
                        if len(conflict_seen) == len(_CONFLICT_FEEDS):
                            zones = _conflict_zones_fallback(
                                conflict_seen["acled_events"],
                                conflict_seen["ucdp_events"],
                            )
                            payload = json.dumps(
                                {
                                    "_progressive": True,
                                    "_done": completed,
                                    "_total": total,
                                    "conflict_zones": zones,
                                },
                                default=str,
                            )
                            yield f"data: {payload}\n\n"

                # Final event with metadata
                meta = {
                    "_progressive": False,
                    "_complete": True,
                    "source_health": _breaker.status() if _breaker else {},
                    "cache_stats": _cache.stats() if _cache else {},
                    "cache_freshness": _cache.freshness() if _cache else {},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                yield f"data: {json.dumps(meta, default=str)}\n\n"

                # Clean up
                for t in tasks:
                    if not t.done():
                        t.cancel()

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("SSE tick failed: %s", exception_summary(exc))
                yield f"data: {json.dumps({'error': safe_error(exc)})}\n\n"
            await asyncio.sleep(30)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def api_static(request):
    """Return static geospatial datasets instantly (no API calls).

    The dashboard fetches this on boot so the infrastructure layer
    populates immediately without waiting for the full SSE gather.
    """
    return JSONResponse(
        {
            "military_bases": {"bases": MILITARY_BASES, "count": len(MILITARY_BASES)},
            "strategic_ports": {
                "ports": STRATEGIC_PORTS,
                "count": len(STRATEGIC_PORTS),
            },
            "pipelines": {"pipelines": PIPELINES, "count": len(PIPELINES)},
            "nuclear_facilities": {
                "facilities": NUCLEAR_FACILITIES,
                "count": len(NUCLEAR_FACILITIES),
            },
            "waterways": {
                "waterways": STRATEGIC_WATERWAYS,
                "count": len(STRATEGIC_WATERWAYS),
            },
            "cable_corridors": {
                "corridors": [
                    {
                        "name": n,
                        "lat_range": c["lat_range"],
                        "lon_range": c["lon_range"],
                        "cables": c["cables"],
                    }
                    for n, c in CABLE_CORRIDORS.items()
                ],
                "count": len(CABLE_CORRIDORS),
            },
            "trade_routes": {"routes": TRADE_ROUTES, "count": len(TRADE_ROUTES)},
            "cloud_regions": {"regions": CLOUD_REGIONS, "count": len(CLOUD_REGIONS)},
            "financial_centers": {
                "centers": FINANCIAL_CENTERS,
                "count": len(FINANCIAL_CENTERS),
            },
        },
    )


async def api_aois(request):
    """User-defined AOI geofences for the map layer, each with what the
    last collector sweep counted inside it.

    Reads the stored change snapshot rather than gathering live: drawing
    a shape must never cost the 60-90 s of upstream fetches a brief
    does. An AOI that has never been swept reports ``last_sweep: null``
    rather than a row of zeros, and a broken store is a 503 with the
    reason, never an empty list that reads as "no areas defined".
    """
    try:
        store = _open_aoi_store()
        try:
            aois = []
            for row in store.list_all():
                snap = store.get_snapshot(row["name"])
                last_sweep = None
                if snap is not None:
                    counts = {
                        domain: len(items or {})
                        for domain, items in (snap.get("domains") or {}).items()
                    }
                    last_sweep = {
                        "taken_at": snap.get("taken_at"),
                        "counts": counts,
                        "total": sum(counts.values()),
                    }
                aois.append({**row, "last_sweep": last_sweep})
        finally:
            store.close()
    except Exception as exc:
        logger.warning("AOI layer unavailable: %s", exception_summary(exc))
        return JSONResponse(
            {"error": f"AOI store unavailable: {safe_error(exc)}", "aois": [], "count": 0},
            status_code=503,
        )
    return JSONResponse(
        {"aois": aois, "count": len(aois), "source": "aoi-store"}
    )


_MIGRATION_DEFAULT_LIMIT = 40
_MIGRATION_MAX_LIMIT = 150
_MIGRATION_RISING_LIMIT = 15


def _capital_point(iso3: str, wb: dict[str, dict]) -> dict | None:
    """The country's capital as a map anchor, from the World Bank country list.
    None for UNHCR's non-country buckets (Unknown, Stateless), for aggregates and
    for places the World Bank gives no capital coordinates."""
    c = wb.get(iso3)
    if not c or c.get("aggregate") or c.get("latitude") is None or c.get("longitude") is None:
        return None
    return {"lat": c["latitude"], "lon": c["longitude"], "capital": c.get("capital")}


def _place_corridors(corridors: list[dict], wb: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Split corridors into (plotted, unplotted). A plotted corridor gains
    ``origin_point`` / ``destination_point``; an unplotted one is summarised with
    the endpoints that have no coordinates, so nothing is dropped silently."""
    plotted, unplotted = [], []
    for c in corridors:
        o = _capital_point(c["origin_iso3"], wb)
        d = _capital_point(c["destination_iso3"], wb)
        if o and d:
            plotted.append({**c, "origin_point": o, "destination_point": d})
            continue
        unplotted.append({
            "origin": c["origin"], "origin_iso3": c["origin_iso3"],
            "destination": c["destination"], "destination_iso3": c["destination_iso3"],
            "people": c["people"],
            "no_coordinates": [side for side, pt in (("origin", o), ("destination", d)) if pt is None],
        })
    return plotted, unplotted


async def api_migration(request):
    """Largest origin -> destination migration corridors (UNHCR) anchored on
    capital-city coordinates (World Bank), plus the fastest-rising corridors.

    ``?limit=`` (1-150, default 40) is how many of the largest corridors to
    examine; ``?year=`` pins the UNHCR year. Corridors whose endpoint has no
    coordinates are counted in ``unplotted`` and listed in ``unplotted_corridors``.
    An upstream failure is a 503 carrying ``error``, never an empty corridor
    list, so the map cannot show an outage as a quiet world. The rising list is
    secondary: if only it fails, the largest corridors are still returned with
    ``rising_error`` set.
    """
    from world_intel_mcp.sources import migration  # manual endpoint only
    try:
        limit = int(request.query_params.get("limit", _MIGRATION_DEFAULT_LIMIT))
        year_param = request.query_params.get("year")
        year = int(year_param) if year_param else None
    except ValueError:
        return JSONResponse({"error": "limit and year must be integers"}, status_code=400)
    if not 1 <= limit <= _MIGRATION_MAX_LIMIT:
        return JSONResponse(
            {"error": f"limit must be between 1 and {_MIGRATION_MAX_LIMIT}"}, status_code=400
        )

    fetcher = _ensure_fetcher()
    try:
        flows, surges, wb = await asyncio.gather(
            migration.fetch_migration_flows(fetcher, year=year, limit=limit),
            migration.fetch_migration_surges(fetcher, year=year, limit=_MIGRATION_RISING_LIMIT),
            migration.wb_countries(fetcher),
        )
    except Exception as exc:
        logger.warning("Migration layer unavailable: %s", exception_summary(exc))
        return JSONResponse(
            {"error": f"Migration fetch failed: {safe_error(exc)}", "corridors": [], "rising": []},
            status_code=503,
        )
    if "error" in flows:
        return JSONResponse(sanitize_error_fields({**flows, "corridors": [], "rising": []}), status_code=503)
    if wb is None:
        return JSONResponse(
            {"error": "World Bank country coordinates unavailable", "source": "worldbank",
             "corridors": [], "rising": []},
            status_code=503,
        )

    plotted, unplotted = _place_corridors(flows["corridors"], wb)
    payload = {
        "year": flows["year"],
        "limit": limit,
        "corridors": plotted,
        "plotted": len(plotted),
        "unplotted": len(unplotted),
        "unplotted_corridors": unplotted,
        "corridor_count": flows["corridor_count"],
        "prior_year_available": flows["prior_year_available"],
        "anchor": "capital city (World Bank country list); a corridor is drawn capital to capital, "
                  "not along a route",
        "measure": flows["measure"],
        "source": "unhcr+worldbank",
        "timestamp": flows["timestamp"],
    }
    if "error" in surges:
        payload.update(rising=[], rising_unplotted=0,
                       rising_error=sanitize_error_fields({"error": surges["error"]})["error"])
    else:
        rising, rising_unplotted = _place_corridors(surges["corridor_increases"], wb)
        payload.update(rising=rising, rising_unplotted=len(rising_unplotted),
                       compared_with=surges["compared_with"])
    return JSONResponse(payload)


async def api_health(request):
    """Health check."""
    return JSONResponse({"status": "ok"})


async def api_report_pdf(request):
    """PDF report generation (removed — use the live dashboard instead)."""
    return JSONResponse(
        {"error": "PDF reports removed — use the live dashboard at /"},
        status_code=410,
    )


async def sse_vector_analytics(request):
    """SSE endpoint — pushes vector store analytics every 30 seconds."""

    async def event_generator():
        while True:
            try:
                payload = sanitize_error_fields(await _fetch_vector_analytics())
                yield f"data: {json.dumps(payload, default=str)}\n\n"
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Vector analytics SSE tick failed: %s", exception_summary(exc))
                yield f"data: {json.dumps({'error': safe_error(exc)})}\n\n"
            await asyncio.sleep(30)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _fetch_vector_analytics() -> dict:
    """Gather vector store stats, domain summary, and trend detection."""
    if _settings is not None:
        return {"available": False}
    _ensure_fetcher()
    if _vector_store is None:
        return {
            "available": False,
            "error": "Vector store not available (Qdrant not running)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    stats_coro = _vector_store.collection_stats()
    domain_coro = _vector_store.domain_summary(hours=24.0)
    trend_coro = _vector_store.trend_detection(recent_hours=6.0, baseline_hours=48.0)

    gathered = await asyncio.gather(
        stats_coro, domain_coro, trend_coro, return_exceptions=True
    )

    result: dict = {"available": True}
    names = ("stats", "domain_summary", "trends")
    for name, data in zip(names, gathered):
        if isinstance(data, Exception):
            logger.warning("Vector analytics %s failed: %s", name, exception_summary(data))
            result[name] = {"error": safe_error(data)}
        else:
            result[name] = data
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result


_VECTOR_SEARCH_MAX_BODY_BYTES = 16 * 1024
_VECTOR_SEARCH_MAX_QUERY_CHARS = 2048
_VECTOR_SEARCH_MAX_CATEGORY_CHARS = 128
_VECTOR_SEARCH_MAX_HOURS = 8760


def _vector_search_json_constant(value):
    raise ValueError("Nonfinite JSON numbers are not allowed")


def _vector_search_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON numbers are not allowed")
    return number


def _validate_vector_search(body):
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if len(query) > _VECTOR_SEARCH_MAX_QUERY_CHARS:
        raise ValueError(f"query must not exceed {_VECTOR_SEARCH_MAX_QUERY_CHARS} characters")

    limit = body.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    hours = body.get("hours")
    if hours is not None and (
        type(hours) not in (int, float) or not 0 < hours <= _VECTOR_SEARCH_MAX_HOURS
    ):
        raise ValueError(f"hours must be a number greater than 0 and at most {_VECTOR_SEARCH_MAX_HOURS}")
    category = body.get("category")
    if category is not None and (
        not isinstance(category, str) or len(category) > _VECTOR_SEARCH_MAX_CATEGORY_CHARS
    ):
        raise ValueError(f"category must be a string of at most {_VECTOR_SEARCH_MAX_CATEGORY_CHARS} characters")
    return {"query": query.strip(), "limit": limit, "category": category,
            "hours": float(hours) if hours is not None else None}


async def api_vector_search(request):
    """Validate a bounded POST body before initializing semantic search."""
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _VECTOR_SEARCH_MAX_BODY_BYTES:
                return JSONResponse({"error": "Request body too large"}, status_code=413)
            raw.extend(chunk)
        body = json.loads(
            raw, parse_constant=_vector_search_json_constant,
            parse_float=_vector_search_json_float,
        )
    except (ValueError, UnicodeError, RecursionError, ClientDisconnect):
        return JSONResponse(
            {"error": "Invalid JSON body"},
            status_code=400,
        )
    try:
        arguments = _validate_vector_search(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        _ensure_fetcher()
        if _vector_store is None:
            return JSONResponse(
                {"error": "Vector store not available (Qdrant not running)"},
                status_code=503,
            )
        results = await _vector_store.semantic_search(**arguments)
        if isinstance(results, dict) and "error" in results:
            logger.error("Vector search failed: %s", sanitize_error_fields({"error": results["error"]})["error"])
            return JSONResponse({"error": "Vector search failed"}, status_code=500)
        return JSONResponse(results)
    except Exception as exc:
        logger.error("Vector search failed: %s", exception_summary(exc))
        return JSONResponse(
            {"error": "Vector search failed"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app):
    """Start/stop vector store worker alongside the app."""
    global _fetcher, _cache, _breaker
    _ensure_fetcher()
    if _vector_store:
        await _vector_store.start()
        logger.info("Dashboard vector store worker started")
    try:
        yield
    finally:
        if _vector_store:
            await _vector_store.stop()
            logger.info("Dashboard vector store worker stopped")
        if _settings is not None:
            try:
                if _fetcher is not None:
                    await _fetcher.close()
            finally:
                if _cache is not None:
                    _cache.close()
                _fetcher = _cache = _breaker = None


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/profile", api_profile),
        Route("/api/overview", api_overview),
        Route("/api/stream", api_stream),
        Route("/api/static", api_static),
        Route("/api/aois", api_aois),
        Route("/api/migration", api_migration),
        Route("/api/health", api_health),
        Route("/api/report/pdf", api_report_pdf),
        Route("/sse/vector-analytics", sse_vector_analytics),
        Route("/api/vector-search", api_vector_search, methods=["POST"]),
    ],
    lifespan=lifespan,
    middleware=[Middleware(BrowserIsolationMiddleware)],
)


def _parse_run_args(argv: list[str] | None = None) -> tuple[str, int]:
    """Parse dashboard CLI args for the console script and module runner."""
    import argparse
    import os

    default_port = int(
        os.environ.get(
            "WORLD_INTEL_DASHBOARD_PORT",
            os.environ.get("PORT", str(_settings.port) if _settings else "8501"),
        )
    )
    parser = argparse.ArgumentParser(description="Run the World Intelligence dashboard")
    parser.add_argument(
        "--host",
        default=os.environ.get("WORLD_INTEL_DASHBOARD_HOST", "127.0.0.1"),
        help="Interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Port to bind (default: 8501, or WORLD_INTEL_DASHBOARD_PORT/PORT)",
    )
    args = parser.parse_args(argv)
    return args.host, args.port


def run(host: str | None = None, port: int | None = None) -> None:
    """Launch the dashboard server."""
    import uvicorn

    if host is None or port is None:
        parsed_host, parsed_port = _parse_run_args()
        host = host or parsed_host
        port = parsed_port if port is None else port

    logger.info("Starting Intelligence Dashboard on http://%s:%d", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    run()
