"""Focused display normalization only; shared military/MCP contracts stay intact."""
import math


def display_aircraft(value):
    if not isinstance(value, dict) or not isinstance(value.get("aircraft"), list):
        return value
    result = dict(value)
    provider = value.get("source")
    aircraft = []
    for item in value["aircraft"]:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        altitude = item.get("altitude_m")
        if type(altitude) not in (int, float) or not math.isfinite(altitude):
            altitude = None
        elif provider == "adsb.lol":
            # This adapter historically copies alt_baro (feet) into altitude_m.
            # An explicit normalized unit on a focused copy makes this idempotent.
            unit = item.get("altitude_unit")
            altitude = altitude if unit == "m" else altitude * 0.3048 if unit is None else None
        elif provider != "opensky":
            altitude = None  # No guessing for unrecognized provider contracts.
        item["altitude_m"] = altitude
        item["altitude_unit"] = "m" if altitude is not None else "unknown"
        aircraft.append(item)
    result["aircraft"] = aircraft
    return result
