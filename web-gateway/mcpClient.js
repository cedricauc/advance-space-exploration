import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER_SCRIPT = path.resolve(__dirname, "../mcp-server/server.py");

// --- Automatic Python venv detection for Windows ---
// Priority:
// 1. PYTHON_COMMAND env var
// 2. Local venv python.exe
// 3. System python
const DEFAULT_VENV_PYTHON = path.resolve(
  __dirname,
  "../mcp-server/venv/Scripts/python.exe"
);

// "python3" isn't a registered command on most Windows installs (only
// "python" is, especially inside a venv). Allow overriding via env var,
// default to "python" on Windows and "python3" everywhere else.
const PYTHON_COMMAND = process.env.PYTHON_COMMAND || DEFAULT_VENV_PYTHON || (process.platform === "win32" ? "python" : "python3");

let client = null;
let connecting = null;

async function getClient() {
  if (client) return client;
  if (connecting) return connecting;

  connecting = (async () => {
    const transport = new StdioClientTransport({
      command: PYTHON_COMMAND,
      args: [SERVER_SCRIPT],
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