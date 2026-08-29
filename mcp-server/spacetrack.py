"""
spacetrack.py — Space-Track.org high-fidelity GP orbital elements & decay predictions.

Uses cookie-session authentication (POST /ajaxauth/login) and queries the
basicspacedata REST API for:
  - Latest GP element set (TLE_LINE1 / TLE_LINE2 + full osculating elements)
  - Decay predictions (predicted reentry epoch, probability)

After fetching a GP record the TLE lines are re-propagated with Skyfield to
return a live position snapshot in the same shape as satellite_utils.propagate().

Requires env vars:
  SPACETRACK_USER      — registered Space-Track.org username / email
  SPACETRACK_PASSWORD  — corresponding password
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from skyfield.api import EarthSatellite, load

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SPACETRACK_BASE = os.environ.get("SPACETRACK_BASE", "https://www.space-track.org")
SPACETRACK_USER = os.environ.get("SPACETRACK_USER", "")
SPACETRACK_PASSWORD = os.environ.get("SPACETRACK_PASSWORD", "")

# Session cookie cache: {"cookie": str, "cached_at": float}
_session_cache: dict = {}
_SESSION_TTL_SECONDS = 6 * 60 * 60  # 6 hours

_ts = load.timescale()


# ---------------------------------------------------------------------------
# Session authentication
# ---------------------------------------------------------------------------

async def _get_session_cookie(*, force_refresh: bool = False) -> str:
    """Authenticate with Space-Track and return a valid session cookie string.

    The cookie is cached for up to 6 hours. Pass force_refresh=True to
    discard the cache and re-authenticate (used on 401 responses).
    """
    now = time.monotonic()
    if (
        not force_refresh
        and _session_cache.get("cookie")
        and (now - _session_cache.get("cached_at", 0)) < _SESSION_TTL_SECONDS
    ):
        return _session_cache["cookie"]

    if not SPACETRACK_USER or not SPACETRACK_PASSWORD:
        raise RuntimeError(
            "SPACETRACK_USER and SPACETRACK_PASSWORD must be set to use Space-Track."
        )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SPACETRACK_BASE}/ajaxauth/login",
            data={"identity": SPACETRACK_USER, "password": SPACETRACK_PASSWORD},
        )
        resp.raise_for_status()

    # Build a cookie header string from all cookies returned
    cookie_str = "; ".join(f"{k}={v}" for k, v in resp.cookies.items())
    _session_cache["cookie"] = cookie_str
    _session_cache["cached_at"] = now
    return cookie_str


async def _st_get(path: str) -> list | dict:
    """Perform an authenticated GET against the Space-Track basicspacedata API.

    Handles a single 401 re-auth retry automatically.
    """
    cookie = await _get_session_cookie()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{SPACETRACK_BASE}{path}",
            headers={"Cookie": cookie},
        )

        if resp.status_code == 401:
            # Session expired — force re-auth and retry once
            cookie = await _get_session_cookie(force_refresh=True)
            resp = await client.get(
                f"{SPACETRACK_BASE}{path}",
                headers={"Cookie": cookie},
            )

        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# GP element queries
# ---------------------------------------------------------------------------

async def fetch_latest_gp(norad_id: int) -> dict:
    """Return the latest GP element set for a single satellite from Space-Track.

    The returned dict includes all osculating orbital elements plus TLE_LINE1
    and TLE_LINE2 which are used by propagate_from_gp().

    Raises ValueError if no GP record is found.
    """
    path = (
        f"/basicspacedata/query/class/gp"
        f"/NORAD_CAT_ID/{norad_id}"
        f"/orderby/EPOCH%20desc/limit/1/format/json"
    )
    data = await _st_get(path)
    if not data:
        raise ValueError(f"No GP record found for NORAD ID {norad_id} on Space-Track.")
    return data[0]


async def fetch_decay_prediction(norad_id: int) -> Optional[dict]:
    """Return the latest decay prediction for a satellite, or None if unavailable.

    The decay class is only populated for objects with an active reentry prediction.
    """
    path = (
        f"/basicspacedata/query/class/decay"
        f"/NORAD_CAT_ID/{norad_id}"
        f"/orderby/DECAY_EPOCH%20desc/limit/1/format/json"
    )
    data = await _st_get(path)
    if not data:
        return None
    return data[0]


# ---------------------------------------------------------------------------
# Skyfield propagation from GP TLE lines
# ---------------------------------------------------------------------------

def propagate_from_gp(gp: dict) -> dict:
    """Propagate the GP record to the current time using Skyfield SGP4.

    Uses TLE_LINE1 and TLE_LINE2 from the GP dict (Space-Track returns these
    directly — no reconstruction from osculating elements required).

    Returns a position dict with the same keys as satellite_utils.propagate():
      name, norad_id, latitude, longitude, altitude_km, speed_km_s
    """
    line1 = gp.get("TLE_LINE1", "")
    line2 = gp.get("TLE_LINE2", "")
    name = gp.get("OBJECT_NAME", gp.get("SATNAME", str(gp.get("NORAD_CAT_ID", "UNKNOWN"))))

    if not line1 or not line2:
        raise ValueError(
            f"GP record for {name} is missing TLE_LINE1 or TLE_LINE2. "
            "Space-Track may not have TLE data for this object."
        )

    sat = EarthSatellite(line1, line2, name, _ts)
    t = _ts.now()
    geocentric = sat.at(t)
    subpoint = geocentric.subpoint()
    velocity_km_s = geocentric.velocity.km_per_s
    speed = math.sqrt(sum(v ** 2 for v in velocity_km_s))

    epoch_dt = sat.epoch.utc_datetime()
    tle_age_hours = (datetime.now(timezone.utc) - epoch_dt).total_seconds() / 3600.0

    return {
        "name": name.strip(),
        "norad_id": int(gp.get("NORAD_CAT_ID", sat.model.satnum)),
        "latitude": round(float(subpoint.latitude.degrees), 4),
        "longitude": round(float(subpoint.longitude.degrees), 4),
        "altitude_km": round(float(subpoint.elevation.km), 2),
        "speed_km_s": round(float(speed), 3),
        "tle_age_hours": round(float(tle_age_hours), 1),
        "source": "Space-Track GP + Skyfield SGP4",
    }
