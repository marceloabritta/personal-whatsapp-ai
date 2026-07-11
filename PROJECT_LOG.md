# Project Log — personal-whatsapp-ai

The living registry of the project: what it is, how it's deployed and operated, the
verified external-service contracts, and a dated log of how it has evolved. Start here
when picking the project up. Keep it current — append to the changelog (§10) as the
project changes.

This file is secret-free (no IPs or keys) and safe to commit. The real droplet IP and
all API keys live only in the DigitalOcean console and in local `.env` files,
deliberately kept out of version control.

---

## 1. What this is

A self-hosted personal AI secretary on WhatsApp. You type `@brain <order>` in any
chat; the system reads that chat's recent context, an LLM **router** classifies the
intent, and a task-specific **skill** does the work and replies to you on WhatsApp.
The brain is **stateful**: a flow starts on `@brain` and can then continue without the
tag (see §6 — the delete confirmation and, later, scheduling clarifications).

Stack: **Evolution API** (WhatsApp gateway, self-hosted) + a Node app called the
**brain** (orchestrator + skills) + **Claude** (reasoning) + **Redis** (per-chat session
state, shared with Evolution's cache) + per-skill external APIs (Google Calendar,
AssemblyAI). Everything runs in Docker on a single DigitalOcean droplet. See
`ARCHITECTURE.md` for the full "what is sent to each service" data flow.

Two skills exist today:
- `calendar_action` — **creates** and **cancels/deletes** Google Calendar events
  (edit/reschedule planned; see `New Features Plans/calendar-actions.md`). Cancel is confirm-first: the
  owner just types `yes`.
- `transcribe_audio` — reply to a voice message + `@brain transcribe`; downloads the
  audio from WhatsApp and transcribes it via AssemblyAI.

---

## 2. Current status (read this first)

- **Code:** `brain` (orchestrator + auto-discovered skills) is the only version now.
  The original single-agent `brain/v1.0` was removed 2026-07-10; it lives in git history
  (commit `3ce1e69` / `c01d817`) if ever needed.
- **GitHub:** repo `personal-whatsapp-ai` is **PRIVATE**. This local folder is a working
  **git clone** tracking `origin/main` (`gh` provides auth).
- **Production (the droplet): ✅ v2.0 is DEPLOYED and LIVE** (cut over 2026-07-10). Trigger
  kept as **`@brain`** and instance kept as **`secretaria`** (via compose env overrides
  `SECRETARY_TAG=@brain`, `EVOLUTION_INSTANCE=secretaria`) so WhatsApp stayed linked. Old
  v3.3 code backed up at `/opt/brain_v3.3_backup`; compose backup at
  `/opt/evolution/docker-compose.yml.v3.3.bak`.
- **Deploy pipeline: ✅ set up.** Read-only GitHub **deploy key** on the droplet + repo
  cloned at `/opt/personal-whatsapp-ai`; `/opt/brain` is a **symlink** to `brain`, so
  `git pull` updates the live code. SSH from this Mac via alias **`secretaria-droplet`**
  (key `~/.ssh/whatsapp_droplet`; real IP in `~/.ssh/config`, kept out of this file).

**What works now:** `calendar_action` end-to-end — **create** (real events + invite emails;
Google OAuth token re-minted + consent screen published, see §8) and **cancel/delete**
(confirm-first via a stateful session: `@brain cancel` replying to an invite → type `yes`).
`transcribe_audio` — reply-detection bug fixed (see §8); verify end-to-end when convenient.
The stateful session layer (Redis) is live; see §6.

> ✅ **DONE 2026-07-10 — folder-flatten migration (kept for reference).** The repo
> dropped the `brain/v2.0/` level — `brain/` is the app root. The droplet's `/opt/brain`
> symlink was re-pointed from `brain/v2.0` to `brain/` and the brain restarted (a fresh
> `npm install` ran because `node_modules` moved with the flatten). Steps that were run:
> ```bash
> ssh secretaria-droplet 'cd /opt/personal-whatsapp-ai && git pull --ff-only'
> ssh secretaria-droplet 'ln -sfn /opt/personal-whatsapp-ai/brain /opt/brain'
> ssh secretaria-droplet 'cd /opt/evolution && docker compose restart brain'
> ```
> The normal runbook below now applies to all further deploys.

