---
name: space-onboard
description: Use when a developer is setting up the Space Exploration with AI project for the first time, wants to verify their environment, asks about prerequisites, setup, "how do I get started", onboarding, or running the app locally.
---

# Space Exploration with AI — Onboarding Skill

You are helping a developer get this project running from scratch, or verifying an existing setup. Follow every step — do not skip ahead.

## Step 1 — Run the automated health check

```
execute_command: python .bob/skills/space-onboard/setup-check.py
```

Read every line of output. The script checks Python/Node versions, venv, packages, `.env`, port availability, and git status — in a single pass.

## Step 2 — Fix FAIL items (in order)

For each ❌ FAIL, provide the exact fix. Most common:

**Python venv missing:**
```bash
cd mcp-server
python -m venv venv
# Windows:  venv\Scripts\activate
# Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
```

**Node.js packages missing:**
```bash
cd web-gateway && npm install
```

After applying fixes, re-run the health check to confirm they cleared.

## Step 3 — Configure credentials (.env)

If `.env` is missing, generate it:
```bash
cp .env.example mcp-server/.env
```

Guide the developer through the three optional credential blocks:

**watsonx.ai** (Granite AI briefings)
- `WATSONX_API_KEY`, `WATSONX_URL`, `WATSONX_PROJECT_ID`
- Get from: https://cloud.ibm.com → create Watson Studio → watsonx.ai project
- Without: AI briefings use a local rule-based template (still works, clearly labelled)

**Space-Track.org** (authoritative TLEs + decay predictions)
- `SPACETRACK_USER`, `SPACETRACK_PASSWORD`
- Free account: https://www.space-track.org/auth/createAccount

**N2YO** (pass predictions + overhead queries)
- `N2YO_API_KEY`
- Free key: https://www.n2yo.com/api/

All three are optional — the core orbital mechanics and space weather work with no credentials.

## Step 4 — Start and validate

```bash
cd web-gateway && npm start
```

Expected: `space-mission gateway listening on http://localhost:3000`

Validate with three quick checks:
```bash
curl http://localhost:3000/api/iss
curl http://localhost:3000/api/space-weather
curl http://localhost:3000/api/satellite/25544
```

## Step 5 — Final summary

Tell the developer:
- Which features are enabled vs disabled based on their credentials
- The two URLs to open in the browser (map + dashboard)
- How to push their first feature: `git checkout -b feat/my-feature` → GitHub Actions auto-deploys to staging on push

For deployment help, say: "Use `/space-release` when you are ready to commit and push."
