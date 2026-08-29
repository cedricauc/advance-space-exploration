/*
 * globe3d.js — plain Three.js satellite tracker (no globe.gl).
 *
 * History note: an earlier attempt used globe.gl as a wrapper library and
 * hit three separate, genuinely different CDN/version failures in a row.
 * This version avoids that whole class of problem by using plain Three.js
 * directly, self-hosted from the real npm package.
 *
 * Architecture note: data polling (fetching satellite positions from our
 * own API) is intentionally decoupled from 3D scene setup and started
 * FIRST. If WebGL/Three.js initialization fails for any reason (unsupported
 * browser, disabled hardware acceleration, a texture 404, etc.), that must
 * not prevent the status pill / data layer from working and reporting a
 * real error — otherwise a rendering failure looks identical to "stuck
 * forever connecting", which has no diagnostic value for the person seeing it.
 */

import * as THREE from "three";
import { OrbitControls } from "/vendor/OrbitControls.js";

const EARTH_RADIUS = 100; // scene units
const statusColor = {
  OK: 0x3ddc84,
  DEGRADED: 0xffb020,
  OUTAGE: 0xff4d5e,
};

// ---- DOM elements ----
const container = document.getElementById("globe3dContainer");
const statusPill = document.getElementById("status-pill");
const groupSelect = document.getElementById("groupSelect");
const customGroupLabel = document.getElementById("customGroupLabel");
const customGroupInput = document.getElementById("customGroupInput");
const limitInput = document.getElementById("limitInput");
const applyBtn = document.getElementById("applyBtn");
const infoPanel = document.getElementById("infoPanel");
const infoPanelContent = document.getElementById("infoPanelContent");
const infoPanelClose = document.getElementById("infoPanelClose");

// ---- Selection persistence (own storage key — independent of the 2D map page) ----
const STORAGE_KEY = "space-selection";
const POLL_INTERVAL_MS = 15000;

function loadSelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    return {
      group: saved.group || "stations",
      customGroup: saved.customGroup || "",
      limit: saved.limit || 12,
    };
  } catch {
    return { group: "stations", customGroup: "", limit: 12 };
  }
}

function saveSelection(selection) {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      group: selection.group === "__custom" ? "__custom" : selection.group,
      customGroup: selection.customGroup || "",
      limit: selection.limit,
    })
  );
}

function currentSelection() {
  const group = groupSelect.value;
  const isCustom = group === "__custom";
  return {
    group: isCustom ? customGroupInput.value.trim() || "stations" : group,
    customGroup: customGroupInput.value.trim(),
    limit: Math.max(1, Math.min(200, Number(limitInput.value) || 8)),
  };
}

const saved = loadSelection();

groupSelect.value = saved.group === "__custom" ? "__custom" : saved.group;
customGroupInput.value = saved.customGroup;
limitInput.value = saved.limit;

customGroupLabel.style.display =
  groupSelect.value === "__custom" ? "flex" : "none";

groupSelect.addEventListener("change", () => {
  customGroupLabel.style.display = groupSelect.value === "__custom" ? "flex" : "none";
});

// ============================================================================
// DATA LAYER — starts immediately, independent of whether 3D rendering works.
// ============================================================================

// These get assigned once (if) the 3D scene below initializes successfully.
// loadSatellites() checks this flag before touching any Three.js objects.
let renderingReady = false;
let onSatellitesUpdated = null; // set by the rendering layer once ready

let pollTimer = null;

async function loadSatellites() {
  const { group, limit } = currentSelection();
  applyBtn.disabled = true;
  try {
    const resp = await fetch(`/api/analyze-constellation?group=${encodeURIComponent(group)}&limit=${limit}`);
    if (!resp.ok) {
      statusPill.textContent = `error: HTTP ${resp.status} from /api/analyze-constellation`;
      statusPill.classList.remove("connected");
      return;
    }
    const data = await resp.json();

    if (!Array.isArray(data?.satellites)) {
      statusPill.textContent = "error: " + (data?.error || "unexpected response shape");
      statusPill.classList.remove("connected");
      return;
    }

    if (renderingReady && onSatellitesUpdated) {
      onSatellitesUpdated(data.satellites);
    }

    const errorCount = data.errors?.length || 0;
    const errorNote = errorCount ? ` (${errorCount} failed)` : "";
    const renderNote = renderingReady ? "" : " (3D view unavailable — see console)";
    statusPill.textContent = `live · ${data.satellites.length} tracked (${group})${errorNote}${renderNote}`;
    statusPill.classList.add("connected");
  } catch (err) {
    // This is the case that used to look like "connecting forever": a
    // network failure or thrown error now always updates the pill instead
    // of leaving it on its initial static text.
    statusPill.textContent = "error: " + err.message;
    statusPill.classList.remove("connected");
    console.error("[globe3d] loadSatellites failed:", err);
  } finally {
    applyBtn.disabled = false;
  }
}

