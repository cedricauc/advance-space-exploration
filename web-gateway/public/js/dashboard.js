const statusPill = document.getElementById("status-pill");
const groupSelect = document.getElementById("groupSelect");
const customGroupLabel = document.getElementById("customGroupLabel");
const customGroupInput = document.getElementById("customGroupInput");
const limitInput = document.getElementById("limitInput");
const applyBtn = document.getElementById("applyBtn");

const STORAGE_KEY = "space-dashboard-selection";

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
  localStorage.setItem(STORAGE_KEY, JSON.stringify(selection));
}

function currentSelection() {
  const group = groupSelect.value;
  const isCustom = group === "__custom";
  return {
    group: isCustom ? customGroupInput.value.trim() || "stations" : group,
    customGroup: customGroupInput.value.trim(),
    limit: Math.max(1, Math.min(12000, Number(limitInput.value) || 12)),
  };
}

// Restore last selection on load
const saved = loadSelection();
groupSelect.value = saved.customGroup ? "__custom" : saved.group;
customGroupInput.value = saved.customGroup;
limitInput.value = saved.limit;
customGroupLabel.style.display = groupSelect.value === "__custom" ? "flex" : "none";

groupSelect.addEventListener("change", () => {
  customGroupLabel.style.display = groupSelect.value === "__custom" ? "flex" : "none";
});

applyBtn.addEventListener("click", () => {
  const selection = currentSelection();
  saveSelection(selection);
  loadDashboard();
});

let riskChart = null;

function renderKpis(satellites) {
  const total = satellites.length;
  const ok = satellites.filter((s) => s.ai_analysis.status === "OK").length;
  const degraded = satellites.filter((s) => s.ai_analysis.status === "DEGRADED").length;
  const outage = satellites.filter((s) => s.ai_analysis.status === "OUTAGE").length;
  const avgRisk = total
    ? (satellites.reduce((sum, s) => sum + s.ai_analysis.risk_score, 0) / total).toFixed(2)
    : "–";

  document.getElementById("kpi-total").textContent = total;
  document.getElementById("kpi-ok").textContent = ok;
  document.getElementById("kpi-degraded").textContent = degraded;
  document.getElementById("kpi-outage").textContent = outage;
  document.getElementById("kpi-risk").textContent = avgRisk;
}

function renderTable(satellites) {
  const tbody = document.querySelector("#statusTable tbody");
  tbody.innerHTML = "";
  satellites.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.position.name}</td>
      <td><span class="badge badge-${s.ai_analysis.status}">${s.ai_analysis.status}</span></td>
      <td>${s.ai_analysis.risk_score}</td>
      <td>${s.position.altitude_km}</td>
      <td>${s.coverage.coverage_radius_km}</td>
      <td>${s.health_signals.tle_age_hours}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderChart(satellites) {
  const ctx = document.getElementById("riskChart").getContext("2d");
  const labels = satellites.map((s) => s.position.name.slice(0, 18));
  const data = satellites.map((s) => s.ai_analysis.risk_score);
  const colors = satellites.map((s) => {
    if (s.ai_analysis.status === "OK") return "#3ddc84";
    if (s.ai_analysis.status === "DEGRADED") return "#ffb020";
    return "#ff4d5e";
  });

  if (riskChart) riskChart.destroy();
  riskChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: "Risk score", data, backgroundColor: colors }],
    },
    options: {
      scales: {
        y: { min: 0, max: 1, ticks: { color: "#93a0c9" }, grid: { color: "#253156" } },
        x: { ticks: { color: "#93a0c9" }, grid: { display: false } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

function renderWeather(weather) {
  const el = document.getElementById("weatherPanel");
  if (weather.error) {
    el.textContent = "Unable to load space weather: " + weather.error;
    return;
  }
  el.innerHTML = `
    Planetary K-index: <strong>${weather.latest_kp_index ?? "n/a"}</strong>
    (observed ${weather.observed_time ?? "n/a"})
    <div class="note">Higher K-index = more geomagnetic activity, correlated with increased atmospheric drag on LEO satellites.</div>
  `;
}

async function loadDashboard() {
  const { group, limit } = currentSelection();
  statusPill.textContent = `loading ${group} (limit ${limit})…`;
  applyBtn.disabled = true;
  try {
    const [constellationData, weather] = await Promise.all([
      fetch(`/api/analyze-constellation?group=${encodeURIComponent(group)}&limit=${limit}`).then((r) => r.json()),
      fetch("/api/space-weather").then((r) => r.json()),
    ]);

    const satellites = constellationData?.satellites;
    if (!Array.isArray(satellites)) throw new Error(constellationData?.error || "unexpected response");

    renderKpis(satellites);
    renderTable(satellites);
    renderChart(satellites);
    renderWeather(weather);

    const errorCount = constellationData?.errors?.length || 0;
    const errorNote = errorCount ? ` (${errorCount} satellite(s) failed to analyze)` : "";
    statusPill.textContent = `updated ${new Date().toLocaleTimeString()} · ${group}${errorNote}`;
    statusPill.classList.add("connected");
  } catch (err) {
    statusPill.textContent = "error: " + err.message;
    statusPill.classList.remove("connected");
  } finally {
    applyBtn.disabled = false;
  }
}

loadDashboard();
// Refresh periodically — real deployment would tie this to the daily
// Langflow -> Granite -> MCP pipeline run rather than a client-side poll.
setInterval(loadDashboard, 60000);