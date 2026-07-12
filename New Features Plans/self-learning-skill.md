# Self-Learning — Implementation Plan

## Goal

When the secretary **fails a task** — or when the **owner tells it that it got something
wrong** — it should automatically capture what happened and turn it into actionable
engineering work, with no manual effort from the owner.

The system:

1. **Captures** the failure (error + recent logs + chat transcript) into a structured
   Markdown report written to `secretary/improvements/` in production. Two sources feed
   capture:
   - **Machine-detected** — a thrown error, an unrouted order, or a soft "I couldn't…" reply.
   - **Owner-reported** — `@secretary you made a mistake here`, on the spot, in the chat
     (Component F). Failures the code can't see itself — a wrong answer, a false positive,
     a right-but-annoying behaviour — only ever enter the loop this way.
2. **Syncs** those reports off the droplet to the top-level `Improvements/` folder in the
   git repo (pull-based, run from the Mac).
3. **Triages** — a coding agent with full codebase access reads each report, finds the
   root cause, and writes an implementation plan for the owner to review.

## Capture is a lib; the owner-facing note is a (thin) skill

The first draft of this plan built the whole thing as `2. Skills/5. Self Learning/`. That's
wrong. Every loaded skill is auto-appended to `CATALOG` (`server.js:99`), which *is* the
router's menu. A skill the router must never pick is a misroute hazard with no upside — and
we decided against a router-facing `@secretary what went wrong?` entry.

So **failure capture is orchestrator infrastructure**: it lives in `lib/` next to
`sessions.js` and `evolution.js`, and `server.js` imports it directly.

