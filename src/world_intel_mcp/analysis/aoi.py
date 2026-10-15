"""User-defined areas of interest (AOIs): geofences (issue #16).

The intel community calls this an AOI; consumer software calls it a
geofence. This module composes the same domain fetch functions used
elsewhere in the codebase (seismology, military, conflict, wildfire,
aviation, news) around a user-named point-plus-radius, the way
``analysis/daily_digest.py`` composes them around "right now" instead of
"this place". It lives in ``analysis/`` rather than ``sources/`` because,
like ``daily_digest.py`` and ``situation.py``, its job is cross-domain
composition and citation, not talking to a single upstream API.

Follows the same citation discipline as ``situation.py``/``daily_digest.py``:
every listed item carries a ``[n]`` reference into a numbered ``sources``
list, and ``cited`` is only true when the brief text actually contains one.
``data_gaps`` names every domain that could not be geographically scoped
rather than silently omitting it.

AOI persistence (``AOIStore``) uses a dedicated ``aois`` table in the same
SQLite database file the process-wide ``Cache`` already opened, using the
same ``db_path`` and the same WAL / busy_timeout pragmas, via its own connection.
SQLite's WAL mode is explicitly designed for multiple connections against
one file, which is already how this repo's ``Cache`` coexists with
external readers. A dedicated table (not cache-entry abuse) because an AOI
is a named row a user expects to list and delete, not a TTL'd blob; a
separate connection (not new methods bolted onto ``Cache``) because
``Cache`` is a generic TTL key-value store and mixing a structured CRUD
table into it would blur that class's one job, at the cost of a well-
tested 188-line module untouched by this change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .escalation import score_hotspot
from ..diagnostics import exception_summary, safe_error
from .situation import _add_source, _cite, _has_valid_citation

logger = logging.getLogger("world-intel-mcp.analysis.aoi")

MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 2000.0

_EARTH_RADIUS_KM = 6371.0
_KM_PER_DEGREE_LAT = 111.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometers."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(min(1.0, a)))


def _lat_band(lat: float, radius_km: float) -> tuple[float, float]:
    dlat = radius_km / _KM_PER_DEGREE_LAT
    return max(-90.0, lat - dlat), min(90.0, lat + dlat)


def _lon_halfwidth_deg(lat: float, radius_km: float) -> float:
    """Half-width in longitude degrees of a circle of ``radius_km`` at
    ``lat``. Longitude degrees shrink toward the poles; the cosine factor
    is clamped so a near-polar AOI gets a wide-but-finite width instead
    of a division blowup."""
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    return radius_km / (_KM_PER_DEGREE_LAT * cos_lat)


def bboxes_from_radius_km(lat: float, lon: float, radius_km: float) -> list[str]:
    """Derive ``lamin,lomin,lamax,lomax`` bounding boxes (the format
    ``sources/military.py``'s ``fetch_military_flights(bbox=...)`` takes)
    that together fully contain a circle of ``radius_km`` around
    (lat, lon).

    Normally one box. When the circle crosses the antimeridian the
    single-box clamp used before v0.4 silently cut off everything on the
    far side of the dateline (a Bering Strait or Fiji AOI lost half its
    coverage); such circles now split into two boxes, one ending at
    +180 and one starting at -180. A circle whose longitude half-width
    reaches 180 degrees rings the pole and gets one full-longitude box.

    These are bounding rectangles, not the circle itself; callers that
    need the exact radius still haversine-filter the candidates they get
    back (see ``filter_by_radius``), the same way
    ``analysis/convergence.py``'s grid cells are a coarse pre-filter, not
    the final answer.
    """
    lamin, lamax = _lat_band(lat, radius_km)
    dlon = _lon_halfwidth_deg(lat, radius_km)

    def _box(lomin: float, lomax: float) -> str:
        return f"{lamin:.4f},{lomin:.4f},{lamax:.4f},{lomax:.4f}"

    if dlon >= 180.0:
        return [_box(-180.0, 180.0)]
    lomin = lon - dlon
    lomax = lon + dlon
    if lomin < -180.0:
        return [_box(-180.0, lomax), _box(lomin + 360.0, 180.0)]
    if lomax > 180.0:
        return [_box(lomin, 180.0), _box(-180.0, lomax - 360.0)]
    return [_box(lomin, lomax)]


def _bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in radians."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlon
    )
    return math.atan2(y, x)


def segment_distance_km(
    plat: float,
    plon: float,
    alat: float,
    alon: float,
    blat: float,
    blon: float,
) -> float:
    """Distance from point P to the great-circle SEGMENT A-B, in km.

    Cross-track distance where the closest point of the great circle
    falls within the segment; distance to the nearer endpoint where it
    falls beyond either end. The endpoint clamp is what makes this a
    segment distance: a point far past B on the same great circle is
    near the *line* but not near the *pipeline*."""
    d_ab = haversine_km(alat, alon, blat, blon) / _EARTH_RADIUS_KM
    d_ap_km = haversine_km(plat, plon, alat, alon)
    if d_ab < 1e-9:
        return d_ap_km
    d_ap = d_ap_km / _EARTH_RADIUS_KM
    theta_ap = _bearing_rad(alat, alon, plat, plon)
    theta_ab = _bearing_rad(alat, alon, blat, blon)
    if math.cos(theta_ap - theta_ab) < 0:
        # Closest point of the great circle lies behind A.
        return d_ap_km
    xt = math.asin(max(-1.0, min(1.0, math.sin(d_ap) * math.sin(theta_ap - theta_ab))))
    cos_xt = math.cos(xt)
    if abs(cos_xt) < 1e-12:
        return abs(xt) * _EARTH_RADIUS_KM
    at = math.acos(max(-1.0, min(1.0, math.cos(d_ap) / cos_xt)))
    if at > d_ab:
        # Closest point of the great circle lies beyond B.
        return haversine_km(plat, plon, blat, blon)
    return abs(xt) * _EARTH_RADIUS_KM


# ---------------------------------------------------------------------------
# Polygon + corridor geometry (Phase 23)
# ---------------------------------------------------------------------------

MIN_POLYGON_VERTICES = 3
MAX_SHAPE_POINTS = 64
MIN_CORRIDOR_WIDTH_KM = 1.0
MAX_CORRIDOR_WIDTH_KM = 500.0
_MAX_POLYGON_BOUNDING_KM = MAX_RADIUS_KM
_MAX_CORRIDOR_BOUNDING_KM = 5000.0


def _shift_lon(lon: float, ref_lon: float) -> float:
    """Longitude re-expressed in the continuous window ref_lon +/- 180,
    so dateline-straddling shapes become ordinary planar polygons."""
    return ((lon - ref_lon + 180.0) % 360.0) - 180.0


def polygon_centroid(vertices: list) -> tuple[float, float]:
    """Arithmetic centroid of the vertices, dateline-aware (longitudes
    are averaged in a window centered on the first vertex). A vertex
    average, not an area centroid: good enough for the bounding-circle
    prefilters and distance annotations it feeds."""
    ref = float(vertices[0][1])
    lat = sum(float(v[0]) for v in vertices) / len(vertices)
    shifted = sum(_shift_lon(float(v[1]), ref) for v in vertices) / len(vertices)
    lon = shifted + ref
    if lon > 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0
    return (lat, lon)


def point_in_polygon(lat: float, lon: float, vertices: list) -> bool:
    """Ray-casting point-in-polygon on the lat/lon plane, dateline-aware
    (all longitudes shifted into a window centered on the first vertex).
    Treats edges as straight lines in coordinate space, which is the
    same approximation the rest of this module's bounding boxes make;
    fine at AOI scales, not for continent-sized polygons near the poles."""
    ref = float(vertices[0][1])
    px = _shift_lon(lon, ref)
    py = lat
    inside = False
    n = len(vertices)
    for i in range(n):
        y1, x1 = float(vertices[i][0]), _shift_lon(float(vertices[i][1]), ref)
        y2, x2 = (
            float(vertices[(i + 1) % n][0]),
            _shift_lon(float(vertices[(i + 1) % n][1]), ref),
        )
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_cross:
                inside = not inside
    return inside


def corridor_distance_km(lat: float, lon: float, waypoints: list) -> float:
    """Distance from a point to a corridor's route: the minimum
    great-circle segment distance over consecutive waypoint pairs."""
    return min(
        segment_distance_km(
            lat,
            lon,
            float(a[0]),
            float(a[1]),
            float(b[0]),
            float(b[1]),
        )
        for a, b in zip(waypoints, waypoints[1:])
    )


def _valid_points(points: Any, min_points: int) -> str | None:
    if not isinstance(points, list) or len(points) < min_points:
        return f"expected a list of at least {min_points} [lat, lon] pairs."
    if len(points) > MAX_SHAPE_POINTS:
        return f"at most {MAX_SHAPE_POINTS} points are supported."
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return "each point must be a [lat, lon] pair."
        p_lat, p_lon = p
        for v in (p_lat, p_lon):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return f"coordinates must be numbers (got {v!r})."
        if not (-90.0 <= p_lat <= 90.0):
            return f"lat must be between -90 and 90 (got {p_lat})."
        if not (-180.0 <= p_lon <= 180.0):
            return f"lon must be between -180 and 180 (got {p_lon})."
    return None


def validate_polygon(name: Any, vertices: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string."
    err = _valid_points(vertices, MIN_POLYGON_VERTICES)
    if err:
        return f"vertices: {err}"
    ref = float(vertices[0][1])
    shifted = [_shift_lon(float(v[1]), ref) for v in vertices]
    if max(shifted) - min(shifted) > 180.0:
        return "polygon spans more than 180 degrees of longitude."
    c_lat, c_lon = polygon_centroid(vertices)
    bounding = max(
        haversine_km(c_lat, c_lon, float(v[0]), float(v[1])) for v in vertices
    )
    if bounding > _MAX_POLYGON_BOUNDING_KM:
        return (
            f"polygon is too large: bounding radius {bounding:.0f} km exceeds "
            f"{_MAX_POLYGON_BOUNDING_KM:.0f} km."
        )
    return None


def validate_corridor(name: Any, waypoints: Any, width_km: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string."
    err = _valid_points(waypoints, 2)
    if err:
        return f"waypoints: {err}"
    if not isinstance(width_km, (int, float)) or isinstance(width_km, bool):
        return f"width_km must be a number (got {width_km!r})."
    if not (MIN_CORRIDOR_WIDTH_KM <= width_km <= MAX_CORRIDOR_WIDTH_KM):
        return (
            f"width_km must be between {MIN_CORRIDOR_WIDTH_KM} and "
            f"{MAX_CORRIDOR_WIDTH_KM} (got {width_km})."
        )
    c_lat, c_lon = polygon_centroid(waypoints)
    bounding = (
        max(haversine_km(c_lat, c_lon, float(w[0]), float(w[1])) for w in waypoints)
        + float(width_km) / 2.0
    )
    if bounding > _MAX_CORRIDOR_BOUNDING_KM:
        return (
            f"corridor is too large: bounding radius {bounding:.0f} km exceeds "
            f"{_MAX_CORRIDOR_BOUNDING_KM:.0f} km."
        )
    return None


def aoi_contains(aoi_row: dict, lat: float, lon: float) -> bool:
    """Shape-aware membership: is the point inside this AOI's actual
    shape (not just its bounding circle)?"""
    kind = aoi_row.get("kind", "circle")
    if kind == "polygon":
        return point_in_polygon(lat, lon, aoi_row["geometry"]["vertices"])
    if kind == "corridor":
        geometry = aoi_row["geometry"]
        return (
            corridor_distance_km(lat, lon, geometry["waypoints"])
            <= float(geometry["width_km"]) / 2.0
        )
    return (
        haversine_km(aoi_row["lat"], aoi_row["lon"], lat, lon) <= aoi_row["radius_km"]
    )


def _aoi_annotation_distance(aoi_row: dict, lat: float, lon: float) -> float:
    """The distance_km an item is annotated and sorted with: distance to
    the center for circles and polygons, distance to the route for
    corridors (a corridor has no meaningful single center)."""
    if aoi_row.get("kind") == "corridor":
        return corridor_distance_km(lat, lon, aoi_row["geometry"]["waypoints"])
    return haversine_km(aoi_row["lat"], aoi_row["lon"], lat, lon)


def filter_by_aoi(
    items: list[dict],
    aoi_row: dict,
    lat_key: str = "latitude",
    lon_key: str = "longitude",
) -> list[dict]:
    """Shape-aware version of ``filter_by_radius``: keeps items inside
    the AOI's actual shape, annotated with ``distance_km`` and sorted
    nearest first. For circle AOIs the result is identical to
    ``filter_by_radius`` (verified by test); items with missing or
    unparseable coordinates are dropped, same as there."""
    out = []
    for item in items:
        raw_lat, raw_lon = item.get(lat_key), item.get(lon_key)
        if raw_lat is None or raw_lon is None:
            continue
        try:
            p_lat, p_lon = float(raw_lat), float(raw_lon)
        except (TypeError, ValueError):
            continue
        if not aoi_contains(aoi_row, p_lat, p_lon):
            continue
        out.append(
            {
                **item,
                "distance_km": round(
                    _aoi_annotation_distance(aoi_row, p_lat, p_lon), 1
                ),
            }
        )
    out.sort(key=lambda i: i["distance_km"])
    return out


def _distance_or_none(
    lat: float, lon: float, item_lat: Any, item_lon: Any
) -> float | None:
    if item_lat is None or item_lon is None:
        return None
    try:
        return haversine_km(lat, lon, float(item_lat), float(item_lon))
    except (TypeError, ValueError):
        return None


def filter_by_radius(
    items: list[dict],
    lat: float,
    lon: float,
    radius_km: float,
    lat_key: str = "latitude",
    lon_key: str = "longitude",
) -> list[dict]:
    """Keep only items whose coordinates fall within ``radius_km`` of
    (lat, lon), each annotated with ``distance_km`` and sorted nearest
    first. Items with missing or unparseable coordinates are dropped, not
    silently kept: an AOI brief that includes events it can't place on
    the map isn't a geofence, it's a global feed with extra steps."""
    out = []
    for item in items:
        dist = _distance_or_none(lat, lon, item.get(lat_key), item.get(lon_key))
        if dist is not None and dist <= radius_km:
            out.append({**item, "distance_km": round(dist, 1)})
    out.sort(key=lambda i: i["distance_km"])
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_aoi_params(name: Any, lat: Any, lon: Any, radius_km: Any) -> str | None:
    """Return a human-readable error message, or ``None`` if the AOI
    parameters are valid. Never raises: every branch it can take from
    missing/wrong-typed MCP arguments ends in a string, not an exception,
    so callers can return ``{"error": ...}`` uniformly."""
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string."
    for label, value in (("lat", lat), ("lon", lon), ("radius_km", radius_km)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{label} must be a number (got {value!r})."
    if not (-90.0 <= lat <= 90.0):
        return f"lat must be between -90 and 90 (got {lat})."
    if not (-180.0 <= lon <= 180.0):
        return f"lon must be between -180 and 180 (got {lon})."
    if not (MIN_RADIUS_KM <= radius_km <= MAX_RADIUS_KM):
        return (
            f"radius_km must be between {MIN_RADIUS_KM} and "
            f"{MAX_RADIUS_KM} (got {radius_km})."
        )
    return None


# ---------------------------------------------------------------------------
# Persistence: dedicated table in the shared cache database
# ---------------------------------------------------------------------------


class AOIAlreadyExists(Exception):
    """Raised by ``AOIStore.define()`` when the (case-insensitive) name is
    already taken. Carries the existing row so the caller can echo it back
    politely instead of just saying no."""

    def __init__(self, existing: dict[str, Any]):
        self.existing = existing
        super().__init__(f"AOI '{existing['name']}' already exists")


class AOIStore:
    """Named point-radius geofences, persisted in a dedicated ``aois``
    table inside the same SQLite file the process's ``Cache`` uses (pass
    its resolved ``db_path`` in, not a fresh default-path computation,
    so this always lands in the literal file the running cache settled
    on, fallback path included).
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aois (
                name_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                radius_km REAL NOT NULL,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'circle',
                geometry TEXT
            )
            """
        )
        # Migration for databases created before shapes existed (v0.4 and
        # earlier): old rows read as circles, which is exactly what they
        # were. ALTER ADD COLUMN is a no-op-safe idempotent check.
        existing_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(aois)").fetchall()
        }
        if "kind" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE aois ADD COLUMN kind TEXT NOT NULL DEFAULT 'circle'"
            )
        if "geometry" not in existing_cols:
            self._conn.execute("ALTER TABLE aois ADD COLUMN geometry TEXT")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aoi_snapshots (
                name_key TEXT PRIMARY KEY,
                taken_at REAL NOT NULL,
                snapshot TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _key(name: str) -> str:
        return name.strip().lower()

    _COLUMNS = "name_key, name, lat, lon, radius_km, created_at, kind, geometry"

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        _name_key, name, lat, lon, radius_km, created_at, kind, geometry = row
        parsed_geometry = None
        if geometry:
            try:
                parsed_geometry = json.loads(geometry)
            except (TypeError, ValueError):
                parsed_geometry = None
        return {
            "name": name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "created_at": created_at,
            "kind": kind or "circle",
            "geometry": parsed_geometry,
        }

    def define(
        self,
        name: str,
        lat: float,
        lon: float,
        radius_km: float,
        kind: str = "circle",
        geometry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a new AOI. For polygons and corridors, lat/lon/radius_km
        are the DERIVED bounding circle (centroid + radius reaching the
        farthest point), which is what every coarse prefilter in this
        module consumes; ``geometry`` carries the exact shape. Raises
        ``AOIAlreadyExists`` (carrying the existing row) if the name is
        already taken."""
        existing = self.get(name)
        if existing is not None:
            raise AOIAlreadyExists(existing)
        clean_name = name.strip()
        now = time.time()
        self._conn.execute(
            "INSERT INTO aois (name_key, name, lat, lon, radius_km, created_at, "
            "kind, geometry) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._key(name),
                clean_name,
                lat,
                lon,
                radius_km,
                now,
                kind,
                json.dumps(geometry) if geometry is not None else None,
            ),
        )
        self._conn.commit()
        return {
            "name": clean_name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "created_at": now,
            "kind": kind,
            "geometry": geometry,
        }

    def get(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM aois WHERE name_key = ?",
            (self._key(name),),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM aois ORDER BY name"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, name: str) -> bool:
        key = self._key(name)
        cur = self._conn.execute("DELETE FROM aois WHERE name_key = ?", (key,))
        self._conn.execute("DELETE FROM aoi_snapshots WHERE name_key = ?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

    def update(
        self,
        name: str,
        *,
        new_name: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float | None = None,
    ) -> dict[str, Any]:
        """Apply the given field changes to an existing AOI and return the
        updated row. A rename migrates the AOI's change snapshot with it;
        validation, collision checks, and the geometry-change snapshot
        drop are the caller's job (see ``update_aoi``). Raises ``KeyError``
        if the AOI does not exist."""
        existing = self.get(name)
        if existing is None:
            raise KeyError(name)
        old_key = self._key(name)
        merged_name = new_name.strip() if new_name is not None else existing["name"]
        merged = {
            "name": merged_name,
            "lat": lat if lat is not None else existing["lat"],
            "lon": lon if lon is not None else existing["lon"],
            "radius_km": radius_km if radius_km is not None else existing["radius_km"],
            "created_at": existing["created_at"],
        }
        new_key = self._key(merged_name)
        self._conn.execute(
            "UPDATE aois SET name_key = ?, name = ?, lat = ?, lon = ?, radius_km = ? "
            "WHERE name_key = ?",
            (
                new_key,
                merged["name"],
                merged["lat"],
                merged["lon"],
                merged["radius_km"],
                old_key,
            ),
        )
        if new_key != old_key:
            self._conn.execute(
                "UPDATE aoi_snapshots SET name_key = ? WHERE name_key = ?",
                (new_key, old_key),
            )
        self._conn.commit()
        updated = self.get(merged_name)
        assert updated is not None  # the row was just written
        return updated

    # -- Change-detection snapshots -----------------------------------

    def get_snapshot(self, name: str) -> dict[str, Any] | None:
        """The last saved change-detection snapshot for an AOI:
        ``{"taken_at": epoch, "domains": {domain: {item_key: summary}}}``,
        or ``None`` if no sweep has been recorded."""
        row = self._conn.execute(
            "SELECT taken_at, snapshot FROM aoi_snapshots WHERE name_key = ?",
            (self._key(name),),
        ).fetchone()
        if row is None:
            return None
        taken_at, snapshot_json = row
        try:
            domains = json.loads(snapshot_json)
        except (TypeError, ValueError):
            return None
        return {"taken_at": taken_at, "domains": domains}

    def save_snapshot(self, name: str, domains: dict[str, dict[str, str]]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO aoi_snapshots (name_key, taken_at, snapshot) "
            "VALUES (?, ?, ?)",
            (self._key(name), time.time(), json.dumps(domains)),
        )
        self._conn.commit()

    def delete_snapshot(self, name: str) -> None:
        self._conn.execute(
            "DELETE FROM aoi_snapshots WHERE name_key = ?", (self._key(name),)
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Tool-facing: define / list / delete
# ---------------------------------------------------------------------------


def define_aoi(store: AOIStore, name: Any, lat: Any, lon: Any, radius_km: Any) -> dict:
    error = validate_aoi_params(name, lat, lon, radius_km)
    if error:
        return {"error": error}
    try:
        aoi = store.define(name, float(lat), float(lon), float(radius_km))
    except AOIAlreadyExists as exc:
        return {
            "error": f"AOI '{name.strip()}' already exists.",
            "existing": exc.existing,
        }
    return {"aoi": aoi, "source": "aoi-define", "timestamp": _utc_now_iso()}


def define_polygon_aoi(store: AOIStore, name: Any, vertices: Any) -> dict:
    """Define a polygon AOI from a list of [lat, lon] vertices. The
    stored lat/lon/radius_km are the derived bounding circle (centroid
    plus radius reaching the farthest vertex), which is what the coarse
    prefilters consume; membership tests use the exact polygon."""
    error = validate_polygon(name, vertices)
    if error:
        return {"error": error}
    clean_vertices = [[float(v[0]), float(v[1])] for v in vertices]
    c_lat, c_lon = polygon_centroid(clean_vertices)
    bounding_km = max(haversine_km(c_lat, c_lon, v[0], v[1]) for v in clean_vertices)
    try:
        aoi = store.define(
            name,
            c_lat,
            c_lon,
            max(MIN_RADIUS_KM, bounding_km),
            kind="polygon",
            geometry={"vertices": clean_vertices},
        )
    except AOIAlreadyExists as exc:
        return {
            "error": f"AOI '{name.strip()}' already exists.",
            "existing": exc.existing,
        }
    return {"aoi": aoi, "source": "aoi-define-polygon", "timestamp": _utc_now_iso()}


def define_corridor_aoi(
    store: AOIStore, name: Any, waypoints: Any, width_km: Any
) -> dict:
    """Define a corridor AOI: a route of [lat, lon] waypoints plus a
    total width in km. Membership means within width/2 of the
    great-circle route; the stored lat/lon/radius_km are the derived
    bounding circle for the coarse prefilters."""
    error = validate_corridor(name, waypoints, width_km)
    if error:
        return {"error": error}
    clean_waypoints = [[float(w[0]), float(w[1])] for w in waypoints]
    c_lat, c_lon = polygon_centroid(clean_waypoints)
    bounding_km = (
        max(haversine_km(c_lat, c_lon, w[0], w[1]) for w in clean_waypoints)
        + float(width_km) / 2.0
    )
    try:
        aoi = store.define(
            name,
            c_lat,
            c_lon,
            max(MIN_RADIUS_KM, bounding_km),
            kind="corridor",
            geometry={"waypoints": clean_waypoints, "width_km": float(width_km)},
        )
    except AOIAlreadyExists as exc:
        return {
            "error": f"AOI '{name.strip()}' already exists.",
            "existing": exc.existing,
        }
    return {"aoi": aoi, "source": "aoi-define-corridor", "timestamp": _utc_now_iso()}


def list_aois(store: AOIStore) -> dict:
    aois = store.list_all()
    return {
        "aois": aois,
        "count": len(aois),
        "source": "aoi-list",
        "timestamp": _utc_now_iso(),
    }


def delete_aoi(store: AOIStore, name: Any) -> dict:
    if not isinstance(name, str) or not name.strip():
        return {"error": "name must be a non-empty string."}
    deleted = store.delete(name)
    if not deleted:
        return {"error": f"AOI '{name}' not found."}
    return {
        "deleted": name.strip(),
        "source": "aoi-delete",
        "timestamp": _utc_now_iso(),
    }


def update_aoi(
    store: AOIStore,
    name: Any,
    new_name: Any = None,
    lat: Any = None,
    lon: Any = None,
    radius_km: Any = None,
) -> dict:
    """Change any subset of an existing AOI's name, center, or radius.
    A rename keeps the change-detection snapshot; a geometry change
    (center or radius) drops it, because the old snapshot described a
    different piece of the planet and diffing against it would
    manufacture fake enter/leave events."""
    if not isinstance(name, str) or not name.strip():
        return {"error": "name must be a non-empty string."}
    existing = store.get(name)
    if existing is None:
        return {
            "error": f"AOI '{name}' not found. Use intel_aoi_list to see defined areas."
        }
    if new_name is None and lat is None and lon is None and radius_km is None:
        return {
            "error": "Nothing to update: provide at least one of "
            "new_name, lat, lon, radius_km."
        }
    if existing.get("kind", "circle") != "circle" and any(
        v is not None for v in (lat, lon, radius_km)
    ):
        return {
            "error": f"AOI '{existing['name']}' is a {existing['kind']}; its "
            "lat/lon/radius_km are derived from the shape and cannot be set "
            "directly. Rename is allowed; to change the shape, delete and "
            "re-define it."
        }

    merged_name = new_name if new_name is not None else existing["name"]
    merged_lat = lat if lat is not None else existing["lat"]
    merged_lon = lon if lon is not None else existing["lon"]
    merged_radius = radius_km if radius_km is not None else existing["radius_km"]
    if existing.get("kind", "circle") == "circle":
        error = validate_aoi_params(merged_name, merged_lat, merged_lon, merged_radius)
    else:
        # Non-circle rename: only the name is user-settable; the derived
        # bounding values (e.g. a corridor's >2000 km radius) must not be
        # re-validated against circle limits.
        error = (
            None
            if isinstance(merged_name, str) and merged_name.strip()
            else "name must be a non-empty string."
        )
    if error:
        return {"error": error}

    if new_name is not None:
        new_key = AOIStore._key(new_name)
        if new_key != AOIStore._key(name):
            collision = store.get(new_name)
            if collision is not None:
                return {
                    "error": f"AOI '{new_name.strip()}' already exists.",
                    "existing": collision,
                }

    geometry_changed = any(
        value is not None and float(value) != existing[field]
        for field, value in (("lat", lat), ("lon", lon), ("radius_km", radius_km))
    )

    updated = store.update(
        name,
        new_name=str(merged_name),
        lat=float(merged_lat),
        lon=float(merged_lon),
        radius_km=float(merged_radius),
    )
    if geometry_changed:
        store.delete_snapshot(updated["name"])

    return {
        "aoi": updated,
        "previous": existing,
        "snapshot_dropped": geometry_changed,
        "source": "aoi-update",
        "timestamp": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Nearby static infrastructure: pure, no I/O, config datasets only
# ---------------------------------------------------------------------------


def _nearby_points(
    items: list[dict],
    lat: float,
    lon: float,
    radius_km: float,
    extra_keys: tuple[str, ...] = (),
) -> list[dict]:
    out = []
    for item in items:
        dist = _distance_or_none(lat, lon, item.get("lat"), item.get("lon"))
        if dist is None or dist > radius_km:
            continue
        # Coordinates ride along so shape-aware AOIs (polygon/corridor)
        # can refine this bounding-circle candidate list to exact
        # membership.
        entry = {
            "name": item.get("name"),
            "distance_km": round(dist, 1),
            "lat": item.get("lat"),
            "lon": item.get("lon"),
        }
        for key in extra_keys:
            entry[key] = item.get(key)
        out.append(entry)
    out.sort(key=lambda e: e["distance_km"])
    return out


def nearby_bases(lat: float, lon: float, radius_km: float) -> list[dict]:
    from ..config.geospatial import MILITARY_BASES

    return _nearby_points(
        MILITARY_BASES, lat, lon, radius_km, extra_keys=("country", "operator", "type")
    )


def nearby_ports(lat: float, lon: float, radius_km: float) -> list[dict]:
    from ..config.geospatial import STRATEGIC_PORTS

    return _nearby_points(
        STRATEGIC_PORTS, lat, lon, radius_km, extra_keys=("country", "type")
    )


def nearby_nuclear(lat: float, lon: float, radius_km: float) -> list[dict]:
    from ..config.geospatial import NUCLEAR_FACILITIES

    return _nearby_points(
        NUCLEAR_FACILITIES,
        lat,
        lon,
        radius_km,
        extra_keys=("country", "type", "status"),
    )


def nearby_datacenters(lat: float, lon: float, radius_km: float) -> list[dict]:
    from ..config.datacenters import AI_DATACENTERS

    return _nearby_points(AI_DATACENTERS, lat, lon, radius_km, extra_keys=("country",))


def nearby_spaceports(lat: float, lon: float, radius_km: float) -> list[dict]:
    from ..config.spaceports import SPACEPORTS

    return _nearby_points(
        SPACEPORTS, lat, lon, radius_km, extra_keys=("country", "status")
    )


def _segment_distance_or_none(
    lat: float,
    lon: float,
    lat1: Any,
    lon1: Any,
    lat2: Any,
    lon2: Any,
) -> float | None:
    try:
        if None in (lat1, lon1, lat2, lon2):
            return None
        return segment_distance_km(
            lat, lon, float(lat1), float(lon1), float(lat2), float(lon2)
        )
    except (TypeError, ValueError):
        return None


def nearby_pipelines(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Pipelines are line features. Proximity is the great-circle-segment
    distance between the AOI center and the span from
    (``lat_start``, ``lon_start``) to (``lat_end``, ``lon_end``), so a
    pipeline whose midspan crosses the AOI is detected even when both
    endpoints are far away (endpoint-only proximity missed that case
    before v0.4). Still an approximation: the config dataset carries no
    intermediate waypoints, so a pipeline that bends far off the
    endpoint-to-endpoint great circle can be misjudged; documented here
    rather than silently wrong."""
    from ..config.geospatial import PIPELINES

    out = []
    for p in PIPELINES:
        dist = _segment_distance_or_none(
            lat,
            lon,
            p.get("lat_start"),
            p.get("lon_start"),
            p.get("lat_end"),
            p.get("lon_end"),
        )
        if dist is None:
            d_start = _distance_or_none(
                lat, lon, p.get("lat_start"), p.get("lon_start")
            )
            d_end = _distance_or_none(lat, lon, p.get("lat_end"), p.get("lon_end"))
            candidates = [d for d in (d_start, d_end) if d is not None]
            if not candidates:
                continue
            dist = min(candidates)
        if dist <= radius_km:
            out.append(
                {
                    "name": p.get("name"),
                    "distance_km": round(dist, 1),
                    "route": p.get("route"),
                    "type": p.get("type"),
                    "status": p.get("status"),
                }
            )
    out.sort(key=lambda e: e["distance_km"])
    return out


def nearby_cables(lat: float, lon: float, radius_km: float) -> list[dict]:
    """Cables are multi-point routes. Proximity is the minimum
    great-circle-segment distance over consecutive published landing
    points, so a cable whose run passes the AOI between landings is
    detected (landing-point-only proximity missed that case before
    v0.4). The nearest published landing point is still reported for
    context. Real cable paths deviate from great circles; the segment is
    an approximation of the run, not its surveyed route."""
    from ..config.cables import UNDERSEA_CABLES

    out = []
    for cable in UNDERSEA_CABLES:
        points = [
            (lp.get("lat"), lp.get("lon"), lp.get("name"))
            for lp in cable.get("landing_points", [])
        ]

        best_landing_dist: float | None = None
        best_landing: str | None = None
        for plat, plon, pname in points:
            dist = _distance_or_none(lat, lon, plat, plon)
            if dist is not None and (
                best_landing_dist is None or dist < best_landing_dist
            ):
                best_landing_dist = dist
                best_landing = pname

        best_dist = best_landing_dist
        for (alat, alon, _), (blat, blon, _) in zip(points, points[1:]):
            dist = _segment_distance_or_none(lat, lon, alat, alon, blat, blon)
            if dist is not None and (best_dist is None or dist < best_dist):
                best_dist = dist

        if best_dist is not None and best_dist <= radius_km:
            out.append(
                {
                    "name": cable.get("name"),
                    "distance_km": round(best_dist, 1),
                    "nearest_landing_point": best_landing,
                    "status": cable.get("status"),
                }
            )
    out.sort(key=lambda e: e["distance_km"])
    return out


def nearby_infrastructure(
    lat: float, lon: float, radius_km: float
) -> dict[str, list[dict]]:
    """All seven static infrastructure categories near an AOI, each item
    carrying ``distance_km``. Pure: no I/O, static config datasets only."""
    return {
        "military_bases": nearby_bases(lat, lon, radius_km),
        "ports": nearby_ports(lat, lon, radius_km),
        "pipelines": nearby_pipelines(lat, lon, radius_km),
        "nuclear_facilities": nearby_nuclear(lat, lon, radius_km),
        "undersea_cables": nearby_cables(lat, lon, radius_km),
        "datacenters": nearby_datacenters(lat, lon, radius_km),
        "spaceports": nearby_spaceports(lat, lon, radius_km),
    }


_INFRA_LABELS = {
    "military_bases": "Military Bases",
    "ports": "Ports",
    "pipelines": "Pipelines",
    "nuclear_facilities": "Nuclear Facilities",
    "undersea_cables": "Undersea Cables",
    "datacenters": "Datacenters",
    "spaceports": "Spaceports",
}


# ---------------------------------------------------------------------------
# Wildfires: region-mapping (FIRMS has no point+radius query)
# ---------------------------------------------------------------------------


def _overlapping_wildfire_regions(
    lat: float, lon: float, radius_km: float
) -> list[str]:
    """Which of ``sources/wildfire.py``'s ``REGIONS`` continental bboxes
    the AOI's bounding box overlaps. FIRMS has no point+radius query, and
    ``fetch_wildfires`` exposes only these 9 continental regions, so an
    AOI is "region-mappable" whenever its box intersects at least one of
    them: true for all populated land; an AOI in open ocean far from any
    region is the honest gap. Uses the same antimeridian-aware boxes as
    the rest of the module, so an AOI just east of the dateline still
    maps into the oceania box that ends at lon 180."""
    from ..sources.wildfire import REGIONS

    overlapping = []
    for aoi_box in bboxes_from_radius_km(lat, lon, radius_km):
        a_south, a_west, a_north, a_east = (float(x) for x in aoi_box.split(","))
        for region_name, bbox in REGIONS.items():
            if region_name in overlapping:
                continue
            west, south, east, north = (float(x) for x in bbox.split(","))
            if (
                a_west <= east
                and a_east >= west
                and a_south <= north
                and a_north >= south
            ):
                overlapping.append(region_name)
    return overlapping


async def _safe_fetch(coro, label: str) -> dict:
    try:
        result = await coro
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        logger.warning("AOI brief: %s failed: %s", label, exception_summary(exc))
        return {"error": safe_error(exc)}


async def _fetch_wildfires_for_aoi(fetcher, wildfire_regions: list[str]) -> dict:
    from ..sources import wildfire

    if not wildfire_regions:
        return {"error": "AOI does not overlap any FIRMS coverage region"}

    results = await asyncio.gather(
        *[wildfire.fetch_wildfires(fetcher, region=r) for r in wildfire_regions]
    )
    clusters: list[dict] = []
    errors: list[str] = []
    for res in results:
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            errors.append(res["error"])
            continue
        for region_data in res.get("fires_by_region", {}).values():
            clusters.extend(region_data.get("top_clusters", []))
    if not clusters and errors:
        return {"error": "; ".join(sorted(set(errors)))}
    return {"clusters": clusters}


async def _fetch_military_merged(fetcher, bboxes: list[str]) -> dict:
    """Fetch military flights for each bounding box (two when the AOI
    crosses the antimeridian) and merge, deduplicating by icao24 /
    callsign — the same aircraft can appear in both boxes' data because
    the adsb.lol path returns a global list regardless of bbox. Reports
    an error only when every box failed; a partial failure is surfaced
    via ``partial_errors`` so the caller can name the gap honestly
    instead of presenting half-coverage as full."""
    from ..sources import military

    results = await asyncio.gather(
        *[
            _safe_fetch(
                military.fetch_military_flights(fetcher, bbox=b), "military_flights"
            )
            for b in bboxes
        ]
    )
    aircraft: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    any_ok = False
    for res in results:
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            errors.append(str(res["error"]))
            continue
        any_ok = True
        for ac in res.get("aircraft", []):
            key = ac.get("icao24") or ac.get("callsign")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            aircraft.append(ac)
    if not any_ok:
        return {
            "error": "; ".join(sorted(set(errors))) or "military flights fetch failed"
        }
    out: dict[str, Any] = {"aircraft": aircraft}
    if errors:
        out["partial_errors"] = sorted(set(errors))
    return out


async def _aoi_news_query(fetcher, aoi_row: dict) -> tuple[str, dict]:
    """Build the AOI's news query from where the area *is*.

    Every other domain in an AOI brief is scoped geometrically; news
    was the exception, searched by mention of whatever the user named
    the area. Measured live 2026-09-02: an AOI named "Home" returned a
    Chinese A-share IPO article and one named "PGH Square" an Indian
    op-ed, both for a fence around Pittsburgh.

    Reverse-geocoding the AOI centre gives the settlement and county
    containing it, which is what a reader means by "news near here".
    When that is unavailable (geocoder down, or genuinely unnamed
    ground such as open ocean) the AOI name is the only thing left, so
    it is used - and the returned record says so, because that is
    precisely when the articles may be unrelated.

    Returns ``(query, scoping)``.
    """
    from ..sources import geocode

    aoi_name = str(aoi_row.get("name") or "").strip()
    place_ctx = await geocode.fetch_place_context(
        fetcher, aoi_row.get("lat"), aoi_row.get("lon")
    )

    if isinstance(place_ctx, dict) and place_ctx.get("error"):
        return aoi_name, {
            "basis": "aoi_name",
            "query": aoi_name,
            "reason": f"Reverse geocoding failed: {place_ctx['error']}",
        }

    terms = place_ctx.get("terms") or [] if isinstance(place_ctx, dict) else []
    if not terms:
        return aoi_name, {
            "basis": "aoi_name",
            "query": aoi_name,
            "reason": (
                place_ctx.get("note")
                if isinstance(place_ctx, dict) and place_ctx.get("note")
                else "No named place at these coordinates."
            ),
        }

    # Quote each term so multi-word places stay phrases, and OR them so
    # a county-level story about the area still lands. The parentheses
    # are load-bearing: GDELT's DOC API returns nothing at all for a
    # bare `"a" OR "b"` and works for `("a" OR "b")` (measured live
    # 2026-09-02), so omitting them would silently empty AOI news.
    quoted = [f'"{t}"' for t in terms]
    query = quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"
    return query, {
        "basis": "geocoded",
        "query": query,
        "place": place_ctx.get("place"),
        "county": place_ctx.get("county"),
        "display_name": place_ctx.get("display_name"),
        "source": "nominatim-reverse",
    }


async def _gather_scoped_domains(fetcher, aoi_row: dict) -> dict[str, dict]:
    """Fetch all six dynamic domains in parallel and scope each to the
    AOI. The single shared scoping path for ``fetch_aoi_brief`` and
    ``fetch_aoi_changes``, so the two tools can never disagree about
    what is inside the fence.

    Per domain: ``{"items": [...]}`` (shape-filtered via
    ``filter_by_aoi``, annotated with ``distance_km``, nearest first) or
    ``{"error": msg}``. Coarse prefilters (military bboxes, wildfire
    regions) use the AOI's bounding circle; membership uses the exact
    shape. Military may additionally carry ``partial_errors``
    (antimeridian AOIs query two boxes; one can fail alone). News items
    are the raw articles, scoped by the place the AOI sits in (see
    ``_aoi_news_query``); ``scoped["news"]["scoping"]`` records whether
    that came from geocoding or fell back to the AOI's own name."""
    from ..sources import aviation, conflict, news, seismology

    lat, lon, radius_km = aoi_row["lat"], aoi_row["lon"], aoi_row["radius_km"]
    news_query, news_scoping = await _aoi_news_query(fetcher, aoi_row)
    bboxes = bboxes_from_radius_km(lat, lon, radius_km)
    wildfire_regions = _overlapping_wildfire_regions(lat, lon, radius_km)

    (
        eq_result,
        mil_result,
        conflict_result,
        wildfire_result,
        aviation_result,
        news_result,
    ) = await asyncio.gather(
        _safe_fetch(seismology.fetch_earthquakes(fetcher, hours=72), "earthquakes"),
        _fetch_military_merged(fetcher, bboxes),
        _safe_fetch(conflict.fetch_acled_events(fetcher, days=7), "acled_events"),
        _safe_fetch(_fetch_wildfires_for_aoi(fetcher, wildfire_regions), "wildfires"),
        _safe_fetch(aviation.fetch_domestic_flights(fetcher), "domestic_flights"),
        _safe_fetch(
            news.fetch_gdelt_search(
                fetcher, query=news_query, mode="artlist", limit=10
            ),
            "news",
        ),
    )

    scoped: dict[str, dict] = {}

    def _scope(
        result: Any, list_key: str, domain: str, lat_key: str, lon_key: str
    ) -> None:
        if isinstance(result, dict) and result.get("error"):
            scoped[domain] = {"error": str(result["error"])}
            return
        items = result.get(list_key, []) if isinstance(result, dict) else []
        entry: dict[str, Any] = {
            "items": filter_by_aoi(items, aoi_row, lat_key, lon_key)
        }
        if isinstance(result, dict) and result.get("partial_errors"):
            entry["partial_errors"] = result["partial_errors"]
        scoped[domain] = entry

    _scope(eq_result, "earthquakes", "earthquakes", "latitude", "longitude")
    _scope(mil_result, "aircraft", "military_flights", "latitude", "longitude")
    _scope(conflict_result, "events", "conflict_events", "latitude", "longitude")
    _scope(wildfire_result, "clusters", "wildfires", "lat", "lon")
    _scope(aviation_result, "sampled", "aviation", "lat", "lon")

    if isinstance(news_result, dict) and news_result.get("error"):
        scoped["news"] = {"error": str(news_result["error"]), "scoping": news_scoping}
    else:
        scoped["news"] = {
            "items": (
                news_result.get("articles", []) if isinstance(news_result, dict) else []
            ),
            "scoping": news_scoping,
        }
    return scoped


# ---------------------------------------------------------------------------
# intel_aoi_brief
# ---------------------------------------------------------------------------


async def fetch_aoi_brief(fetcher, store: AOIStore, name: Any) -> dict:
    """Compose a cited brief for a user-defined AOI: earthquakes, military
    flights (bbox-derived), wildfires (region-mapped), ACLED conflict
    events, sampled aviation traffic, nearby static infrastructure, and
    news headline mentions of the AOI name. Every listed item carries a
    ``[n]`` citation; ``data_gaps`` names every domain that could not be
    scoped to the AOI rather than silently omitting it.
    """
    if not isinstance(name, str) or not name.strip():
        return {"error": "name must be a non-empty string."}

    aoi = store.get(name)
    if aoi is None:
        return {
            "error": f"AOI '{name}' not found. Use intel_aoi_list to see defined areas."
        }

    lat, lon, radius_km = aoi["lat"], aoi["lon"], aoi["radius_km"]
    aoi_name = aoi["name"]
    aoi_kind = aoi.get("kind", "circle")

    scoped = await _gather_scoped_domains(fetcher, aoi)

    sources: list[dict] = []
    citations: dict[str, list[int]] = {}
    data_gaps: list[str] = []
    counts: dict[str, int] = {}
    sections: dict[str, list[str]] = {}

    def _line(domain: str, text: str) -> None:
        sections.setdefault(domain, []).append(text)

    # Earthquakes ------------------------------------------------------
    if scoped["earthquakes"].get("error"):
        data_gaps.append(f"Earthquakes: {scoped['earthquakes']['error']}")
        counts["earthquakes"] = 0
    else:
        eq_in_range = scoped["earthquakes"]["items"]
        counts["earthquakes"] = len(eq_in_range)
        for eq in eq_in_range:
            n = _add_source(
                sources,
                "earthquakes",
                f"M{eq.get('magnitude')} earthquake, {eq.get('place') or 'unknown location'} "
                f"({eq['distance_km']} km from {aoi_name})",
                url=eq.get("url"),
                timestamp=eq.get("time"),
            )
            _cite(citations, "earthquakes", n)
            _line(
                "earthquakes",
                f"- [{n}] M{eq.get('magnitude')} {eq.get('place')} ({eq['distance_km']} km)",
            )

    # Military flights ---------------------------------------------------
    if scoped["military_flights"].get("error"):
        data_gaps.append(f"Military flights: {scoped['military_flights']['error']}")
        counts["military_flights"] = 0
    else:
        mil_in_range = scoped["military_flights"]["items"]
        for partial in scoped["military_flights"].get("partial_errors", []):
            data_gaps.append(f"Military flights: partial coverage: {partial}")
        counts["military_flights"] = len(mil_in_range)
        if mil_in_range:
            n = _add_source(
                sources,
                "military_flights",
                f"{len(mil_in_range)} military aircraft within {radius_km} km of {aoi_name}",
            )
            _cite(citations, "military_flights", n)
            for ac in mil_in_range[:10]:
                _line(
                    "military_flights",
                    f"- [{n}] {ac.get('callsign') or ac.get('icao24')} "
                    f"({ac.get('origin_country') or 'unknown origin'}, {ac['distance_km']} km)",
                )

    # Conflict (ACLED) ----------------------------------------------------
    if scoped["conflict_events"].get("error"):
        data_gaps.append(f"Conflict events: {scoped['conflict_events']['error']}")
        counts["conflict_events"] = 0
    else:
        conflict_in_range = scoped["conflict_events"]["items"]
        counts["conflict_events"] = len(conflict_in_range)
        for ev in conflict_in_range[:10]:
            loc = (
                ev.get("location")
                or ev.get("admin1")
                or ev.get("country")
                or "unspecified location"
            )
            n = _add_source(
                sources,
                "conflict_events",
                f"{ev.get('event_type') or 'conflict event'}, {loc} "
                f"({ev['distance_km']} km from {aoi_name})",
                timestamp=ev.get("event_date"),
            )
            _cite(citations, "conflict_events", n)
            _line(
                "conflict_events",
                f"- [{n}] {ev.get('event_type')}, {loc} ({ev['distance_km']} km)",
            )

    # Wildfires ------------------------------------------------------------
    if scoped["wildfires"].get("error"):
        data_gaps.append(f"Wildfires: {scoped['wildfires']['error']}")
        counts["wildfires"] = 0
    else:
        fire_in_range = scoped["wildfires"]["items"]
        counts["wildfires"] = len(fire_in_range)
        for fc in fire_in_range:
            n = _add_source(
                sources,
                "wildfires",
                f"{fc.get('fire_count')} fire detections ({fc['distance_km']} km from {aoi_name})",
            )
            _cite(citations, "wildfires", n)
            _line(
                "wildfires",
                f"- [{n}] {fc.get('fire_count')} detections, max FRP {fc.get('max_frp')} ({fc['distance_km']} km)",
            )

    # Aviation (sampled) ---------------------------------------------------
    if scoped["aviation"].get("error"):
        data_gaps.append(f"Aviation: {scoped['aviation']['error']}")
        counts["aviation"] = 0
    else:
        aviation_in_range = scoped["aviation"]["items"]
        counts["aviation"] = len(aviation_in_range)
        if aviation_in_range:
            n = _add_source(
                sources,
                "aviation",
                f"{len(aviation_in_range)} sampled aircraft within {radius_km} km of {aoi_name} "
                "(1-in-10 global sample, not exhaustive)",
            )
            _cite(citations, "aviation", n)
            _line(
                "aviation",
                f"- [{n}] {len(aviation_in_range)} sampled aircraft observed",
            )

    # News (scoped to the place the AOI sits in) ---------------------------
    news_scoping = scoped["news"].get("scoping") or {}
    if news_scoping.get("basis") == "aoi_name":
        data_gaps.append(
            "News is scoped by the AOI's name, not its location "
            f"({news_scoping.get('reason', 'reverse geocoding unavailable')}); "
            "articles may be unrelated to the area."
        )
    if scoped["news"].get("error"):
        data_gaps.append(f"News: {scoped['news']['error']}")
        counts["news"] = 0
        articles: list = []
    else:
        articles = scoped["news"]["items"]
        counts["news"] = len(articles)
    for art in articles[:10]:
        title = art.get("title")
        if not title:
            continue
        n = _add_source(
            sources, "news", title, url=art.get("url"), timestamp=art.get("seendate")
        )
        _cite(citations, "news", n)
        _line("news", f"- [{n}] {title} ({art.get('domain') or 'unknown source'})")

    # Nearby static infrastructure -------------------------------------
    # Candidates come from the bounding circle; for polygon/corridor AOIs
    # the point categories are then refined to the exact shape. Line
    # features (pipelines, cables) keep bounding-circle matching, named
    # honestly below rather than silently approximated.
    infra = nearby_infrastructure(lat, lon, radius_km)
    _LINE_CATEGORIES = {"pipelines", "undersea_cables"}
    if aoi_kind != "circle":
        for category, items in infra.items():
            if category in _LINE_CATEGORIES:
                continue
            infra[category] = [
                item
                for item in items
                if item.get("lat") is not None
                and item.get("lon") is not None
                and aoi_contains(aoi, float(item["lat"]), float(item["lon"]))
            ]
        if infra["pipelines"] or infra["undersea_cables"]:
            data_gaps.append(
                f"Pipelines/undersea cables: matched against the bounding "
                f"circle, not the exact {aoi_kind} shape."
            )
    for category, items in infra.items():
        counts[category] = len(items)
        for item in items:
            n = _add_source(
                sources,
                category,
                f"{item['name']} ({item['distance_km']} km from {aoi_name})",
            )
            _cite(citations, category, n)
            _line(category, f"- [{n}] {item['name']} ({item['distance_km']} km)")

    # --- Markdown ---------------------------------------------------------
    if aoi_kind == "polygon":
        shape_line = (
            f"Polygon: {len(aoi['geometry']['vertices'])} vertices "
            f"(bounding center {lat:.4f}, {lon:.4f}; radius {radius_km:.0f} km)"
        )
    elif aoi_kind == "corridor":
        geometry = aoi["geometry"]
        shape_line = (
            f"Corridor: {len(geometry['waypoints'])} waypoints, width "
            f"{geometry['width_km']:.0f} km (bounding center {lat:.4f}, "
            f"{lon:.4f}; radius {radius_km:.0f} km)"
        )
    else:
        shape_line = f"Center: {lat}, {lon} (radius {radius_km} km)"
    md_parts = [
        f"# AOI Brief: {aoi_name}",
        shape_line,
        "",
    ]
    section_order = [
        ("earthquakes", "Earthquakes"),
        ("military_flights", "Military Flights"),
        ("conflict_events", "Conflict Events (ACLED)"),
        ("wildfires", "Wildfires"),
        ("aviation", "Aviation (sampled)"),
        ("news", "News Headline Mentions"),
        *[(k, v) for k, v in _INFRA_LABELS.items()],
    ]
    for domain, label in section_order:
        lines = sections.get(domain)
        md_parts.append(f"## {label}")
        md_parts.append("\n".join(lines) if lines else "_None observed in range._")
        md_parts.append("")

    if data_gaps:
        md_parts.append("## Data Gaps")
        md_parts.extend(f"- {gap}" for gap in data_gaps)

    markdown = "\n".join(md_parts)
    cited = _has_valid_citation(markdown, len(sources))

    return {
        "aoi": {
            "name": aoi_name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "kind": aoi_kind,
        },
        "markdown": markdown,
        "counts": counts,
        "sources": sources,
        "cited": cited,
        "data_gaps": data_gaps,
        "news_scoping": news_scoping,
        "source": "aoi-brief",
        "timestamp": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# intel_aoi_escalation (optional, reuses analysis/escalation.py unmodified)
# ---------------------------------------------------------------------------


async def fetch_aoi_escalation(fetcher, store: AOIStore, name: Any) -> dict:
    """Run the existing hotspot escalation scoring (``score_hotspot`` in
    ``analysis/escalation.py``, unmodified) on a user-defined AOI instead
    of only the 22 built-in intel hotspots. Gathers military and conflict
    signals scoped to the AOI's own radius (via haversine), rather than
    the fixed 2-degree window ``fetch_hotspot_escalation`` uses for the
    built-ins."""
    if not isinstance(name, str) or not name.strip():
        return {"error": "name must be a non-empty string."}

    aoi = store.get(name)
    if aoi is None:
        return {
            "error": f"AOI '{name}' not found. Use intel_aoi_list to see defined areas."
        }

    from ..sources import conflict as conflict_mod

    lat, lon, radius_km = aoi["lat"], aoi["lon"], aoi["radius_km"]
    aoi_name = aoi["name"]
    bboxes = bboxes_from_radius_km(lat, lon, radius_km)

    acled_result, mil_result = await asyncio.gather(
        _safe_fetch(conflict_mod.fetch_acled_events(fetcher, days=7), "acled_events"),
        _fetch_military_merged(fetcher, bboxes),
    )

    data_gaps: list[str] = []
    conflict_count = fatalities = protests = 0
    if isinstance(acled_result, dict) and acled_result.get("error"):
        data_gaps.append(f"Conflict events: {acled_result['error']}")
    else:
        events = filter_by_aoi(
            acled_result.get("events", []) if isinstance(acled_result, dict) else [],
            aoi,
            "latitude",
            "longitude",
        )
        for ev in events:
            if "protest" in (ev.get("event_type") or "").lower():
                protests += 1
            else:
                conflict_count += 1
            fatalities += ev.get("fatalities") or 0

    military_count = 0
    if isinstance(mil_result, dict) and mil_result.get("error"):
        data_gaps.append(f"Military flights: {mil_result['error']}")
    else:
        for partial in (
            mil_result.get("partial_errors", []) if isinstance(mil_result, dict) else []
        ):
            data_gaps.append(f"Military flights: partial coverage: {partial}")
        aircraft = filter_by_aoi(
            mil_result.get("aircraft", []) if isinstance(mil_result, dict) else [],
            aoi,
            "latitude",
            "longitude",
        )
        military_count = len(aircraft)

    hotspot_config = {
        "lat": lat,
        "lon": lon,
        "baseline_escalation": 0,
        "associated_countries": [],
    }
    scored = score_hotspot(
        hotspot_config,
        news_mentions=None,
        military_count=military_count,
        conflict_events=conflict_count,
        convergence_score=None,
        fatalities=fatalities,
        protests=protests,
    )

    return {
        "aoi": {
            "name": aoi_name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "kind": aoi.get("kind", "circle"),
        },
        "unavailable_components": ["news", "convergence"],
        "data_gaps": data_gaps,
        "source": "aoi-escalation",
        "timestamp": _utc_now_iso(),
        **scored,
    }


# ---------------------------------------------------------------------------
# intel_aoi_changes: geofence change detection
# ---------------------------------------------------------------------------

# How each diffable domain identifies an item across sweeps and summarizes
# it for the report. Aviation is deliberately absent: the 1-in-10 global
# sample is pure churn between sweeps, and diffing it would manufacture
# fake enter/leave events every run. Static infrastructure is absent
# because it cannot change between sweeps.
_CHANGE_DOMAINS: dict[str, tuple[Any, Any]] = {
    "earthquakes": (
        lambda i: str(
            i.get("id")
            or f"eq:{i.get('magnitude')}@{i.get('latitude')},{i.get('longitude')}"
        ),
        lambda i: f"M{i.get('magnitude')} {i.get('place') or 'unknown location'} "
        f"({i.get('distance_km')} km)",
    ),
    "military_flights": (
        lambda i: str(i.get("icao24") or i.get("callsign") or "unknown"),
        lambda i: f"{i.get('callsign') or i.get('icao24')} "
        f"({i.get('origin_country') or 'unknown origin'}, {i.get('distance_km')} km)",
    ),
    "conflict_events": (
        lambda i: f"{i.get('event_date')}|{i.get('event_type')}|"
        f"{i.get('location') or i.get('admin1') or i.get('country')}",
        lambda i: f"{i.get('event_type') or 'conflict event'}, "
        f"{i.get('location') or i.get('admin1') or i.get('country') or 'unspecified'} "
        f"({i.get('distance_km')} km)",
    ),
    "wildfires": (
        lambda i: f"fire:{round(float(i.get('lat', 0.0)), 2)},"
        f"{round(float(i.get('lon', 0.0)), 2)}",
        lambda i: f"{i.get('fire_count')} fire detections ({i.get('distance_km')} km)",
    ),
    "news": (
        lambda i: str(i.get("url") or i.get("title") or "untitled"),
        lambda i: str(i.get("title") or i.get("url") or "untitled"),
    ),
}

_CHANGE_GAP_LABELS = {
    "earthquakes": "Earthquakes",
    "military_flights": "Military flights",
    "conflict_events": "Conflict events",
    "wildfires": "Wildfires",
    "news": "News",
}


async def fetch_aoi_changes(fetcher, store: AOIStore, name: Any) -> dict:
    """What entered or left a user-defined AOI since the last sweep: the
    geofence alerting primitive. Runs the same scoped gather as
    ``fetch_aoi_brief``, diffs each diffable domain against the AOI's
    stored snapshot, then saves the new snapshot.

    Honesty invariants: the first sweep is a ``baseline`` (nothing is
    claimed to have entered or left); a domain whose fetch failed goes to
    ``data_gaps``, is excluded from ``changes`` (a failed fetch must
    never read as "everything left the area"), and keeps its previous
    snapshot slice so the next successful sweep diffs against the last
    real observation."""
    if not isinstance(name, str) or not name.strip():
        return {"error": "name must be a non-empty string."}

    aoi = store.get(name)
    if aoi is None:
        return {
            "error": f"AOI '{name}' not found. Use intel_aoi_list to see defined areas."
        }

    lat, lon, radius_km = aoi["lat"], aoi["lon"], aoi["radius_km"]
    aoi_name = aoi["name"]

    scoped = await _gather_scoped_domains(fetcher, aoi)

    previous = store.get_snapshot(aoi_name)
    prev_domains: dict[str, dict[str, str]] = previous["domains"] if previous else {}
    baseline = previous is None

    changes: dict[str, dict[str, Any]] = {}
    data_gaps: list[str] = []
    counts: dict[str, int] = {}
    next_domains: dict[str, dict[str, str]] = {}

    for domain, (key_fn, summary_fn) in _CHANGE_DOMAINS.items():
        result = scoped.get(domain, {})
        label = _CHANGE_GAP_LABELS[domain]
        if result.get("error"):
            data_gaps.append(f"{label}: {result['error']}")
            # Keep the last real observation for the next successful diff.
            if domain in prev_domains:
                next_domains[domain] = prev_domains[domain]
            continue
        for partial in result.get("partial_errors", []):
            data_gaps.append(f"{label}: partial coverage: {partial}")

        current: dict[str, str] = {}
        for item in result.get("items", []):
            try:
                current[key_fn(item)] = summary_fn(item)
            except (TypeError, ValueError):
                continue
        counts[domain] = len(current)
        next_domains[domain] = current

        if baseline or domain not in prev_domains:
            changes[domain] = {
                "new": [],
                "departed": [],
                "unchanged": len(current),
                "baseline": True,
            }
            continue

        prev_items = prev_domains[domain]
        new_keys = [k for k in current if k not in prev_items]
        departed_keys = [k for k in prev_items if k not in current]
        changes[domain] = {
            "new": [{"key": k, "summary": current[k]} for k in new_keys],
            "departed": [{"key": k, "summary": prev_items[k]} for k in departed_keys],
            "unchanged": len(current) - len(new_keys),
        }

    store.save_snapshot(aoi_name, next_domains)

    return {
        "aoi": {
            "name": aoi_name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "kind": aoi.get("kind", "circle"),
        },
        "baseline": baseline,
        "previous_taken_at": (
            datetime.fromtimestamp(previous["taken_at"], tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if previous
            else None
        ),
        "changes": changes,
        "counts": counts,
        "data_gaps": data_gaps,
        "source": "aoi-changes",
        "timestamp": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# intel_aoi_digest: one-call change sweep across all AOIs
# ---------------------------------------------------------------------------

_DIGEST_CONCURRENCY = 3


async def fetch_aoi_digest(fetcher, store: AOIStore, names: Any = None) -> dict:
    """Run the change sweep (``fetch_aoi_changes``) across every defined
    AOI - or the named subset - in one call, and compose a cross-AOI
    digest. This IS a sweep: each AOI's change snapshot advances, so a
    scheduler can call this one tool on an interval and get "what
    entered or left each of my areas since last time".

    Concurrency is capped (upstream fetches for the shared global
    domains hit the cache across AOIs; per-AOI fetches like military
    bboxes and news queries do not). Honest shapes throughout: no AOIs
    is a note, not an error; unknown requested names ARE an error; a
    domain failure stays inside that AOI's ``data_gaps``.
    """
    all_aois = store.list_all()
    if names is not None:
        if not isinstance(names, list) or not all(
            isinstance(n, str) and n.strip() for n in names
        ):
            return {"error": "names must be a list of non-empty strings."}
        by_key = {a["name"].strip().lower(): a for a in all_aois}
        missing = [n for n in names if n.strip().lower() not in by_key]
        if missing:
            return {
                "error": "Unknown AOI name(s): "
                + ", ".join(sorted(missing))
                + ". Use intel_aoi_list to see defined areas."
            }
        selected = [by_key[n.strip().lower()] for n in names]
    else:
        selected = all_aois

    if not selected:
        return {
            "aois": [],
            "count": 0,
            "note": "No AOIs defined. Use intel_aoi_define (or the polygon/"
            "corridor variants) to create one.",
            "source": "aoi-digest",
            "timestamp": _utc_now_iso(),
        }

    semaphore = asyncio.Semaphore(_DIGEST_CONCURRENCY)

    async def _sweep(row: dict) -> dict:
        async with semaphore:
            return await fetch_aoi_changes(fetcher, store, row["name"])

    results = await asyncio.gather(*[_sweep(row) for row in selected])

    entries: list[dict] = []
    total_new = total_departed = 0
    md_parts = ["# AOI Digest", ""]
    for row, result in zip(selected, results):
        if "error" in result and "changes" not in result:
            # A per-AOI hard failure (e.g. deleted concurrently): keep
            # it visible without sinking the whole digest.
            entries.append({"name": row["name"], "error": result["error"]})
            md_parts += [f"## {row['name']}", f"_Error: {result['error']}_", ""]
            continue
        new_count = sum(len(c["new"]) for c in result["changes"].values())
        departed_count = sum(len(c["departed"]) for c in result["changes"].values())
        total_new += new_count
        total_departed += departed_count
        entries.append(
            {
                "name": result["aoi"]["name"],
                "kind": result["aoi"]["kind"],
                "baseline": result["baseline"],
                "counts": result["counts"],
                "changes": result["changes"],
                "data_gaps": result["data_gaps"],
            }
        )
        md_parts.append(f"## {result['aoi']['name']} ({result['aoi']['kind']})")
        if result["baseline"]:
            md_parts.append(
                "_Baseline established; changes will report from the next sweep._"
            )
        elif new_count == 0 and departed_count == 0:
            md_parts.append("_No changes since the last sweep._")
        else:
            for domain, change in result["changes"].items():
                for item in change["new"]:
                    md_parts.append(f"- NEW [{domain}] {item['summary']}")
                for item in change["departed"]:
                    md_parts.append(f"- DEPARTED [{domain}] {item['summary']}")
        for gap in result["data_gaps"]:
            md_parts.append(f"- data gap: {gap}")
        md_parts.append("")

    return {
        "aois": entries,
        "count": len(entries),
        "totals": {"new_items": total_new, "departed_items": total_departed},
        "markdown": "\n".join(md_parts),
        "source": "aoi-digest",
        "timestamp": _utc_now_iso(),
    }


async def _notify_aoi_changes(fetcher, digest: dict) -> dict:
    """POST a non-quiet sweep digest to the operator's webhook.

    Returns the honest delivery record attached to the digest as
    ``notification``: unconfigured, skipped (quiet sweep), sent with
    the status code, or failed with the error. Never raises — the
    sweep already advanced its snapshots and must not fail because a
    sink is down; but the failure is recorded, never swallowed.
    """
    url = os.environ.get("WORLD_INTEL_AOI_WEBHOOK", "").strip()
    if not url:
        return {"configured": False}
    fmt = os.environ.get("WORLD_INTEL_AOI_WEBHOOK_FORMAT", "json").strip().lower()
    if fmt not in ("json", "text"):
        return {
            "configured": True,
            "sent": False,
            "error": (
                f"Unknown WORLD_INTEL_AOI_WEBHOOK_FORMAT {fmt!r}; use 'json' or 'text'."
            ),
        }
    totals = digest.get("totals") or {}
    new_items = totals.get("new_items") or 0
    departed = totals.get("departed_items") or 0
    if new_items + departed == 0:
        return {"configured": True, "sent": False, "reason": "no changes this sweep"}
    title = f"AOI sweep: {new_items} new, {departed} departed"
    try:
        if fmt == "text":
            resp = await fetcher.post(
                url,
                content=digest.get("markdown", ""),
                headers={"Title": title, "Content-Type": "text/markdown"},
            )
        else:
            resp = await fetcher.post(
                url,
                json={
                    "title": title,
                    "totals": totals,
                    "markdown": digest.get("markdown", ""),
                    "timestamp": digest.get("timestamp"),
                },
            )
    except Exception as exc:
        return {"configured": True, "sent": False, "error": safe_error(exc)[:200]}
    return {
        "configured": True,
        "sent": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
    }


async def fetch_aoi_sweep(fetcher) -> dict:
    """Collector-daemon entry point for the AOI change sweep.

    The collector calls every source as ``fn(fetcher)`` and cannot
    supply the store ``fetch_aoi_digest`` needs, so this wrapper opens
    the AOI store on the fetcher's own cache database — the same SQLite
    file the MCP server and CLI write AOIs to — runs the digest (every
    AOI's snapshot advances), and closes the store handle. If
    ``WORLD_INTEL_AOI_WEBHOOK`` is set, a non-quiet digest is POSTed
    there and the delivery record lands in ``notification``.
    """
    store = AOIStore(fetcher.cache.db_path)
    try:
        digest = await fetch_aoi_digest(fetcher, store)
    finally:
        store.close()
    digest["notification"] = await _notify_aoi_changes(fetcher, digest)
    return digest
