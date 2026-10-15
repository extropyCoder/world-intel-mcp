"""Aircraft cohorts and historical ADS-B activity from adsb.lol.

Two data paths, both from adsb.lol (free, no key; data licensed ODbL 1.0
per the API's own OpenAPI description):

* **Live cohort snapshots** via ``api.adsb.lol/v2/type/{T1,T2,...}`` (one
  request covers every business-jet ICAO type designator) and
  ``/v2/mil``. Each snapshot also *learns* the cohort: every ICAO address
  seen is upserted into a ``jet_cohort`` table with first/last-seen
  times, so the historical analyses have a hex set to filter on.
* **Half-hour heatmaps** at ``adsb.lol/globe_history/YYYY/MM/DD/heatmap/
  NN.bin.ttf`` (slot NN = UTC half-hour 00..47). These are readsb's
  heatmap files: ~14 MB gzip, 10-second slices of every tracked
  aircraft's position. The binary layout is readsb's documented fixed
  ``struct heatEntry`` (globe_index.h/.c): little-endian
  ``int32 addr, int32 lat*1e6, int32 lon*1e6, int16 alt/25ft,
  int16 gs*10``; a slice starts with a marker entry whose addr is
  0x0E7F7C9D and whose lat/lon carry the slice epoch-ms high/low words;
  entries with lat bit 30 set are callsign/squawk info records.
  Altitude -123 means on ground, -124 unknown; gs -1 invalid. Bits
  27-31 of addr carry the address type; bit 24 marks non-ICAO.

Heatmaps for past slots are immutable once written, so downloaded files
are kept in an on-disk LRU next to the cache database
(``WORLD_INTEL_HEATMAP_CACHE_MB``, default 1024) and never re-fetched.

Why a learned cohort rather than a registry import: the public registry
paths that would give a complete hex->type map are either bot-blocked
for scripts (FAA ReleasableAircraft.zip answers 403 to non-browser
clients) or licensed restrictively (ADS-B Exchange / JETNET terms;
``globe.adsbexchange.com/robots.txt`` disallows ``/globe_history/``).
The adsb.lol type endpoint is open, so the cohort is learned from it.
Consequence callers must respect: a cohort learned *during* a window is
biased toward aircraft airborne in that window. The analyses therefore
only use members first seen a configurable lead time before the window.

Approach (business-jet cohort vs calendar baseline) inspired by
kylemcdonald/ews; implementation is independent.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..fetcher import Fetcher
from ..bounded_decode import (
    BodyLimitError, ResponseTooLarge, decode_gzip, positive_bytes,
    DEFAULT_PROCESSING_TIMEOUT, processing_timeout as validate_processing_timeout,
)

logger = logging.getLogger("world-intel-mcp.sources.jet_cohort")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADSBLOL_API = "https://api.adsb.lol/v2"
HEATMAP_URL = "https://adsb.lol/globe_history/{y:04d}/{m:02d}/{d:02d}/heatmap/{slot:02d}.bin.ttf"
HEATMAP_SOURCE = "adsblol-history"
USER_AGENT = "world-intel-mcp (+https://github.com/marc-shade/world-intel-mcp)"
HEATMAP_MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
HEATMAP_MAX_EXPANDED_BYTES = 256 * 1024 * 1024

# ICAO Doc 8643 type designators for business jets. Unknown designators
# simply match nothing on the type endpoint, so the list errs inclusive.
BIZJET_TYPES: tuple[str, ...] = (
    # Gulfstream
    "GLF2", "GLF3", "GLF4", "GLF5", "GLF6", "GA5C", "GA6C", "GA7C", "GA8C",
    "G150", "G280", "GALX", "ASTR",
    # Bombardier
    "GLEX", "GL5T", "GL7T", "CL30", "CL35", "CL60",
    "LJ31", "LJ35", "LJ40", "LJ45", "LJ55", "LJ60", "LJ70", "LJ75",
    # Dassault
    "FA10", "FA20", "FA50", "F2TH", "F900", "FA7X", "FA8X", "FA6X",
    # Cessna / Textron
    "C500", "C501", "C510", "C525", "C25A", "C25B", "C25C", "C25M",
    "C550", "C551", "C55B", "C560", "C56X", "C650", "C680", "C68A",
    "C700", "C750",
    # Embraer
    "E50P", "E55P", "E545", "E550", "E35L",
    # Hawker / Beechcraft
    "H25A", "H25B", "H25C", "HA4T", "PRM1", "BE40",
    # Others
    "HDJT", "PC24", "SF50", "EA50", "WW24",
)

COHORTS = ("bizjet", "military")

# readsb JSON ``dbFlags`` bitfield (readsb README-json): 1 military,
# 2 interesting, 4 PIA (privacy ICAO address), 8 FAA LADD.
DBFLAG_MILITARY = 1
DBFLAG_PIA = 4
DBFLAG_LADD = 8

# Region presets as (lat_min, lon_min, lat_max, lon_max).
REGIONS: dict[str, tuple[float, float, float, float] | None] = {
    "global": None,
    "conus": (24.0, -126.0, 50.0, -66.0),
    "north_america": (15.0, -170.0, 72.0, -50.0),
    "europe": (35.0, -25.0, 72.0, 45.0),
    "middle_east": (12.0, 25.0, 42.0, 65.0),
    "east_asia": (15.0, 95.0, 55.0, 155.0),
}

SLICE_MARKER = 0x0E7F7C9D
_INFO_BIT = 1 << 30
_NON_ICAO_BIT = 1 << 24
_ADDR_MASK = 0x1FFFFFF
_ALT_GROUND = -123
_ALT_UNKNOWN = -124
_GS_INVALID = -1
_ENTRY = struct.Struct("<iiihh")

# Thinning: keep a fix if this many seconds passed since the last kept
# fix for that aircraft, or if its ground/air state or altitude band
# changed. Bounds memory at <= 60 fixes per aircraft per half hour.
_THIN_SECONDS = 30.0
_THIN_ALT_FT = 500


# ---------------------------------------------------------------------------
# Region parsing
# ---------------------------------------------------------------------------

def resolve_region(region: str | None) -> tuple[str, tuple[float, float, float, float] | None]:
    """Map a preset name or "lamin,lomin,lamax,lomax" to a bbox.

    Raises ValueError on an unknown preset or malformed/inverted bbox.
    """
    if region is None or region == "":
        return "global", None
    key = region.strip().lower()
    if key in REGIONS:
        return key, REGIONS[key]
    parts = [p.strip() for p in region.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"region must be one of {sorted(REGIONS)} or 'lamin,lomin,lamax,lomax'"
        )
    lat_min, lon_min, lat_max, lon_max = (float(p) for p in parts)
    if not (-90 <= lat_min < lat_max <= 90 and -180 <= lon_min < lon_max <= 180):
        raise ValueError("bbox must satisfy -90<=lamin<lamax<=90 and -180<=lomin<lomax<=180")
    return "bbox", (lat_min, lon_min, lat_max, lon_max)


# ---------------------------------------------------------------------------
# Cohort store
# ---------------------------------------------------------------------------

class CohortStore:
    """Learned aircraft cohorts, in a ``jet_cohort`` table of the cache DB."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jet_cohort (
                cohort TEXT NOT NULL,
                hex TEXT NOT NULL,
                type TEXT,
                registration TEXT,
                db_flags INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                PRIMARY KEY (cohort, hex)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, cohort: str, aircraft: list[dict[str, Any]], seen_at: float) -> int:
        """Record sightings; returns how many hexes were new to the cohort."""
        before = self.count(cohort)
        rows = [
            (
                cohort,
                a["hex"],
                a.get("type"),
                a.get("registration"),
                int(a.get("db_flags") or 0),
                seen_at,
                seen_at,
            )
            for a in aircraft
        ]
        self._conn.executemany(
            """
            INSERT INTO jet_cohort
                (cohort, hex, type, registration, db_flags, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cohort, hex) DO UPDATE SET
                type = COALESCE(excluded.type, jet_cohort.type),
                registration = COALESCE(excluded.registration, jet_cohort.registration),
                db_flags = excluded.db_flags,
                last_seen = MAX(jet_cohort.last_seen, excluded.last_seen),
                first_seen = MIN(jet_cohort.first_seen, excluded.first_seen)
            """,
            rows,
        )
        self._conn.commit()
        return self.count(cohort) - before

    def count(self, cohort: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM jet_cohort WHERE cohort = ?", (cohort,)
        ).fetchone()
        return int(row[0])

    def members(self, cohort: str, learned_before: float | None = None) -> dict[int, dict[str, Any]]:
        """Return {addr_int: {hex, type, registration, db_flags}} for a cohort,
        optionally only members first seen strictly before ``learned_before``."""
        if learned_before is None:
            cur = self._conn.execute(
                "SELECT hex, type, registration, db_flags FROM jet_cohort WHERE cohort = ?",
                (cohort,),
            )
        else:
            cur = self._conn.execute(
                "SELECT hex, type, registration, db_flags FROM jet_cohort "
                "WHERE cohort = ? AND first_seen < ?",
                (cohort, learned_before),
            )
        out: dict[int, dict[str, Any]] = {}
        for hex_, type_, reg, flags in cur.fetchall():
            try:
                out[int(hex_, 16)] = {
                    "hex": hex_,
                    "type": type_,
                    "registration": reg,
                    "db_flags": flags,
                }
            except ValueError:
                continue
        return out

    def stats(self, cohort: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*), MIN(first_seen), MAX(last_seen) FROM jet_cohort WHERE cohort = ?",
            (cohort,),
        ).fetchone()
        n, first, last = row
        return {
            "members": int(n),
            "learning_started": _iso(first) if first else None,
            "last_sighting": _iso(last) if last else None,
        }


