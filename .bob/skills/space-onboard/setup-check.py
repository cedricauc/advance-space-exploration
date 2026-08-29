#!/usr/bin/env python3
"""
setup-check.py — Automated onboarding health check for Space Exploration with AI.

Checks every prerequisite a new developer needs before the project runs.
Prints a structured PASS / WARN / FAIL report.
Exit code: 0 = all required checks pass, 1 = one or more FAIL.
"""

import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
MCP  = ROOT / "mcp-server"
GW   = ROOT / "web-gateway"

PASS = "✅ PASS"
WARN = "⚠️  WARN"
FAIL = "❌ FAIL"

results = []
failures = 0


def check(label, status, detail=""):
    global failures
    results.append({"label": label, "status": status, "detail": detail})
    if status == FAIL:
        failures += 1


# 1. Python version
pv = sys.version_info
if pv >= (3, 10):
    check("Python version", PASS, f"{pv.major}.{pv.minor}.{pv.micro}")
elif pv >= (3, 9):
    check("Python version", WARN, f"{pv.major}.{pv.minor} — 3.10+ recommended (X | Y union types)")
else:
    check("Python version", FAIL, f"{pv.major}.{pv.minor} — 3.10+ required")

# 2. Node.js version
node = shutil.which("node")
if node:
    try:
        nv = subprocess.check_output(["node", "--version"], text=True).strip()
        major = int(nv.lstrip("v").split(".")[0])
        check("Node.js version",
              PASS if major >= 18 else (WARN if major >= 16 else FAIL),
              f"{nv}{' (18+ recommended)' if major < 18 else ''}")
    except Exception as e:
        check("Node.js version", FAIL, f"Could not determine version: {e}")
else:
    check("Node.js version", FAIL, "node not found — install from https://nodejs.org")

# 3. Python venv
venv_python = MCP / "venv" / (
    "Scripts/python.exe" if platform.system() == "Windows" else "bin/python"
)
if venv_python.exists():
    check("Python venv", PASS, str(venv_python))
else:
    check("Python venv", FAIL,
          "Not found — run: cd mcp-server && python -m venv venv && pip install -r requirements.txt")

# 4. Python packages
req_file = MCP / "requirements.txt"
if req_file.exists():
    pkgs = [l.strip().split(">=")[0].split("==")[0]
            for l in req_file.read_text().splitlines()
            if l.strip() and not l.startswith("#")]
    missing = [p for p in pkgs
               if importlib.util.find_spec(p.replace("-", "_").lower()) is None
               and importlib.util.find_spec(p.lower()) is None]
    if missing:
        check("Python packages", FAIL,
              f"Missing: {', '.join(missing)} — run: pip install -r mcp-server/requirements.txt")
    else:
        check("Python packages", PASS, f"{len(pkgs)} packages installed")
else:
    check("Python packages", FAIL, "requirements.txt not found")

# 5. Node.js packages
nm = GW / "node_modules"
pkg_json = GW / "package.json"
if nm.is_dir() and pkg_json.exists():
    deps = list(json.loads(pkg_json.read_text()).get("dependencies", {}).keys())
    missing_nm = [d for d in deps if not (nm / d).is_dir()]
    if missing_nm:
        check("Node.js packages", FAIL,
              f"Missing: {', '.join(missing_nm)} — run: cd web-gateway && npm install")
    else:
        check("Node.js packages", PASS, f"{len(deps)} packages present")
else:
    check("Node.js packages", FAIL,
          "web-gateway/node_modules not found — run: cd web-gateway && npm install")

# 6. .env file
env_file = MCP / ".env"
if env_file.exists():
    env_keys = {l.split("=")[0].strip()
                for l in env_file.read_text().splitlines()
                if "=" in l and not l.startswith("#")}
    check(".env file", PASS, f"{len(env_keys)} variables defined")
else:
    check(".env file", WARN,
          "mcp-server/.env not found — copy .env.example and fill credentials. "
          "App runs without it (reduced features).")

# 7. .env.example
env_example = ROOT / ".env.example"
check(".env.example", PASS if env_example.exists() else WARN,
      str(env_example) if env_example.exists() else "Not found — credential docs missing")

# 8. Feature capability
KNOWN_CREDS = {
    "WATSONX_API_KEY":   "AI briefings (Granite)",
    "SPACETRACK_USER":   "Space-Track TLEs + decay",
    "N2YO_API_KEY":      "Pass predictions + overhead",
}
env_vals = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env_vals[k.strip()] = v.strip()

configured = {k for k in KNOWN_CREDS if env_vals.get(k) or os.environ.get(k)}
check("Optional credentials",
      PASS if configured else WARN,
      f"{len(configured)}/{len(KNOWN_CREDS)} credential blocks set: "
      + ", ".join(f"{v}={'✓' if k in configured else '✗'}"
                  for k, v in KNOWN_CREDS.items()))

# 9. Port 3000
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        in_use = s.connect_ex(("127.0.0.1", 3000)) == 0
    check("Port 3000",
          WARN if in_use else PASS,
          "Already in use — gateway may be running, or set PORT= in .env"
          if in_use else "Available")
except Exception:
    check("Port 3000", WARN, "Could not check")

# 10. Git status
try:
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL).strip()
    check("Git status",
          WARN if dirty else PASS,
          f"Branch: {branch}" + (f" — {len(dirty.splitlines())} uncommitted change(s)" if dirty else " — clean"))
except Exception:
    check("Git status", WARN, "Not a git repo or git not in PATH")

# ── Report ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
print("  Space Exploration with AI — Setup Health Check")
print("=" * 62)
for r in results:
    print(f"  {r['status']}  {r['label'].ljust(26)}  {r['detail']}")
print("=" * 62)

if failures == 0:
    print(f"\n  ✅  Ready to run!\n")
    print("  cd web-gateway && npm start")
    print("  → http://localhost:3000          (live satellite map)")
    print("  → http://localhost:3000/dashboard.html  (analytics dashboard)\n")
else:
    print(f"\n  ❌  {failures} check(s) failed — fix the items above first.\n")

print("  Feature availability:")
always_on = [
    "Satellite positions (Celestrak + SGP4)",
    "Space weather (NOAA SWPC — 4 streams)",
    "ISS position + crew + launch schedule",
    "Ground-station contact windows (SGP4)",
]
for f in always_on:
    print(f"  🟢  {f}")
for k, label in KNOWN_CREDS.items():
    icon = "🟢" if k in configured else "🔴"
    state = "enabled" if k in configured else "disabled — set credential in .env"
    print(f"  {icon}  {label.ljust(32)} {state}")
print()

sys.exit(0 if failures == 0 else 1)
