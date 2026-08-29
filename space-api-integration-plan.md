# Space API Integration Plan

## Top-Level Overview

**Goal:** Extend the existing MCP server (`mcp-server/`) and web gateway (`web-gateway/`) to integrate four new data domains on top of what already exists:

1. **Space-Track High-Fidelity TLEs** — use Space-Track.org GP history and decay prediction endpoints for individual satellites; also re-propagate with Skyfield to return live position (same shape as `get_satellite_position`). Celestrak stays for constellation group fetches.
2. **Multi-Ground-Station Contact Windows** — pure SGP4 computation (no new external API) that accepts a caller-supplied list of ground stations and a NORAD ID and returns the next N contact opportunities.
3. **Enriched Space Weather** — add NOAA solar wind, X-ray flux, and geomagnetic storm alerts alongside the existing K-index call.
4. **N2YO Pass Predictions & Overhead Queries** — use the N2YO REST API to surface satellite pass predictions and "what's overhead" queries for a given observer location.

**Implementation order:** Space-Track → Contact Windows → Space Weather → N2YO → Docs

**Scope:**
- Edit `mcp-server/server.py` to add new MCP tools.
- Edit `mcp-server/satellite_utils.py` to add the contact-window helper.
- Add `mcp-server/spacetrack.py` for Space-Track session auth + GP/decay + Skyfield re-propagation.
- Add `mcp-server/space_weather.py` for the enriched NOAA calls.
- Add `mcp-server/n2yo.py` for N2YO API calls.
- Edit `web-gateway/server.js` to expose new REST endpoints.
- Edit `mcp-server/requirements.txt` if new packages are needed.
- No frontend (UI) changes.

**Design decisions locked:**
- Ground station list is always required from the caller (no hardcoded defaults).
- `get_spacetrack_tle` returns both raw GP fields AND live lat/lon/alt/speed from Skyfield propagation.
- Sub-task ordering: Space-Track first (highest fidelity), then contact windows, space weather, N2YO, docs.

**Non-goals:**
- No parallelization of `analyze_constellation` (separate concern).
- No Langflow wiring.
- No frontend Leaflet map changes.

---

## Sub-Tasks

---

### Sub-Task 1 — Space-Track High-Fidelity TLEs (`mcp-server/spacetrack.py` + 2 MCP tools)

**Intent:**
Space-Track.org provides authoritative GP (general perturbations) orbital element history and decay predictions that are more detailed than Celestrak's group feeds. This sub-task adds a session-authenticated client module and two new MCP tools. `get_spacetrack_tle` also re-propagates the fetched GP elements with Skyfield to return live position (same shape as the existing `get_satellite_position` tool).

**Expected Outcomes:**
- New file `mcp-server/spacetrack.py` with cookie-session auth and GP/decay query helpers.
- Two new MCP tools in `server.py`:
  - `get_spacetrack_tle(norad_id)` — latest high-fidelity GP element set + live lat/lon/alt/speed from Skyfield.
  - `get_spacetrack_decay(norad_id)` — decay prediction data (predicted reentry date, probability).
- Two new REST endpoints in `web-gateway/server.js`:
  - `GET /api/spacetrack/tle/:noradId`
  - `GET /api/spacetrack/decay/:noradId`
- `SPACETRACK_USER` and `SPACETRACK_PASSWORD` env vars documented.

**Todo List:**
1. Create `mcp-server/spacetrack.py`.
2. Add `SPACETRACK_BASE` constant: `https://www.space-track.org`.
3. Add `_get_session_cookie()` — async function that POSTs to `/ajaxauth/login` with `{identity, password}` form data and returns the session cookie. Cache the cookie in a module-level variable with a 6-hour TTL (re-auth on 401).
4. Add `fetch_latest_gp(norad_id)` — calls `GET /basicspacedata/query/class/gp/NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/1/format/json` with session cookie, returns raw GP dict.
5. Add `fetch_decay_prediction(norad_id)` — calls `GET /basicspacedata/query/class/decay/NORAD_CAT_ID/{norad_id}/orderby/DECAY_EPOCH%20desc/limit/1/format/json`, returns decay data or `null` if none predicted.
6. Add `propagate_from_gp(gp)` — use `TLE_LINE1` and `TLE_LINE2` fields from the GP dict directly, load into Skyfield `EarthSatellite`, propagate to now, return `{lat, lon, altitude_km, speed_km_s}` (same keys as `propagate()` in `satellite_utils.py`).
7. In `server.py`, add MCP tool `get_spacetrack_tle` that calls `fetch_latest_gp()` then `propagate_from_gp()` and returns both the raw GP fields and the live position dict.
8. In `server.py`, add MCP tool `get_spacetrack_decay` that calls `fetch_decay_prediction()`.
9. Guard both tools: if `SPACETRACK_USER` or `SPACETRACK_PASSWORD` are not set, return a clear credential-missing message.
10. Handle 401 re-auth: on HTTP 401, clear cached cookie, re-authenticate once, retry.
11. In `web-gateway/server.js`, add `GET /api/spacetrack/tle/:noradId` and `GET /api/spacetrack/decay/:noradId` routes.