function restartPolling() {
  if (pollTimer) clearInterval(pollTimer);
  loadSatellites();
  pollTimer = setInterval(loadSatellites, POLL_INTERVAL_MS);
}

applyBtn.addEventListener("click", () => {
  saveSelection(currentSelection());
  restartPolling();
});

// Start data polling immediately — before any 3D setup is attempted.
restartPolling();

// ============================================================================
// RENDERING LAYER — wrapped defensively; a failure here is visible on-page
// and in the console, and does not block the data layer above.
// ============================================================================

function showRenderError(err) {
  console.error("[globe3d] 3D rendering failed to initialize:", err);
  container.innerHTML = `
    <div style="padding:24px; color:#ff8080; font-family:monospace; font-size:13px; max-width:600px;">
      <strong>3D rendering failed to start.</strong><br/><br/>
      ${err.message || err}<br/><br/>
      This does not affect satellite data — the status pill above still
      updates. Check the browser console for the full error. Common causes:
      WebGL unsupported/disabled, or a texture file failing to load
      (check the Network tab for a 404 under /vendor/textures/).
    </div>
  `;
}

try {
  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 10000);
  camera.position.set(0, 60, 320);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  const textureLoader = new THREE.TextureLoader();

  // Track texture load failures explicitly — TextureLoader fails silently
  // (a blank/black material) by default, which previously could look like
  // "nothing is happening" with no error at all.
  function loadTexture(url) {
    return textureLoader.load(
      url,
      undefined,
      undefined,
      (err) => console.error(`[globe3d] failed to load texture ${url}:`, err)
    );
  }

  scene.background = loadTexture("/vendor/textures/night-sky.png");

  const sunLight = new THREE.DirectionalLight(0xffffff, 2.2);
  sunLight.position.set(300, 150, 200);
  scene.add(sunLight);
  scene.add(new THREE.AmbientLight(0x556677, 1.1));

  const earthGeometry = new THREE.SphereGeometry(EARTH_RADIUS, 64, 64);
  const earthMaterial = new THREE.MeshPhongMaterial({
    map: loadTexture("/vendor/textures/earth-blue-marble.jpg"),
    bumpMap: loadTexture("/vendor/textures/earth-topology.png"),
    bumpScale: 1.5,
    shininess: 5,
  });
  const earthMesh = new THREE.Mesh(earthGeometry, earthMaterial);
  scene.add(earthMesh);

  const atmosphereGeometry = new THREE.SphereGeometry(EARTH_RADIUS * 1.015, 64, 64);
  const atmosphereMaterial = new THREE.MeshBasicMaterial({
    color: 0x4a90ff,
    transparent: true,
    opacity: 0.08,
    side: THREE.BackSide,
  });
  scene.add(new THREE.Mesh(atmosphereGeometry, atmosphereMaterial));

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.3;
  controls.minDistance = EARTH_RADIUS * 1.1;
  controls.maxDistance = EARTH_RADIUS * 8;
  controls.addEventListener("start", () => {
    controls.autoRotate = false;
  });

  const satelliteMeshes = new Map(); // noradId -> { mesh, data }
  const satelliteGroup = new THREE.Group();
  scene.add(satelliteGroup);

  function latLonAltToVector3(lat, lon, altitudeKm) {
    const radius = EARTH_RADIUS * (1 + Math.min(altitudeKm / 6371, 3) * 0.15) + EARTH_RADIUS * 0.03;
    const phi = (90 - lat) * (Math.PI / 180);
    const theta = (lon + 180) * (Math.PI / 180);
    return new THREE.Vector3(
      -radius * Math.sin(phi) * Math.cos(theta),
      radius * Math.cos(phi),
      radius * Math.sin(phi) * Math.sin(theta)
    );
  }

  function createSatelliteMesh(sat) {
    const color = statusColor[sat.ai_analysis.status] ?? 0x5b8cff;
    const geometry = new THREE.SphereGeometry(1.8, 12, 12);
    const material = new THREE.MeshBasicMaterial({ color });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.userData.satellite = sat;
    return mesh;
  }

  function upsertSatelliteMesh(sat) {
    const noradId = sat.position.norad_id;
    const pos = latLonAltToVector3(sat.position.latitude, sat.position.longitude, sat.position.altitude_km);

    if (satelliteMeshes.has(noradId)) {
      const entry = satelliteMeshes.get(noradId);
      entry.mesh.position.copy(pos);
      entry.mesh.material.color.setHex(statusColor[sat.ai_analysis.status] ?? 0x5b8cff);
      entry.mesh.userData.satellite = sat;
      entry.data = sat;
    } else {
      const mesh = createSatelliteMesh(sat);
      mesh.position.copy(pos);
      satelliteGroup.add(mesh);
      satelliteMeshes.set(noradId, { mesh, data: sat });
    }
  }

  function pruneStaleMeshes(currentNoradIds) {
    const currentSet = new Set(currentNoradIds);
    for (const [noradId, entry] of satelliteMeshes.entries()) {
      if (!currentSet.has(noradId)) {
        satelliteGroup.remove(entry.mesh);
        entry.mesh.geometry.dispose();
        entry.mesh.material.dispose();
        satelliteMeshes.delete(noradId);
      }
    }
  }

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function renderInfoPanel(sat, { loadingExplanation } = {}) {
    const p = sat.position;
    const h = sat.health_signals;
    const a = sat.ai_analysis;
    const explanationHtml = loadingExplanation
      ? `<p class="explanation"><em>Loading AI explanation…</em></p>`
      : `<p class="explanation">${a.explanation ?? "No explanation available."}</p>
         <p class="note">Source: ${a.explanation_source}</p>`;

    infoPanelContent.innerHTML = `
      <h3>${p.name}</h3>
      <span class="badge badge-${a.status}">${a.status}</span>
      <dl>
        <dt>NORAD ID</dt><dd>${p.norad_id}</dd>
        <dt>Latitude</dt><dd>${p.latitude}°</dd>
        <dt>Longitude</dt><dd>${p.longitude}°</dd>
        <dt>Altitude</dt><dd>${p.altitude_km} km</dd>
        <dt>Speed</dt><dd>${p.speed_km_s} km/s</dd>
        <dt>Inclination</dt><dd>${p.inclination_deg}°</dd>
        <dt>Eccentricity</dt><dd>${p.eccentricity}</dd>
        <dt>Coverage radius</dt><dd>${sat.coverage.coverage_radius_km} km</dd>
        <dt>Risk score</dt><dd>${a.risk_score}</dd>
        <dt>TLE staleness</dt><dd>${h.tle_staleness} (${h.tle_age_hours}h old)</dd>
        <dt>Drag risk</dt><dd>${h.drag_risk_score}</dd>
        <dt>Orbit anomaly</dt><dd>${h.orbit_anomaly_flag ? "Yes" : "No"}</dd>
      </dl>
      ${explanationHtml}
    `;
    infoPanel.classList.remove("hidden");
  }

  // Bulk constellation data (from analyze_constellation) intentionally omits
  // the AI-generated explanation — computing that for every satellite would
  // mean thousands of watsonx calls just to render the map. Instead: show
  // the instantly-available data (position, risk score, status — all local
  // math) immediately on click, then fetch the single-satellite full
  // analysis (analyze_satellite, which DOES call watsonx/Granite) in the
  // background and fill in the explanation once it arrives.
  let infoPanelRequestToken = 0;

  async function showInfoPanel(sat) {
    const noradId = sat.position.norad_id;
    const thisRequest = ++infoPanelRequestToken;

    renderInfoPanel(sat, { loadingExplanation: true });

    try {
      const resp = await fetch(`/api/analyze/${noradId}`);
      const detailed = await resp.json();

      // If the user clicked a different satellite while this was in flight,
      // drop this stale response rather than overwriting the newer panel.
      if (thisRequest !== infoPanelRequestToken) return;

      if (!resp.ok || detailed?.error) {
        renderInfoPanel(sat); // fall back to the instant data, no explanation
        return;
      }

      renderInfoPanel(detailed);
    } catch (err) {
      if (thisRequest !== infoPanelRequestToken) return;
      console.error("[globe3d] failed to fetch satellite detail:", err);
      renderInfoPanel(sat); // fall back to the instant data, no explanation
    }
  }

  infoPanelClose.addEventListener("click", () => infoPanel.classList.add("hidden"));

  renderer.domElement.addEventListener("click", (event) => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(pointer, camera);
    const meshes = [...satelliteMeshes.values()].map((e) => e.mesh);
    const hits = raycaster.intersectObjects(meshes, false);

    if (hits.length > 0) {
      showInfoPanel(hits[0].object.userData.satellite);
    }
  });

  function syncSize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (w === 0 || h === 0) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  new ResizeObserver(syncSize).observe(container);
  syncSize();

  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  // Wire the data layer to the rendering layer, now that setup succeeded.
  onSatellitesUpdated = (satellites) => {
    satellites.forEach(upsertSatelliteMesh);
    pruneStaleMeshes(satellites.map((s) => s.position.norad_id));
  };
  renderingReady = true;
} catch (err) {
  showRenderError(err);
}