def default_store(fetcher: Fetcher) -> CohortStore:
    return CohortStore(fetcher.cache.db_path)


# ---------------------------------------------------------------------------
# Live snapshot (learns the cohort)
# ---------------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_live_aircraft(ac: dict[str, Any]) -> dict[str, Any] | None:
    hex_ = str(ac.get("hex") or "").strip().lower()
    if len(hex_) != 6 or hex_.startswith("~"):
        return None  # non-ICAO (TIS-B track files etc.) cannot be cohort members
    try:
        int(hex_, 16)
    except ValueError:
        return None
    alt = ac.get("alt_baro")
    on_ground = alt == "ground"
    return {
        "hex": hex_,
        "type": (ac.get("t") or None),
        "registration": (ac.get("r") or None),
        "callsign": (ac.get("flight") or "").strip() or None,
        "db_flags": int(ac.get("dbFlags") or 0),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "alt_ft": None if on_ground or not isinstance(alt, (int, float)) else alt,
        "on_ground": on_ground,
        "gs_kt": ac.get("gs"),
    }


def _in_bbox(lat: Any, lon: Any, bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    if lat is None or lon is None:
        return False
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


async def fetch_cohort_snapshot(
    fetcher: Fetcher,
    cohort: str = "bizjet",
    region: str | None = None,
    store: CohortStore | None = None,
) -> dict[str, Any]:
    """Live airborne snapshot of a cohort, and cohort learning as a side effect.

    Every ICAO address in the global response is recorded in the cohort
    store (regardless of ``region``, which only filters the summary).
    """
    if cohort not in COHORTS:
        return {"error": f"cohort must be one of {list(COHORTS)}", "source": "adsb.lol"}
    try:
        region_name, bbox = resolve_region(region)
    except ValueError as exc:
        return {"error": str(exc), "source": "adsb.lol"}

    if cohort == "bizjet":
        url = f"{ADSBLOL_API}/type/{','.join(BIZJET_TYPES)}"
        cache_key = "jet_cohort:live:bizjet"
    else:
        url = f"{ADSBLOL_API}/mil"
        cache_key = "military:adsblol:mil"  # shared with intel_military_flights

    data = await fetcher.get_json(
        url,
        source="adsblol",
        cache_key=cache_key,
        cache_ttl=300,
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
    )
    now = time.time()
    if not isinstance(data, dict) or not isinstance(data.get("ac"), list):
        return {
            "cohort": cohort,
            "error": "adsb.lol live endpoint unavailable",
            "source": "adsb.lol",
            "timestamp": _iso(now),
        }

    aircraft = [a for a in (_normalize_live_aircraft(x) for x in data["ac"]) if a]
    own_store = store is None
    store = store or default_store(fetcher)
    try:
        new_members = store.upsert(cohort, aircraft, now)
        stats = store.stats(cohort)
    finally:
        if own_store:
            store.close()

    in_region = [a for a in aircraft if _in_bbox(a["lat"], a["lon"], bbox)]
    airborne = [a for a in in_region if not a["on_ground"]]
    by_type: dict[str, int] = {}
    for a in airborne:
        by_type[a["type"] or "unknown"] = by_type.get(a["type"] or "unknown", 0) + 1
    pia = sum(1 for a in airborne if a["db_flags"] & DBFLAG_PIA)
    ladd = sum(1 for a in airborne if a["db_flags"] & DBFLAG_LADD)

    result: dict[str, Any] = {
        "cohort": cohort,
        "region": region_name,
        "bbox": list(bbox) if bbox else None,
        "airborne": len(airborne),
        "on_ground": len(in_region) - len(airborne),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:25]),
        "privacy_flags": {"pia": pia, "ladd": ladd},
        "cohort_store": {**stats, "new_members_this_snapshot": new_members},
        "aircraft": [
            {k: a[k] for k in ("hex", "type", "registration", "callsign", "lat", "lon", "alt_ft", "gs_kt")}
            for a in airborne[:200]
        ],
        "source": "adsb.lol",
        "license": "ODbL-1.0",
        "timestamp": _iso(now),
    }
    if cohort == "bizjet":
        result["type_designators_queried"] = len(BIZJET_TYPES)
    if data.get("_stale"):
        result["stale"] = True
        result["stale_age_seconds"] = data.get("_stale_age_seconds")
    return result


