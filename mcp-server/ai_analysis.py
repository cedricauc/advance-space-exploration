"""
ai_analysis.py — turns real orbital + health signals into AI-enhanced insight.

Two modes:
1. If WATSONX_API_KEY / WATSONX_URL / WATSONX_PROJECT_ID are set, sends the
   structured signals to a hosted Granite model on watsonx.ai for a natural-
   language explanation (this is the real integration point for the
   Langflow -> Granite -> watsonx pipeline described in the architecture).
2. Otherwise, falls back to a transparent, rule-based local classifier so the
   system runs end-to-end without cloud credentials, and is honest that the
   explanation is template-generated rather than model-generated.

The risk score / status classification themselves are always computed the
same deterministic way from real signals (see score_and_classify) — only the
*natural-language explanation* differs between the two modes.
"""

from __future__ import annotations

import os

import httpx

WATSONX_API_KEY = os.environ.get("WATSONX_API_KEY")
WATSONX_URL = os.environ.get("WATSONX_URL")
WATSONX_PROJECT_ID = os.environ.get("WATSONX_PROJECT_ID")
GRANITE_MODEL_ID = os.environ.get("GRANITE_MODEL_ID", "ibm/granite-13b-instruct-v2")


def score_and_classify(health: dict) -> dict:
    """
    Deterministic risk score (0-1) and status classification, built from real
    signals only: TLE staleness + drag risk + orbit anomaly flag.
    """
    risk = health["drag_risk_score"]

    if health["tle_staleness"] == "STALE":
        risk = min(risk + 0.4, 1.0)
    elif health["tle_staleness"] == "AGING":
        risk = min(risk + 0.15, 1.0)

    if health["orbit_anomaly_flag"]:
        risk = min(risk + 0.2, 1.0)

    risk = round(risk, 2)

    if risk >= 0.7 or health["tle_staleness"] == "STALE":
        status = "OUTAGE"
    elif risk >= 0.35:
        status = "DEGRADED"
    else:
        status = "OK"

    return {"risk_score": risk, "status": status}


def _build_weather_context(space_weather: dict | None, altitude_km: float | None = None) -> str:
    """Format live space weather into a concise, orbit-relevant context string."""
    if not space_weather or isinstance(space_weather.get("kp_index"), dict) and space_weather["kp_index"].get("error"):
        return "  - Space weather: unavailable"

    lines = []

    kp = space_weather.get("kp_index", {})
    kp_val = kp.get("kp_index")
    kp_level = kp.get("geomagnetic_level", "unknown")
    if kp_val is not None:
        # Relate Kp to drag impact depending on orbital altitude band
        if altitude_km is not None and altitude_km < 600:
            drag_note = (
                "; at this LEO altitude elevated Kp directly increases atmospheric density and drag"
                if kp_val >= 4 else
                "; minimal drag impact at current Kp level"
            )
        else:
            drag_note = "; MEO/GEO orbit — drag not affected, but radiation environment may be elevated" if kp_val >= 5 else ""
        lines.append(f"  - Geomagnetic activity: Kp={kp_val} ({kp_level}){drag_note}")

    wind = space_weather.get("solar_wind", {})
    speed = wind.get("solar_wind_speed_km_s")
    density = wind.get("proton_density_cm3")
    if speed is not None:
        sw_note = " — high-speed stream arriving; expect elevated Kp over next 12–24h" if speed > 0.6 else ""
        lines.append(f"  - Solar wind: {speed} km/s, density {density} p/cm³{sw_note}")

    xray = space_weather.get("xray_flux", {})
    flare = xray.get("flare_class")
    long_flux = xray.get("long_band_flux_W_m2")
    if flare:
        flare_note = (
            " — X-class flare; potential radio blackout on sunlit side and single-event upsets in electronics"
            if flare == "X" else
            " — M-class flare; minor radio degradation and possible energetic particle increase"
            if flare == "M" else
            " — background level, no significant impact expected"
            if flare in ("A", "B") else ""
        )
        lines.append(f"  - Solar X-ray flux: class {flare} ({long_flux:.2e} W/m²){flare_note}")

    alerts = space_weather.get("alerts", {})
    geomag = alerts.get("active_geomagnetic_storm_alerts", [])
    radiation = alerts.get("active_radiation_storm_alerts", [])
    if geomag:
        lines.append(f"  - ACTIVE geomagnetic storm alert: {geomag[0]['message'][:120]}")
    if radiation:
        lines.append(f"  - ACTIVE solar radiation storm alert: {radiation[0]['message'][:120]}")
    if not geomag and not radiation:
        lines.append("  - No active NOAA storm alerts")

    return "\n".join(lines) if lines else "  - Space weather: no data"