**Relevant Context:**
- Existing TLE fetch pattern: [`satellite_utils.py` `fetch_tle_by_norad_id`](mcp-server/satellite_utils.py) — same httpx.AsyncClient pattern, 15s timeout.
- Module-level cache pattern: [`satellite_utils.py` `_tle_cache`](mcp-server/satellite_utils.py) — dict with TTL timestamp; reuse this pattern for session cookie cache.
- Existing Skyfield propagation: [`satellite_utils.py` `propagate`](mcp-server/satellite_utils.py) — reference for `EarthSatellite` usage and returned position shape.
- GP dict from Space-Track includes `TLE_LINE1` and `TLE_LINE2` directly — no orbital-element reconstruction needed.
- Space-Track API docs: https://www.space-track.org/documentation

**Status:** [x] done

---

### Sub-Task 2 — Ground-Station Contact Windows (`satellite_utils.py` helper + 1 MCP tool)

**Intent:**
Given a caller-supplied list of ground stations (each with lat/lon/min_elevation) and a satellite NORAD ID, compute the next N contact windows using SGP4 propagation already present in `satellite_utils.py`. No new external API is needed — this is pure orbital mechanics using Skyfield.

**Expected Outcomes:**
- New function `compute_contact_windows(norad_id, ground_stations, duration_hours, step_seconds)` added to `satellite_utils.py`.
- One new MCP tool in `server.py`: `get_contact_windows(norad_id, ground_stations, duration_hours)`.
- One new REST endpoint in `web-gateway/server.js`: `POST /api/contact-windows` (POST because the ground station list is the request body).

**Todo List:**
1. In `satellite_utils.py`, add `compute_contact_windows(norad_id, ground_stations, duration_hours=24, step_seconds=30)`:
   a. Fetch TLE for `norad_id` using existing `fetch_tle_by_norad_id()`.
   b. Load into Skyfield `EarthSatellite` (same as `propagate()`).
   c. For each ground station `{name, lat, lon, min_elevation_deg}`:
      - Step through the time range in `step_seconds` increments using `ts.utc()`.
      - Compute satellite elevation above the station using Skyfield's `difference.altaz()`.
      - Detect rising edge (elevation crosses `min_elevation_deg` upward) and setting edge.
      - Record each window: `{station_name, aos_utc, los_utc, max_elevation_deg, duration_seconds}`.
   d. Return all windows across all stations, sorted by `aos_utc`.
2. In `server.py`, add MCP tool `get_contact_windows`:
   - Parameters: `norad_id: int`, `ground_stations: list[dict]`, `duration_hours: float = 24`.
   - Call `satellite_utils.compute_contact_windows()`.
   - Return JSON list of contact window objects.
3. In `web-gateway/server.js`, add `POST /api/contact-windows`:
   - Read `noradId`, `groundStations`, optional `durationHours` from request body.
   - Call MCP tool `get_contact_windows`.

**Relevant Context:**
- Existing SGP4 propagation: [`satellite_utils.py` `propagate`](mcp-server/satellite_utils.py) — uses `EarthSatellite`, `load.timescale()`.
- Skyfield `difference.altaz()` returns `(altitude, azimuth, distance)` where altitude is the elevation angle above the horizon.
- No new packages needed (Skyfield already in `requirements.txt`).

**Status:** [x] done

---

### Sub-Task 3 — Enriched Space Weather (`mcp-server/space_weather.py` + updated MCP tool)

**Intent:**
The existing `get_space_weather` tool only fetches NOAA's 1-minute planetary K-index. This sub-task replaces that single inline call with a richer aggregation that also fetches solar wind speed/density, X-ray flux, and geomagnetic storm alerts from NOAA SWPC — all public endpoints, no API key needed.

**Expected Outcomes:**
- New file `mcp-server/space_weather.py` with four async fetch functions.
- `get_space_weather` MCP tool in `server.py` updated to call the new module and return all four data points in a single structured dict.
- Existing `GET /api/space-weather` gateway endpoint returns the enriched payload (same path, no new route needed).

