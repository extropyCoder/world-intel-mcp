"""Focused-only request witness, before adapters discard failures/stale flags.

No transport replacement and no changes to shared source/Fetcher semantics.
Each adapter gets its own witness and per-request context, including nested XML
delegation. Public records contain only the trusted dashboard source name,
operation and outcome, never request URLs, headers, labels or cache keys.
"""
from contextvars import ContextVar
import hashlib
import json
import math
import sqlite3
import time

from ..fetcher import Fetcher
from ..cache import Cache


OPERATIONS = ("get_json", "get_text", "get_xml", "get_bytes")
OUTCOMES = {"current", "missing", "stale", "error", "malformed", "unknown", "cancelled"}


def rows(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def json_shape(feed, value):
    """Protocol recognition, not a replacement for source normalization.

    Unknown formats are unverified rather than evidence of a healthy empty feed.
    These labels come from trusted adapters and are used internally only.
    """
    if feed in ("feodo-tracker", "sans-dshield"):
        field = "ip_address" if feed == "feodo-tracker" else "ip"
        return rows(value) and all(isinstance(row.get(field), str) and row[field] for row in value)
    if feed == "cisa-kev":
        return isinstance(value, dict) and rows(value.get("vulnerabilities")) and all(isinstance(row.get("cveID"), str) for row in value["vulnerabilities"])
    if feed == "urlhaus":
        return isinstance(value, dict) and (rows(value.get("urls")) or value.get("query_status") == "no_results")
    if feed == "nga-msi":
        candidates = [value] if isinstance(value, list) else [value.get(key) for key in ("broadcast-warn", "broadcastWarn", "warnings", "result", "data")] if isinstance(value, dict) else []
        return any(rows(candidate) and all(isinstance(row.get("text"), str) for row in candidate) for candidate in candidates)
    if feed == "yahoo-finance":
        chart = value.get("chart") if isinstance(value, dict) else None
        results = chart.get("result") if isinstance(chart, dict) else None
        return bool(rows(results) and results and not chart.get("error") and all(isinstance(row.get("meta"), dict) and numeric(row["meta"].get("regularMarketPrice")) for row in results))
    if feed == "coingecko":
        if isinstance(value, list):
            return rows(value) and all("id" in row and numeric(row.get("current_price")) for row in value)
        data = value.get("data") if isinstance(value, dict) else None
        return isinstance(data, dict) and isinstance(data.get("market_cap_percentage"), dict) and numeric(data["market_cap_percentage"].get("btc"))
    if feed == "mempool":
        return isinstance(value, dict) and numeric(value.get("fastestFee"))
    if feed == "alternative-me":
        return isinstance(value, dict) and rows(value.get("data")) and all("value" in row for row in value["data"])
    if feed == "ecb-forex":
        return isinstance(value, dict) and isinstance(value.get("rates"), dict) and all(numeric(rate) for rate in value["rates"].values())
    if feed == "adsblol":
        return isinstance(value, dict) and rows(value.get("ac"))
    if feed == "opensky":
        return isinstance(value, dict) and "states" in value and (value["states"] is None or isinstance(value["states"], list) and all(isinstance(row, list) and len(row) >= 17 for row in value["states"]))
    if feed == "ioda":
        return rows(value) or isinstance(value, dict) and rows(value.get("data"))
    if feed == "fred":
        return isinstance(value, dict) and rows(value.get("observations"))
    # A protocol not recognized here must not manufacture a verified zero.
    return None


class CacheWitness:
    def __init__(self, owner, cache):
        self.owner, self.cache = owner, cache

    def __getattr__(self, name):
        return getattr(self.cache, name)

    def key(self, key):
        return "focused:coverage:v2:" + self.owner.name + ":" + hashlib.sha256(str(key).encode()).hexdigest()

    @staticmethod
    def digest(value):
        # Canonicalize the JSON-decoded stored value, not a mutable caller object.
        # This digest stays inside the private cache; it is never public coverage.
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def outcome(self, metadata, generation, value):
        records = metadata.get("requests") if isinstance(metadata, dict) else None
        try:
            bound = metadata.get("entry") == {**generation, "digest": self.digest(value)}
        except (AttributeError, TypeError, ValueError):
            bound = False
        if not bound or not (isinstance(records, list) and records and all(isinstance(r, dict) and r.get("operation") in (*OPERATIONS, "cache") and r.get("outcome") in OUTCOMES for r in records)):
            return "unknown"
        return "current" if all(r["outcome"] == "current" for r in records) else "stale" if any(r["outcome"] == "stale" for r in records) else "unknown"

    def get(self, key, *args, **kwargs):
        if self.owner.active.get() is not None:
            return self.cache.get(key, *args, **kwargs)
        if type(self.cache) is not Cache or args or kwargs:
            value = self.cache.get(key, *args, **kwargs)
            if value is not None:
                self.owner.record("cache", "unknown")
            return value
        # One SQLite statement supplies the entry and its sidecar from the same
        # snapshot. Separate cache.get()/get_stale_meta() reads can race a writer.
        row = self.cache._get_conn().execute(
            "SELECT e.rowid,e.value,e.created_at,e.expires_at,m.value,m.expires_at "
            "FROM cache e LEFT JOIN cache m ON m.key=? WHERE e.key=?", (self.key(key), key)
        ).fetchone()
        now = time.time()
        if row is None or now > row[3]:
            return None
        rowid, raw, created, expires, metadata, meta_expires = row
        value = json.loads(raw)
        try:
            metadata = json.loads(metadata) if metadata is not None and now <= meta_expires else None
        except (TypeError, ValueError):
            metadata = None
        if value is not None:
            self.owner.record("cache", self.outcome(metadata, {"rowid": rowid, "created_at": created, "expires_at": expires}, value))
        return value

    def set(self, key, value, ttl_seconds, *args, **kwargs):
        if self.owner.active.get() is not None or type(self.cache) is not Cache or args or kwargs:
            return self.cache.set(key, value, ttl_seconds, *args, **kwargs)
        records = list(self.owner.records)
        if isinstance(value, dict) and (value.get("error") or value.get("degraded") or value.get("_stale")):
            records.append({"source": self.owner.name, "operation": "cache", "outcome": "stale" if value.get("_stale") else "unknown"})
        # Focused-only atomic write, using the existing Cache schema, serialization
        # and best-effort OperationalError semantics. Never change shared Cache.set.
        # INSERT OR REPLACE supplies a new rowid even for same-value/same-time writes
        # while the old sidecar exists. Digest+created/expiry also bind the value.
        try:
            raw = json.dumps(value, default=str)
            try:
                digest = self.digest(json.loads(raw))
            except (TypeError, ValueError):
                digest = None
            now = time.time()
            conn = self.cache._get_conn()
            with conn:
                cursor = conn.execute("INSERT OR REPLACE INTO cache (key,value,expires_at,created_at) VALUES (?,?,?,?)", (key, raw, now + ttl_seconds, now))
                entry = {"rowid": cursor.lastrowid, "created_at": now, "expires_at": now + ttl_seconds, "digest": digest}
                conn.execute("INSERT OR REPLACE INTO cache (key,value,expires_at,created_at) VALUES (?,?,?,?)", (self.key(key), json.dumps({"entry": entry, "requests": records}), now + ttl_seconds, now))
        except sqlite3.OperationalError:
            # Failure cannot leave a newly bound current sidecar committed alone.
            return None


class RequestWitness:
    def __init__(self, delegate, name):
        self.delegate, self.name = delegate, name
        self.records = []
        self.active = ContextVar("focused_request", default=None)

    def __getattr__(self, name):
        if name == "cache":
            return CacheWitness(self, self.delegate.cache)
        return getattr(self.delegate, name)

    def record(self, operation, outcome):
        self.records.append({"source": self.name, "operation": operation, "outcome": outcome})

    def _stale_fallback(self, *args, **kwargs):
        value = self.delegate._stale_fallback(*args, **kwargs)
        request = self.active.get()
        if request is not None and value is not None:
            request["stale"] = True
        return value

    async def request(self, operation, args, kwargs):
        method = getattr(self.delegate, operation)
        # Bind only real Fetcher entry points to this forwarding proxy so its
        # stale hook sees non-dict fallback too. Keep injected/overridden method
        # semantics untouched (including HTTP mocks and subclass super()).
        function = getattr(method, "__func__", None)
        native = function is getattr(Fetcher, operation)
        invoke = (lambda: function(self, *args, **kwargs)) if native else (lambda: method(*args, **kwargs))
        if self.active.get() is not None:
            if not native:
                self.active.get()["opaque"] = True
            return await invoke()  # get_xml -> get_text is one request
        request = {"stale": False}
        token = self.active.set(request)
        outcome = "cancelled"
        try:
            value = await invoke()
            if request["stale"] or isinstance(value, dict) and value.get("_stale"):
                outcome = "stale"
            elif value is None:
                outcome = "missing"
            elif isinstance(value, dict) and (value.get("error") or value.get("degraded")):
                outcome = "error"
            elif operation == "get_json":
                feed = kwargs.get("source", args[1] if len(args) > 1 else "")
                valid = json_shape(feed, value)
                outcome = "current" if valid is True else "unknown" if valid is None else "malformed"
            elif operation in ("get_text", "get_xml"):
                feed = kwargs.get("source", args[1] if len(args) > 1 else "")
                if isinstance(value, (str, bytes)) and value and isinstance(feed, str) and (feed.startswith("status:") or feed == "usni-fleet-tracker"):
                    from ..feed_parser import parse_feed_content
                    parsed = parse_feed_content(value)
                    outcome = "current" if parsed.version and parsed.feed.get("title") and not parsed.bozo else "malformed"
                else:
                    outcome = "unknown"
            else:
                outcome = "current" if getattr(value, "content", None) is not None and not getattr(value, "error", None) else "missing"
            if outcome == "current" and (not native or request.get("opaque")):
                outcome = "unknown"  # overridden methods' hidden fallback cannot be witnessed
            return value
        except Exception:
            outcome = "error"
            raise
        finally:
            self.record(operation, outcome)
            self.active.reset(token)

    async def get_json(self, *args, **kwargs):
        return await self.request("get_json", args, kwargs)

    async def get_text(self, *args, **kwargs):
        return await self.request("get_text", args, kwargs)

    async def get_xml(self, *args, **kwargs):
        return await self.request("get_xml", args, kwargs)

    async def get_bytes(self, *args, **kwargs):
        return await self.request("get_bytes", args, kwargs)

    def finish(self, value):
        value = dict(value) if isinstance(value, dict) else {"error": "Adapter result unavailable"}
        counts = {key: sum(r["outcome"] == key for r in self.records) for key in OUTCOMES}
        current, stale = counts["current"], counts["stale"]
        failed = sum(counts[key] for key in ("missing", "error", "malformed", "cancelled"))
        if value.get("error"):
            state = "unavailable"
        elif failed:
            state = "partial" if current or stale else "unavailable"
        elif stale or value.get("_stale"):
            state = "partial" if current else "stale"
        elif counts["unknown"]:
            state = "partial" if current else "unknown"
        elif not self.records or value.get("degraded"):
            state = "unknown"
        else:
            state = "current"
        value["_coverage"] = {"state": state, "attempted": len(self.records), "current": current,
                              "stale": stale, "failed": failed, "unknown": counts["unknown"], "requests": list(self.records)}
        if state != "current":
            value["degraded"] = True
            if stale:
                value["_stale"] = True
            if self.name == "cable_health" and isinstance(value.get("corridors"), dict):
                value["corridors"] = {key: {**info, "status_label": "unknown", "status_score": None}
                                      for key, info in value["corridors"].items() if isinstance(info, dict)}
        return value