The **one** thing that *is* router-facing is the owner's note — `@secretary you made a
mistake here`. That's an order, typed by the owner, arriving through the tag: the router is
the only thing that can catch it. It gets a deliberately thin skill (`2. Skills/5. Feedback/`)
whose entire job is to gather evidence and call the same `captureFailure` lib. This is the
"thin skill folder wraps the lib" escape hatch, cashed in on day one — not a second system.

## The two hard constraints this design is built around

1. **Container mount boundary.** The container only mounts the app dir
   (`/opt/secretary:/app`). Nothing outside `secretary/` is writable at runtime. → Reports
   are written *inside* `secretary/improvements/`, then relocated by the sync step.
2. **The droplet's GitHub deploy key is READ-ONLY** (`PROJECT_LOG.md:59`). The droplet
   **cannot `git push`**. → Sync is **pull-based from the Mac** over the existing
   `secretaria-droplet` SSH alias (user is `root`, so the droplet-side archive step has
   permission to move container-written files).

> **Consequence of the symlink.** `/opt/secretary` is a symlink to
> `/opt/personal-whatsapp-ai/secretary`, so runtime reports land **inside the production
> git working tree**. The `.gitignore` entry in Component E is therefore load-bearing, not
> cosmetic, and **must be committed and pulled before the first report is ever written**.

---

## Pipeline overview

```
 PRODUCTION (droplet container)          MAC (dev-side, on demand)
 ────────────────────────────           ─────────────────────────────
 task fails                 │   owner says "you made a mistake here"
   (throw / unrouted / soft)│      (router → feedback skill)
        └─────────┬─────────┘
                  ▼
 captureFailure(ctx, info)
   • error + stack  |  soft message  |  unrouted order  |  OWNER'S NOTE + quoted message
   • recent logs (ring buffer, redacted)
   • chat transcript (ctx.transcript)
   • optional Claude root-cause note
        │
        ▼
 write  secretary/improvements/<ts>-<phase>.md  ──rsync over SSH──►  Improvements/inbox/*.md
                                                                          │
                                                                          ▼
                                                             /triage-failures (slash command):
                                                             read report + investigate codebase →
                                                             write Improvements/<date>-<slug>.md →
                                                             move raw report to _reports/ → commit
                                                                          │
                                                                          ▼
                                                            owner reviews & pushes/deploys
```

---

## Component A — Log ring buffer  (new)

**File:** `secretary/1. Orchestrator/lib/logbuffer.js`

There is no log file today — everything is `console.log`/`console.error` to stdout (read
via `docker logs`). Nothing can read its own logs in-process. A tiny in-memory ring buffer
lets `captureFailure` attach "the last N log lines".

- `installLogBuffer({ capacity = 500, maxLineChars = 2000 })` — called **once at the top of
  `server.js`'s body**. Wraps `console.log`/`console.error` so they (a) still print to
  stdout unchanged (`docker logs` keeps working) and (b) push `{ t, level, text }` into a
  fixed-size ring. Safe to call after the imports: no imported module logs at module
  scope, they only log inside functions.
- `getRecentLogs(n = 80)` — the last `n` entries, newest last, as a string.
- **Truncate long entries** to `maxLineChars`. `server.js:269` logs the entire transcript on
  every webhook (`TRANSCRIPT>>>…`); untruncated it would dominate every report.
- **Redaction before storage** — logs can contain secrets and reports live in git:
  - `sk-ant-[A-Za-z0-9_-]{10,}` (Anthropic keys)
  - `AIza[0-9A-Za-z_-]{30,}` (Google API keys)
  - `1//[A-Za-z0-9_-]{20,}` (Google refresh tokens)
  - `Bearer\s+\S+`
  - `(api[-_]?key|apikey|authorization|token|secret|password)\s*[:=]\s*\S+` (case-insensitive)
  - `\b[A-Za-z0-9_\-]{60,}\b` → `«redacted:blob»` (long high-entropy strings / base64)

  Redaction is a **defence-in-depth** measure, not a guarantee. The repo is private and
  reports are reviewed by the owner before anything is acted on.

## Component B — The capture lib  (new)

**File:** `secretary/1. Orchestrator/lib/selflearning.js`

Exports `captureFailure(ctx, info)` where
`info = { phase, taskId, error?, softMessage?, unroutedOrder?, report? }` and `phase` is one of
`throw:continuation` | `throw:router` | `throw:skill` | `soft` | `unrouted` | **`reported`**.

`report` is only set by the `reported` phase — it carries the owner's note and the offending
message (Component F).

**It does:**

1. **Per-turn guard.** No-op if `ctx._turn.captured` is set; otherwise set it. One report per
   webhook turn, max.

   > **This is the bug in the first draft.** The original plan used a plain `ctx._captured`
   > field. `ctx.callSkill` does `fn({ ...ctx, _skillDepth: depth })` (`server.js:313`) — a
   > **spread**, so a flag set by a callee mutates a copy and never reaches the caller. The
   > flag must live on a **shared mutable object** (`ctx._turn = {}`), whose *reference* the
   > spread copies, so mutations are visible to every frame in the turn.

2. **Dedupe.** Hash `(phase + taskId + first line of error.message)`; skip if the same hash
   was captured in the last 10 min (module-level `Map` with TTL). Plus a **global cap of
   ~20 reports/hour** so a crash loop can't fill the droplet's disk.

   > **`reported` is exempt from both.** The dedupe window and the hourly cap exist to
   > survive a *crash loop* — a machine emitting the same failure hundreds of times a
   > minute. An owner-reported note is a human typing on a phone: it can't loop, it's never
   > redundant (two notes 30 seconds apart are two distinct complaints), and dropping one
   > silently is the worst failure this feature has — the owner would believe it was filed.
   > Guard it with its own generous cap (~10/hour) purely as a disk backstop, and **tell the
   > owner** if that cap ever rejects a note instead of confirming a lie.

3. **Gather:** ISO timestamp (America/Sao_Paulo), `remoteJid` + `contact`, `ctx.order`,
   `phase`/`taskId`, `error.message` + `error.stack` **or** the soft message **or** the
   unrouted order **or** the owner's report, plus `getRecentLogs(n)` and `ctx.transcript`.
   - `n = 80` normally — the failure just happened, it's the tail of the log.
   - **`n = 250` for `reported`** — the mistake happened in an *earlier webhook turn*, so the
     evidence is further back in the ring (the current turn's own router/skill logging is on
     top of it). The ring holds 500 entries, so this stays inside capacity.
4. **Optional auto-analysis (best-effort).** One cheap `ctx.anthropic` call producing a 3–5
   line "likely cause + suspected file/area" note. In its own try/catch — on any failure the
   report ships without it.

   > **The model id is not on `ctx`.** `ctx.model` is the *main* model (`MODEL`,
   > `claude-sonnet-5`); `TRANSLATE_MODEL` is a module-local const in `server.js:45–46` and is
   > never passed down. `selflearning.js` is a lib, so it reads it the same way `server.js`
   > does — `process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001"` — rather than
   > reaching for a `ctx` field that doesn't exist. (`ctx.env` is `process.env` too, if you
   > prefer going through the context.)
5. **Render** the Markdown report from a fixed template (below).
6. **Write** to `secretary/improvements/<YYYY-MM-DDTHH-MM-SS>-<phase>-<taskId>.md`
   (`fs.mkdir(recursive)` then `fs.writeFile`). Under the app root → inside the container
   mount → writable.
7. **Never throws.** The whole body is wrapped. Capture must never break the user flow or
   mask the original error. On any internal failure: one `console.error` and return.

### Report template

```markdown
# Failure report — <phase> / <taskId>  (<ISO timestamp>)

| Field       | Value                                   |
|-------------|-----------------------------------------|
| When        | <ISO, America/Sao_Paulo>                |
| Chat        | <remoteJid>  (<contact>)                |
| Trigger     | throw:skill | soft | unrouted | reported |
| Source      | machine-detected  |  OWNER-REPORTED     |
| Failed task | <taskId / phase>                        |
| Status      | needs-plan                              |

## What the user asked
<ctx.order — on `reported`, this is the NOTE, not the original order>

## What happened
<error.message  |  the soft message  |  "router matched no skill">

​```
<error.stack, or n/a>
​```

## Owner's report      ← `reported` phase only; omitted otherwise
**What the owner says went wrong:** <what_went_wrong>
**What they expected instead:** <expected, or "not stated">

### The offending message (quoted)
​```
<quoted.text — the secretary's own output the owner replied to, or "not quoted">
​```

## Auto-analysis (best-effort, unverified)
<Claude note, or "n/a">

## Recent logs
​```
<last 80 log entries, redacted + truncated>
​```

## Conversation transcript
​```
<ctx.transcript>
​```
```

## Component C — Wire capture into the orchestrator

**File:** `secretary/1. Orchestrator/server.js`

> **Line numbers below are verified against the current `server.js` (375 lines).** They moved
> 2–3 lines since the first draft; anchor on the quoted code, not the number, if it drifts again.

- Top of the file body: `installLogBuffer()`, before anything else that logs — i.e. above the
  boot block at `:191` (`await loadSkills()`), whose `skill loaded: …` lines are then captured too.
- When building `ctx` (`:272`), add the shared per-turn object: `_turn: { captured: false }`.
  Comment it — the spread in `callSkill` is exactly why it's an object and not a boolean.
- Add a guarded helper so a broken capture can never surface to the user:
  ```js
  async function fireCapture(ctx, info) {
    try { await captureFailure(ctx, info); }
    catch (e) { console.error("fireCapture failed:", e?.message || e); }
  }
  ```
- **Three hard throws** — after the existing `console.error` + user reply, so the owner
  always gets their error message first:
  - continuation catch (`:326–329`) → `{ phase: "throw:continuation", taskId: session.skill, error: e }`
  - router catch (`:342–345`) → `{ phase: "throw:router", taskId: "router", error: e }`
  - skill catch (`:362–365`) → `{ phase: "throw:skill", taskId: task, error: e }`
- **Unrouted** (`:350–354`, the `notUnderstood` branch) → `{ phase: "unrouted", taskId: "router",
  unroutedOrder: ctx.order }`. The router ran fine and understood nothing: not a bug, a
  **missing capability** — the highest-signal *machine* report of the four.
- The outer webhook catch (`:367–369`) has no `ctx` in scope for the early-return cases. Leave
  it: those are pre-context failures with nothing useful to capture.

### Soft failures — DECLARED, never inferred  *(revised during the build, 2026-07-12)*

> **The regex design in the first draft was wrong, and the build proved it.** It was wrong in
> *both* directions at once. It **missed half the failure copy already in the repo** —
> `thinkingError` ("I hit an error while thinking") appears in three skills and contains no
> failure word; so do Tasks' "Something went wrong", `transcriptionFailed`, `noAction`. And
> when the patterns were widened to catch those, they began **firing on guidance**: "I couldn't
> find: buy milk. *Which one did you mean?*" is a clarifying **question**, not a defect.
>
> Prose cannot be classified by keyword. Only the skill knows whether it just failed the owner
> or just asked him something.

**The rule.** A malfunction is exactly three things: (1) a **code error**, (2) a **soft landing
of an uncompleted task**, (3) the **owner reporting a mistake**. Everything else is **guidance**
— and guidance is the secretary *working*.

- **`ctx.sendFailure(number, text)`** (`server.js`) — sends exactly like `ctx.send` and
  **always** files a report (`phase: "soft"`, `taskId: ctx._turn.skill`). A skill uses it for
  every reply meaning *"you asked me to do something and I didn't do it"*. **29 call sites**:
  Calendar 14, Tasks 7, Feature Requests 5, Audio 3.
- **`ctx.send` is never scanned.** No regex, no sniffing, at runtime. Guidance files nothing.
- **Includes partial failures:** Tasks' "Couldn't do these:" after a batch half-applied. The
  message reads like a success; the to-dos that didn't happen are still failures.
- **Includes "I didn't understand":** the `unrouted` branch and the skills' `noAction`. It
  reads like guidance, but the owner asked and got nothing — and it's the clearest signal of a
  **missing capability**. *(Owner's call, 2026-07-12: keep filing these.)*
- **The guard against forgetting is a LINT, not a guess.** `scripts/selflearning-selftest.mjs`
  reads the skill sources and fails the run if a reply key named `*Error`/`*Failed`/`*NoMatch`/
  `noAction` is sent with plain `send()`, naming file and line. Verified by reverting a call
  site: it failed with `1. Calendar Actions/skill.js:473 — 'createGoogleError' must use
  ctx.sendFailure`. One exemption, commented: `feedback.logFailed` (capture itself failing).
- **`ORCH_MSG` replies use the bare `send()`**, not `ctx.send` — they're already covered by the
  catch block or the `unrouted` branch that produced them.
- Caption text sent via `evolution.sendMedia` bypasses `ctx.send` entirely — accepted.

## Component D — Owner-reported mistakes: the `feedback` skill  (new)

**Files:** `secretary/2. Skills/5. Feedback/{skill.js, prompt.js, SKILL.md}`

The other three triggers only fire when the code *knows* it failed. The failures that matter
most are invisible to it: a **false positive**, a wrong answer delivered confidently, a task
filed under the wrong date, an event created for the wrong person. Nothing throws. Nothing
says "I couldn't". The only detector is the owner reading the message and thinking *that's
wrong*.

This skill is that detector's front door — and it's the **highest-signal report in the
system**, because unlike `soft` (a regex guess) or `unrouted` (a routing miss) it's a
**human-verified** defect.

**Shape:** the thinnest possible skill. It gathers evidence, calls `captureFailure(ctx, {
phase: "reported", … })`, confirms in one line. No session, no slot-filling, no doc render.

### How the owner files one

Two forms, and the difference matters a lot for report quality:

1. **Reply to the offending message** + `@secretary you made a mistake here` → `ctx.quoted.text`
   carries **the secretary's own wrong output, verbatim**. This is the gold path; the report
   is unambiguous.
2. **Bare note** — `@secretary you got the timezone wrong on that last event`. No quote; the
   evidence has to come from `ctx.transcript` and the logs.

Encourage form 1 in the SKILL.md, but never *require* it — a feature the owner has to
remember the ritual for is a feature that doesn't get used. Form 2 must work.

> **Evidence check.** `isOwnMessage(quoted.text)` (`lib/identity.js`, already used at
> `server.js:220` to spot the secretary's own messages) confirms the quoted message really is
> secretary output. When true, the report says so — the triage agent then knows the quoted
> block is the *bug*, not just context.

### Contract & flow

- `manifest = { id: "feedback", description }`, `run(ctx)` — discovered at boot like every
  other skill. No `capabilities` export.
- **One structured call** (`FEEDBACK_SCHEMA`, via `jsonFormat`/`readReply` from `lib/llm.js`)
  over `ctx.order` + `ctx.quoted?.text` + `ctx.transcript`, returning:
  ```js
  { title, what_went_wrong, expected, suspected_skill, enough_context }  // all English
  ```
  - `title` — a short slug-able summary, for the report filename and the triage list.
  - `what_went_wrong` / `expected` — the owner's claim, restated plainly. **Not** the model's
    theory of the bug; that's what the (separate, best-effort) auto-analysis is for. Keeping
    owner-truth and machine-guess in different sections of the report is the whole point —
    the triage agent must be able to tell them apart.
  - `suspected_skill` — one of the `CATALOG` ids (available on `ctx.catalog`) or `null`. Fills
    `taskId` in the report, so reports about the calendar cluster together.

    > **Nullable enum → `anyOf`, not a type-union.** The structured-output validator **rejects**
    > `{ type: ["string","null"], enum: [...] }`. Use the pattern the calendar skill already
    > documents at `1. Calendar Actions/prompt.js:48–52`:
    > ```js
    > suspected_skill: {
    >   anyOf: [{ type: "null" },
    >           { type: "string", enum: catalog.map((c) => c.id) }],  // built at call time
    > },
    > ```
    > The enum is **derived from `ctx.catalog`**, so a newly added skill is a valid answer the
    > day it ships — no hard-coded id list to rot. Everything else follows house rules:
    > `additionalProperties: false` + a complete `required` list on every object.

  - `enough_context` — see below.
- **Always writes the report.** The LLM call is best-effort: if it throws or returns null, the
  skill still fires `captureFailure` with the raw note and the transcript. A report the triage
  agent has to work harder on beats a complaint that silently evaporated.
- **Confirm in one line** via `ctx.send` (localized `reply(lang)`, en + pt), e.g.
  *"Noted — I logged that as a mistake to investigate: «title». It'll go into the next
  improvement pass."* Never claim it's fixed, only that it's **filed**.

### The one clarifying question — **write first, then enrich**

If the note is bare *and* nothing was quoted (`enough_context: false` — "you made a mistake"
and no more), the report would be thin. In that one case the skill asks a single question —
*"Which message was wrong, and what should it have said?"* — and opens a **short session**
(`skill: "feedback"`, `stage: "clarifying"`, `awaitFrom: "owner"`, TTL 900).

> **The report is written BEFORE the question is asked — never after.**
>
> The obvious design (ask → wait → capture on the answer) **loses the complaint**, and the
> code says so plainly: `server.js:334` does `if (session) await sessions.clear(remoteJid)`
> on **any** new tagged order. So: owner reports a bug → secretary asks *"which message?"* →
> owner gets distracted and types `@secretary schedule lunch` → **the session is dropped and
> the complaint is gone forever.** He'd never know; he'd just stop reporting after the second
> one that went nowhere. The session also expires on its own after 15 minutes.
>
> So `captureFailure` runs **first**, with whatever is on hand, and `captureFailure` returns
> **the report's path**, which the skill parks in the session. The clarifying answer then
> **appends** an `## Owner's follow-up` section to that same file. If the append fails (file
> gone, disk error), write a fresh linked report instead of dropping the answer.
>
> Net: the answer *improves* a report that already exists. It is never load-bearing for one
> existing at all. Every other branch of this feature is lossy-by-degrees; this one would have
> been lossy-by-silence.

**Bounded to exactly one round-trip:** the next owner message enriches **unconditionally**,
whatever it says, and closes the session. No second ask, ever. An owner already annoyed enough
to report a bug will not tolerate an interview about it — and a thin report that got filed
beats a perfect one that was abandoned halfway.

### Router disambiguation — the real hazard

The router will be tempted to send *"you scheduled that meeting at the wrong time"* to
**`calendar_action`**, because it's about a calendar event. That would be silently fatal to
this feature: the complaint gets executed as a new order instead of filed as a defect.

Three mitigations, all needed:

1. **Sharp manifest wording**, in the `NOT for…` style the other manifests already use:
   > "the owner is telling you that YOU, the secretary, did something WRONG — a mistake, a
   > false positive, a wrong answer, bad behaviour. Use for 'you made a mistake', 'that's
   > wrong', 'you got X wrong', 'that shouldn't have happened'. This is about a defect in the
   > secretary's own past output. NOT for asking to build something new (`feature_request`),
   > NOT for a fresh calendar/task order (`calendar_action`/`task_action`), and NOT for an
   > answer to a question the secretary is currently asking (that's a continuation)."
2. **A router rule** in `router/prompt.js`, next to the existing quoted-calendar-link rule:
   > If the owner is complaining about something the secretary **already did** — past tense,
   > blaming the secretary, "you got it wrong" — route to `feedback`, **even when the subject
   > is a calendar event or a task**. The subject matter is not the intent.
3. **Both, when he wants both.** *"@secretary you got the time wrong — fix it to 5pm"* is
   legitimately two tasks. The router already returns an **array** (`tasks: string[]`,
   `ROUTER_SCHEMA`) and `server.js:357` dispatches them in order — so it can emit
   `["feedback", "calendar_action"]`: **file the defect and fix the event.** This is the
   honest answer to "does reporting a mistake also fix it?" — reporting never fixes anything
   on its own, and the owner asking for a fix is just a second, ordinary order. Say so
   explicitly in the router prompt; it's the difference between a feature that feels smart
   and one that feels pedantic.

### Interactions with the rest of capture

- The confirmation reply goes through `ctx.send`, which Component C wraps with the
  soft-failure scanner — but the phrasing matches no "couldn't/failed to" pattern, **and**
  `_turn.captured` was already set by the capture that just ran. Double-safe. (Worth a
  comment: a future reword of the confirmation into *"I couldn't fix that, but I logged it"*
  would trip the scanner if the flag weren't there.)
- `phase: "reported"` skips dedupe and the hourly cap (Component B).
- The mistake being reported may **itself** have already produced a machine report minutes
  earlier (owner reports a wrong answer that also logged a soft failure). That's **fine and
  intended** — two reports, two angles, same incident. Triage merges them; the timestamps and
  `remoteJid` line them up. Do not try to be clever about correlating them in production.

## Component E — The improvements spool  (new dir + gitignore)

- `secretary/improvements/.gitkeep` — tracked, so the dir exists on the droplet after pull.
- `.gitignore`:
  ```
  # Self-learning: runtime failure reports (spool; synced to top-level Improvements/)
  secretary/improvements/*.md
  secretary/improvements/_synced/
  ```
  Untracked runtime reports keep `git pull --ff-only` on the droplet unblocked and keep
  private chat content off the production checkout. The **top-level `Improvements/`** (on
  the Mac) *is* tracked.
- **Order matters:** commit + deploy this gitignore entry **before** any code that writes
  reports.

## Component F — Sync + triage  (dev-side, on demand)

**Neither directory exists yet** — `scripts/` and `.claude/commands/` are both new (`mkdir -p`),
and `chmod +x` the script.

**Sync script:** `scripts/self-learning-pull.sh` (runs on the Mac)

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$REPO/Improvements/inbox"
# 1. pull new reports off the droplet
rsync -az --ignore-existing \
  secretaria-droplet:/opt/secretary/improvements/*.md \
  "$REPO/Improvements/inbox/" 2>/dev/null || true
# 2. archive them on the droplet so they aren't pulled twice
ssh secretaria-droplet 'cd /opt/secretary/improvements && mkdir -p _synced && \
  mv *.md _synced/ 2>/dev/null || true'
echo "inbox: $(ls -1 "$REPO/Improvements/inbox" | wc -l) report(s)"
```

**Triage:** a `/triage-failures` slash command (`.claude/commands/triage-failures.md`) the
owner runs when they feel like it. It runs the pull script, then:

> For each report in `Improvements/inbox/`, read it, investigate the codebase to find the
> root cause, and write an implementation plan to `Improvements/<YYYY-MM-DD>-<slug>.md` in
> the style of `New Features Plans/*.md`. Then move the raw report to `Improvements/_reports/`.
> Commit each with a descriptive message. **Do not push and do not deploy.**
>
> **Take `Source: OWNER-REPORTED` reports first, and believe them.** A machine report is a
> stack trace — it proves *something broke*. An owner report is a human saying *this output
> was wrong*, and it is the only evidence that exists for a whole class of bug (false
> positives, wrong-but-confident answers) where the code ran perfectly and produced garbage.
> The "Owner's report" section is **ground truth about the symptom** — do not argue with it or
> explain it away; the job is to find the code path that produced it. The "Auto-analysis"
> section, by contrast, is an unverified guess from a cheap model — treat it as a lead, and
> discard it freely.
>
> If several reports describe the same incident from different angles (same chat, timestamps
> within a few minutes — e.g. a `soft` report *and* the owner's note about it), **merge them
> into one plan.**

Deliberately **not** a launchd cron job. The capture layer is the hard, novel part; the
triage agent is a prompt. Scheduling it unattended — with commit access to the repo — is the
riskiest piece of the whole feature and buys nothing until capture is proven in production.
Automate later if the manual loop gets tedious.

Final top-level layout:

```
Improvements/
  inbox/            # raw reports freshly pulled from the droplet
  _reports/         # raw reports after a plan was written (archive)
  <date>-<slug>.md  # implementation plans (owner reviews)
scripts/self-learning-pull.sh
.claude/commands/triage-failures.md
```

---

## Build order

1. **`.gitignore` + `secretary/improvements/.gitkeep`** — commit and deploy **first**, so the
   spool is ignored before anything can write to it.
2. `lib/logbuffer.js` (ring + truncation + redaction), installed at the top of `server.js`.
3. `lib/selflearning.js` — `captureFailure`: `_turn` guard, dedupe + hourly cap, gather,
   optional analysis, template, write. Never throws.
4. Wire `fireCapture` into the three catch blocks **and** the `notUnderstood` branch;
   add `ctx._turn`.
5. Soft-failure scan in the `ctx.send` wrapper.
6. **`2. Skills/5. Feedback/`** — the owner-reported path (skill + prompt + SKILL.md), the
   `reported` phase in `selflearning.js`, and the router-prompt rule. *Ship this **before**
   the sync/triage tooling if anything has to be cut:* the machine triggers only catch
   crashes, and the bugs that actually bother the owner day-to-day are the silent-wrong ones
   that only he can see. This is where the signal is.
7. `scripts/self-learning-pull.sh` + `.claude/commands/triage-failures.md`.
8. Docs: `ARCHITECTURE.md` (new section), `PROJECT_LOG.md`, `ORCHESTRATOR.md`, and the new
   `SKILL.md`.
9. Deploy (**gated — requires an explicit ask**) + verify live.

## Verification

The app can't easily be exercised end-to-end on the Mac (it needs the Evolution webhook), so
verification is split.

**Local, before deploy** — `scripts/selflearning-selftest.mjs`: import `logbuffer.js` and
`selflearning.js` directly, build a fake `ctx` (stub `anthropic`, a canned `transcript`, a
`_turn` object), and assert:
- a hard-throw capture writes a report containing the error, stack, transcript, and logs;
- a log line containing `sk-ant-abc…` and `Bearer xyz` comes back **redacted**;
- the same error twice in a row produces **one** report (dedupe);
- a capture fired through a `{ ...ctx }` spread (simulating `callSkill`) is **suppressed** by
  the shared `_turn` flag — this is the regression test for the bug the first draft had;
- `captureFailure` with a deliberately broken `fs` **does not throw**;
- **two `reported` captures in a row both write a report** — the inverse of the dedupe test,
  pinning the exemption so a later "tidy-up" of the dedupe code can't silently start dropping
  the owner's notes;
- a `reported` capture whose **LLM call throws** still writes a report (carrying the raw note).

**Router regression (local, no droplet needed)** — call `route(ctx)` directly with a stub
`anthropic` swapped for the real one, over a fixture of orders, and assert the chosen tasks:
- `"you made a mistake here"` → `["feedback"]`
- `"you scheduled that at the wrong time"` → `["feedback"]` ← **the misroute this guards**;
  the naive answer is `calendar_action`
- `"you got the time wrong, fix it to 5pm"` → `["feedback", "calendar_action"]`
- `"I want a feature that snoozes tasks"` → `["feature_request"]` (unchanged — check the new
  skill didn't steal it)
- `"schedule lunch with Ana tomorrow"` → `["calendar_action"]` (unchanged)

This is worth real effort. Every mitigation in Component D's "router disambiguation" section
is a *prompt*, and prompts regress silently — a wrong route here doesn't error, it quietly
executes the owner's complaint as a fresh command.

**Live, after deploy** —
- send an order that routes nowhere → confirm an `unrouted` report on the droplet;
- reply `@secretary transcribe` to a non-audio message → confirm the `soft` path fires once;
- **reply to a real secretary message** with `@secretary you made a mistake here` → confirm a
  `reported` report exists, that it contains the **quoted message text** and the note, and
  that the chat got a one-line "logged it" confirmation;
- **bare note, nothing quoted** (`@secretary you got that wrong`) → confirm the single
  clarifying question fires, and that the **next** message captures **whatever** it says (no
  second question);
- **`@secretary you got the time wrong — fix it to 5pm`** on a real event → confirm **both** a
  `reported` report **and** the event actually moving;
- run `scripts/self-learning-pull.sh` → reports land in `Improvements/inbox/`, and are moved
  to `_synced/` on the droplet;
- run `/triage-failures` → a plan `.md` is produced and the raw report moves to `_reports/`.

## Risks & notes

- **Secrets in logs** → regex redaction in the ring buffer before storage. Defence in depth,
  not a guarantee; repo is private, owner reviews every report.
- **Private chat content** → reports stay in the private repo and are never sent anywhere
  external. The optional auto-analysis call sends transcript context to the Anthropic API —
  the same trust boundary every skill already uses.
- **Disk fill from a crash loop** → 10-min dedupe + ~20 reports/hour global cap.
- **Soft-failure false positives** → conservative patterns + per-turn flag + dedupe.
- **Capture must never break the user** → `captureFailure` is fully guarded and runs *after*
  the user has already received their error reply.
- **Read-only droplet key** → pull-based sync from the Mac, by design.
- **`ctx` spread in `callSkill`** → the per-turn flag is an object, not a boolean. Covered by
  a self-test so a future refactor can't silently reintroduce the bug.
- **A complaint gets executed as a command** (*"you scheduled that at the wrong time"* →
  `calendar_action` re-schedules something) → the sharpest risk the `feedback` skill adds, and
  the only one that's actively harmful rather than merely lossy. Three mitigations in
  Component D + a router regression fixture that pins exactly this case.
- **The owner's note is silently dropped** (dedupe, hourly cap, a swallowed error, **or a
  cleared session**) → worse than never building the feature: he'd stop reporting after the
  second note that went nowhere. `reported` is exempt from dedupe and the normal cap, always
  writes a report even when its LLM call fails, **writes before it asks its clarifying
  question** (`server.js:334` would otherwise bin the session on the next tagged order — see
  Component D), and **only** confirms after the file is on disk. If the disk backstop ever
  does reject a note, say so in the chat.
- **Reporting becomes a chore** → one message, no ritual, at most one follow-up question,
  ever. The quoted-reply form is *encouraged*, never required.

---

## Pre-build review — 2026-07-12

Re-verified line by line against the working tree (`server.js` @ 375 lines, `lib/` incl. the
new `llm.js`/`confirm.js`/`google.js`, all four skills). Findings, all folded in above:

| Checked | Result |
|---|---|
| `ctx` shape (`anthropic`, `catalog`, `quoted`, `sessions`, `send`, `lang`, `env`) | ✅ everything Component D needs is already there |
| `getQuoted` → `{ id, hasAudio, mediaType, text, calendarLink }` | ✅ as assumed — `quoted.text` carries the offending message |
| `isOwnMessage`, `headerFor`, `frame`, `sessions.set(jid, val, ttl)`, `jsonFormat`/`readReply` | ✅ all exported and usable as written |
| Router returns `tasks: string[]`, dispatched in order (`:357`) | ✅ the `["feedback", "calendar_action"]` both-at-once path is real |
| `ORCH_MSG` replies use bare `send()`, not `ctx.send` | ✅ confirmed — no double-capture through the soft scanner |
| **`server.js` line numbers** | ⚠️ **all drifted 2–3 lines** (`:98`→`:99`, `:269`→`:272`, `:296`→`:299`, `:310`→`:313`, `:324`→`:326`, `:340`→`:342`, `:347`→`:350`, `:360`→`:362`, `:365`→`:367`) — **fixed** |
| **Clarify session vs. `sessions.clear` on a new tagged order (`:334`)** | 🔴 **would have silently lost the complaint** — fixed: **write the report first, enrich on the answer** |
| **`suspected_skill` nullable enum** | 🔴 the validator rejects `type:["string","null"]` + `enum` — fixed: `anyOf`, per `1. Calendar Actions/prompt.js:48` |
| `TRANSLATE_MODEL` on `ctx` | ⚠️ not there (`ctx.model` is Sonnet) — the lib reads `process.env` instead |
| `secretary/improvements/` in `.gitignore` | ❌ absent — Component E must land **first**, as the plan already insists |
| `scripts/`, `.claude/commands/`, `Improvements/` | ❌ none exist — all new (`mkdir -p`) |
| New deps | none — `fs/promises` + the existing Anthropic SDK |

No blockers. **Ready to build.**

*Self-learning — plan revised after verifying every assumption against the code.*
*Amended (2026-07-12): added the owner-reported path (Component D) — the secretary can now be
told it was wrong, not just discover it — and re-reviewed the whole plan against the codebase.*
