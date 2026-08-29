"""
Space Mission Intelligence MCP Server

Exposes real, verifiable space data as MCP tools:
- Live satellite position via SGP4/TLE propagation (Celestrak)
- Coverage footprint via real line-of-sight geometry
- AI-enhanced health analysis (risk score, status, Granite/watsonx explanation)
- Space weather (NOAA SWPC)
- ISS position, crew, and launch schedule

Run standalone:  python server.py
Consumed by web-gateway/mcpClient.js as a subprocess over stdio.
"""

import os
from dotenv import load_dotenv

# MUST run before any sibling module is imported: satellite_utils.py reads
# CELESTRAK_* env vars at module import time, so if load_dotenv() runs after
# that import, those variables are still None when it needs them.
load_dotenv()

from mcp.server.fastmcp import FastMCP

import satellite_utils as sat
import ai_analysis
import spacetrack
import space_weather as sw
import n2yo

mcp = FastMCP("space-mission-intelligence")

SPACETRACK_USER = os.environ.get("SPACETRACK_USER", "")
SPACETRACK_PASSWORD = os.environ.get("SPACETRACK_PASSWORD", "")

ISS_NOW_URL = os.environ.get("ISS_NOW_URL", "http://api.open-notify.org/iss-now.json")
ISS_ASTROS_URL = os.environ.get("ISS_ASTROS_URL", "http://api.open-notify.org/astros.json")
LAUNCH_LIBRARY_URL = os.environ.get("LAUNCH_LIBRARY_URL", "https://ll.thespacedevs.com/2.2.0/launch/upcoming/")


@mcp.tool()
async def get_satellite_position(norad_id: int) -> dict:
    """Get a satellite's real-time position via SGP4 orbit propagation.

    Args:
        norad_id: NORAD catalog number (e.g. 25544 for the ISS)
    """
    tle = await sat.fetch_tle_by_norad_id(norad_id)
    return sat.propagate(tle)


@mcp.tool()
async def get_constellation_snapshot(group: str = "starlink", limit: int = 20) -> dict:
    """Get real-time positions for a whole satellite constellation/group.
    Returns an object with a 'satellites' list (never a bare list).

    Args:
        group: Celestrak group name, e.g. 'starlink', 'stations', 'weather', 'gps-ops'
        limit: max number of satellites to propagate (default 20; keep modest, each is a live compute)
    """
    try:
        tles = await sat.fetch_tle_group(group, limit)
    except Exception as exc:
        return {
            "group": group,
            "count": 0,
            "satellites": [],
            "error": f"Failed to fetch TLE group '{group}' from Celestrak: {exc}",
        }
    positions = [sat.propagate(t) for t in tles]
    return {"group": group, "count": len(positions), "satellites": positions}


@mcp.tool()
def get_coverage_zone(altitude_km: float, min_elevation_deg: float = 10.0) -> dict:
    """Compute a satellite's ground coverage radius from real line-of-sight geometry.

    Args:
        altitude_km: satellite altitude above Earth in km
        min_elevation_deg: minimum elevation angle for a usable link (default 10 degrees)
    """
    return sat.coverage_footprint(altitude_km, min_elevation_deg)


@mcp.tool()
async def analyze_satellite(norad_id: int) -> dict:
    """Full AI-enhanced satellite health analysis: fetches live position, computes
    physically-grounded risk signals, and produces a Granite/watsonx-generated
    (or local fallback) plain-language status explanation.

    Args:
        norad_id: NORAD catalog number
    """
    tle = await sat.fetch_tle_by_norad_id(norad_id)
    position = sat.propagate(tle)
    health = sat.health_signals(position)
    coverage = sat.coverage_footprint(position["altitude_km"])
    ai_result = await ai_analysis.analyze(position["name"], health)

    return {
        "position": position,
        "health_signals": health,
        "coverage": coverage,
        "ai_analysis": ai_result,
    }


