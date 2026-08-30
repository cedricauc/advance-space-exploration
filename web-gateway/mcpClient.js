import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER_SCRIPT = path.resolve(__dirname, "../mcp-server/server.py");

// --- Automatic Python command resolution ---
// Priority:
// 1. PYTHON_COMMAND env var  (set to "python3" in render.yaml for Render/Linux)
// 2. Local venv python.exe   (Windows dev machines with a local venv)
// 3. "python" on Windows / "python3" everywhere else (system-wide fallback)
const VENV_PYTHON = path.resolve(__dirname, "../mcp-server/venv/Scripts/python.exe");

// Only use the venv path when the executable actually exists on disk.
// On Render (Linux) the Windows venv is never present, so DEFAULT_VENV_PYTHON
// must NOT be used there — otherwise PYTHON_COMMAND env var is silently ignored.
const PYTHON_COMMAND =
  process.env.PYTHON_COMMAND ||
  (existsSync(VENV_PYTHON) ? VENV_PYTHON : null) ||
  (process.platform === "win32" ? "python" : "python3");

let client = null;
let connecting = null;

async function getClient() {
  if (client) return client;
  if (connecting) return connecting;

  connecting = (async () => {
    // On Render (render.yaml), pip installs to mcp-server/venv-packages/ via --target.
    // We forward PYTHONPATH so the Python subprocess can find those packages
    // without a virtual environment. Locally, the venv handles this automatically.
    const extraEnv = {};
    const venvPackages = path.resolve(__dirname, "../mcp-server/venv-packages");
    if (existsSync(venvPackages)) {
      const existing = process.env.PYTHONPATH || "";
      extraEnv.PYTHONPATH = existing ? `${venvPackages}:${existing}` : venvPackages;
    }

    const transport = new StdioClientTransport({
      command: PYTHON_COMMAND,
      args: [SERVER_SCRIPT],
      env: { ...process.env, ...extraEnv },
    });

    const c = new Client({ name: "space-mission-gateway", version: "1.0.0" }, { capabilities: {} });
    await c.connect(transport);
    client = c;
    return c;
  })();

  return connecting;
}

/**
 * Calls a named MCP tool and returns the parsed JSON result.
 * Tools that return collections always wrap them in a named field
 * (e.g. { satellites: [...] }, { launches: [...] }) rather than a bare
 * array, so no shape-guessing is needed here.
 */
export async function callTool(toolName, args = {}) {
  const c = await getClient();
  const result = await c.callTool({ name: toolName, arguments: args });

  if (result.isError) {
    const message = result.content?.find((b) => b.type === "text")?.text || "MCP tool error";
    throw new Error(message);
  }

  if (result.structuredContent !== undefined) {
    return result.structuredContent;
  }

  const block = result.content?.find((b) => b.type === "text");
  if (!block) return null;

  try {
    return JSON.parse(block.text);
  } catch {
    return block.text;
  }
}

export async function listTools() {
  const c = await getClient();
  const { tools } = await c.listTools();
  return tools;
}