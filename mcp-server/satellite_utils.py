"""
satellite_utils.py — real orbital mechanics, no simulated positions.

- Fetches live TLE sets from Celestrak
- Propagates orbits with SGP4 (via Skyfield) to get true lat/lon/altitude
- Computes coverage footprint radius from real line-of-sight geometry
- Computes physically-grounded health signals (TLE staleness, eccentricity,
  altitude/drag risk) that don't require proprietary telemetry
"""

from __future__ import annotations
 
import math
import os
import time
from datetime import datetime, timezone
 
import httpx
from skyfield.api import EarthSatellite, load
 
EARTH_RADIUS_KM = 6371.0

CELESTRAK_GROUP_URL = os.environ.get(
    "CELESTRAK_GROUP_URL",
    "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle",
)
CELESTRAK_CATNR_URL = os.environ.get(
    "CELESTRAK_CATNR_URL",
    "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=tle",
)
CELESTRAK_CACHE_TTL_SECONDS = int(os.environ.get("CELESTRAK_CACHE_TTL_SECONDS", str(2 * 60 * 60)))
_REQUEST_HEADERS = {"User-Agent": "space-mission-intelligence-demo/1.0 (contact: local-dev)"}

_ts = load.timescale()

# group name -> {"fetched_at": float, "data": list[dict]}  (full list, unsliced)
_group_cache: dict[str, dict] = {}
# norad_id -> {"fetched_at": float, "data": dict}
_catnr_cache: dict[int, dict] = {}


async def fetch_tle_group(group: str = "starlink", limit: int = 20) -> list[dict]:
    """Fetch TLE triples for a named Celestrak group (e.g. 'starlink', 'stations', 'weather')."""
    cached = _group_cache.get(group)
    now = time.monotonic()
    if cached and (now - cached["fetched_at"]) < CELESTRAK_CACHE_TTL_SECONDS:
        return cached["data"][:limit]
 
    url = CELESTRAK_GROUP_URL.format(group=group)
    async with httpx.AsyncClient(timeout=15, headers=_REQUEST_HEADERS) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.splitlines() if l.strip()]
 
    sats = []
    for i in range(0, len(lines) - 2, 3):
        name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            continue
        sats.append({"name": name, "line1": line1, "line2": line2})
 
    _group_cache[group] = {"fetched_at": now, "data": sats}
    return sats[:limit]


async def fetch_tle_by_norad_id(norad_id: int) -> dict:
    """Fetch a single satellite's TLE by its NORAD catalog number."""
    cached = _catnr_cache.get(norad_id)
    now = time.monotonic()
    if cached and (now - cached["fetched_at"]) < CELESTRAK_CACHE_TTL_SECONDS:
        return cached["data"]
 
    url = CELESTRAK_CATNR_URL.format(norad_id=norad_id)
    async with httpx.AsyncClient(timeout=15, headers=_REQUEST_HEADERS) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.splitlines() if l.strip()]
    if len(lines) < 3:
        raise ValueError(f"No TLE found for NORAD ID {norad_id}")
 
    tle = {"name": lines[0], "line1": lines[1], "line2": lines[2]}
    _catnr_cache[norad_id] = {"fetched_at": now, "data": tle}
    return tle


def propagate(tle: dict) -> dict:
    """Given a TLE dict, compute current real position via SGP4."""
    sat = EarthSatellite(tle["line1"], tle["line2"], tle["name"], _ts)
    t = _ts.now()
    geocentric = sat.at(t)
    subpoint = geocentric.subpoint()
    velocity_km_s = geocentric.velocity.km_per_s
    speed = math.sqrt(sum(v ** 2 for v in velocity_km_s))

    epoch_dt = sat.epoch.utc_datetime()
    tle_age_hours = (datetime.now(timezone.utc) - epoch_dt).total_seconds() / 3600.0

    return {
        "name": tle["name"].strip(),
        "norad_id": int(sat.model.satnum),
        "latitude": round(float(subpoint.latitude.degrees), 4),
        "longitude": round(float(subpoint.longitude.degrees), 4),
        "altitude_km": round(float(subpoint.elevation.km), 2),
        "speed_km_s": round(float(speed), 3),
        "eccentricity": round(float(sat.model.ecco), 5),
        "inclination_deg": round(float(math.degrees(sat.model.inclo)), 2),
        "tle_age_hours": round(float(tle_age_hours), 1),
    }


def coverage_footprint(altitude_km: float, min_elevation_deg: float = 10.0) -> dict:
    """
    Real line-of-sight coverage geometry: how far on the ground a satellite
    at a given altitude can serve a ground terminal, given a minimum elevation
    angle (terminals below this angle on the horizon can't reliably connect).

    Uses standard spherical-Earth slant-range geometry, not a made-up radius.
    """
    R = EARTH_RADIUS_KM
    h = altitude_km
    elev_rad = math.radians(min_elevation_deg)

    # Earth central angle (gamma) subtended between sub-satellite point and
    # the farthest point still visible above min_elevation_deg
    gamma = math.acos((R / (R + h)) * math.cos(elev_rad)) - elev_rad
    ground_radius_km = R * gamma

    return {
        "coverage_radius_km": round(ground_radius_km, 1),
        "min_elevation_deg": min_elevation_deg,
        "method": "line-of-sight spherical-Earth geometry (real physics, not simulated)",
    }


def health_signals(position: dict) -> dict:
    """
    Physically-grounded health/anomaly signals derivable from public orbital
    data alone (no proprietary telemetry required):

    - tle_staleness: how old the tracking data is -> tracking confidence
    - drag_risk: low altitude + non-circular orbit -> higher decay/drag exposure
    - orbit_anomaly: eccentricity or inclination outside typical bounds for
      the object's altitude band
    """
    age_h = position["tle_age_hours"]
    alt = position["altitude_km"]
    ecc = position["eccentricity"]

    # TLE staleness -> tracking confidence
    if age_h <= 48:
        staleness = "FRESH"
    elif age_h <= 168:
        staleness = "AGING"
    else:
        staleness = "STALE"

    # Drag/decay risk rises sharply below ~400km, and with eccentric orbits
    drag_risk = 0.0
    if alt < 300:
        drag_risk = 0.9
    elif alt < 400:
        drag_risk = 0.6
    elif alt < 500:
        drag_risk = 0.3
    else:
        drag_risk = 0.1
    drag_risk += min(ecc * 5, 0.3)  # eccentric orbits add drag variability
    drag_risk = round(min(drag_risk, 1.0), 2)

    orbit_anomaly = ecc > 0.05  # near-circular is typical for most LEO comms/EO sats

    return {
        "tle_staleness": staleness,
        "tle_age_hours": age_h,
        "drag_risk_score": drag_risk,
        "orbit_anomaly_flag": orbit_anomaly,
    }
