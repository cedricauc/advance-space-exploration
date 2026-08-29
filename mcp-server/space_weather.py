"""
space_weather.py — Enriched space weather from NOAA Space Weather Prediction Center.

Aggregates four public NOAA SWPC data streams in parallel:
  1. Planetary K-index (1-minute cadence) — geomagnetic activity level
  2. Real-time solar wind  (1-minute cadence) — speed & density
  3. GOES X-ray flux  (1-day archive) — solar flare class
  4. SWPC alert messages  — active geomagnetic storm / radiation storm warnings

All endpoints are public; no API key is required.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# NOAA SWPC endpoint URLs (overridable via env vars)
# ---------------------------------------------------------------------------

NOAA_KINDEX_URL = os.environ.get(
    "NOAA_KINDEX_URL",
    "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json",
)
NOAA_SOLAR_WIND_URL = os.environ.get(
    "NOAA_SOLAR_WIND_URL",
    "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json",
)
NOAA_XRAY_URL = os.environ.get(
    "NOAA_XRAY_URL",
    "https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json",
)
NOAA_ALERTS_URL = os.environ.get(
    "NOAA_ALERTS_URL",
    "https://services.swpc.noaa.gov/products/alerts.json",
)

_TIMEOUT = 12


# ---------------------------------------------------------------------------
# Individual fetch helpers
# ---------------------------------------------------------------------------

async def fetch_kindex() -> dict[str, Any]:
    """Fetch the latest 1-minute planetary K-index from NOAA SWPC."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(NOAA_KINDEX_URL)
        resp.raise_for_status()
        data = resp.json()
    latest = data[-1] if data else {}
    return {
        "kp_index": latest.get("kp_index"),
        "observed_time": latest.get("time_tag"),
        "geomagnetic_level": _kp_to_level(latest.get("kp_index")),
    }


async def fetch_solar_wind() -> dict[str, Any]:
    """Fetch the latest real-time solar wind speed and proton density from NOAA RTSW."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(NOAA_SOLAR_WIND_URL)
        resp.raise_for_status()
        data = resp.json()

    # Walk from the end to find the most recent record with valid speed/density
    latest: dict = {}
    for record in reversed(data):
        if record.get("proton_speed") is not None and record.get("proton_density") is not None:
            latest = record
            break

    speed = latest.get("proton_speed")
    density = latest.get("proton_density")
    return {
        "solar_wind_speed_km_s": round(float(speed) / 1000.0, 2) if speed is not None else None,
        "proton_density_cm3": round(float(density), 2) if density is not None else None,
        "observed_time": latest.get("time_tag"),
        "note": "Solar wind speed converted from km/s; NOAA reports in km/s directly",
    }


async def fetch_xray_flux() -> dict[str, Any]:
    """Fetch the latest GOES X-ray flux and derive solar flare class."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(NOAA_XRAY_URL)
        resp.raise_for_status()
        data = resp.json()

    if not data:
        return {"short_flux": None, "long_flux": None, "flare_class": None, "observed_time": None}

    # GOES xray JSON: list of {energy, flux, observed_time, ...}
    # energy band: "0.05-0.4nm" = short (GOES-R: "0.1-0.8nm" = long)
    short: dict = {}
    long_: dict = {}
    for record in reversed(data):
        energy = record.get("energy", "")
        if not short and ("0.05" in energy or "0.05-0.4" in energy):
            short = record
        if not long_ and ("0.1-0.8" in energy or "1-8" in energy):
            long_ = record
        if short and long_:
            break

    long_flux = long_.get("flux") if long_ else None
    flare_class = _flux_to_flare_class(long_flux)

    return {
        "short_band_flux_W_m2": short.get("flux") if short else None,
        "long_band_flux_W_m2": long_flux,
        "flare_class": flare_class,
        "observed_time": long_.get("time_tag") if long_ else None,
        "bands": {"short": "0.05–0.4 nm", "long": "0.1–0.8 nm"},
    }


async def fetch_geomagnetic_alerts() -> dict[str, Any]:
    """Fetch active NOAA SWPC alert messages and filter for storm-level events."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(NOAA_ALERTS_URL)
        resp.raise_for_status()
        data = resp.json()

    # Each item: {"product_id": "...", "issue_datetime": "...", "message": "..."}
    active_geomag: list[dict] = []
    active_radiation: list[dict] = []

    storm_keywords = ("G1", "G2", "G3", "G4", "G5", "Geomagnetic Storm")
    radiation_keywords = ("S1", "S2", "S3", "S4", "S5", "Solar Radiation Storm")

    for item in data:
        msg = item.get("message", "")
        if any(k in msg for k in storm_keywords):
            active_geomag.append({
                "issue_datetime": item.get("issue_datetime"),
                "message": msg.strip(),
            })
        elif any(k in msg for k in radiation_keywords):
            active_radiation.append({
                "issue_datetime": item.get("issue_datetime"),
                "message": msg.strip(),
            })

    return {
        "active_geomagnetic_storm_alerts": active_geomag,
        "active_radiation_storm_alerts": active_radiation,
        "total_alerts": len(active_geomag) + len(active_radiation),
    }


# ---------------------------------------------------------------------------
# Aggregated entry point
# ---------------------------------------------------------------------------

async def get_enriched_space_weather() -> dict[str, Any]:
    """Fetch all four NOAA SWPC data streams in parallel and return a merged dict."""
    kindex, solar_wind, xray, alerts = await asyncio.gather(
        fetch_kindex(),
        fetch_solar_wind(),
        fetch_xray_flux(),
        fetch_geomagnetic_alerts(),
        return_exceptions=True,
    )

    def _safe(result: Any, label: str) -> Any:
        if isinstance(result, Exception):
            return {"error": f"Failed to fetch {label}: {result}"}
        return result

    return {
        "kp_index": _safe(kindex, "K-index"),
        "solar_wind": _safe(solar_wind, "solar wind"),
        "xray_flux": _safe(xray, "X-ray flux"),
        "alerts": _safe(alerts, "geomagnetic alerts"),
        "source": "NOAA Space Weather Prediction Center (SWPC)",
    }


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _kp_to_level(kp: Any) -> str:
    """Map a K-index value to a human-readable geomagnetic activity level."""
    if kp is None:
        return "unknown"
    kp = float(kp)
    if kp < 1:
        return "quiet"
    if kp < 3:
        return "unsettled"
    if kp < 5:
        return "active"
    if kp < 6:
        return "G1-minor-storm"
    if kp < 7:
        return "G2-moderate-storm"
    if kp < 8:
        return "G3-strong-storm"
    if kp < 9:
        return "G4-severe-storm"
    return "G5-extreme-storm"


def _flux_to_flare_class(flux: Any) -> str | None:
    """Map long-band X-ray flux (W/m²) to standard GOES solar flare class."""
    if flux is None:
        return None
    flux = float(flux)
    if flux < 1e-8:
        return "A"
    if flux < 1e-7:
        return "B"
    if flux < 1e-6:
        return "C"
    if flux < 1e-5:
        return "M"
    return "X"
