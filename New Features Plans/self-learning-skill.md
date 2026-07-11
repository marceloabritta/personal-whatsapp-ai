# Self-Learning Skill — Implementation Plan

## Goal

When the secretary **fails a task**, it should automatically capture what happened
and turn it into actionable engineering work — with no manual effort from the owner.

A failure is either:

- a **hard failure** — a skill/router/continuation `throw` caught in `server.js`
  (the user gets one of the `*Error` "Error in the log." messages), or
- a **soft failure** — the secretary sends a message like *"I was not able to do
  this"* / *"não consegui"* **without throwing**.

On any such failure the system:

1. **Captures** the error + recent logs + the chat transcript into a structured
   Markdown report written to `secretary/improvements/` (in production).
2. **Syncs** those reports off the droplet to the top-level `Improvements/` folder
   in the git repo (pull-based, from the Mac).
3. **Plans** — a scheduled coding agent with full codebase access reads each report,
   finds the root cause, and writes an implementation plan, then commits it for the
   owner to review.

## Why it fits the architecture

- **Chat transcript** is already on `ctx.transcript` (built every webhook, `server.js:264`)
  — free to include.
- **All failure paths already funnel through `server.js`** — three `catch` blocks
  (`continuationError` `:324`, `routerError` `:340`, `skillError` `:360`) plus the
  outer webhook catch `:365`. One capture hook covers every hard failure.
- **The skill contract** (`manifest` + `run` + optional `capabilities`) lets us ship
  this as a normal skill folder that the orchestrator calls **programmatically** via
  the existing `ctx.callSkill` capability registry — no special-casing in the router.
- **Report generation** mirrors the proven `feature_request` pattern (generate
  Markdown, write/deliver it).

## The two hard constraints this design is built around

1. **Container mount boundary.** The running container only mounts the app dir
   (`/opt/secretary:/app`). Folders *outside* `secretary/` (like the top-level
   `Improvements/`) are **not writable at runtime**. → Reports are first written
   *inside* `secretary/improvements/` (mounted), then relocated to the top-level
   `Improvements/` by the sync step.
2. **The droplet's GitHub deploy key is READ-ONLY** (`PROJECT_LOG.md:57`). The
   droplet **cannot `git push`**. → The sync must be **pull-based from the Mac**
   over the existing `secretaria-droplet` SSH alias (`rsync`/`scp`). The Mac has
   push access; the droplet never touches the remote for this feature.

---

## Pipeline overview

```
 PRODUCTION (droplet container)          MAC (dev-side, scheduled)
 ────────────────────────────           ─────────────────────────────
 task fails (throw or soft msg)
        │
        ▼
 self_learning.captureFailure(ctx,…)
   • error + stack
   • recent logs (ring buffer)
   • chat transcript (ctx.transcript)
   • optional Claude root-cause note
        │
        ▼
 write  secretary/improvements/<ts>-<task>.md   ──rsync over SSH──►  Improvements/inbox/*.md
                                                                          │
                                                                          ▼
                                                          coding agent (Claude Code):
                                                          read report + codebase →
                                                          write Improvements/<date>-<slug>.md
                                                          (implementation plan) → git commit
                                                                          │
                                                                          ▼
                                                            owner reviews & pushes/deploys
```

---

## Component A — Log ring buffer  (new)

**File:** `secretary/1. Orchestrator/lib/logbuffer.js`

There is no log file today — everything is `console.log`/`console.error` to stdout
(read via `docker logs`). Nothing can read its own logs in-process. We add a tiny
in-memory ring buffer so `captureFailure` can attach "the last N log lines".

- `installLogBuffer({ capacity = 500 })` — called **once at the very top of
  `server.js`**, before any other logging. Wraps `console.log`/`console.error` so
  they (a) still print to stdout unchanged (docker logs keep working) and (b) push
  `{ t, level, text }` into a fixed-size ring.
- `getRecentLogs(n = 80)` — returns the last `n` lines, newest last, as a string.
- **Redaction:** before storing, scrub obvious secrets with a small regex set
  (`apikey`, `authorization`, `token`, `Bearer …`, long base64 blobs) → `«redacted»`.
  Logs can contain API keys; reports will live in git.

## Component B — The `self_learning` skill  (new)

**Folder:** `secretary/2. Skills/5. Self Learning/` → `skill.js`, `prompt.js`, `SKILL.md`

`skill.js` exports:

- `manifest = { id: "self_learning", description: "Diagnose and document a failed
  task: capture the error, logs and chat into an improvement report." }`
