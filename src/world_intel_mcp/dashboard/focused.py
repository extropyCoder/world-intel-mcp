"""Trusted source registry and shallow dashboard-only composition.

Never calls global risk/posture/alert/weekly/situation composites. No history,
embedding/SDK imports or environment-selected destinations.
"""
import asyncio
from datetime import datetime, timezone
from importlib import import_module
from .coverage import RequestWitness
from .aircraft import display_aircraft


REGISTRY = {
    "market_quotes": ("markets", "fetch_market_quotes"),
    "crypto_quotes": ("markets", "fetch_crypto_quotes"),
    "forex_rates": ("forex", "fetch_forex_rates"),
    "commodity_quotes": ("markets", "fetch_commodity_quotes"),
    "yield_curve": ("bonds", "fetch_yield_curve"),
    "macro_signals": ("markets", "fetch_macro_signals"),
    "cyber_threats": ("cyber", "fetch_cyber_threats"),
    "service_status": ("service_status", "fetch_service_status"),
    "internet_outages": ("infrastructure", "fetch_internet_outages"),
    "cable_health": ("infrastructure", "fetch_cable_health"),
    "military_flights": ("military", "fetch_military_flights"),
    "theater_posture": ("military", "fetch_theater_posture"),
    "nav_warnings": ("maritime", "fetch_nav_warnings"),
    "acled_events": ("conflict", "fetch_acled_events"),
    "ucdp_events": ("conflict", "fetch_ucdp_events"),
    "usni_fleet": ("usni_fleet", "fetch_usni_fleet"),
}


def factories(settings, fetcher):
    def factory(name):
        module, function = REGISTRY[name]
        witness = RequestWitness(fetcher, name)
        async def call():
            source = import_module("world_intel_mcp.sources." + module)
            value = await getattr(source, function)(witness)
            if name == "military_flights":
                value = display_aircraft(value)
            return witness.finish(value)
        call.witness = witness
        return call
    return {name: factory(name) for name in settings.sources}


def usable(value):
    return (isinstance(value, dict) and value.get("_coverage", {}).get("state") == "current"
            and not any(value.get(k) for k in ("error", "_stale", "degraded")))


def compose(settings, results):
    """Only evidence already provided by selected sources; no I/O."""
    alerts, checked, unavailable = [], [], []
    if "cable" in settings.alerts and "cable_health" in settings.sources:
        checked.append("cable_health")
        cable = results.get("cable_health")
        if usable(cable) and isinstance(cable.get("corridors"), dict):
            corridors = [name for name, info in cable.get("corridors", {}).items()
                         if isinstance(info, dict) and type(info.get("status_score")) is int
                         and info["status_score"] >= settings.cable_score]
            if corridors:
                alerts.append({"id": "focused-cable", "domain": "infrastructure", "priority": "high",
                               "message": f"{len(corridors)} cable corridors at estimated warning level",
                               "corridors": corridors, "value": len(corridors), "timestamp": cable.get("timestamp")})
        else:
            unavailable.append("cable_health")
    if "cyber" in settings.alerts and "cyber_threats" in settings.sources:
        checked.append("cyber_threats")
        cyber = results.get("cyber_threats")
        if (usable(cyber) and isinstance(cyber.get("by_severity"), dict)
                and type(cyber["by_severity"].get("critical", 0)) is int):
            count = cyber.get("by_severity", {}).get("critical", 0)
            if count >= settings.cyber_critical:
                alerts.append({"id": "focused-cyber", "domain": "cyber", "priority": "high",
                               "message": f"{count} critical-severity threat indicators reported (not attacks)",
                               "value": count, "timestamp": cyber.get("timestamp"),
                               "indicators": cyber.get("threats", [])[:10]})
        else:
            unavailable.append("cyber_threats")
    return {"alerts": alerts, "alert_count": len(alerts), "domains_checked": checked,
            "unavailable_sources": unavailable, "degraded": bool(unavailable),
            "source": "focused-selected-results", "timestamp": datetime.now(timezone.utc).isoformat()}


async def snapshot(settings, fetcher, emit=None):
    semaphore = asyncio.Semaphore(4)
    async def one(name, call):
        async with semaphore:
            try:
                value = await asyncio.wait_for(call(), 45)
            except Exception:
                # No source exception text or URLs in this new boundary.
                value = {"error": "Source unavailable"}
                if hasattr(call, "witness"):
                    value = call.witness.finish(value)
            if emit is not None:
                emit(name, value)
            return name, value
    result = dict(await asyncio.gather(*(one(name, call) for name, call in factories(settings, fetcher).items())))
    result["alert_digest"] = compose(settings, result)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result


async def events(settings, fetcher):
    """Progressive selected results, with one owned/cancellable snapshot task."""
    queue = asyncio.Queue()
    task = asyncio.create_task(snapshot(settings, fetcher, lambda name, value: queue.put_nowait((name, value))))
    try:
        total = len(settings.sources)
        for done in range(total):
            name, value = await queue.get()
            yield {"_progressive": True, "_done": done + 1, "_total": total, name: value}
        result = await task
        yield {**result, "_complete": True}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