def _local_explanation(name: str, health: dict, verdict: dict,
                        space_weather: dict | None = None,
                        altitude_km: float | None = None) -> str:
    """Honest, template-based explanation — used when watsonx isn't configured."""
    staleness = health["tle_staleness"]
    age_h = health["tle_age_hours"]
    drag = health["drag_risk_score"]
    anomaly = health["orbit_anomaly_flag"]
    status = verdict["status"]
    risk = verdict["risk_score"]

    # Primary orbital driver
    if staleness == "STALE":
        primary = (
            f"The primary concern is tracking data staleness: the last TLE update was "
            f"{age_h}h ago, so ground operators no longer have precise knowledge of its orbit."
        )
        action = "Re-establish contact and request a fresh TLE from the tracking network."
    elif staleness == "AGING":
        primary = (
            f"Tracking data is aging ({age_h}h since last update), reducing orbital "
            f"position confidence. Continued degradation will escalate to OUTAGE."
        )
        action = "Schedule a tracking pass to refresh the TLE within the next 48 hours."
    elif drag >= 0.6:
        primary = (
            f"The satellite is in a high-drag regime (drag risk score {drag}/1.0). "
            f"Atmospheric drag at this altitude will cause measurable orbit decay without station-keeping."
        )
        action = "Review station-keeping fuel budget and plan an orbit maintenance manoeuvre."
    elif anomaly:
        primary = (
            "An orbit anomaly flag is active: eccentricity is outside the near-circular "
            "range expected at this altitude, suggesting a past manoeuvre or unmodelled perturbation."
        )
        action = "Cross-check the latest TLE against the mission nominal orbit profile."
    else:
        primary = "All monitored orbital signals are within normal range."
        action = "No immediate action required; continue routine monitoring."

    consequence = {
        "OUTAGE":   "The satellite should be treated as operationally unavailable until tracking is restored.",
        "DEGRADED": "Reduced confidence in position knowledge; dependent services should apply conservative margins.",
        "OK":       "The satellite is operating nominally with no mission impact.",
    }.get(status, "")

    # Space weather addendum
    weather_note = ""
    if space_weather and not isinstance(space_weather.get("kp_index"), dict):
        kp = (space_weather.get("kp_index") or {}).get("kp_index")
        flare = (space_weather.get("xray_flux") or {}).get("flare_class")
        alerts_count = (space_weather.get("alerts") or {}).get("total_alerts", 0)
        parts = []
        if kp is not None:
            parts.append(f"Kp={kp}")
        if flare and flare not in ("A", "B"):
            parts.append(f"{flare}-class solar flare active")
        if alerts_count:
            parts.append(f"{alerts_count} active NOAA storm alert(s)")
        if parts:
            weather_note = f" Current space weather: {', '.join(parts)}."

    return (
        f"{name} is classified {status} (risk {risk}/1.0). "
        f"{primary} {consequence}{weather_note} {action} "
        f"[locally generated — configure WATSONX_API_KEY to route this through Granite]"
    ).strip()