@mcp.tool()
async def analyze_constellation(group: str = "starlink", limit: int = 10) -> dict:
    """Fast, local-only health classification across a whole constellation
    snapshot — for rendering the map/globe at scale (tested at 11,000+
    satellites in ~3s). Uses deterministic physics-based scoring only (see
    ai_analysis.score_and_classify), with NO per-satellite AI/watsonx call —
    that would be far too slow and expensive at this scale (11,000 network
    round-trips to an LLM is a non-starter). Each satellite's 'explanation'
    field is null here; call analyze_satellite for one specific satellite
    (e.g. when the user clicks it) to get the full AI-generated explanation
    on demand.

    Args:
        group: Celestrak group name
        limit: max satellites to analyze
    """
    try:
        tles = await sat.fetch_tle_group(group, limit)
    except Exception as exc:
        return {
            "group": group,
            "count": 0,
            "satellites": [],
            "errors": [f"Failed to fetch TLE group '{group}' from Celestrak: {exc}"],
        }

    results = []
    errors = []
    for t in tles:
        try:
            position = sat.propagate(t)
            health = sat.health_signals(position)
            coverage = sat.coverage_footprint(position["altitude_km"])
            verdict = ai_analysis.score_and_classify(health)
            results.append({
                "position": position,
                "health_signals": health,
                "coverage": coverage,
                "ai_analysis": {
                    "status": verdict["status"],
                    "risk_score": verdict["risk_score"],
                    "explanation": None,
                    "explanation_source": "not requested — call analyze_satellite for the full AI explanation",
                },
            })
        except Exception as exc:
            errors.append(str(exc))

    return {"group": group, "count": len(results), "satellites": results, "errors": errors}


@mcp.tool()
async def get_spacetrack_tle(norad_id: int) -> dict:
    """Fetch the latest high-fidelity GP orbital elements from Space-Track.org and
    propagate them to the current time with Skyfield SGP4.

    Returns both the raw GP fields (epoch, BSTAR, mean motion, eccentricity, etc.)
    and a live position snapshot in the same shape as get_satellite_position.

    Requires SPACETRACK_USER and SPACETRACK_PASSWORD environment variables.

    Args:
        norad_id: NORAD catalog number
    """
    if not SPACETRACK_USER or not SPACETRACK_PASSWORD:
        return {
            "error": "Space-Track credentials not configured.",
            "detail": "Set SPACETRACK_USER and SPACETRACK_PASSWORD in the .env file. "
                      "Register a free account at https://www.space-track.org/auth/createAccount",
        }
    gp = await spacetrack.fetch_latest_gp(norad_id)
    position = spacetrack.propagate_from_gp(gp)
    return {
        "position": position,
        "gp_elements": {
            "epoch": gp.get("EPOCH"),
            "mean_motion_rev_per_day": gp.get("MEAN_MOTION"),
            "eccentricity": gp.get("ECCENTRICITY"),
            "inclination_deg": gp.get("INCLINATION"),
            "ra_of_asc_node_deg": gp.get("RA_OF_ASC_NODE"),
            "arg_of_pericenter_deg": gp.get("ARG_OF_PERICENTER"),
            "mean_anomaly_deg": gp.get("MEAN_ANOMALY"),
            "bstar": gp.get("BSTAR"),
            "semi_major_axis_km": gp.get("SEMIMAJOR_AXIS"),
            "period_minutes": gp.get("PERIOD"),
            "apoapsis_km": gp.get("APOAPSIS"),
            "periapsis_km": gp.get("PERIAPSIS"),
            "object_type": gp.get("OBJECT_TYPE"),
            "classification": gp.get("CLASSIFICATION_TYPE"),
            "tle_line1": gp.get("TLE_LINE1"),
            "tle_line2": gp.get("TLE_LINE2"),
        },
        "source": "Space-Track.org basicspacedata GP class",
    }


@mcp.tool()
async def get_spacetrack_decay(norad_id: int) -> dict:
    """Fetch the latest reentry / decay prediction for a satellite from Space-Track.org.

    Returns predicted decay epoch, probability, and source region if available.
    Returns a 'no_prediction' message for objects with no active decay record.

    Requires SPACETRACK_USER and SPACETRACK_PASSWORD environment variables.

    Args:
        norad_id: NORAD catalog number
    """
    if not SPACETRACK_USER or not SPACETRACK_PASSWORD:
        return {
            "error": "Space-Track credentials not configured.",
            "detail": "Set SPACETRACK_USER and SPACETRACK_PASSWORD in the .env file. "
                      "Register a free account at https://www.space-track.org/auth/createAccount",
        }
    decay = await spacetrack.fetch_decay_prediction(norad_id)
    if decay is None:
        return {
            "norad_id": norad_id,
            "no_prediction": True,
            "message": "No active decay prediction on Space-Track for this object.",
        }
    return {
        "norad_id": norad_id,
        "predicted_decay_epoch": decay.get("DECAY_EPOCH"),
        "window": decay.get("WINDOW"),
        "decay_region": decay.get("DECAY_REGION"),
        "source": decay.get("SOURCE"),
        "msg_epoch": decay.get("MSG_EPOCH"),
        "precedence": decay.get("PRECEDENCE"),
        "source": "Space-Track.org basicspacedata decay class",
    }