# ---------------------------------------------------------------------------
# Heatmap slots
# ---------------------------------------------------------------------------

def slot_start(ts: datetime) -> datetime:
    """Floor a UTC datetime to its half-hour slot start."""
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=0 if ts.minute < 30 else 30, second=0, microsecond=0)


def slot_index(start: datetime) -> int:
    return start.hour * 2 + (1 if start.minute >= 30 else 0)


def heatmap_url(start: datetime) -> str:
    return HEATMAP_URL.format(y=start.year, m=start.month, d=start.day, slot=slot_index(start))


@dataclass
class SlotData:
    """Parsed, cohort-filtered contents of one half-hour heatmap."""

    start: datetime
    slice_times: list[float]
    tracks: dict[int, list[tuple[float, float, float, int | None, bool, float | None]]] = field(
        default_factory=dict
    )
    total_aircraft: int = 0  # every address with a position in the file (any cohort)

    @property
    def coverage(self) -> float:
        """Fraction of the expected 180 ten-second slices present."""
        return min(1.0, len(self.slice_times) / 180.0)


def parse_heatmap(
    blob: bytes,
    wanted: set[int] | None,
    bbox: tuple[float, float, float, float] | None = None,
    *,
    max_compressed_bytes: int = HEATMAP_MAX_COMPRESSED_BYTES,
    max_expanded_bytes: int = HEATMAP_MAX_EXPANDED_BYTES,
    processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
) -> tuple[list[float], dict[int, list[tuple]], int]:
    """Decode a readsb heatmap body (gzip or raw).

    Returns ``(slice_epoch_seconds, tracks, total_addresses)`` where
    ``tracks[addr]`` is a time-ordered, thinned list of
    ``(ts, lat, lon, alt_ft|None, on_ground, gs_kt|None)`` for addresses
    in ``wanted`` (all ICAO addresses if None) inside ``bbox``.

    Raises ValueError for a truncated body or one with no slice markers.
    """
    positive_bytes(max_compressed_bytes)
    positive_bytes(max_expanded_bytes)
    deadline = time.monotonic() + validate_processing_timeout(processing_timeout)
    if len(blob) > max_compressed_bytes:
        raise ResponseTooLarge("heatmap input exceeds compressed/input cap")
    if blob[:2] == b"\x1f\x8b":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BodyLimitError("heatmap processing deadline exceeded")
        blob = decode_gzip(blob, max_expanded_bytes, timeout=remaining)
    if len(blob) > max_expanded_bytes:
        raise ResponseTooLarge("heatmap exceeds expanded cap")
    if len(blob) % _ENTRY.size:
        raise ValueError(f"heatmap body length {len(blob)} is not a multiple of 16")

    slices: list[float] = []
    tracks: dict[int, list[tuple]] = {}
    seen_all: set[int] = set()
    current_ts: float | None = None
    if bbox is not None:
        lat_lo, lon_lo, lat_hi, lon_hi = (int(v * 1_000_000) for v in bbox)

    for index, (addr_raw, lat_i, lon_i, alt, gs) in enumerate(_ENTRY.iter_unpack(blob)):
        if index % 4096 == 0 and time.monotonic() >= deadline:
            raise BodyLimitError("heatmap processing deadline exceeded")
        if addr_raw == SLICE_MARKER:
            epoch_ms = ((lat_i & 0xFFFFFFFF) << 32) | (lon_i & 0xFFFFFFFF)
            current_ts = epoch_ms / 1000.0
            slices.append(current_ts)
            continue
        if current_ts is None or lat_i & _INFO_BIT:
            continue  # leading slice index, or a callsign/squawk record
        addr = addr_raw & _ADDR_MASK
        if addr & _NON_ICAO_BIT:
            continue
        seen_all.add(addr)
        if wanted is not None and addr not in wanted:
            continue
        if bbox is not None and not (lat_lo <= lat_i <= lat_hi and lon_lo <= lon_i <= lon_hi):
            continue
        on_ground = alt == _ALT_GROUND
        alt_ft = None if alt in (_ALT_GROUND, _ALT_UNKNOWN) else alt * 25
        gs_kt = None if gs == _GS_INVALID else gs / 10.0
        fix = (current_ts, lat_i / 1e6, lon_i / 1e6, alt_ft, on_ground, gs_kt)
        track = tracks.get(addr)
        if track is None:
            tracks[addr] = [fix]
            continue
        last = track[-1]
        if (
            current_ts - last[0] >= _THIN_SECONDS
            or on_ground != last[4]
            or (alt_ft is not None and (last[3] is None or abs(alt_ft - last[3]) >= _THIN_ALT_FT))
        ):
            track.append(fix)

    if time.monotonic() >= deadline:
        raise BodyLimitError("heatmap processing deadline exceeded")
    if not slices:
        raise ValueError("heatmap body contains no slice markers")
    return slices, tracks, len(seen_all)


