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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from skyfield.api import EarthSatellite, Topos, load

import socket
 
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


async def compute_contact_windows(
    norad_id: int,
    ground_stations: list[dict[str, Any]],
    duration_hours: float = 24,
    step_seconds: int = 30,
) -> list[dict]:
    """Compute upcoming satellite–ground contact windows using SGP4 propagation.

    For each ground station, steps through the next `duration_hours` at
    `step_seconds` intervals and detects elevation crossings above each
    station's minimum elevation angle. Returns all windows across all stations,
    sorted by AOS (Acquisition of Signal) time.

    Args:
        norad_id:        NORAD catalog number of the target satellite.
        ground_stations: List of dicts, each with:
                           name            (str)   — human-readable station label
                           lat             (float) — latitude in degrees
                           lon             (float) — longitude in degrees (east positive)
                           min_elevation_deg (float) — minimum usable elevation angle
        duration_hours:  How many hours ahead to search (default 24).
        step_seconds:    Time step in seconds (default 30; smaller = more precise AOS/LOS).

    Returns:
        List of contact window dicts:
          station_name, aos_utc, los_utc, max_elevation_deg, duration_seconds
    """
    tle = await fetch_tle_by_norad_id(norad_id)
    sat = EarthSatellite(tle["line1"], tle["line2"], tle["name"], _ts)

    now_dt = datetime.now(timezone.utc)
    total_steps = int(duration_hours * 3600 / step_seconds)

    # Pre-build the time array for all stations to share
    t_array = _ts.utc(
        [now_dt.year] * (total_steps + 1),
        [now_dt.month] * (total_steps + 1),
        [now_dt.day] * (total_steps + 1),
        [now_dt.hour] * (total_steps + 1),
        [now_dt.minute] * (total_steps + 1),
        [now_dt.second + i * step_seconds for i in range(total_steps + 1)],
    )

    windows: list[dict] = []

    for gs in ground_stations:
        gs_name = gs.get("name", "unknown")
        gs_lat = float(gs["lat"])
        gs_lon = float(gs["lon"])
        min_elev = float(gs.get("min_elevation_deg", 5.0))

        observer = Topos(latitude_degrees=gs_lat, longitude_degrees=gs_lon)
        difference = sat - observer

        # Compute elevation at every time step
        topocentric = difference.at(t_array)
        alt, _az, _dist = topocentric.altaz()
        elevations = alt.degrees  # numpy array

        in_contact = False
        contact_start_dt: datetime | None = None
        contact_max_elev = 0.0

        for i, elev in enumerate(elevations):
            step_dt = now_dt + timedelta(seconds=i * step_seconds)

            if not in_contact and elev >= min_elev:
                # Rising edge — AOS
                in_contact = True
                contact_start_dt = step_dt
                contact_max_elev = elev
            elif in_contact:
                if elev >= min_elev:
                    contact_max_elev = max(contact_max_elev, elev)
                else:
                    # Setting edge — LOS
                    los_dt = step_dt
                    duration_s = int((los_dt - contact_start_dt).total_seconds())
                    windows.append({
                        "station_name": gs_name,
                        "aos_utc": contact_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "los_utc": los_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "max_elevation_deg": round(float(contact_max_elev), 1),
                        "duration_seconds": duration_s,
                    })
                    in_contact = False
                    contact_start_dt = None
                    contact_max_elev = 0.0

        # Close any open window at the end of the search horizon
        if in_contact and contact_start_dt is not None:
            los_dt = now_dt + timedelta(seconds=total_steps * step_seconds)
            duration_s = int((los_dt - contact_start_dt).total_seconds())
            windows.append({
                "station_name": gs_name,
                "aos_utc": contact_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "los_utc": los_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "max_elevation_deg": round(float(contact_max_elev), 1),
                "duration_seconds": duration_s,
            })

    windows.sort(key=lambda w: w["aos_utc"])
    return windows


async def debug_celestrak_connectivity() -> dict:
    host = "celestrak.org"
    result = {"host": host}
    try:
        result["dns"] = socket.gethostbyname(host)
    except Exception as e:
        result["dns_error"] = f"{type(e).__name__}: {e}"
        return result

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://{host}/NORAD/elements/gp.php?GROUP=galileo&FORMAT=tle")
            result["status_code"] = resp.status_code
            result["bytes_received"] = len(resp.content)
    except Exception as e:
        result["connect_error"] = f"{type(e).__name__}: {e}"
    return result