### Deploy runbook (this is how to ship changes now)

```bash
# 1. from this Mac (folder is a clone; gh is logged in): edit, then
git add -A && git commit -m "..." && git push      # (git status is slow on Google Drive — normal)

# 2. deploy on the droplet
ssh secretaria-droplet 'cd /opt/personal-whatsapp-ai && git pull --ff-only'
ssh secretaria-droplet 'cd /opt/evolution && docker compose restart brain'   # code-only change
#   if /opt/brain/.env (secrets) changed:  docker compose up -d --force-recreate brain

# 3. verify / read logs
ssh secretaria-droplet 'docker logs --tail 50 brain'   # expect "Brain v2.0 (orchestrator) listening..."
```
- **Production writes are gated per Claude Code session** — a fresh session must be
  *explicitly asked* to run the `git pull`/restart (naming the action). Reading logs is
  read-only and not gated.
- `docker compose restart` reloads code but **not** `.env`; after a secret change use
  `up -d --force-recreate`. Rollback: repoint the `/opt/brain` symlink to
  `/opt/brain_v3.3_backup` (+ restore the compose `.bak`), then `--force-recreate`.

---

## 3. Open decisions & next tasks

1. ~~**Deploy v2.0 to the droplet.**~~ ✅ **DONE (2026-07-10)** — clone-on-droplet +
   `git pull` deploys (runbook in §2). App Platform was considered and rejected (can't run
   `docker-compose.yml`; would need paid managed Postgres/Redis + a session-persistence fix).
2. ~~**Cut over the trigger + instance names.**~~ ✅ **DONE** — kept `@brain` / instance
   `secretaria` via compose env overrides so WhatsApp stayed linked (no QR re-scan / webhook reset).
3. **Verify `transcribe_audio` end-to-end (open).** Reply-detection fixed 2026-07-10;
   `calendar_action` is confirmed working. Send a real quoted voice note + `@brain transcreva`
   to confirm the AssemblyAI round-trip.
4. **Security TODO (pre-existing).** Evolution's port `8080` is open to the internet,
   protected only by the API key. Lock it down with `ufw`.
5. **Calendar feature roadmap (open).** See `New Features Plans/calendar-actions.md`: smart scheduling
   (name events by topic; detect & collect missing attendee emails — asking the owner
   *or the attendee themselves* via the stateful `awaitFrom:"contact"` path) and
   edit/reschedule existing events by replying. Recommended next: Phase C (C1→C3).
6. **Product upgrades (backlog).** More skills (each a folder under `2. Skills/`),
   private reply when `@brain` is used in a group. (A "confirm before acting" step now
   exists for cancellations, built on the stateful session layer.)

---

## 4. Repository layout

```
.
├── README.md              # public overview + setup
├── ARCHITECTURE.md        # detailed data flow (what is sent to each service)
├── PROJECT_LOG.md         # this file — the project registry
├── LICENSE                # MIT
├── .gitignore
├── New Features Plans/    # per-feature implementation plans
│   ├── calendar-actions.md     #   smart scheduling + edit/reschedule (next up)
│   ├── message-summarizer.md
│   ├── reminders-followups.md
│   └── task-capture.md         #   BUILT (task_action); pending OAuth tasks-scope to deploy
├── brain/                 # the app — run this (v1.0 removed; in git history)
│   ├── package.json       #   at the brain/ ROOT (shared node_modules for orchestrator+skills)
│   ├── .env.example
│   ├── README.md
│   ├── 1. Orchestrator/
│   │   ├── server.js      #   webhook, start/continue gate, context, dispatch
│   │   ├── lib/{whatsapp,evolution,sessions}.js  # sessions.js = Redis session store
│   │   └── router/{prompt,router}.js
│   └── 2. Skills/
│       ├── 1. Calendar Actions/{skill,prompt}.js   # create + cancel/delete; exports capabilities.startCreate
│       ├── 2. Audio transcriptions/{skill,prompt}.js
│       └── 3. Tasks/{skill,prompt}.js              # Google Tasks (self) / delegates task-for-others to Calendar
└── evolution/
    ├── docker-compose.yml # Evolution API + Postgres + Redis + brain
    └── .env.example
```

