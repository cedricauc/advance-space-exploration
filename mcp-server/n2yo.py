"""
n2yo.py — N2YO REST API client for satellite pass predictions and overhead queries.

Provides two functions:
  get_passes()    — next visible radio passes for a satellite over an observer
  get_overhead()  — satellites currently above the horizon from an observer location

Both require N2YO_API_KEY (free registration at https://www.n2yo.com/api/).

N2YO API docs: https://www.n2yo.com/api/
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

N2YO_BASE_URL = os.environ.get("N2YO_BASE_URL", "https://api.n2yo.com/rest/v1/satellite")
N2YO_API_KEY = os.environ.get("N2YO_API_KEY", "")

_TIMEOUT = 15

_MISSING_KEY_MSG = {
    "error": "N2YO API key not configured.",
    "detail": (
        "Set N2YO_API_KEY in the .env file. "
        "Register a free account at https://www.n2yo.com/api/ to obtain a key."
    ),
}


# ---------------------------------------------------------------------------
# Private HTTP helper
# ---------------------------------------------------------------------------

async def _n2yo_get(path: str) -> Any:
    """Perform a GET request against the N2YO REST API.

    Appends the API key as a query parameter and returns the parsed JSON body.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = f"{N2YO_BASE_URL}{path}"
    separator = "&" if "?" in path else "?"
    url = f"{url}{separator}apiKey={N2YO_API_KEY}"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

async def get_passes(
    norad_id: int,
    lat: float,
    lon: float,
    alt_m: int = 0,
    days: int = 10,
    min_elevation_deg: int = 10,
) -> dict[str, Any]:
    """Return the next visible radio/visual passes for a satellite over an observer.

    Args:
        norad_id:          NORAD catalog number.
        lat:               Observer latitude in decimal degrees.
        lon:               Observer longitude in decimal degrees (east positive).
        alt_m:             Observer altitude above sea level in metres (default 0).
        days:              Number of days ahead to search (1–10, N2YO maximum is 10).
        min_elevation_deg: Minimum elevation angle to consider a pass visible (default 10).

    Returns:
        Dict with satellite info, observer info, and a list of pass objects.
        Each pass: {startAz, startAzCompass, startEl, startUTC,
                    maxAz, maxAzCompass, maxEl, maxUTC,
                    endAz, endAzCompass, endEl, endUTC,
                    mag, duration, aos_utc_iso, los_utc_iso}
    """
    days = min(int(days), 10)
    path = f"/radiopasses/{norad_id}/{lat}/{lon}/{alt_m}/{days}/{min_elevation_deg}"
    data = await _n2yo_get(path)

    passes_raw = data.get("passes") or []
    passes = []
    for p in passes_raw:
        entry = dict(p)
        # Add ISO-formatted AOS/LOS for convenience
        if p.get("startUTC"):
            entry["aos_utc_iso"] = datetime.fromtimestamp(p["startUTC"], tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if p.get("endUTC"):
            entry["los_utc_iso"] = datetime.fromtimestamp(p["endUTC"], tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        passes.append(entry)

    return {
        "norad_id": norad_id,
        "satellite_name": data.get("info", {}).get("satname"),
        "observer": {"lat": lat, "lon": lon, "alt_m": alt_m},
        "search_days": days,
        "min_elevation_deg": min_elevation_deg,
        "pass_count": len(passes),
        "passes": passes,
        "source": "N2YO REST API /radiopasses",
    }


async def get_overhead(
    lat: float,
    lon: float,
    alt_m: int = 0,
    search_radius_deg: int = 70,
    category_id: int = 0,
) -> dict[str, Any]:
    """Return satellites currently above the horizon from an observer location.

    Args:
        lat:               Observer latitude in decimal degrees.
        lon:               Observer longitude in decimal degrees (east positive).
        alt_m:             Observer altitude above sea level in metres (default 0).
        search_radius_deg: Sky search radius in degrees from zenith (max 90; default 70).
        category_id:       N2YO satellite category filter (0 = all categories).
                           Common values: 1=Amateur, 18=Weather, 22=Starlink.

    Returns:
        Dict with observer info and a list of satellites currently above the horizon.
    """
    search_radius_deg = min(int(search_radius_deg), 90)
    path = f"/above/{lat}/{lon}/{alt_m}/{search_radius_deg}/{category_id}"
    data = await _n2yo_get(path)

    above = data.get("above") or []
    return {
        "observer": {"lat": lat, "lon": lon, "alt_m": alt_m},
        "search_radius_deg": search_radius_deg,
        "category_id": category_id,
        "satellite_count": len(above),
        "satellites": above,
        "source": "N2YO REST API /above",
    }