async def _watsonx_explanation(name: str, health: dict, verdict: dict,
                                space_weather: dict | None = None,
                                altitude_km: float | None = None) -> str:
    """Real watsonx.ai call: sends structured signals + live space weather context."""
    token_url = "https://iam.cloud.ibm.com/identity/token"
    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            token_url,
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": WATSONX_API_KEY,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        staleness = health["tle_staleness"]
        age_h = health["tle_age_hours"]
        drag = health["drag_risk_score"]
        anomaly = health["orbit_anomaly_flag"]
        risk = verdict["risk_score"]
        status = verdict["status"]

        orbital_lines = [
            f"  - TLE staleness: {staleness} ({age_h}h since last tracking update)"
            + (" — tracking confidence LOW, precise orbit unknown" if staleness == "STALE"
               else " — tracking confidence REDUCED" if staleness == "AGING"
               else " — tracking data current"),
            f"  - Drag risk: {drag}/1.0"
            + (" — LOW at this altitude" if drag < 0.3
               else " — MODERATE drag exposure" if drag < 0.6
               else " — HIGH; orbit decays without station-keeping"),
            f"  - Orbit anomaly: {'YES — eccentricity outside normal range for this altitude' if anomaly else 'NO — shape normal'}",
            f"  - Risk score: {risk}/1.0 | Status: {status}",
        ]
        if altitude_km is not None:
            orbital_lines.insert(0, f"  - Altitude: {altitude_km} km")

        weather_block = _build_weather_context(space_weather, altitude_km)

        prompt = (
            "You are a spacecraft operations analyst. Write a single, non-repetitive paragraph "
            "(4–5 sentences) that serves as a mission control status briefing. "
            "Do NOT use numbered lists. Do NOT restate the same point twice. "
            "Do NOT invent battery, power, thermal, or link-budget data not listed below. "
            "Write continuously from status → root cause → space-weather impact → recommended action.\n\n"
            f"Satellite: {name}\n\n"
            "Orbital health signals:\n"
            + "\n".join(orbital_lines)
            + "\n\nLive space weather (relate to this satellite's orbit):\n"
            + weather_block
            + "\n\nBriefing (one paragraph, no lists, no repetition):"
        )

        gen_resp = await client.post(
            f"{WATSONX_URL}/ml/v1/text/generation?version=2024-05-01",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "model_id": GRANITE_MODEL_ID,
                "project_id": WATSONX_PROJECT_ID,
                "input": prompt,
                "parameters": {
                    "max_new_tokens": 250,
                    "temperature": 0.2,
                    "repetition_penalty": 1.15,
                    "stop_sequences": ["\n\n", "Orbital health", "Satellite:"],
                },
            },
        )

        if gen_resp.status_code >= 400:
            return f"[watsonx HTTP {gen_resp.status_code}: {gen_resp.text[:500]}]"

        result = gen_resp.json()
        results = result.get("results")
        if not results or not isinstance(results, list):
            return f"[watsonx returned no results: {result}]"

        generated = results[0].get("generated_text")
        if not generated:
            return f"[watsonx returned no text: {result}]"

        return generated.strip()


async def analyze(name: str, health: dict,
                  space_weather: dict | None = None,
                  altitude_km: float | None = None) -> dict:
    """Full AI-enhanced analysis: deterministic score + Granite (or local) explanation.

    Args:
        name:          Satellite name.
        health:        Output of satellite_utils.health_signals().
        space_weather: Optional output of space_weather.get_enriched_space_weather().
                       When provided, the briefing contextualises current Kp, solar wind,
                       and flare conditions relative to this satellite's orbit.
        altitude_km:   Satellite altitude, used to make space-weather impact orbit-specific.
    """
    verdict = score_and_classify(health)

    if WATSONX_API_KEY and WATSONX_URL and WATSONX_PROJECT_ID:
        try:
            explanation = await _watsonx_explanation(name, health, verdict,
                                                     space_weather=space_weather,
                                                     altitude_km=altitude_km)
            source = "watsonx.ai / Granite"
        except Exception as exc:
            explanation = _local_explanation(name, health, verdict,
                                             space_weather=space_weather,
                                             altitude_km=altitude_km)
            explanation += f" (watsonx call failed: {exc})"
            source = "local fallback (watsonx error)"
    else:
        explanation = _local_explanation(name, health, verdict,
                                         space_weather=space_weather,
                                         altitude_km=altitude_km)
        source = "local fallback (watsonx not configured)"

    return {
        "status": verdict["status"],
        "risk_score": verdict["risk_score"],
        "explanation": explanation,
        "explanation_source": source,
    }