---

## 5. Working on it from Claude Code (macOS)

```bash
git clone https://github.com/<your-username>/personal-whatsapp-ai.git
cd personal-whatsapp-ai
```

**Local install / boot check** (won't do real work without the full stack + a linked
WhatsApp, but confirms wiring and skill discovery):

```bash
cd "brain"
npm install
ANTHROPIC_API_KEY=dummy npm start
# expect: "skill loaded: ..." x2, "available skills: calendar_action, transcribe_audio"
# (no Redis locally -> "sessions: Redis unavailable, using memory" is fine; set REDIS_URL= to silence)
```

**Important layout facts:**
- `package.json` sits at the `brain/` root, **not** inside `1. Orchestrator/`. It has to:
  Node resolves `node_modules` by walking up from each file, and the skills live in a
  different branch than the orchestrator, so a single `node_modules` at the `brain/` root
  is the only place both can reach. Start command is `node "1. Orchestrator/server.js"`
  run from `brain/` (that's what `npm start` does).
- Folder names have spaces and numbers (`1. Orchestrator`, `2. Skills/1. Calendar
  Actions`). The orchestrator loads skills via dynamic `import(pathToFileURL(...))`,
  which handles the spaces. Don't convert these to static imports across folders.
- Requires Node 18+ (uses `fetch`, `fileURLToPath`, `pathToFileURL`). The droplet runs
  `node:20-alpine`.

**Full local run (optional):** bring up the whole `evolution/` docker-compose locally,
link a WhatsApp test number, point the webhook at the brain. Heavier; usually not worth
it for iterating on skill logic — prefer mocked tests (§9).

---

## 6. How a skill works (the contract)

Each skill is a folder under `2. Skills/` with a `skill.js`:

```js
export const manifest = {
  id: "unique_id",                 // the router routes to this id
  description: "what it does",      // the router reads this to classify
};
export async function run(ctx) { /* do the work, reply via ctx.send */ }
```

The orchestrator scans `2. Skills/*/skill.js` at boot, builds `{ [id]: run }` and a
catalog `[{id, description}]` that it passes to the router. **Adding a skill = drop in a
folder. No edits to `server.js` or the router.**

`ctx` handed to every skill: `owner, tag, anthropic, model, order, transcript, nowStr,
contact, number, remoteJid, fromMe, quoted, hasQuotedAudio, catalog, env, evolution,
send, sessions, session, lang`.
- `ctx.send(number, text)` — reply on WhatsApp (adds the `[AI Brain]:` header + a blank
  line; no footer). Localizes the body to `ctx.lang` (see the convention below).
- `ctx.lang` — the detected conversation language (ISO code; `"en"` default). The router
  detects it on a fresh command; it's persisted in the session for continuations.
- `ctx.evolution` — `{ sendText, fetchHistory, getMediaBase64 }`.
- `ctx.quoted` — `{ id, hasAudio, mediaType, text, calendarLink }` when the message is a
  reply, else null.
- `ctx.sessions` — the per-chat session store `{ get, set, clear }` (Redis-backed).
- `ctx.session` — the active session for this chat when the message is a **continuation**
  (else null); `ctx.fromMe` says whether the owner (true) or the contact (false) sent it.

**Stateful flow (§ see `brain/1. Orchestrator/ORCHESTRATOR.md`):** a flow STARTS only when the
owner sends `@brain`. While a session is open, the orchestrator hands each message from
the awaited party (`session.awaitFrom`: owner / contact / any) to the owning skill,
which uses the LLM to detect the awaited answer and ignores normal chatter — no reply or
tag needed. The brain never reacts to its own `[AI Brain]:` messages.

Convention: prompt/text lives in the skill's `prompt.js`, logic in `skill.js`.
**Localization:** user-facing strings are a per-language map (`{ en, pt }`) in `prompt.js`,
selected at send time with `ctx.lang`; every new message must ship its `en` *and* `pt`
entries (English is canonical). Dates use `localizeDate(ctx.lang, …)`. Any language without
a map is produced from the `en` copy by the orchestrator's `send()` translation fallback —
a safety net, not a reason to skip `pt`. Never translate the `[AI Brain]:` header;
classification/system prompts stay English. Full convention: `ARCHITECTURE.md`
("Localization convention").

Two-LLM-call design is intentional: the router classifies (call 1), then a skill like
`calendar_action` extracts details (call 2). `transcribe_audio` makes no LLM call.

---

## 7. Deploying v2.0 to the droplet (historical, initial one-time setup)

> This is how the droplet was first set up. It's **done** — for day-to-day deploys use
> the runbook in §2. Kept for reference / disaster recovery.

Run in the DigitalOcean web console (root). Replace `<repo-url>` and confirm paths.

```bash
# 1. Get the repo onto the server
cd /opt && git clone <repo-url> personal-whatsapp-ai

# 2. Point the brain at the app folder.
#    The compose 'brain' service mounts /opt/brain and runs `npm install && npm start`,
#    and npm start = node "1. Orchestrator/server.js". So /opt/brain must contain the
#    CONTENTS of brain (package.json at its root, "1. Orchestrator/", "2. Skills/").
#    Simplest: back up the current /opt/brain, then repoint it:
mv /opt/brain /opt/brain_v1_backup
ln -s /opt/personal-whatsapp-ai/brain /opt/brain     # or copy the contents

# 3. Bring your secrets across (do NOT commit these)
cp /opt/brain_v1_backup/.env /opt/brain/.env
#    then add the new key:  ASSEMBLYAI_API_KEY=...   (and ASSEMBLYAI_LANGUAGE=pt for PT audio)
#    decide trigger/instance: keep the old ones by setting in the compose 'brain' env:
#      SECRETARY_TAG: "@brain"        # if you want to keep the old trigger
#      EVOLUTION_INSTANCE: secretaria # if you want to keep the existing linked instance

# 4. Recreate the brain (force-recreate because .env changed)
cd /opt/evolution && docker compose up -d --force-recreate brain
docker compose logs -f brain     # expect "Brain v2.0 (orchestrator) listening..."
```

Gotcha: `docker compose restart` re-reads code but **not** `.env`. After any secret
change use `up -d --force-recreate`.

Future updates once this is set up: `cd /opt/personal-whatsapp-ai && git pull` then
`cd /opt/evolution && docker compose restart brain`.

If you keep the old instance/trigger, you skip re-linking WhatsApp and re-setting the
webhook. If you adopt the new `secretary` instance, you must re-scan the QR and re-run
the `/webhook/set/secretary` call (see README §Setup step 4).

---

## 8. External services & verified contracts

- **Anthropic (Claude):** `@anthropic-ai/sdk`, `CLAUDE_MODEL` env (default
  `claude-sonnet-5`). Router uses ~200 max_tokens; scheduling ~700.
- **Google Calendar:** OAuth (Client ID + Secret + Refresh Token). `sendUpdates=all`
  makes Google email the invite / the cancellation. Used by `calendar_action`
  (`events.insert` to create, `events.get` + `events.delete` to cancel; the event id
  is decoded from the invite link's `eid`).
- **Google Tasks:** SAME OAuth client, but the refresh token must ALSO carry the
  `https://www.googleapis.com/auth/tasks` scope — re-consent with **both** calendar +
  tasks scopes at once (a wrong re-consent drops calendar). Used by `task_action`
  (`tasks.insert`/`list`/`patch`/`delete` on `GOOGLE_TASKLIST_ID || "@default"`). `due`
  is **date-only** (stored at UTC midnight). Without the scope, calls 401 and the skill
  replies `failed()`. A to-do assigned to another person is created as a Calendar invite
  instead (via the capability registry) — Tasks itself notifies no one.
- **Redis (brain session state):** in addition to being Evolution's cache, the brain
  stores per-chat conversation state in Redis (`lib/sessions.js`, key prefix
  `brain:session:`, TTL'd). `REDIS_URL` defaults to `redis://evolution_redis:6379`
  (same `evolution-net`, no auth). No Redis → automatic in-memory fallback (lost on
  restart). This is what lets a flow continue without re-tagging `@brain`.
- **AssemblyAI:** `ASSEMBLYAI_API_KEY`. Flow (verified): `POST /v2/upload` (raw bytes)
  → `POST /v2/transcript` `{audio_url, language_code}` → poll `GET /v2/transcript/{id}`
  until `status==completed`. `ASSEMBLYAI_LANGUAGE` sets the language (`en` default; set
  `pt` for Portuguese). Note: AssemblyAI is a US cloud service — audio bytes leave the
  droplet, which is the one place the self-hosted privacy model is broken. A self-hosted
  Whisper is the alternative if that matters.
- **Evolution media download (transcription):** `POST
  /chat/getBase64FromMediaMessage/{instance}` with `{message:{key:{id}},convertToMp4}` →
  `{base64, mimetype}`. **Requires `DATABASE_SAVE_DATA_NEW_MESSAGE=true`** in the
  Evolution `.env` (default) — otherwise old audios 404.
  - **Quoted-reply payload gotcha (bug fixed 2026-07-10).** For a plain-text reply,
    Evolution (v2.3.7) delivers the reply context at **`data.contextInfo`** — a SIBLING of
    `data.message`, NOT nested under `data.message.extendedTextMessage.contextInfo`. The
    quoted audio id is `data.contextInfo.stanzaId`; the audio itself is at
    `data.contextInfo.quotedMessage.audioMessage`. `getQuoted()` now takes the whole `data`
    and checks the sibling first. **Debug tip:** Evolution's Postgres `Message` table stores
    only `message` and DROPS the sibling `contextInfo`, so a reply looks like a bare
    `conversation` there — inspect the **raw webhook** via `docker logs evolution_api`
    (search the message id), not the DB, when debugging replies.
- **Google OAuth (Calendar) gotcha.** The OAuth consent screen MUST be published to
  **"In production."** In **"Testing"** status, refresh tokens **expire after ~7 days** and
  the bot then fails calendar creation with `invalid_grant` ("Token expired or revoked").
  Published + token re-minted 2026-07-10 (OAuth Playground, scope
  `https://www.googleapis.com/auth/calendar`, "Use your own OAuth credentials"). To re-mint:
  Playground → gear → own client id/secret → scope above → authorize → exchange → copy the
  `1//…` refresh token into `/opt/brain/.env` `GOOGLE_REFRESH_TOKEN=` → `--force-recreate`.

Env var reference: see `brain/.env.example` and `evolution/.env.example`.

---

## 9. Testing approach

There's no committed test suite yet. During development, skills and the router were
tested by stubbing `global.fetch` (Evolution + AssemblyAI) and injecting a fake
`anthropic` client into `ctx`, then asserting on captured `send()` calls. This exercises
routing, both skill flows, and every guardrail without network or real keys. Worth
formalizing into a `test/` folder with `node --test`. Boot + skill-discovery is the
cheapest smoke test: `ANTHROPIC_API_KEY=dummy npm start`.

---

## 10. Changelog (evolution log)

Reverse-chronological. Append a dated entry whenever the project meaningfully changes.

- **2026-07-11 — task capture skill + cross-skill capability registry (BUILT, not yet
  deployed).** New `task_action` skill (`2. Skills/3. Tasks/`): a to-do inbox backed by
  Google Tasks. A to-do for **yourself** is created immediately in Google Tasks, then a
  short **amend window** (session) lets you correct/delete it with no confirm step; **list**
  reads open tasks; **complete** is confirm-first. A to-do for **someone else** is delegated
  to `calendar_action` as a 5-min invite (15:00 on the due date) so they're emailed — via a
  new **capability registry**: skills may export a `capabilities` object, and the orchestrator
  injects `ctx.hasSkill`/`ctx.callSkill` (auto-injects ctx, `MAX_SKILL_DEPTH` loop guard) so
  skills compose without importing each other's files. Calendar exposes
  `capabilities.startCreate`. en+pt localized per the convention. **Blocking to deploy:**
  re-consent OAuth with the **tasks** scope added to `GOOGLE_REFRESH_TOKEN` (see §8). Docs:
  `2. Skills/3. Tasks/SKILL.md`, ARCHITECTURE "Composing skills", ORCHESTRATOR registry note.
- **2026-07-11 — multilingual brain (DEPLOYED).** The brain now detects the conversation
  language (the router returns `lang`, schema-enforced) and replies in it, system-wide.
  Prose lives per-skill in `prompt.js` as `{ en, pt }` maps selected by `ctx.lang`; the
  `send()` choke point localizes — en/pt pass through, any other language is body-translated
  by a cheap `TRANSLATE_MODEL` (header never translated). Dates via `localizeDate` (3-letter
  month + AM/PM; locale day/month order). `lang` is persisted in the session so
  continuations answer in-language. Maintained: en + pt-BR. Convention: `ARCHITECTURE.md`
  ("Localization convention"). Shipped to production the same day (`git pull` + restart).
- **2026-07-11 — calendar structured outputs.** All four calendar LLM calls moved to
  native structured outputs (`output_config.format` + JSON Schemas in `prompt.js`), with a
  `parseJsonReply`/refusal-guard fallback; `@anthropic-ai/sdk` bumped to `^0.111`.
- **2026-07-10 — folder flattened + docs registry.** `brain/v2.0/` collapsed to
  `brain/` (only one version now; `v1.0` lives in git history). This handover doc
  renamed to `PROJECT_LOG.md` and repurposed as the project registry. Feature plans
  gathered under `New Features Plans/`. Each production component now has a doc
  (`ORCHESTRATOR.md`, `2. Skills/*/SKILL.md`). Deployed to production the same day —
  `/opt/brain` symlink re-pointed to `brain/` (see §2 migration note).
- **2026-07-10 — stateful conversation layer.** Per-chat sessions in Redis
  (`lib/sessions.js`): a flow starts on `@brain` and continues without the tag; the
  brain LLM-detects the awaited answer and ignores chatter. Delete migrated to a
  confirm-first session (type `yes`). Foundation for owner *and* contact answers
  (`awaitFrom`). Message framing switched to `[AI Brain]:` (no footer).
- **2026-07-10 — calendar delete + rename.** `schedule_meeting` → `calendar_action`;
  added cancel/delete (resolve event from the invite link, confirm, `events.delete`).
- **2026-07-10 — v2.0 deployed to production.** Cut over from the old single-agent
  code; `@brain` trigger + instance `secretaria` kept via compose env; deploy pipeline
  (deploy key + `/opt` clone + `/opt/brain` symlink) established.
- **v2.0 baseline** — orchestrator + auto-discovered skills; router classifies intent;
  added `transcribe_audio`. English strings.
- **v1.0** — single-file agent; every message assumed a meeting request; Portuguese.
  Removed from the tree 2026-07-10; preserved in git history.

Next up: the calendar feature roadmap — see `New Features Plans/calendar-actions.md`
(smart scheduling, then edit/reschedule).
