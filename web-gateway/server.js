import express from "express";
import { createServer } from "node:http";
import { Server } from "socket.io";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { callTool } from "./mcpClient.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const httpServer = createServer(app);
const io = new Server(httpServer);

app.use(express.static(path.join(__dirname, "public")));
app.use(express.json());

// ---------------------------------------------------------------------------
// REST endpoints — thin wrappers around MCP tool calls
// ---------------------------------------------------------------------------

app.get("/api/satellite/:noradId", async (req, res) => {
  try {
    const noradId = Number(req.params.noradId);
    res.json(await callTool("get_satellite_position", { norad_id: noradId }));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/constellation", async (req, res) => {
  try {
    const group = req.query.group || "starlink";
    const limit = Number(req.query.limit) || 20;
    res.json(await callTool("get_constellation_snapshot", { group, limit }));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/coverage", async (req, res) => {
  try {
    const altitudeKm = Number(req.query.altitude_km);
    const minElevationDeg = req.query.min_elevation_deg ? Number(req.query.min_elevation_deg) : undefined;
    res.json(
      await callTool("get_coverage_zone", {
        altitude_km: altitudeKm,
        ...(minElevationDeg !== undefined ? { min_elevation_deg: minElevationDeg } : {}),
      })
    );
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/analyze/:noradId", async (req, res) => {
  try {
    const noradId = Number(req.params.noradId);
    res.json(await callTool("analyze_satellite", { norad_id: noradId }));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/analyze-constellation", async (req, res) => {
  try {
    const group = req.query.group || "starlink";
    const limit = Number(req.query.limit) || 10;
    res.json(await callTool("analyze_constellation", { group, limit }));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/space-weather", async (req, res) => {
  try {
    res.json(await callTool("get_space_weather"));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/iss", async (req, res) => {
  try {
    res.json(await callTool("get_iss_position"));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/astronauts", async (req, res) => {
  try {
    res.json(await callTool("get_astronauts_in_space"));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get("/api/launches", async (req, res) => {
  try {
    const limit = Number(req.query.limit) || 5;
    res.json(await callTool("get_launch_schedule", { limit }));
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

// ---------------------------------------------------------------------------
// Live broadcast loop — pushes a constellation snapshot + AI analysis to all
// connected clients on an interval. Analysis is heavier (calls Granite/local
// fallback per satellite) so it runs on a longer interval than a bare
// position-only tracker would.
// ---------------------------------------------------------------------------

const BROADCAST_GROUP = process.env.BROADCAST_GROUP || "stations";
const BROADCAST_LIMIT = Number(process.env.BROADCAST_LIMIT) || 8;
const BROADCAST_INTERVAL_MS = Number(process.env.BROADCAST_INTERVAL_MS) || 15000;

let broadcastTimer = null;

function startBroadcastLoop() {
  if (broadcastTimer) return;
  broadcastTimer = setInterval(async () => {
    if (io.engine.clientsCount === 0) return;
    try {
      const snapshot = await callTool("analyze_constellation", {
        group: BROADCAST_GROUP,
        limit: BROADCAST_LIMIT,
      });
      io.emit("constellation:update", { group: BROADCAST_GROUP, satellites: snapshot.satellites });
    } catch (err) {
      io.emit("constellation:error", { message: err.message });
    }
  }, BROADCAST_INTERVAL_MS);
}

io.on("connection", (socket) => {
  console.log("client connected:", socket.id);
  startBroadcastLoop();

  socket.on("disconnect", () => {
    console.log("client disconnected:", socket.id);
  });
});

const PORT = process.env.PORT || 3000;
httpServer.listen(PORT, () => {
  console.log(`space-mission gateway listening on http://localhost:${PORT}`);
});