- `run(ctx)` — **router-facing** entry, so the owner can force it:
  `@secretary what went wrong? / learn from that` → captures the most recent failure
  context for this chat and replies with a short confirmation ("Logged it — report
  written").
- `capabilities.captureFailure(ctx, info)` — the **programmatic** entry the
  orchestrator calls from its catch blocks. `info = { phase, taskId, error,
  softMessage }`.

**`captureFailure(ctx, info)` does:**

1. **Guard against loops & noise.** No-op if `ctx._captured` is already set this turn
   (so a hard-failure capture + the soft-failure scan of its own error message don't
   both fire). Set `ctx._captured = true`.
2. **Rate-limit duplicates.** Hash `(taskId + error?.message)`; skip if the same hash
   was captured in the last ~10 min (in-memory `Map` with TTL). Prevents a repeating
   error from spamming 100 reports.
3. **Gather context:** ISO timestamp, `remoteJid` + `contact`, `ctx.order` (what the
   user asked), `phase`/`taskId`, `error.message` + `error.stack` **or** the
   `softMessage`, `getRecentLogs(80)`, and `ctx.transcript`.
4. **Optional auto-analysis (best-effort).** One cheap `ctx.anthropic` call
   (`TRANSLATE_MODEL`-tier) producing a 3–5 line "likely cause + suspected file/area"
   note. Wrapped in try/catch — a failure here is silently skipped; the report ships
   without it.
5. **Render** the report from a fixed Markdown template (below).
6. **Write** to `secretary/improvements/<YYYY-MM-DDTHH-MM-SS>-<taskId|slug>.md`
   (`fs.mkdir(recursive)` then `fs.writeFile`). This dir is under the app root → inside
   the container mount → writable.
7. **Never throws.** The entire body is wrapped; capture must never break the user
   flow or mask the original error. On any internal failure it does a single
   `console.error("self_learning capture failed:", …)` and returns.

`prompt.js` holds the report template, the optional analysis prompt, and en/pt strings
for `run()`'s confirmation reply.

### Report template

```markdown
# Failure report — <taskId or "soft-failure">  (<ISO timestamp>)

| Field        | Value                                  |
|--------------|----------------------------------------|
| When         | <ISO, America/Sao_Paulo>               |
| Chat         | <remoteJid>  (<contact>)               |
| Trigger      | hard-throw | soft-message | manual     |
| Failed task  | <taskId / phase>                       |
| Status       | needs-plan                             |

## What the user asked
<ctx.order>

## What happened
<error.message>
```
<error.stack  — or the soft message text>
```

## Auto-analysis (best-effort, unverified)
<Claude note, or "n/a">

## Recent logs
```
<last 80 log lines, redacted>
```

## Conversation transcript
```
<ctx.transcript>
```
```

## Component C — Wire capture into the orchestrator

**File:** `secretary/1. Orchestrator/server.js`

- Top of file: `import { installLogBuffer } from "./lib/logbuffer.js";` and call
  `installLogBuffer()` **before** any other statement that logs.
- Add a safe helper near `orch()`:
  ```js
  async function fireCapture(ctx, info) {
    try {
      if (ctx?.hasSkill?.("self_learning", "captureFailure"))
        await ctx.callSkill("self_learning", "captureFailure", info);
    } catch (e) { console.error("fireCapture failed:", e?.message || e); }
  }
  ```
- In each of the three catch blocks, **after** the existing `console.error` + user
  reply, add:
  - continuation (`:324`): `await fireCapture(ctx, { phase: "continuation", taskId: session.skill, error: e });`
  - router (`:340`): `await fireCapture(ctx, { phase: "router", taskId: "router", error: e });`
  - skill (`:360`): `await fireCapture(ctx, { phase: "skill", taskId: task, error: e });`
- The outer webhook catch (`:365`) has no `ctx` in scope for the early-return cases;
  leave it as-is (those are pre-context failures with nothing useful to capture).

### Soft-failure detection

The "Automatic **+ soft failures**" requirement: catch messages the secretary sends
that *report* a failure without throwing (e.g. a skill's own "I couldn't do that").

- Add a conservative multilingual pattern set in `logbuffer.js`/skill `prompt.js`:
  `/\b(couldn'?t|could not|not able to|failed to|unable to|wasn'?t able)\b/i` and
  pt `/\b(não consegui|não foi possível|não deu (pra|para)|falhei)\b/i`.
- Wrap the `send()` used by `ctx.send` so that after dispatch, if the outgoing **body**
  matches a soft-fail pattern **and** `ctx._captured` is not set, it fires
  `fireCapture(ctx, { phase: "soft", taskId: "soft", softMessage: body })`.
- Debounced by the same `ctx._captured` per-turn flag + the 10-min dedupe, so a chat
  full of the word "couldn't" doesn't spam reports. Owner reviews everything anyway.
- **Flagged as heuristic** — conservative patterns, false positives are cheap (a
  spurious report), false negatives just mean that one failure isn't auto-captured.

## Component D — The improvements spool  (new dir + gitignore)

- Create `secretary/improvements/.gitkeep` (tracked, so the dir exists on the droplet
  after `git pull`).
- Add to `.gitignore`:
  ```
  # Self-learning: runtime failure reports (spool; synced to top-level Improvements/)
  secretary/improvements/*.md
  ```
  Keeping the runtime `*.md` untracked means `git pull --ff-only` on the droplet is
  never blocked by report churn, and private chat content isn't committed on the
  prod checkout. The **top-level `Improvements/`** (on the Mac) *is* tracked.

## Component E — Sync + Plan pipeline  (dev-side, scheduled)

**Sync script:** `scripts/self-learning-pull.sh` (runs on the Mac)

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="/path/to/Personal Whatsapp AI"        # resolved at build time
mkdir -p "$REPO/Improvements/inbox"
# 1. pull new reports off the droplet
rsync -az --ignore-existing \
  secretaria-droplet:/opt/secretary/improvements/*.md \
  "$REPO/Improvements/inbox/" 2>/dev/null || true
# 2. archive them on the droplet so they aren't pulled again
ssh secretaria-droplet 'cd /opt/secretary/improvements && mkdir -p _synced && \
  mv *.md _synced/ 2>/dev/null || true'
```

**Planning agent:** a scheduled Claude Code run (local launchd job, daily — chosen
because the cloud `/schedule` routines can't SSH the droplet, but the SSH+rsync step
must run from the Mac). After the pull it invokes `claude -p` with a fixed prompt:

> For each report in `Improvements/inbox/`, read it, investigate the codebase to find
> the root cause, and write an implementation plan to
> `Improvements/<YYYY-MM-DD>-<slug>.md` in the same style as `New Features Plans/*.md`.
> Then move the raw report to `Improvements/_reports/`. Commit each with a descriptive
> message. **Do not push or deploy** — leave that to the owner.

Final top-level layout:

```
Improvements/
  inbox/            # raw reports freshly pulled from the droplet
  _reports/         # raw reports after a plan was written (archive)
  <date>-<slug>.md  # implementation plans (coding-agent output, owner reviews)
scripts/self-learning-pull.sh
```

---

## Build order

1. `lib/logbuffer.js` (+ redaction) and install at boot.
2. `self_learning` skill: `captureFailure` (dedupe + gather + template + write to
   spool), `run()`, `prompt.js`, `SKILL.md`.
3. Wire `fireCapture` into the three catch blocks in `server.js`.
4. Soft-failure `send()` scan + `ctx._captured` debounce.
5. `secretary/improvements/.gitkeep` + `.gitignore` entry.
6. `scripts/self-learning-pull.sh` + the scheduled planning agent + docs
   (`ARCHITECTURE.md`, `PROJECT_LOG.md`).
7. Deploy (gated ask) + verify.

## Verification

- **Local:** temporarily force a skill to throw; confirm a report appears in
  `secretary/improvements/`, containing the error, redacted logs, and transcript.
- **Soft path:** make a skill send "I couldn't do that" without throwing; confirm a
  soft report is written and that the same turn doesn't double-capture.
- **Dedupe:** trigger the same error twice quickly; confirm only one report.
- **Sync:** run `scripts/self-learning-pull.sh`; confirm the report lands in
  `Improvements/inbox/` and is archived to `_synced/` on the droplet.
- **Plan:** run the planning agent on the inbox; confirm a plan `.md` is produced and
  the raw report moves to `_reports/`.

## Risks & notes

- **Secrets in logs** → regex redaction in the ring buffer before storage.
- **Private chat content** → reports stay in the private repo; never sent to any
  external service. The optional Claude analysis call sends transcript context to the
  Anthropic API (same trust boundary the app already uses for every skill).
- **Soft-failure false positives** → conservative patterns + per-turn debounce +
  10-min dedupe; owner reviews all reports.
- **Capture must never break the user** → `captureFailure` is fully guarded and
  runs *after* the user already got their error reply.
- **Read-only droplet key** → pull-based sync from the Mac (built into the design).
```

---
*Self-learning skill — plan drafted for review before implementation.*