**Todo List:**
1. Create `mcp-server/space_weather.py`.
2. Add `fetch_kindex()` — extract last entry from `https://services.swpc.noaa.gov/json/planetary_k_index_1m.json` (existing logic, moved here).
3. Add `fetch_solar_wind()` — fetch `https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json`, return `{speed_km_s, density_protons_cm3}` from the latest record.
4. Add `fetch_xray_flux()` — fetch `https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json`, return latest short-band (0.05–0.4 nm) and long-band (0.1–0.8 nm) flux values, derive flare class (A/B/C/M/X scale).
5. Add `fetch_geomagnetic_alerts()` — fetch `https://services.swpc.noaa.gov/products/alerts.json`, filter for active geomagnetic storm (G1–G5) and solar radiation storm (S1–S5) messages, return list of active alert strings.
6. Add `get_enriched_space_weather()` — call all four with `asyncio.gather`, return merged dict.
7. In `server.py`, replace the inline NOAA K-index HTTP call in `get_space_weather` with a call to `space_weather.get_enriched_space_weather()`.
8. Remove the now-unused inline `httpx` call and `NOAA_KINDEX_URL` reference from `server.py` (the default URL moves into `space_weather.py`).
9. Verify `requirements.txt` — no new packages needed.

**Relevant Context:**
- Existing K-index call: [`server.py` `get_space_weather`](mcp-server/server.py) — inline `httpx.AsyncClient` call to `NOAA_KINDEX_URL`.
- All four NOAA SWPC endpoints are public, no API key required.
- `asyncio.gather` pattern for parallel fetches (same pattern used conceptually in `analyze_constellation`).

**Status:** [x] done

---

### Sub-Task 4 — N2YO Pass Predictions & Overhead Queries (`mcp-server/n2yo.py` + 2 MCP tools)

**Intent:**
N2YO provides a REST API for satellite pass predictions and "what's overhead" queries given observer latitude/longitude/altitude. This sub-task adds a dedicated module and two new MCP tools.

**Expected Outcomes:**
- New file `mcp-server/n2yo.py` with authenticated N2YO API calls.
- Two new MCP tools in `server.py`:
  - `get_satellite_passes(norad_id, lat, lon, alt_m, days, min_elevation)` — next visible passes.
  - `get_satellites_overhead(lat, lon, alt_m, search_radius_deg, category_id)` — satellites above horizon.
- Two new REST endpoints in `web-gateway/server.js`:
  - `GET /api/passes/:noradId?lat=&lon=&alt=&days=&min_elevation=`
  - `GET /api/overhead?lat=&lon=&alt=&radius=&category=`
- `N2YO_API_KEY` env var documented.

**Todo List:**
1. Create `mcp-server/n2yo.py`.
2. Add `N2YO_BASE_URL` constant: `https://api.n2yo.com/rest/v1/satellite`.
3. Add `_n2yo_get(path)` — private async helper that appends `&apiKey={N2YO_API_KEY}` as a query param and calls `httpx.AsyncClient` with 15s timeout.
4. Add `get_passes(norad_id, lat, lon, alt_m, days, min_elevation_deg)` — calls `/radiopasses/{norad_id}/{lat}/{lon}/{alt}/{days}/{min_elevation_deg}`, returns list of pass objects (rise time UTC, max elevation, set time UTC).
5. Add `get_overhead(lat, lon, alt_m, search_radius_deg, category_id)` — calls `/above/{lat}/{lon}/{alt}/{search_radius_deg}/{category_id}`, returns list of satellites currently above horizon.
6. In `server.py`, add MCP tool `get_satellite_passes` that calls `n2yo.get_passes()`.
7. In `server.py`, add MCP tool `get_satellites_overhead` that calls `n2yo.get_overhead()`.
8. Guard both tools: if `N2YO_API_KEY` is not set, return a clear credential-missing message (same pattern as watsonx fallback in `ai_analysis.py`).
9. In `web-gateway/server.js`, add `GET /api/passes/:noradId` and `GET /api/overhead` routes.

**Relevant Context:**
- Auth pattern: API key appended as query param `apiKey=` (N2YO convention).
- Credential-missing fallback pattern: [`ai_analysis.py` `analyze`](mcp-server/ai_analysis.py) — returns honest message when `WATSONX_API_KEY` not set.
- `httpx.AsyncClient` with 10–15s timeout (same as existing tools in `server.py`).
- N2YO API docs: https://www.n2yo.com/api/

**Status:** [x] done

---

### Sub-Task 5 — `requirements.txt` and `README.MD` Updates

**Intent:**
Consolidate any new package additions and document all new environment variables and endpoints introduced in sub-tasks 1–4.

**Expected Outcomes:**
- `mcp-server/requirements.txt` updated if any new packages are needed (none expected, but confirm at implementation time).
- `README.MD` updated with new env vars (`N2YO_API_KEY`, `SPACETRACK_USER`, `SPACETRACK_PASSWORD`), new MCP tools, and new REST endpoints.

**Todo List:**
1. After sub-tasks 1–4 are complete, check all new imports against `requirements.txt`; add any missing packages.
2. Add new env vars to `README.MD` (alongside the existing watsonx/Celestrak vars).
3. Add new MCP tools to the tool table in `README.MD`.
4. Add new REST endpoints list to `README.MD`.

**Relevant Context:**
- [`mcp-server/requirements.txt`](mcp-server/requirements.txt) — current 10 packages.
- [`README.MD`](README.MD) — existing documentation structure.

**Status:** [x] done
