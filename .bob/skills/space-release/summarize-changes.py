#!/usr/bin/env python3
"""
summarize-changes.py — Document-understanding script for release notes.

Reads git history and changed files since the last tag (or last N commits)
and produces a structured change summary Bob can use to:
  - Write a meaningful commit message
  - Generate release notes
  - Identify which parts of the system changed (Python / Node / CI / docs)
  - Flag high-risk changes (AI prompt edits, API integrations, auth flows)

Usage:
  python summarize-changes.py              # since last git tag
  python summarize-changes.py --commits 5  # last 5 commits
  python summarize-changes.py --staged     # only staged changes
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent


def run(cmd, **kwargs):
    return subprocess.check_output(cmd, cwd=str(ROOT), text=True,
                                   stderr=subprocess.DEVNULL, **kwargs).strip()


def classify_file(path: str) -> str:
    if path.startswith("mcp-server/"):
        if "ai_analysis" in path:   return "AI/Granite"
        if "spacetrack" in path:    return "Space-Track API"
        if "n2yo" in path:          return "N2YO API"
        if "space_weather" in path: return "Space Weather"
        if "satellite_utils" in path: return "Orbital Mechanics"
        if "server.py" in path:     return "MCP Tools"
        return "Python/MCP"
    if path.startswith("web-gateway/"):
        if "public/" in path:       return "Frontend UI"
        return "Node.js Gateway"
    if path.startswith(".github/"):  return "CI/CD"
    if path.startswith(".bob/"):     return "Bob Workflows"
    if "README" in path or ".md" in path.lower(): return "Documentation"
    return "Other"


RISK_PATTERNS = {
    "AI/Granite":       "🔴 HIGH — Granite prompt changes affect all satellite briefings",
    "Space-Track API":  "🟠 MED  — Auth flow or GP query change; verify credentials still work",
    "N2YO API":         "🟠 MED  — API key auth change; verify N2YO responses",
    "MCP Tools":        "🟡 LOW  — New/modified MCP tool; check REST gateway wiring",
    "CI/CD":            "🟡 LOW  — Pipeline change; verify workflow YAML is valid",
    "Node.js Gateway":  "🟡 LOW  — Gateway change; smoke-test /api/iss after deploy",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commits", type=int, default=0,
                        help="Summarize last N commits (default: since last tag)")
    parser.add_argument("--staged", action="store_true",
                        help="Summarize only staged (index) changes")
    args = parser.parse_args()

    summary = {}

    # ── Get the commit range ──────────────────────────────────────────────────
    if args.staged:
        diff_cmd   = ["git", "diff", "--cached", "--name-only"]
        stat_cmd   = ["git", "diff", "--cached", "--stat"]
        log_lines  = []
        range_desc = "staged changes"
    elif args.commits > 0:
        diff_cmd   = ["git", "diff", f"HEAD~{args.commits}", "--name-only"]
        stat_cmd   = ["git", "diff", f"HEAD~{args.commits}", "--stat"]
        log_lines  = run(["git", "log", f"--oneline", f"-{args.commits}"]).splitlines()
        range_desc = f"last {args.commits} commit(s)"
    else:
        try:
            last_tag = run(["git", "describe", "--tags", "--abbrev=0"])
            diff_cmd  = ["git", "diff", last_tag, "--name-only"]
            stat_cmd  = ["git", "diff", last_tag, "--stat"]
            log_lines = run(["git", "log", f"{last_tag}..HEAD", "--oneline"]).splitlines()
            range_desc = f"since tag {last_tag}"
        except Exception:
            # No tags — fall back to last 10 commits
            diff_cmd   = ["git", "diff", "HEAD~10", "--name-only"]
            stat_cmd   = ["git", "diff", "HEAD~10", "--stat"]
            log_lines  = run(["git", "log", "--oneline", "-10"]).splitlines()
            range_desc = "last 10 commits (no tags found)"

    try:
        changed_files = [f for f in run(diff_cmd).splitlines() if f.strip()]
        diff_stat     = run(stat_cmd)
    except Exception as e:
        print(f"ERROR: Could not get git diff: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Classify changes ──────────────────────────────────────────────────────
    by_area: dict[str, list[str]] = {}
    risks: list[str] = []

    for f in changed_files:
        area = classify_file(f)
        by_area.setdefault(area, []).append(f)
        if area in RISK_PATTERNS and area not in [r.split(" — ")[0].lstrip("🔴🟠🟡 ") for r in risks]:
            risks.append(RISK_PATTERNS[area])

    # ── Current branch + last commit ─────────────────────────────────────────
    try:
        branch      = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        last_commit = run(["git", "log", "-1", "--pretty=%H %s"])
        author      = run(["git", "log", "-1", "--pretty=%an"])
    except Exception:
        branch = last_commit = author = "unknown"

    # ── Build the structured summary ─────────────────────────────────────────
    summary = {
        "range":         range_desc,
        "branch":        branch,
        "last_commit":   last_commit,
        "author":        author,
        "commit_count":  len(log_lines),
        "commits":       log_lines[:20],
        "files_changed": len(changed_files),
        "areas":         {area: files for area, files in sorted(by_area.items())},
        "risk_flags":    sorted(risks, reverse=True),  # HIGH first
        "diff_stat":     diff_stat,
    }

    # ── Print human-readable report ───────────────────────────────────────────
    print("\n" + "=" * 64)
    print("  Change Summary — Space Exploration with AI")
    print("=" * 64)
    print(f"  Range  : {range_desc}")
    print(f"  Branch : {branch}")
    print(f"  Author : {author}")
    print(f"  Commits: {len(log_lines)}")
    print(f"  Files  : {len(changed_files)} changed\n")

    if log_lines:
        print("  Recent commits:")
        for line in log_lines[:10]:
            print(f"    {line}")
        print()

    if by_area:
        print("  Changes by area:")
        for area, files in sorted(by_area.items()):
            print(f"    [{area}]  {len(files)} file(s)")
            for f in files[:5]:
                print(f"      - {f}")
            if len(files) > 5:
                print(f"      ... and {len(files) - 5} more")
        print()

    if risks:
        print("  Risk flags:")
        for r in risks:
            print(f"    {r}")
        print()
    else:
        print("  Risk flags: none — low-risk change set\n")

    print("=" * 64)

    # ── Also emit JSON for Bob to parse ──────────────────────────────────────
    print("\n--- JSON_SUMMARY_START ---")
    print(json.dumps(summary, indent=2))
    print("--- JSON_SUMMARY_END ---")


if __name__ == "__main__":
    main()
