---
name: space-release
description: Use when the developer wants to commit and push a feature, release the app, deploy to staging or production, run pre-release checks, write a commit message, push to git, trigger CI/CD, or asks "how do I deploy" on the Space Exploration with AI project.
---

# Space Exploration with AI — Release & Deploy Skill

You are the release engineer for this project. When activated, you run the full
release pipeline: understand what changed, validate everything, write a precise
commit message, push, and confirm the GitHub Actions deployment is triggered.

Never skip a step. Never ask the developer to do something you can do yourself.

---

## Step 1 — Understand what changed (Document Understanding)

Run the change summarizer to read git history and classify every modified file:

```
execute_command: python .bob/skills/space-release/summarize-changes.py
```

Parse the JSON block between `--- JSON_SUMMARY_START ---` and `--- JSON_SUMMARY_END ---`.

From the summary extract:
- `branch` — the current branch name
- `areas` — which subsystems changed (AI/Granite, MCP Tools, CI/CD, etc.)
- `risk_flags` — any HIGH/MED risk areas that need extra attention
- `commits` — the recent commit messages to understand intent
- `files_changed` — total scope

If `branch` does not start with `feat/`, `fix/`, or `main`, warn the developer:
> "You are on branch `{branch}`. GitHub Actions only deploys from `feat/*` branches
> (→ staging) and `main` (→ production). Consider renaming: `git branch -m feat/your-feature`"

---

## Step 2 — Run pre-release validation (Parallel subagents)

**Spawn two subagents in parallel** to run validation while you analyse the diff:

### Subagent A — Python + secrets validation
```
spawn_subagent: Run `python .bob/skills/space-release/pre-release-check.py`
and report back: number of FAIL items, number of WARN items, and the full output.
```

### Subagent B — Node.js syntax check
```
spawn_subagent: In the web-gateway/ directory, run:
  node --check server.js && node --check mcpClient.js && echo "Node syntax OK"
Report back pass/fail and any error messages.
```

Wait for both subagents to complete. If either reports any ❌ FAIL:
1. Show the developer the exact failing check and the fix command.
2. After they confirm the fix is applied, re-run the failed check before continuing.
3. Do NOT proceed to Step 3 until all FAILs are resolved.

⚠️ WARNs do not block the release — surface them but continue.

---

## Step 3 — Draft the commit message

Using the structured change summary from Step 1, write a conventional commit message:

**Format:**
```
<type>(<scope>): <concise imperative summary>

<body: 2-4 lines explaining WHAT changed and WHY, not HOW>

Files: <comma-separated list of key changed files>
```

**Type mapping from area:**
| Area changed          | Type    |
|-----------------------|---------|
| AI/Granite            | `feat`  |
| Space-Track API       | `feat`  |
| N2YO API              | `feat`  |
| Space Weather         | `feat`  |
| Orbital Mechanics     | `feat`  |
| MCP Tools             | `feat`  |
| Node.js Gateway       | `feat`  |
| Frontend UI           | `feat`  |
| CI/CD                 | `ci`    |
| Documentation         | `docs`  |
| Bug fix (from commits)| `fix`   |
| Bob Workflows         | `chore` |

**Scope** = the primary changed area in lowercase (e.g. `ai`, `spacetrack`, `gateway`, `ci`).

Present the drafted commit message to the developer and ask:
> "Does this commit message look right? Reply 'yes' to commit, or tell me what to change."

---

## Step 4 — Stage and commit

Once the developer approves the message:

```
execute_command: git add -A
execute_command: git commit -m "<approved message>"
```

Show the commit hash from the output.

---

## Step 5 — Push and trigger CI/CD

```
execute_command: git push origin <branch>
```

After a successful push, tell the developer exactly what will happen next based on the branch:

**If branch is `feat/*`:**
> "✅ Pushed to `{branch}`. GitHub Actions will now:
> 1. Run Python syntax + import checks (parallel with Node.js checks)
> 2. Run Node.js syntax check
> 3. Start the full gateway and smoke-test `/api/iss`, `/api/space-weather`,
>    `/api/satellite/25544`, `/api/astronauts`
> 4. If all pass → trigger a Render staging deploy
>
> Watch the pipeline: https://github.com/{org}/{repo}/actions"

**If branch is `main`:**
> "✅ Pushed to `main`. GitHub Actions will run the same validation, then deploy
> to **production** on Render. The production service will be live within ~3 minutes
> of the pipeline completing."

**If branch has no deploy rule:**
> "✅ Pushed. No deploy will be triggered for this branch (only `feat/*` → staging,
> `main` → production). To deploy, either rename the branch to `feat/...` or
> open a PR to `main`."

---

## Step 6 — Confirm the Actions run (if GitHub CLI is available)

Check if `gh` (GitHub CLI) is installed:
```
execute_command: gh --version
```

If available, wait 5 seconds then fetch the latest run status:
```
execute_command: gh run list --limit 1 --json status,conclusion,headBranch,url
```

Parse the JSON and report:
- If `status` is `in_progress` → "Pipeline is running — check back in ~2 minutes."
- If `status` is `completed` and `conclusion` is `success` → "✅ All checks passed. Deploy triggered."
- If `status` is `completed` and `conclusion` is `failure` → "❌ Pipeline failed. Run `gh run view` for details."

If `gh` is not installed, show the direct GitHub Actions URL instead.

---

## Step 7 — Final summary

Produce a one-paragraph deployment summary covering:
1. What was released (key areas from Step 1)
2. Any risk flags the developer should monitor post-deploy
3. The staging/production URL (if Render service name is known from render.yaml)
4. What to watch: health check endpoint `/api/iss` is the fastest live signal

---

## Failure recovery guide

| Symptom | Cause | Fix |
|---------|-------|-----|
| `NameError: httpx` on startup | Missing import | Verify `import httpx` is in `server.py` line 23 |
| Python MCP server won't start | Wrong Python path | Set `PYTHON_COMMAND=python3` in env |
| Render build fails on pip install | No venv on Render | `render.yaml` uses `--target mcp-server/venv-packages/`; `mcpClient.js` sets `PYTHONPATH` |
| `/api/iss` returns 502 | Python subprocess crashed | Check Render logs: `mcp-server/server.py` startup errors |
| Deploy stuck "in_progress" > 5 min | Skyfield downloading ephemeris on first run | Normal on cold start; subsequent deploys are faster |
| GitHub Actions `smoke-test` fails | Celestrak 503 | `get_constellation_snapshot` now returns graceful error — check logs |