@mcp.tool()
async def get_contact_windows(
    norad_id: int,
    ground_stations: list,
    duration_hours: float = 24,
) -> dict:
    """Compute upcoming satellite–ground contact windows using live SGP4 propagation.

    Steps through the next `duration_hours` for each supplied ground station and
    returns all windows where the satellite is above the station's minimum elevation
    angle, sorted by AOS time.

    Args:
        norad_id: NORAD catalog number of the target satellite.
        ground_stations: List of objects, each with:
                           name (str), lat (float), lon (float),
                           min_elevation_deg (float, default 5)
        duration_hours: How far ahead to search in hours (default 24, max 72).
    """
    duration_hours = min(float(duration_hours), 72)
    windows = await sat.compute_contact_windows(norad_id, ground_stations, duration_hours)
    return {
        "norad_id": norad_id,
        "duration_hours": duration_hours,
        "station_count": len(ground_stations),
        "window_count": len(windows),
        "windows": windows,
    }


@mcp.tool()
async def get_space_weather() -> dict:
    """Get enriched space weather from NOAA SWPC: K-index, solar wind speed/density,
    GOES X-ray flux with flare class, and active geomagnetic/radiation storm alerts.
    All four data streams are fetched in parallel. High K-index and M/X flares correlate
    with satellite drag increases and potential comms disruption.
    """
    return await sw.get_enriched_space_weather()


@mcp.tool()
async def get_satellite_passes(
    norad_id: int,
    lat: float,
    lon: float,
    alt_m: int = 0,
    days: int = 10,
    min_elevation_deg: int = 10,
) -> dict:
    """Get the next visible passes of a satellite over an observer location via N2YO.

    Returns AOS time, max elevation, LOS time, and duration for each pass.
    Requires N2YO_API_KEY environment variable.

    Args:
        norad_id:          NORAD catalog number.
        lat:               Observer latitude in decimal degrees.
        lon:               Observer longitude in decimal degrees.
        alt_m:             Observer altitude in metres above sea level (default 0).
        days:              Days ahead to search (1–10; default 10).
        min_elevation_deg: Minimum elevation angle for a valid pass (default 10 degrees).
    """
    if not n2yo.N2YO_API_KEY:
        return n2yo._MISSING_KEY_MSG
    return await n2yo.get_passes(norad_id, lat, lon, alt_m, days, min_elevation_deg)


@mcp.tool()
async def get_satellites_overhead(
    lat: float,
    lon: float,
    alt_m: int = 0,
    search_radius_deg: int = 70,
    category_id: int = 0,
) -> dict:
    """Get satellites currently above the horizon from an observer location via N2YO.

    Requires N2YO_API_KEY environment variable.

    Args:
        lat:               Observer latitude in decimal degrees.
        lon:               Observer longitude in decimal degrees.
        alt_m:             Observer altitude in metres above sea level (default 0).
        search_radius_deg: Sky search radius in degrees from zenith (max 90; default 70).
        category_id:       N2YO category filter (0 = all; 18 = weather; 22 = Starlink).
    """
    if not n2yo.N2YO_API_KEY:
        return n2yo._MISSING_KEY_MSG
    return await n2yo.get_overhead(lat, lon, alt_m, search_radius_deg, category_id)


@mcp.tool()
async def get_iss_position() -> dict:
    """Get the ISS's current position from Open Notify (independent of TLE propagation,
    useful as a cross-check)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(ISS_NOW_URL)
        resp.raise_for_status()
        data = resp.json()
    return {
        "latitude": float(data["iss_position"]["latitude"]),
        "longitude": float(data["iss_position"]["longitude"]),
        "timestamp": data["timestamp"],
    }


@mcp.tool()
async def get_astronauts_in_space() -> dict:
    """List people currently in space and which craft they're aboard."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(ISS_ASTROS_URL)
        resp.raise_for_status()
        data = resp.json()
    return {"count": data["number"], "people": data["people"]}


@mcp.tool()
async def get_launch_schedule(limit: int = 5) -> dict:
    """Get upcoming rocket launches. Returns an object with a 'launches' list.

    Args:
        limit: max number of launches to return
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(LAUNCH_LIBRARY_URL, params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
    launches = [
        {
            "name": item["name"],
            "provider": item["launch_service_provider"]["name"],
            "net": item["net"],
            "status": item["status"]["name"],
        }
        for item in data.get("results", [])
    ]
    return {"count": len(launches), "launches": launches}


if __name__ == "__main__":
    mcp.run(transport="stdio")