"""
zones.py — shared city-zone grid for the data-science layer.

The simulator (seed_data.py) and the forecaster (time_series.py) must agree on
what a "zone" is, so the definitions live here. Zones are fixed offsets from a
city center resolved once per process:

  1. CITY_CENTER env var ("lng,lat") if set,
  2. else the first real (non-synthetic) user's location in MongoDB,
  3. else a hard default (the pilot city in Madhya Pradesh, India).

Coordinates follow the platform-wide GeoJSON rule: LONGITUDE FIRST.
"""

import math
import os

import db

_DEFAULT_CENTER = (75.771993, 24.259643)  # lng, lat — pilot city

# name, km east of center, km north of center, relative surplus weight.
# Weights reflect how much surplus a zone generates (dense restaurant/bazaar
# areas high, residential outskirts low).
_ZONE_SPECS = [
    ("City Center",       0.0,  0.0, 1.00),
    ("Old Town Bazaar",  -2.2,  1.4, 0.85),
    ("Station Road",      1.8,  2.6, 0.75),
    ("Market Yard",      -3.5, -1.8, 0.70),
    ("University Ward",   4.2,  1.2, 0.55),
    ("Industrial Estate", 5.5, -3.0, 0.45),
    ("Riverside Colony", -1.5,  4.8, 0.40),
    ("Bypass South",      2.5, -5.2, 0.30),
]

_zones_cache = None


def haversine_km(lng1, lat1, lng2, lat2):
    """Great-circle distance in km between two (lng, lat) points."""
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _resolve_center():
    raw = os.getenv("CITY_CENTER", "").strip()
    if raw:
        try:
            lng, lat = (float(part) for part in raw.split(","))
            return lng, lat
        except ValueError:
            print(f"[zones] WARNING: ignoring malformed CITY_CENTER={raw!r}")
    real_user = db.users.find_one(
        # The bootstrapped admin sits at (0,0) — never let it define the city.
        {"is_synthetic": {"$ne": True}, "role": {"$ne": "admin"}, "location.type": "Point"},
        sort=[("created_at", 1)],
    )
    if real_user:
        lng, lat = real_user["location"]["coordinates"]
        return float(lng), float(lat)
    return _DEFAULT_CENTER


def get_zones():
    """Zone list: [{name, lng, lat, weight}, ...]. Cached per process."""
    global _zones_cache
    if _zones_cache is None:
        center_lng, center_lat = _resolve_center()
        km_per_deg_lat = 111.32
        km_per_deg_lng = 111.32 * math.cos(math.radians(center_lat))
        _zones_cache = [
            {
                "name": name,
                "lng": round(center_lng + east_km / km_per_deg_lng, 6),
                "lat": round(center_lat + north_km / km_per_deg_lat, 6),
                "weight": weight,
            }
            for name, east_km, north_km, weight in _ZONE_SPECS
        ]
    return _zones_cache


def assign_zone(lng, lat):
    """Name of the nearest zone centroid to (lng, lat)."""
    return min(
        get_zones(),
        key=lambda zone: haversine_km(lng, lat, zone["lng"], zone["lat"]),
    )["name"]
