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


def _local_explanation(name: str, health: dict, verdict: dict) -> str:
    """Honest, template-based explanation — used when watsonx isn't configured."""
    staleness = health["tle_staleness"]
    age_h = health["tle_age_hours"]
    drag = health["drag_risk_score"]
    anomaly = health["orbit_anomaly_flag"]
    status = verdict["status"]
    risk = verdict["risk_score"]

    # Primary driver
    if staleness == "STALE":
        primary = (
            f"The primary concern is tracking data staleness: the last TLE update was {age_h}h ago, "
            f"which means ground operators no longer have precise knowledge of its orbit. "
            f"This alone is sufficient to trigger OUTAGE classification."
        )
        action = "Re-establish contact and request a fresh TLE from the tracking network."
    elif staleness == "AGING":
        primary = (
            f"Tracking data is aging ({age_h}h since last update), reducing confidence "
            f"in the predicted orbit. Continued degradation will escalate to OUTAGE."
        )
        action = "Schedule a tracking pass to refresh the TLE within the next 48 hours."
    elif drag >= 0.6:
        primary = (
            f"The satellite is in a high-drag regime (drag risk {drag}). "
            f"At this altitude atmospheric drag will cause measurable orbit decay without station-keeping."
        )
        action = "Review station-keeping fuel budget and plan an orbit maintenance manoeuvre."
    elif anomaly:
        primary = (
            "An orbit anomaly flag is active: the orbital eccentricity is outside the "
            "near-circular range expected at this altitude, which may indicate a past manoeuvre "
            "or unmodelled perturbation."
        )
        action = "Cross-check the latest TLE against the mission's nominal orbit profile."
    else:
        primary = "All monitored orbital signals are within normal range."
        action = "No immediate action required; continue routine monitoring."

    consequence = {
        "OUTAGE":   "The satellite should be treated as operationally unavailable until tracking is restored.",
        "DEGRADED": "Reduced confidence in position knowledge; dependent services should use conservative margins.",
        "OK":       "The satellite is operating normally; no mission impact.",
    }.get(status, "")

    return (
        f"{name} is classified {status} with a risk score of {risk}/1.0. "
        f"{primary} {consequence} {action} "
        f"[locally generated — configure WATSONX_API_KEY to route this through Granite]"
    )


async def _watsonx_explanation(name: str, health: dict, verdict: dict) -> str:
    """Real watsonx.ai call: sends structured signals, gets back a Granite-written explanation."""
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

        # Build a concise bullet list of what each signal means so Granite
        # can cite specific numbers rather than producing a generic summary.
        signal_lines = [
            f"  - TLE staleness: {staleness} ({age_h}h since last tracking update)"
            + (" — tracking confidence is LOW; operators may have lost precise knowledge of its orbit" if staleness == "STALE"
               else " — tracking confidence is REDUCED" if staleness == "AGING"
               else " — tracking data is current"),
            f"  - Drag risk score: {drag} (scale 0–1; values ≥0.6 indicate meaningful decay risk)"
            + (" — LOW drag risk at this altitude" if drag < 0.3
               else " — MODERATE drag / orbit decay exposure" if drag < 0.6
               else " — HIGH drag; orbit will decay noticeably without station-keeping"),
            f"  - Orbit anomaly: {'YES — eccentricity is outside the normal near-circular range for this altitude band' if anomaly else 'NO — orbit shape is normal'}",
            f"  - Overall risk score: {risk} (0 = healthy, 1 = critical)",
            f"  - Status classification: {status}",
        ]
        signals_block = "\n".join(signal_lines)

        prompt = (
            "You are a spacecraft operations analyst writing a plain-language status briefing "
            "for a mission control team. Use ONLY the signals listed below — do NOT invent "
            "battery levels, power readings, temperature values, transponder states, or any "
            "telemetry that is not explicitly provided.\n\n"
            f"Satellite: {name}\n\n"
            "Measured signals:\n"
            f"{signals_block}\n\n"
            "Write a clear, specific 3–4 sentence briefing that:\n"
            "1. States the current status and what it means operationally.\n"
            "2. Identifies the single most important contributing factor, citing the actual value.\n"
            "3. Describes the practical consequence for the satellite's mission or ground operators.\n"
            "4. Recommends one concrete next action.\n\n"
            "Briefing:"
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
                "parameters": {"max_new_tokens": 220, "temperature": 0.2},
            },
        )

        if gen_resp.status_code >= 400:
            # Surface IBM's actual error body instead of swallowing it — a
            # bare status code (e.g. "403 Forbidden") isn't enough to debug
            # region mismatches, missing Runtime associations, or entitlement
            # issues. IBM's response body almost always names the real cause.
            return f"[watsonx HTTP {gen_resp.status_code}: {gen_resp.text[:500]}]"

        result = gen_resp.json()

        results = result.get("results")
        if not results or not isinstance(results, list):
            return f"[watsonx returned no results: {result}]"

        generated = results[0].get("generated_text")
        if not generated:
            return f"[watsonx returned no text: {result}]"

        return generated.strip()


async def analyze(name: str, health: dict) -> dict:
    """Full AI-enhanced analysis: deterministic score + Granite (or local) explanation."""
    verdict = score_and_classify(health)

    if WATSONX_API_KEY and WATSONX_URL and WATSONX_PROJECT_ID:
        try:
            explanation = await _watsonx_explanation(name, health, verdict)
            source = "watsonx.ai / Granite"
        except Exception as exc:
            explanation = _local_explanation(name, health, verdict) + f" (watsonx call failed: {exc})"
            source = "local fallback (watsonx error)"
    else:
        explanation = _local_explanation(name, health, verdict)
        source = "local fallback (watsonx not configured)"

    return {
        "status": verdict["status"],
        "risk_score": verdict["risk_score"],
        "explanation": explanation,
        "explanation_source": source,
    }