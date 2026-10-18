"""Dashboard-only, versioned operator configuration. No destinations or secrets.

An absent explicit path retains the legacy dashboard. The launcher always
supplies a private copy of dashboard.example.toml. MCP APIs never read this file.
"""
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import tomllib


GROUPS = {
    "markets": ("market_quotes", "crypto_quotes", "forex_rates", "commodity_quotes", "yield_curve", "macro_signals"),
    "cyber": ("cyber_threats", "service_status", "internet_outages", "cable_health"),
    "military": ("military_flights", "theater_posture", "nav_warnings", "acled_events", "ucdp_events", "usni_fleet"),
}


class ConfigError(ValueError):
    pass


def keys(table, allowed):
    if type(table) is not dict or set(table) - set(allowed):
        raise ConfigError("Unknown configuration key or invalid table")


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError("Invalid integer/range in dashboard configuration")
    return value


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ConfigError("Invalid finite number/range in dashboard configuration")
    return value


def boolean(value):
    if type(value) is not bool:
        raise ConfigError("Expected a boolean in dashboard configuration")
    return value


def names(value, allowed):
    if type(value) is not list or any(type(v) is not str or v not in allowed for v in value) or len(set(value)) != len(value):
        raise ConfigError("Invalid or duplicate dashboard selection")
    return tuple(value)


@dataclass(frozen=True)
class Settings:
    groups: tuple
    sources: tuple
    refresh_seconds: int
    port: int
    center: tuple
    zoom: float
    sections: tuple
    counters: tuple
    static_map: bool
    aois: bool
    alerts: tuple
    cable_score: int
    cyber_critical: int

    def public(self):
        layers = []
        if "military_flights" in self.sources: layers.append("military")
        if "acled_events" in self.sources or "ucdp_events" in self.sources: layers.append("conflict")
        if "cable_health" in self.sources or self.static_map: layers.append("infra")
        if "nav_warnings" in self.sources: layers.append("navwarnings")
        if self.aois: layers.append("aoi")
        return {"focused": True, "groups": self.groups, "sources": self.sources,
                "cache_key": hashlib.sha256(repr(self).encode()).hexdigest()[:24],
                "group_sources": {g: [s for s in GROUPS[g] if s in self.sources] for g in self.groups},
                "refresh_seconds": self.refresh_seconds, "center": self.center, "zoom": self.zoom,
                "sections": self.sections, "counters": self.counters, "layers": layers,
                "static_map": self.static_map, "aois": self.aois, "vector": False,
                "alerts": bool(self.alerts)}


def parse(data):
    keys(data, ("version", "groups", "sources", "refresh_seconds", "port", "map", "ui", "alerts", "services"))
    if integer(data.get("version"), 1, 1) != 1:
        raise ConfigError("Unsupported dashboard configuration version")
    groups = names(data.get("groups", list(GROUPS)), GROUPS)
    available = tuple(s for g in groups for s in GROUPS[g])
    sources = names(data.get("sources", list(available)), available)
    view = data.get("map", {})
    keys(view, ("center", "zoom", "static", "aois"))
    center = view.get("center", [20, 0])
    if type(center) is not list or len(center) != 2:
        raise ConfigError("Map center must contain latitude and longitude")
    center = (number(center[0], -85, 85), number(center[1], -180, 180))
    ui = data.get("ui", {})
    keys(ui, ("sections", "counters"))
    alerts = data.get("alerts", {})
    keys(alerts, ("categories", "cable_score", "cyber_critical"))
    services = data.get("services", {})
    keys(services, ("vector", "ollama", "history"))
    for value in services.values():
        if boolean(value):
            raise ConfigError("Vector, Ollama and history are not supported by the focused profile")
    return Settings(groups, sources, integer(data.get("refresh_seconds", 60), 30, 3600),
                    integer(data.get("port", 8501), 1, 65535), center, number(view.get("zoom", 2.5), 2, 12),
                    names(ui.get("sections", list(groups)), groups), names(ui.get("counters", list(groups)), groups),
                    boolean(view.get("static", True)), boolean(view.get("aois", False)),
                    names(alerts.get("categories", ["cable", "cyber"]), ("cable", "cyber")),
                    integer(alerts.get("cable_score", 2), 1, 3), integer(alerts.get("cyber_critical", 1), 1, 10000))


def load(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ConfigError("Dashboard configuration exceeds 64 KiB")
        return parse(tomllib.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("Cannot read valid dashboard TOML") from None


def from_environment():
    path = os.environ.get("WORLD_INTEL_CONFIG")
    return load(path) if path else None
