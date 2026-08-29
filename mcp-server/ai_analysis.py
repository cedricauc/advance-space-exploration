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
    reasons = []
    if health["tle_staleness"] != "FRESH":
        reasons.append(f"tracking data is {health['tle_staleness'].lower()} ({health['tle_age_hours']}h old)")
    if health["drag_risk_score"] >= 0.5:
        reasons.append("orbital altitude puts it in a higher atmospheric-drag band")
    if health["orbit_anomaly_flag"]:
        reasons.append("orbital eccentricity is outside the typical range for this altitude")
    if not reasons:
        reasons.append("all monitored orbital signals are within normal range")

    return (
        f"{name} is classified {verdict['status']} (risk score {verdict['risk_score']}). "
        f"Basis: {'; '.join(reasons)}. "
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

        prompt = (
            "You are a spacecraft operations assistant. In two sentences, explain this "
            f"satellite's status to a non-expert.\n\nSatellite: {name}\n"
            f"Status: {verdict['status']}\nRisk score: {verdict['risk_score']}\n"
            f"TLE staleness: {health['tle_staleness']}\n"
            f"Drag risk score: {health['drag_risk_score']}\n"
            f"Orbit anomaly flagged: {health['orbit_anomaly_flag']}"
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
                "parameters": {"max_new_tokens": 120, "temperature": 0.3},
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