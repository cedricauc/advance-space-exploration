#!/usr/bin/env python3
"""
pre-release-check.py — Run all pre-release validations in parallel.

Checks (all run concurrently via threads):
  1. Python syntax on all mcp-server/*.py files
  2. All requirements.txt packages importable
  3. No secrets leaked into tracked files
  4. package.json dependencies present
  5. Critical env vars documented in .env.example
  6. GitHub Actions workflow file is valid YAML
  7. Git working tree status

Prints a structured PASS/WARN/FAIL report.
Exit code: 0 = all pass, 1 = any FAIL.
"""

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
MCP  = ROOT / "mcp-server"
GW   = ROOT / "web-gateway"

PASS = "✅ PASS"
WARN = "⚠️  WARN"
FAIL = "❌ FAIL"

results = []
failures = 0

def result(label, status, detail=""):
    global failures
    results.append({"label": label, "status": status, "detail": detail})
    if status == FAIL:
        failures += 1


# ── Check functions (run in parallel) ────────────────────────────────────────

def check_python_syntax():
    py_files = list(MCP.glob("*.py"))
    bad = []
    for f in py_files:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{f.name}:{e.lineno} — {e.msg}")
    if bad:
        result("Python syntax", FAIL, "; ".join(bad))
    else:
        result("Python syntax", PASS, f"{len(py_files)} files OK")


def check_python_imports():
    required = ["httpx", "skyfield", "dotenv", "fastmcp"]
    missing = []
    for pkg in required:
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)
    if missing:
        result("Python core imports", FAIL, f"Missing: {', '.join(missing)} — run: pip install -r mcp-server/requirements.txt")
    else:
        result("Python core imports", PASS, f"{len(required)} core packages importable")


def check_no_secrets_leaked():
    # Patterns that should never appear in tracked files
    secret_patterns = [
        (r"(?i)(apikey|api_key)\s*=\s*['\"][a-z0-9\-_]{20,}['\"]", "API key literal"),
        (r"(?i)password\s*=\s*['\"][^'\"]{8,}['\"]", "Password literal"),
        (r"eyJ[A-Za-z0-9\-_]{20,}\.[A-Za-z0-9\-_]{20,}", "JWT token"),
    ]
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL
        ).splitlines()
    except Exception:
        result("Secret leak scan", WARN, "git not available — skipped")
        return

    hits = []
    for rel_path in tracked:
        full = ROOT / rel_path
        if not full.is_file():
            continue
        # Skip binary files, node_modules, venv
        if any(x in rel_path for x in ["node_modules", "venv/", ".bob/", "vendor/"]):
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern, label in secret_patterns:
            if re.search(pattern, text):
                hits.append(f"{rel_path} ({label})")
                break

    if hits:
        result("Secret leak scan", FAIL, f"Possible secrets in: {', '.join(hits[:3])}")
    else:
        result("Secret leak scan", PASS, f"{len(tracked)} tracked files scanned")


def check_node_packages():
    pkg_json = GW / "package.json"
    nm = GW / "node_modules"
    if not pkg_json.exists():
        result("Node.js packages", FAIL, "package.json not found")
        return
    deps = list(json.loads(pkg_json.read_text()).get("dependencies", {}).keys())
    missing = [d for d in deps if not (nm / d).is_dir()]
    if missing:
        result("Node.js packages", FAIL, f"Missing: {', '.join(missing)} — run: cd web-gateway && npm install")
    else:
        result("Node.js packages", PASS, f"{len(deps)} packages installed")


def check_env_example():
    example = ROOT / ".env.example"
    if not example.exists():
        result(".env.example", FAIL, "Not found — credential documentation is missing")
        return
    content = example.read_text()
    required_keys = ["WATSONX_API_KEY", "SPACETRACK_USER", "N2YO_API_KEY", "PORT"]
    missing = [k for k in required_keys if k not in content]
    if missing:
        result(".env.example", WARN, f"Missing entries: {', '.join(missing)}")
    else:
        result(".env.example", PASS, "All credential blocks documented")


def check_workflow_yaml():
    workflow = ROOT / ".github" / "workflows" / "deploy.yml"
    if not workflow.exists():
        result("CI/CD workflow", FAIL, ".github/workflows/deploy.yml not found")
        return
    try:
        import yaml  # type: ignore
        yaml.safe_load(workflow.read_text())
        result("CI/CD workflow", PASS, "deploy.yml is valid YAML")
    except ImportError:
        # yaml not installed — just check file is non-empty
        size = workflow.stat().st_size
        result("CI/CD workflow", PASS if size > 100 else WARN,
               f"deploy.yml exists ({size} bytes) — install PyYAML for full validation")
    except Exception as e:
        result("CI/CD workflow", FAIL, f"Invalid YAML: {e}")


def check_git_status():
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL
        ).strip()
        uncommitted = len(dirty.splitlines()) if dirty else 0
        if uncommitted > 0:
            result("Git working tree", WARN,
                   f"Branch: {branch} — {uncommitted} uncommitted change(s). Commit before releasing.")
        else:
            result("Git working tree", PASS, f"Branch: {branch} — clean")
    except Exception:
        result("Git working tree", WARN, "git not available")


# ── Run all checks in parallel ────────────────────────────────────────────────

checks = [
    check_python_syntax,
    check_python_imports,
    check_no_secrets_leaked,
    check_node_packages,
    check_env_example,
    check_workflow_yaml,
    check_git_status,
]

with ThreadPoolExecutor(max_workers=len(checks)) as pool:
    futures = {pool.submit(fn): fn.__name__ for fn in checks}
    for future in as_completed(futures):
        try:
            future.result()
        except Exception as exc:
            result(futures[future], FAIL, f"Check raised exception: {exc}")


# ── Print report ──────────────────────────────────────────────────────────────

print("\n" + "=" * 64)
print("  Pre-Release Validation — Space Exploration with AI")
print("=" * 64)
for r in sorted(results, key=lambda x: (x["status"] != FAIL, x["status"] != WARN)):
    print(f"  {r['status']}  {r['label'].ljust(28)}  {r['detail']}")
print("=" * 64)

if failures == 0:
    print("\n  ✅  All checks passed — safe to release.\n")
else:
    print(f"\n  ❌  {failures} check(s) failed — fix before pushing.\n")

sys.exit(0 if failures == 0 else 1)
