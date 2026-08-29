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

import httpx
from mcp.server.fastmcp import FastMCP

import satellite_utils as sat
import ai_analysis

mcp = FastMCP("space-mission-intelligence")

ISS_NOW_URL = os.environ.get("ISS_NOW_URL", "http://api.open-notify.org/iss-now.json")
ISS_ASTROS_URL = os.environ.get("ISS_ASTROS_URL", "http://api.open-notify.org/astros.json")
LAUNCH_LIBRARY_URL = os.environ.get("LAUNCH_LIBRARY_URL", "https://ll.thespacedevs.com/2.2.0/launch/upcoming/")
NOAA_KINDEX_URL = os.environ.get("NOAA_KINDEX_URL", "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json")


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
    tles = await sat.fetch_tle_group(group, limit)
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
    tles = await sat.fetch_tle_group(group, limit)

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
async def get_space_weather() -> dict:
    """Get current space weather: planetary K-index (geomagnetic activity) from NOAA SWPC.
    High K-index correlates with satellite drag increases and potential comms disruption.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(NOAA_KINDEX_URL)
        resp.raise_for_status()
        data = resp.json()
    latest = data[-1] if data else {}
    return {
        "latest_kp_index": latest.get("kp_index"),
        "observed_time": latest.get("time_tag"),
        "source": "NOAA SWPC planetary K-index",
    }


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