class HeatmapDiskCache:
    """On-disk LRU of raw heatmap bodies (immutable once published)."""

    def __init__(
        self, root: Path, cap_bytes: int, *,
        max_compressed_bytes: int = HEATMAP_MAX_COMPRESSED_BYTES,
        max_expanded_bytes: int = HEATMAP_MAX_EXPANDED_BYTES,
        processing_timeout: float = DEFAULT_PROCESSING_TIMEOUT,
    ):
        self.root = Path(root)
        self.cap_bytes = cap_bytes
        self.max_compressed_bytes = positive_bytes(max_compressed_bytes)
        self.max_expanded_bytes = positive_bytes(max_expanded_bytes)
        self.processing_timeout = validate_processing_timeout(processing_timeout)

    def path_for(self, start: datetime) -> Path:
        return self.root / f"{start:%Y}" / f"{start:%m}" / f"{start:%d}" / f"{slot_index(start):02d}.bin.gz"

    def get(self, start: datetime) -> bytes | None:
        p = self.path_for(start)
        if not p.exists():
            return None
        os.utime(p)  # LRU recency
        body = bytearray()
        with p.open("rb") as stream:
            while chunk := stream.read(min(64 * 1024, self.max_compressed_bytes - len(body) + 1)):
                if len(body) + len(chunk) > self.max_compressed_bytes:
                    raise ResponseTooLarge("cached heatmap exceeds input cap")
                body.extend(chunk)
        return bytes(body)

    def put(self, start: datetime, body: bytes) -> None:
        p = self.path_for(start)
        p.parent.mkdir(parents=True, exist_ok=True)
        if body[:2] != b"\x1f\x8b":
            if len(body) > self.max_expanded_bytes:
                raise ResponseTooLarge("heatmap exceeds expanded cap")
            body = gzip.compress(body, compresslevel=6)
        if len(body) > self.max_compressed_bytes:
            raise ResponseTooLarge("heatmap exceeds compressed cap")
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(p)
        self.evict()

    def evict(self) -> None:
        files = sorted(self.root.rglob("*.bin.gz"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while files and total > self.cap_bytes:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)


def default_disk_cache(fetcher: Fetcher) -> HeatmapDiskCache:
    cap_mb = float(os.environ.get("WORLD_INTEL_HEATMAP_CACHE_MB", "1024"))
    root = Path(fetcher.cache.db_path).parent / "heatmaps" / "adsblol"
    return HeatmapDiskCache(root, int(cap_mb * 1024 * 1024))


@dataclass
class SlotFetch:
    start: datetime
    data: SlotData | None
    status: str  # "ok" | "not_published" | "error"
    error: str | None = None
    cached: bool = False


async def fetch_slot(
    fetcher: Fetcher,
    start: datetime,
    wanted: set[int] | None,
    bbox: tuple[float, float, float, float] | None,
    disk: HeatmapDiskCache,
) -> SlotFetch:
    """Fetch (disk cache first) and parse one half-hour heatmap slot."""
    try:
        body = disk.get(start)
    except (ValueError, OSError) as exc:
        return SlotFetch(start, None, "error", f"unreadable heatmap: {exc}", True)
    cached = body is not None
    if body is None:
        res = await fetcher.get_bytes(
            heatmap_url(start),
            source=HEATMAP_SOURCE,
            headers={"User-Agent": USER_AGENT},
            timeout=120.0,
            max_bytes=disk.max_compressed_bytes,
        )
        if res.status == 404:
            return SlotFetch(start, None, "not_published")
        if not res.ok:
            return SlotFetch(start, None, "error", res.error or f"HTTP {res.status}")
        body = res.content
    try:
        slices, tracks, total = await asyncio.to_thread(
            parse_heatmap, body, wanted, bbox,
            max_compressed_bytes=disk.max_compressed_bytes,
            max_expanded_bytes=disk.max_expanded_bytes,
            processing_timeout=disk.processing_timeout,
        )
    except (ValueError, OSError, EOFError) as exc:
        return SlotFetch(start, None, "error", f"unparseable heatmap: {exc}", cached)
    if not cached:
        try:
            await asyncio.to_thread(disk.put, start, body)
        except (ValueError, OSError) as exc:
            return SlotFetch(start, None, "error", f"uncacheable heatmap: {exc}")
    return SlotFetch(start, SlotData(start, slices, tracks, total), "ok", None, cached)


async def latest_published_slot(
    fetcher: Fetcher,
    wanted: set[int] | None,
    bbox: tuple[float, float, float, float] | None,
    disk: HeatmapDiskCache,
    now: datetime | None = None,
) -> SlotFetch:
    """Most recent completed slot that adsb.lol has published (tries 3 back)."""
    now = now or datetime.now(timezone.utc)
    candidate = slot_start(now) - timedelta(minutes=30)
    last = await fetch_slot(fetcher, candidate, wanted, bbox, disk)
    for _ in range(2):
        if last.status != "not_published":
            return last
        candidate -= timedelta(minutes=30)
        last = await fetch_slot(fetcher, candidate, wanted, bbox, disk)
    return last
