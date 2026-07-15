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

A self-hosted personal AI secretary on WhatsApp. You type `@secretary <order>` in any
chat; the system reads that chat's recent context, an LLM **router** classifies the
intent, and a task-specific **skill** does the work and replies to you on WhatsApp.
The secretary is **stateful**: a flow starts on `@secretary` and can then continue without the
tag (see §6 — the delete confirmation and, later, scheduling clarifications).

Stack: **Evolution API** (WhatsApp gateway, self-hosted) + a Node app called the
**secretary** (orchestrator + skills) + **Claude** (reasoning) + **Redis** (per-chat session
state, shared with Evolution's cache) + per-skill external APIs (Google Calendar,
AssemblyAI). Everything runs in Docker on a single DigitalOcean droplet. See
`ARCHITECTURE.md` for the full "what is sent to each service" data flow.

Seven skills exist today:
- `calendar_action` — **creates**, **edits/reschedules**, **cancels/deletes**, and
  **reads/lists** Google Calendar events. Create and cancel are confirm-first (the owner
  types `yes`); edit is a reply-driven change (move/relength/rename/add-remove attendee),
  confirm-first and stays open until saved, clarifying when ambiguous; **list is read-only**
  (no session, no confirm, no write — "what's on tomorrow?", "what's my next meeting?").
- `transcribe_audio` — reply to a voice message + `@secretary transcribe`; downloads the
  audio from WhatsApp and transcribes it via AssemblyAI.
- `task_action` — a to-do inbox: add / list / complete / edit / delete todos, **one or many
  per message**, via a single list-aware planner that matches the owner's words to tasks on
  file. Stays **engaged without re-tagging** for a window. A todo for the owner goes to Google
  Tasks; one assigned to someone else becomes a 5-min Calendar invite (via `calendar_action`'s
  `startCreate` capability). See `New Features Plans/task-improvements.md`.
- `feature_request` — talk through a new feature idea; the secretary interviews the owner, then
  writes a Markdown spec and sends it as a `.md` document.
- `feedback` — **tell the secretary it made a mistake and it files itself a bug report.** Reply
  to the wrong message with `@secretary you made a mistake here`; the complaint, the offending
  message and the logs become a report in `secretary/improvements/`, which
  `scripts/self-learning-pull.sh` + `/triage-failures` turn into an implementation plan. The
  only way a **false positive** or a confidently-wrong answer ever gets caught — the code has
  no idea it failed. Part of **self-learning** (see `ARCHITECTURE.md`).
- `flight_search` — search flights from a sentence, **confirm-first**, then the **3 cheapest
  options a human would actually pick**: a mandatory client-side filter throws away the
  multi-stop and carrier-chained (self-transfer) itineraries Kiwi floats to the top of a
  cheapest-first list, **before** the sort. One follow-up turn hands over the booking link
  (`link for option 2`, tagged or not). It **never buys**. Provider: Kiwi's keyless MCP
  endpoint. See `secretary/2. Skills/6. Flight Search/SKILL.md`.
- `assistant_settings` — **change how you summon her, by asking her.** `@assistant, change your
  tag to @assist`: she deduces whether the other language's call should change too, says the
  reasoning in prose, shows the **complete** new tag list, and applies it only on a `yes`. The
  confirmed list is **persisted** (Redis, no TTL) and **wins over `SECRETARY_TAG`**, which is now
  only the seed. See `secretary/2. Skills/7. Assistant Settings/SKILL.md`.

**Two skill trees run in parallel (A/B), selected by summon tag** (see §2 and `ARCHITECTURE.md`).
The seven descriptions above are the OLD (`@assistant`) tree, `secretary/2. Skills/` — each skill
holds its own propose/confirm dialogue. **`@mary` routes to a second, fully-converted tree,
`secretary/3. Mary Skills/`**, where the same seven skills are **pure tasks**: the orchestrator
model runs every conversation and each skill only validates its declared `inputs`, acts, and
returns a value (calendar/tasks/flights use a READ-then-ACT contract; the calendar↔tasks
`startCreate` coupling exists only in the old tree). Both trees are discovered at boot into their
own maps; a bug in one cannot reach the other. See each `3. Mary Skills/<N>/SKILL.md`.

---

## 2. Current status (read this first)

- **Code:** `secretary` (orchestrator + auto-discovered skills) is the only version now.
  The original single-agent `brain/v1.0` was removed 2026-07-10; it lives in git history
  (commit `3ce1e69` / `c01d817`) if ever needed.
- **GitHub:** repo `personal-whatsapp-ai` is **PRIVATE**. This local folder is a working
  **git clone** tracking `origin/main` (`gh` provides auth).
- **Production (the droplet): ✅ v2.0 is DEPLOYED and LIVE** (cut over 2026-07-10). Trigger
  now **`@secretaria` / `@secretary`** (comma-separated `SECRETARY_TAG=@secretaria,@secretary`;
  the legacy `@brain` is retired and silently ignored) and instance kept as **`secretaria`**
  (`EVOLUTION_INSTANCE=secretaria`) so WhatsApp stayed linked. Old
  v3.3 code backed up at `/opt/brain_v3.3_backup`; compose backup at
  `/opt/evolution/docker-compose.yml.v3.3.bak`.
- **Deploy pipeline: ✅ set up.** Read-only GitHub **deploy key** on the droplet + repo
  cloned at `/opt/personal-whatsapp-ai`; `/opt/secretary` is a **symlink** to `secretary`, so
  `git pull` updates the live code. SSH from this Mac via alias **`secretaria-droplet`**
  (key `~/.ssh/whatsapp_droplet`; real IP in `~/.ssh/config`, kept out of this file).

- **@mary now runs a fully-converted stack (2026-07-15).** The `@mary` flow discovers its own
  isolated tree `secretary/3. Mary Skills/` (all seven skills converted to pure tasks — the
  orchestrator holds every conversation; calendar/tasks/flights are READ-then-ACT). `@assistant`
  is unchanged (OLD `2. Skills/` tree + legacy flow). This is an **A/B parallel run**; the default
  flip (retiring the old tree) is a later card, and it is gated on the human's live
  `router-selftest.mjs` against the new catalog.

**What works now:** `calendar_action` end-to-end — **create** (real events + invite emails;
Google OAuth token re-minted + consent screen published, see §8) and **cancel/delete**
(confirm-first via a stateful session: `@secretary cancel` replying to an invite → type `yes`).
`transcribe_audio` — reply-detection bug fixed (see §8); verify end-to-end when convenient.
The stateful session layer (Redis) is live; see §6.

> **Awaiting deploy (2026-07-14):** the "specs & plans auto-land on the kanban backlog" card is
> BUILT. Its **Mac-side half is live with no deploy** (a triaged `bugfix-*.md` becomes a card
> tonight). Its **droplet-side half needs a deploy** — the `feature_request` skill now spools each
> spec to `secretary/specs/` before sending, and that runs in the container: a plain `git pull` +
> `docker compose restart secretary` (§2 runbook). Until then the malfunction half runs and the
> feature half does not.

> ✅ **DONE 2026-07-10 — folder-flatten migration (kept for reference).** The repo
> dropped the `brain/v2.0/` level — `secretary/` is the app root. The droplet's `/opt/secretary`
> symlink was re-pointed from `brain/v2.0` to `secretary/` and the secretary restarted (a fresh
> `npm install` ran because `node_modules` moved with the flatten). Steps that were run:
> ```bash
> ssh secretaria-droplet 'cd /opt/personal-whatsapp-ai && git pull --ff-only'
> ssh secretaria-droplet 'ln -sfn /opt/personal-whatsapp-ai/secretary /opt/secretary'
> ssh secretaria-droplet 'cd /opt/evolution && docker compose restart secretary'
> ```
> The normal runbook below now applies to all further deploys.

### Deploy runbook (this is how to ship changes now)

```bash
# 1. from this Mac (folder is a clone; gh is logged in): edit, then
git add -A && git commit -m "..." && git push      # (git status is slow on Google Drive — normal)

# 2. deploy on the droplet
ssh secretaria-droplet 'cd /opt/personal-whatsapp-ai && git pull --ff-only'
ssh secretaria-droplet 'cd /opt/evolution && docker compose restart secretary'   # code-only change
#   if /opt/secretary/.env (secrets) changed:  docker compose up -d --force-recreate secretary

# 3. verify / read logs
ssh secretaria-droplet 'docker logs --tail 50 secretary'   # expect "Secretary v2.0 (orchestrator) listening..."
```
- **Production writes are gated per Claude Code session** — a fresh session must be
  *explicitly asked* to run the `git pull`/restart (naming the action). Reading logs is
  read-only and not gated.
- `docker compose restart` reloads code but **not** `.env`; after a secret change use
  `up -d --force-recreate`. Rollback: repoint the `/opt/secretary` symlink to
  `/opt/brain_v3.3_backup` (+ restore the compose `.bak`), then `--force-recreate`.

---

## 3. Open decisions & next tasks

1. ~~**Deploy v2.0 to the droplet.**~~ ✅ **DONE (2026-07-10)** — clone-on-droplet +
   `git pull` deploys (runbook in §2). App Platform was considered and rejected (can't run
   `docker-compose.yml`; would need paid managed Postgres/Redis + a session-persistence fix).
2. ~~**Cut over the trigger + instance names.**~~ ✅ **DONE** — kept instance
   `secretaria` via compose env overrides so WhatsApp stayed linked (no QR re-scan / webhook reset).
3. **Verify `transcribe_audio` end-to-end (open).** Reply-detection fixed 2026-07-10;
   `calendar_action` is confirmed working. Send a real quoted voice note + `@secretary transcreva`
   to confirm the AssemblyAI round-trip.
4. **Security TODO (pre-existing).** Evolution's port `8080` is open to the internet,
   protected only by the API key. Lock it down with `ufw`.
5. **Calendar feature backlog (open).** Smart scheduling (Phase C) and edit/reschedule
   (Phase B) are ✅ **shipped**. Remaining, see `New Features Plans/calendar-actions.md`:
   conflict/availability check on create, read/query events, and recurring events.
6. **Product upgrades (backlog).** More skills (each a folder under `2. Skills/`),
   private reply when `@secretary` is used in a group. (A "confirm before acting" step now
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
├── New Features Plans/    # per-feature implementation plans; ALSO the pulled feature-spec spool
│   ├── calendar-actions.md     #   calendar backlog (conflict-check, query, recurring)
│   ├── message-summarizer.md
│   ├── reminders-followups.md
│   ├── board-inbox-auto-cards.md #  plan: specs/plans auto-land on the kanban backlog
│   └── task-improvements.md    #   NEXT for tasks: batch create/complete + edit existing
│                               #   (task-capture.md retired + deleted after task_action shipped)
├── Board Inbox/           # staging between the two funnels and the kanban board
│   ├── ledger.tsv         #   TRACKED — the exactly-once authority; losing it re-opens old cards
│   ├── README.md          #   what the queue / ledger / delivered / lock are
│   └── .gitignore         #   queue/, delivered/, .drain.lock are runtime state (ledger is kept)
├── scripts/               # Mac-side automation (pull, triage, board ingest, self-tests)
│   ├── board-ingest.mjs   #   seed / enqueue / drain — spec+plan -> backlog card, over the board HTTP API
│   ├── board-ingest.sh    #   thin node wrapper (PATH + cd) for the daily job and the timer
│   └── com.marcelo.board-ingest.plist  # launchd drain timer (every 5 min)
├── secretary/             # the app — run this (v1.0 removed; in git history)
│   ├── package.json       #   at the secretary/ ROOT (shared node_modules for orchestrator+skills)
│   ├── .env.example
│   ├── README.md
│   ├── improvements/      #   runtime failure-report spool (pulled to Bugs and Malfunctions/)
│   ├── specs/             #   runtime feature-spec spool (pulled to New Features Plans/)
│   ├── 1. Orchestrator/
│   │   ├── server.js      #   webhook, start/continue gate, context, dispatch
│   │   ├── lib/{whatsapp,evolution,sessions}.js  # sessions.js = Redis session store
│   │   └── router/{prompt,router}.js
│   └── 2. Skills/
│       ├── 1. Calendar Actions/{skill,prompt}.js   # create + cancel/delete; exports capabilities.startCreate
│       ├── 2. Audio transcriptions/{skill,prompt}.js
│       ├── 3. Tasks/{skill,prompt}.js              # Google Tasks (self) / delegates task-for-others to Calendar
│       ├── 4. Feature Requests/{skill,prompt}.js   # clarify conversation → Markdown spec sent as a .md document
│       ├── 5. Feedback/{skill,prompt}.js           # "you got this wrong" → a self-learning failure report
│       └── 6. Flight Search/{skill,prompt}.js      # confirm-first flight search (Kiwi); 3 cheapest AFTER the junk filter
└── evolution/
    ├── docker-compose.yml # Evolution API + Postgres + Redis + secretary
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
cd "secretary"
npm install
ANTHROPIC_API_KEY=dummy npm start
# expect: "skill loaded: ..." x2, "available skills: calendar_action, transcribe_audio"
# (no Redis locally -> "sessions: Redis unavailable, using memory" is fine; set REDIS_URL= to silence)
```

**Important layout facts:**
- `package.json` sits at the `secretary/` root, **not** inside `1. Orchestrator/`. It has to:
  Node resolves `node_modules` by walking up from each file, and the skills live in a
  different branch than the orchestrator, so a single `node_modules` at the `secretary/` root
  is the only place both can reach. Start command is `node "1. Orchestrator/server.js"`
  run from `secretary/` (that's what `npm start` does).
- Folder names have spaces and numbers (`1. Orchestrator`, `2. Skills/1. Calendar
  Actions`). The orchestrator loads skills via dynamic `import(pathToFileURL(...))`,
  which handles the spaces. Don't convert these to static imports across folders.
- Requires Node 18+ (uses `fetch`, `fileURLToPath`, `pathToFileURL`). The droplet runs
  `node:20-alpine`.

**Full local run (optional):** bring up the whole `evolution/` docker-compose locally,
link a WhatsApp test number, point the webhook at the secretary. Heavier; usually not worth
it for iterating on skill logic — prefer mocked tests (§9).

---

## 6. How a skill works (the contract)

Each skill is a folder under `2. Skills/` with a `skill.js`:

```js
export const manifest = {
  id: "unique_id",                 // the router routes to this id
  description: "what it does",      // the router reads this to classify
  inputs: { /* … */ } || null,      // the inputs the router pre-extracts for you (or null)
};
export async function run(ctx) { /* do the work, reply via ctx.send */ }
```

The orchestrator scans `2. Skills/*/skill.js` at boot, builds `{ [id]: run }` and a
catalog `[{id, description, inputs}]` that it passes to the router. **Adding a skill = drop in a
folder. No edits to `server.js` or the router.**

`manifest.inputs` is the skill's **declared input contract** (`lib/inputs.js`). The router asks
the model to fill it in the SAME call that classifies the order, plain code validates the reply
against the declaration, and a valid payload arrives as `ctx.info` — so the skill acts without a
second round-trip. `inputs: null` means "no inputs; I read the conversation myself", and such a
skill is never handed a payload. A skill that ignores `ctx.info` behaves exactly as before.

`ctx` handed to every skill: `owner, tag, anthropic, model, order, transcript, nowStr,
contact, number, remoteJid, fromMe, quoted, hasQuotedAudio, catalog, env, evolution,
send, sessions, session, lang`.
- `ctx.send(number, text)` — reply on WhatsApp (adds the language-aware header from
  `headerFor(lang)` — `[Marcelo's AI Secretary]:` (en) / `[Secretaria IA do Marcelo]:` (pt) — + a blank
  line; no footer). Localizes the body to `ctx.lang` (see the convention below).
- `ctx.lang` — the detected conversation language (ISO code; `"en"` default). The router
  detects it on a fresh command; it's persisted in the session for continuations.
- `ctx.evolution` — `{ sendText, fetchHistory, getMediaBase64 }`.
- `ctx.quoted` — `{ id, hasAudio, mediaType, text, calendarLink }` when the message is a
  reply, else null.
- `ctx.sessions` — the per-chat session store `{ get, set, clear }` (Redis-backed).
- `ctx.session` — the active session for this chat when the message is a **continuation**
  (else null); `ctx.fromMe` says whether the owner (true) or the contact (false) sent it.

**Stateful flow (§ see `secretary/1. Orchestrator/ORCHESTRATOR.md`):** a flow STARTS only when the
owner sends `@secretary`. While a session is open, the orchestrator hands each message from
the awaited party (`session.awaitFrom`: owner / contact / any) to the owning skill,
which uses the LLM to detect the awaited answer and ignores normal chatter — no reply or
tag needed. Trigger tags + the reply header live in the shared module
`secretary/1. Orchestrator/lib/identity.js` (exports `TAGS`, `headerFor`, `isOwnMessage`,
`matchedTag`); the secretary never reacts to its own header'd messages (`isOwnMessage`
matches every header variant, incl. the legacy `[AI Brain]:`).

Convention: prompt/text lives in the skill's `prompt.js`, logic in `skill.js`.
**Localization:** user-facing strings are a per-language map (`{ en, pt }`) in `prompt.js`,
selected at send time with `ctx.lang`; every new message must ship its `en` *and* `pt`
entries (English is canonical). Dates use `localizeDate(ctx.lang, …)`. Any language without
a map is produced from the `en` copy by the orchestrator's `send()` translation fallback —
a safety net, not a reason to skip `pt`. Never translate the reply header
(`[Marcelo's AI Secretary]:` / `[Secretaria IA do Marcelo]:`);
classification/system prompts stay English. Full convention: `ARCHITECTURE.md`
("Localization convention").

**ONE LLM call on a fresh order** (card 9af6967a, 2026-07-13). The router classifies AND
extracts the chosen skill's declared inputs in a single round-trip; plain code — no AI — then
checks the payload, and only if that check fails does the skill run its own clarification call.
It used to be two calls (three on a create), and per-turn latency is linear in the number of
round-trips. `transcribe_audio` makes no LLM call.

---

## 7. Deploying v2.0 to the droplet (historical, initial one-time setup)

> This is how the droplet was first set up. It's **done** — for day-to-day deploys use
> the runbook in §2. Kept for reference / disaster recovery.

Run in the DigitalOcean web console (root). Replace `<repo-url>` and confirm paths.

```bash
# 1. Get the repo onto the server
cd /opt && git clone <repo-url> personal-whatsapp-ai

# 2. Point the secretary at the app folder.
#    The compose 'secretary' service mounts /opt/secretary and runs `npm install && npm start`,
#    and npm start = node "1. Orchestrator/server.js". So /opt/secretary must contain the
#    CONTENTS of secretary (package.json at its root, "1. Orchestrator/", "2. Skills/").
#    Simplest: back up the current /opt/secretary, then repoint it:
mv /opt/secretary /opt/brain_v1_backup
ln -s /opt/personal-whatsapp-ai/secretary /opt/secretary     # or copy the contents

# 3. Bring your secrets across (do NOT commit these)
cp /opt/brain_v1_backup/.env /opt/secretary/.env
#    then add the new key:  ASSEMBLYAI_API_KEY=...   (and ASSEMBLYAI_LANGUAGE=pt for PT audio)
#    decide trigger/instance: set them in the compose 'secretary' env:
#      SECRETARY_TAG: "@secretaria,@secretary"   # comma-separated; the legacy @brain is retired
#      EVOLUTION_INSTANCE: secretaria # if you want to keep the existing linked instance

# 4. Recreate the secretary (force-recreate because .env changed)
cd /opt/evolution && docker compose up -d --force-recreate secretary
docker compose logs -f secretary     # expect "Secretary v2.0 (orchestrator) listening..."
```

Gotcha: `docker compose restart` re-reads code but **not** `.env`. After any secret
change use `up -d --force-recreate`.

Future updates once this is set up: `cd /opt/personal-whatsapp-ai && git pull` then
`cd /opt/evolution && docker compose restart secretary`.

The linked WhatsApp **instance is `secretaria`** (`EVOLUTION_INSTANCE=secretaria`) and was
deliberately kept through the de-brand — renaming an Evolution instance means re-scanning the
QR and re-registering the webhook, so it wasn't worth it just for a name. Note the instance
(`secretaria`) is intentionally distinct from the container/service name (`secretary`); don't
conflate them. The webhook URL still uses the *container* hostname `http://secretary:3000/webhook`
(re-registered on the `secretaria` instance during the container rename — see §10).

---

## 8. External services & verified contracts

- **Anthropic (Claude):** `@anthropic-ai/sdk`, `CLAUDE_MODEL` env (default
  `claude-sonnet-5`). The merged router+extractor call uses **1024** max_tokens (it returns a
  payload, not just a classification); the calendar extraction/review calls use **4096**.
  **Every call sends `thinking: {type:"disabled"}`** — the single client is wrapped once, in
  `server.js`, by `withThinkingDefault()` (`lib/llm.js`). Extended thinking is ON by default on
  `claude-sonnet-5` and we discard every thinking block, so we were waiting for and paying for
  output nobody read. A call site that genuinely wants reasoning passes its own `thinking`.
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
- **Kiwi (flight search) — `https://mcp.kiwi.com`, verified live 2026-07-12.** Used by
  `flight_search` via a plain `fetch` (no SDK, no new dependency). JSON-RPC `tools/call`, tool
  name `search-flight`. **No API key, no account, no billing** — and **no `initialize` handshake
  and no `Mcp-Session-Id`**: a cold `tools/call` works. `Accept` **must** list BOTH
  `application/json` **and** `text/event-stream` (json alone → **HTTP 406**). The response is
  **always SSE-framed, with CRLF line terminators** (`event: message\r\ndata: {…}\r\n\r\n`); the
  payload is `result.structuredContent`. **A bad argument comes back on an HTTP 200** with
  `isError: true`, **no `structuredContent`**, and `content[0].text` as a **plain, non-JSON
  string** — so `isError` is checked *before* anything is parsed (a naive
  `JSON.parse(content[0].text)` throws). Wire types: **`departureDate`/`returnDate` are
  `dd/mm/yyyy`, NOT ISO** (an ISO date returns `isError: true`); `cabinClass` is the enum
  `M|W|C|F`; on a one-way, `inbound` is **present and `null`**; a past date or an unresolvable
  city returns a cheerful `resultsCount: 0`. The `kiwi.com/u/…` booking links do not expire.
  `currency` comes from `FLIGHT_CURRENCY` (default `BRL`); `locale` is fixed at `pt` (it drives
  Kiwi's booking page, not our reply). Latency measured across four probes: 9723 / 4179 / 1564 /
  1659 ms — timeout 20s, no interim ack.
  **Two warnings, both learned the hard way:**
  (a) **Kiwi is a self-described prototype with no SLA** — the shape can change or the endpoint
  can vanish. Every unreadable answer lands on `ctx.sendFailure` → the Bugs board, rather than
  masquerading as "no flights today".
  (b) **ITS RESULTS ARE VOLATILE.** The identical query, run four times, returned **four disjoint
  result sets** — 11 of 15 itineraries surviving the skill's filter on one, **15 of 15** on
  another. *"The filter dropped everything today and nothing yesterday"* is **expected, not a
  bug**, and it is why `scripts/flights-selftest.mjs`'s fixture is **frozen and hand-built**: a
  fixture regenerated from a live call would stop discriminating and silently gut the suite.
- **Redis (secretary session state):** in addition to being Evolution's cache, the secretary
  stores per-chat conversation state in Redis (`lib/sessions.js`, key prefix
  `secretary:session:`, TTL'd). `REDIS_URL` defaults to `redis://evolution_redis:6379`
  (same `evolution-net`, no auth). No Redis → automatic in-memory fallback (lost on
  restart). This is what lets a flow continue without re-tagging `@secretary`.
- **AssemblyAI:** `ASSEMBLYAI_API_KEY`. Flow (verified): `POST /v2/upload` (raw bytes)
  → `POST /v2/transcript` `{audio_url, language_code}` → poll `GET /v2/transcript/{id}`
  until `status==completed`. `ASSEMBLYAI_LANGUAGE` sets the language (`en` default; set
  `pt` for Portuguese). Note: AssemblyAI is a US cloud service — audio bytes leave the
  droplet, which is the one place the self-hosted privacy model is broken. A self-hosted
  Whisper is the alternative if that matters.
- **Evolution media SEND (feature spec doc) — ⚠ UNVERIFIED contract.** `POST
  /message/sendMedia/{instance}` with `{ number, mediatype:"document",
  mimetype:"text/markdown", media:<base64>, fileName, caption }`. Used by `feature_request`
  (`evolution.sendMedia`) to deliver the generated `.md` as a document. Field names follow
  the Evolution v2 API but were **not yet confirmed against the running image** — verify
  with one send (or check the instance's Swagger) before relying on it; adjust if the image
  expects a nested `mediaMessage`/`options` shape.
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
  `1//…` refresh token into `/opt/secretary/.env` `GOOGLE_REFRESH_TOKEN=` → `--force-recreate`.

Env var reference: see `secretary/.env.example` and `evolution/.env.example`.

---

## 9. Testing approach

There's no committed test suite yet. During development, skills and the router were
tested by stubbing `global.fetch` (Evolution + AssemblyAI) and injecting a fake
`anthropic` client into `ctx`, then asserting on captured `send()` calls. This exercises
routing, both skill flows, and every guardrail without network or real keys. Worth
formalizing into a `test/` folder with `node --test`. Boot + skill-discovery is the
cheapest smoke test: `ANTHROPIC_API_KEY=dummy npm start`.

**Committed self-tests** (the first of the `test/` folder above, in spirit). *No numeral here on
purpose — this list went stale once already by counting.*

- `node scripts/flights-selftest.mjs` — **offline** (no network, no keys: `fetch` and
  `ctx.anthropic` are stubbed, `createSessions()` runs on its in-memory Map). 65 assertions over
  `flight_search`. The two it exists for: **the result filter runs BEFORE the sort** (a
  sort-then-filter build shows the owner self-transfer junk — on a real capture the four cheapest
  results were all carrier chains), and **the options tombstone is written at FLOW START** (or a
  search that finds nothing leaves the *previous* search's booking links addressable). Its Kiwi
  fixtures are **frozen and hand-built — never regenerate them from a live call** (Kiwi's results
  are volatile; a refreshed fixture stops discriminating and the suite passes on the very bug it
  exists to catch — test `#4a` guards this).
- `node scripts/selflearning-selftest.mjs` — the self-learning capture invariants, fully
  offline (fake `ctx`, stub `anthropic`, reports redirected to a temp dir via
  `SELF_LEARNING_DIR`). Asserts redaction, machine dedupe, the `{ ...ctx }`-spread guard, that
  an **owner-reported note is never deduped or dropped**, and that capture never throws.
- `node scripts/history-selftest.mjs` — offline. Fails if anyone drops the **dual-JID** history
  query (`remoteJid` + `remoteJidAlt`): WhatsApp addresses the same 1:1 chat under both a phone
  JID and a LID, and reading only one of them made the secretary blind to half the conversation.
- `node scripts/board-ingest-selftest.mjs` — offline (stub board, no network, no keys). The
  exactly-once / nothing-dropped core of the board ingest: a lost POST ack is reconciled by the
  `source:` footer (never a second card), the drain is single-flight (a dead lock is broken, a live
  one blocks), the seed+ledger interlock stops an unseeded run opening a card for every file on
  disk, the owner-reported predicate is checked against **what the real `captureFailure` writes**,
  and the wrong-board tripwire catches a board that stopped honouring `kind` (guards `./update.sh`
  swapping the vendored board out from under us).
- `node scripts/pull-archive-selftest.mjs` — offline (runs the **live** `self-learning-pull.sh`
  against stub `ssh`/`rsync`). Exists to catch the **silent-drop bug**: a file written into the
  spool mid-pull must stay in the spool, not be archived out having never transferred; the two
  funnels stay independent (one empty/failing spool never stops the other); and
  `rsync --remove-source-files` is never passed.
- `node scripts/identity-selftest.mjs` — offline. Asserts the trigger tag and reply header values
  in `lib/identity.js` (`TAGS` defaults to `@assistente,@assistant`; `headerFor()` returns the
  Assistant pair; the old tags no longer match) **and** — the one that protects something — that
  `isOwnMessage()` still recognises the **retired** headers (`[Marcelo's AI Secretary]:` /
  `[Secretaria IA do Marcelo]:` / `[AI Brain]:`), bolded and unbolded. Those `LEGACY_HEADERS`
  entries look like dead code and are not: they are what keeps the feedback skill able to see the
  bot's own back-catalogue of messages as its own. Delete them and every quoted old bot message is
  silently reclassified as "context only".
- `ANTHROPIC_API_KEY=… node scripts/tasks-addressed-selftest.mjs` — the Tasks planner's
  **addressed** bit. Two halves: a **live** half (16 planner calls, a few cents) proving the
  overheard chatter produces an empty plan *and* that genuine untagged follow-ups still act, and
  an **offline** half linting the wiring (all three `planTaskOps` call sites pass `ctx.isTagged`,
  never a literal). The lint alone: `TASKS_SELFTEST_OFFLINE=1 node scripts/tasks-addressed-selftest.mjs`.
  The acceptance run is `RUNS=3` — the fix is probabilistic.
- `ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs` — calls the **live** router against
  the real catalog and asserts that a *complaint* ("you scheduled that at the wrong time") is
  **filed as feedback, not executed as a calendar order**. Costs a few cents. Run it after any
  edit to `router/prompt.js` or to a skill manifest: every guard there is a prompt, and prompts
  regress silently.

---

## 10. Changelog (evolution log)

Reverse-chronological. Append a dated entry whenever the project meaningfully changes.

- **2026-07-15 — New Architecture: @mary's full isolated skill stack — all seven skills
  converted to pure tasks.** `@mary` now discovers and routes to its OWN skill tree,
  `secretary/3. Mary Skills/` — a byte-isolated copy of `2. Skills/` in which every skill is a
  **pure task**: `manifest.conversation:"orchestrator"`, declared `inputs`, and a `run(ctx)` that
  only validates → acts → sends ONE outcome → **RETURNS** a JSON value the orchestrator reads back.
  No new-tree skill imports `lib/confirm.js` or opens a session; the orchestrator model runs every
  propose/ask/confirm dialogue over `listen` turns. `calendar_action`, `task_action` and
  `flight_search` adopt a **READ-then-ACT** contract (a `find`/`list`/`search` READ returns
  id-bearing candidates the model reads back; a later ACT targets one by id). The calendar↔tasks
  `startCreate` coupling is **dropped in the new tree** (new calendar exports no
  `capabilities.startCreate`; new tasks makes no `ctx.callSkill` — a to-do for someone else is now
  the model chaining a `calendar_action` create). `transcribe_audio` stays `inputs:null`, and its
  `noAudio` branch is plain `ctx.send` (guidance, not a malfunction — the server.js:490 rule).
  **Two additive `server.js` rails changes:** (a) **per-flow discovery** — `loadSkills(dir =
  SKILLS_DIR)` is parametrized, a second `loadSkills(NEW_SKILLS_DIR)` builds `NEW_SKILLS`/
  `NEW_CATALOG`, and `NEW_FLOW.catalog` + the six NEW-loop refs repoint to them; `SKILLS`/
  `CATALOG`/`CAPS` stay on the OLD tree, so the legacy Tasks→Calendar `startCreate` delegation
  still resolves. (b) an **`inputs:null ⇒ dispatch-without-validation`** branch in the orchestrator
  dispatch gate, so `transcribe_audio` dispatches instead of repair-looping. **@assistant (the OLD
  `2. Skills/` tree + legacy flow) is byte-for-byte unchanged.** New offline test:
  `scripts/mary-skills-selftest.mjs`. The **live router check** against the new catalog
  (`ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs`, after repointing its hardcoded
  `2. Skills` path to `3. Mary Skills`) is the human's real-money gate — NOT run by the build. No
  new dependency, no new env, no Google scope change.
- **2026-07-15 — feat(calendar): create recurring events.** `calendar_action` **create** now
  extracts a **recurrence** (daily, weekly-by-day, an interval, a count, an until, and
  day-of-month monthly), carries it on the draft through gather/confirm/modify, states it in the
  confirm and done bubbles in words (en+pt via `describeRecurrence`), and — on "yes" — writes a
  real **RRULE** on `events.insert`. A new deterministic compile layer (`toRRule` /
  `toRRuleUntil`, skill.js, exported) is the single validator: COUNT-XOR-UNTIL, and an
  uncompilable / past-`until` recurrence degrades silently to a one-off. **All-day recurring** is
  supported with a value-type-correct `UNTIL` (DATE `YYYYMMDD` for all-day, datetime-Z for timed —
  RFC 5545). `recurrence` is the twelfth `CAL_SCHEMA`/`manifest.inputs.fields` field (T2.10 set
  updated; three create-payload fixtures + the stale "ELEVEN"→"TWELVE" comments moved in lockstep)
  and rides `REVIEW_SCHEMA` (`applyDraftUpdate` reads it directly so "just once" can clear it).
  **Create-only**: series edit/delete is unchanged and still single-instance (documented
  limitation; a future card). New offline test: `scripts/calendar-recurrence-selftest.mjs`. No
  rails change, no new dependency, no Google scope change.
- **2026-07-14 — The orchestrator holds the conversation (NEW flow), run in PARALLEL with the OLD
  flow, selected by summon tag (DEPLOYED to the droplet 2026-07-14 — `@assistant`/`@assistente` =
  OLD flow unchanged, `@mary` = NEW flow, both live; `SECRETARY_TAG_NEW="@mary"` added to compose;
  live router check deferred — the owner validates `@mary` by live test).** Two
  changes shipped together. (1) A new orchestrator **turn loop**: the model drives a three-state
  cycle — `listen` (ask / propose / stay silent), `execute` (run skill(s)), `done` (close) — and
  `execute` is non-terminal (a converted skill's return value drives a READ-BACK turn). `route(ctx,
  turn)` returns `{ say, next, skills, info, lang, awaitFrom }`; the loop enforces the caps
  (`MAX_TURNS=10`, `MAX_DISPATCHES=3`, `MAX_REPAIRS=2`), the write invariant (a read-back may not
  execute), silence-is-free, and a repair loop for validation failures. `assistant_settings` is the
  converted **pilot** (`manifest.conversation:"orchestrator"`, declares `inputs`); the other six
  skills declare `conversation:"skill"` (or default to it) and are unchanged. (2) **Dual-tag
  parallel run** so the owner can test the NEW system live without touching his real one:
  **`@assistant` (`SECRETARY_TAG`) → the OLD flow, byte-for-byte the committed behaviour**;
  **`@mary` (`SECRETARY_TAG_NEW`) → the NEW turn loop.** Both live in one running server, branched
  on the summon tag as early as possible in the webhook handler. The OLD flow runs entirely on
  **frozen copies** of the pre-card code under `secretary/1. Orchestrator/legacy/` (its own router,
  prompt, input-contract and the deleted propose/confirm `assistant_settings`) that the NEW flow
  never imports; the NEW flow's `assistant_settings` mutates a **separate** tag list (`NEW_TAGS` via
  `setNewTags`) persisted to a **separate** settings key (`secretary:settings:new:tags`). That
  structural separation is the invariant: **a bug anywhere in the `@mary` path is incapable of
  changing what `@assistant` does.** Also folded in: the **repair-prompt fix** — a repair turn now
  gets its own `buildRepairUser` prompt that INVITES a corrected execute (the read-back prompt that
  forbids executing was fighting the repair loop it was reused for). **Rails:** `server.js` (the
  turn loop + the dual-tag branch), `router/router.js` + `router/prompt.js` (three-state contract,
  `CONVERSATION:` catalog line, `buildReadbackUser`/`buildRepairUser`), `lib/inputs.js` (additive:
  scalar-`of`, `describeProblems`, the `CONVERSATION:` render), `lib/whatsapp.js` (additive
  `buildLabeledTranscript`), `lib/identity.js` (additive `NEW_TAGS`/`setNewTags`/`matchedTagNew`),
  `lib/settings.js` (additive `ns` namespace) — all authorized, additive except the caps/loop that
  are new code; **no existing signature changed and no existing caller altered.** Because
  `router/prompt.js` and every catalog entry changed, **a live `scripts/router-selftest.mjs` run is
  required** (human-gated, costs money) before this is trusted in production. Tests: a new
  `scripts/settings-selftest.mjs` (three-state cycle end-to-end + write invariant + caps + repair
  loop + a **dual-tag** assertion pair proving OLD and NEW run isolated in one server); the four
  offline suites stay green. **Deploy:** the owner sets `SECRETARY_TAG=@assistant` and
  `SECRETARY_TAG_NEW=@mary` and runs the usual `git pull` + `docker compose restart secretary`.

- **2026-07-14 — Feature specs & triaged bugfix plans land on the kanban backlog by themselves
  (BUILT; feature half awaits a droplet deploy).** The self-learning loop gained an end. The
  `feature_request` skill now spools every generated spec to `secretary/specs/` (timestamped
  filename + a `title`/`one_liner`/`when` frontmatter header) **before** the WhatsApp send, so a
  failed send never loses it; the attachment itself is byte-for-byte unchanged. The Mac's daily
  pull now pulls **two** spools independently (reports → `Bugs and Malfunctions/inbox/`, specs →
  `New Features Plans/`), and a new deterministic ingest (`scripts/board-ingest.mjs`: `seed` /
  `enqueue` / `drain`) turns each new spec, each triaged `bugfix-*.md`, and each **owner-reported**
  failure no plan claims into one card on the board's **backlog**, typed (`kind: feature|maintenance`)
  and unrouted — over the board's existing HTTP API, **without modifying the board** and at **zero
  LLM cost** (a valid `kind` skips the board's triage call). Staging lives in `Board Inbox/` (a
  `queue/`, a **tracked** `ledger.tsv`, a `delivered/` archive, a single-flight `.drain.lock`); the
  ledger is what makes it exactly-once and what stops anything predating this card from becoming a
  card. A launchd timer (`com.marcelo.board-ingest.plist`) drains every 5 min. Two new offline
  self-tests: `board-ingest-selftest.mjs` and `pull-archive-selftest.mjs`.
  **This also closed a pre-existing SILENT-DROP bug that had already shipped:** the old
  `self-learning-pull.sh:38` archived with a blind `mv *.md _synced/` **after** the rsync, so any
  report written into the droplet spool in the window between the transfer and the archive was moved
  out of the spool **having never been transferred** — destroyed, unreported. Latent for reports; it
  would have been on the happy path for specs. The restructured pull now captures the file list
  **before** the transfer and archives only those exact names (`xargs mv`, never a glob, never
  `rsync --remove-source-files`); a file that appears mid-pull stays in the spool and is pulled next
  run. `pull-archive-selftest.mjs` reproduces the bug against the live script and locks the fix.
  Rails: **none** — no `ctx` field, no `lib/` module, no `manifest.description` change (so no live
  router check, no API spend). **Deploy:** the spec spool is a droplet-side skill change and needs a
  `git pull` + `docker compose restart secretary`; until then the malfunction half runs and the
  feature half does not.
- **2026-07-13 — Editing an all-day event: move it, change its range, flip it to timed and back
  (SHIPPED, DEPLOYED — expedited card 64ff1f1d).** Closes the OPEN GAP card 0822a8e0 left behind.
  Reply to a biópsia invite with *"move a biópsia para quarta"* and the bubble reads **"15 de jul. de
  2026 · Dia todo"**; on `sim` the event lands on Wednesday, **still all-day**. *"na verdade vai até
  sexta"* → **"15 – 17 de jul. de 2026 · Dia todo (3 dias)"**. *"na verdade é o dia todo"* converts a
  timed event; *"na verdade é às 10h"* converts it back. **No more `(sem horário)`, no more
  `(1440 min)`.** Confirm-first is untouched: nothing reaches Google until he says "sim".
  **The edit path used to REFUSE the write** — the guard 0822a8e0 installed. An all-day event has no
  `start.dateTime`, so the draft had no day, and the only start shape `applyEditDraft` knew how to
  write was a `dateTime` one. The guard's **intent is honoured, not deleted**: an all-day draft is now
  written in the **all-day wire shape** (`start:{date}`/`end:{date}`, the end EXCLUSIVE), so there is
  nothing left to refuse.
  **⚠️ `events.UPDATE`, not `patch` — deliberately.** Clearing a nested `start.dateTime` through
  `patch` rests on Google's patch semantics, **which no offline test can prove** — a green suite would
  have meant nothing. A **full-resource replace** makes the half-converted event *structurally
  impossible*. Its one real cost: **what the body does not carry, Google CLEARS** — so the caller hands
  over the **freshly fetched** event and `updateEvent` spreads it (`colorId`, reminders, recurrence,
  sequence ride along). `resumeEditConfirm` already re-fetched the event, so this costs **no extra API
  call**. A `colorId` tripwire pins it.
  **⚠️ THE RULE, enforced in code and not in prompt hope: `new_all_day === false` is honoured ONLY
  alongside a `new_start_iso`.** `EDIT_SCHEMA` *requires* the field, so a model answering an ordinary
  **rename** can emit `false` rather than `null` — and a naive fold would then silently convert the
  owner's all-day event into a 45-minute block, *the exact harm the old guard existed to prevent,
  re-entering through the front door*. Turning all-day OFF means **giving the event a time**. Always.
  A rename-only tripwire pins it.
  Three shared helpers keep **exactly ONE place days are converted**: `allDayWireDates` (INCLUSIVE →
  EXCLUSIVE, now shared by create *and* edit), `normalizeAllDay` (the two clamps — a move that strands
  the old range end behind the new start now self-heals), and `allDayFromEvent` (the **read** direction
  — it returns the event's own day as `start_iso`, **without which a RENAME of an all-day event would
  reach the wire conversion with a null start and land the event in 1970**).
  New `scripts/calendar-edit-selftest.mjs` (offline, house style, 33 checks) pins the four writes on
  the wire; `turn-latency-selftest`'s stub gains `events.update` and its T3.3 now watches **the WRITE,
  not the verb** — same intent, and strictly *stricter*: a premature `events.update` can no longer slip
  past the "nothing before sim" assertion.
  **STILL OWED — the live check.** The suite pins the model's outputs, so it proves the **CODE** writes
  the right shape to Google. It **cannot** prove a live Claude reads *"na verdade é o dia todo"* as a
  change, nor that it picks the correct **INCLUSIVE** last day for *"até sexta"* (that off-by-one is
  the likeliest thing for the model to get wrong, and it is invisible offline). **That half is a human
  check in WhatsApp.**

- **2026-07-13 — the owner can change the tag he summons her with, by asking her (SHIPPED, DEPLOYED
  — expedited card 793566bd).** `@assistant, change your tag to @assist` → she **deduces**
  whether the other language's call should change too, **states the reasoning in prose** and the
  **complete** new tag list, and asks. On `yes` it is applied live and persisted; the old tags stop
  working. New skill `assistant_settings` (`2. Skills/7. Assistant Settings/`), new durable store
  `lib/settings.js` (Redis, key `secretary:settings:tags`, **no TTL**), `TAGS` made live in
  `lib/identity.js` (`setTags()` mutates in place; `normalizeTags()` is the shared validator).

  **`SECRETARY_TAG` is now the SEED, not the last word** — a stored list wins at boot, and the boot
  log names the source. So **a restart no longer reverts a tag change**: the store outlives it. The
  recovery path, if the owner ever locks himself out, is
  `docker exec evolution_redis redis-cli DEL secretary:settings:tags` + restart.

  **The prefix landmine, fixed on the way through.** `matchedTag()` was `TAGS.find(t =>
  low.startsWith(t))` — *first* match wins — and `server.js` slices the order by the matched tag's
  **length**. The moment the owner lands on `@assist` + `@assistente` (which this feature makes an
  ordinary thing to land on), `"@assistente marque uma reunião"` matched `@assist`, 7 chars came
  off, and the router was handed **`"ente marque uma reunião"`** — every Portuguese command silently
  corrupted, no error, no log line. `matchedTag()` now matches the **longest tag first** (sorting a
  **copy** — `TAGS[0]` is the primary tag `ctx.tag` falls back to) **and requires the tag to end at
  a word boundary**. The boundary half is not decoration: without it a *retired* tag that merely
  extends a live one keeps working — with `@assist` live, `"@assistant do X"` still starts with it
  and the router gets `"ant do X"`. A retired tag has to be gone, not half-working.

  **Two things she will never do.** She will not report a save she did not get: `saveTags()` returns
  true only on a real write, and the success message is sent **only** from that branch — if the
  store was unreachable she says the change is live but unsaved and will not survive a restart. And
  she will not apply anything on a maybe: `lib/confirm.js` returns `"unrelated"` on any doubt, which
  is a no-op. Guarded by `scripts/settings-tag-selftest.mjs` (apply→live→persist, offline) and
  assertion 9 of `scripts/identity-selftest.mjs` (the prefix trap).

  **Verified against the live router before it was switched on** (the offline suite structurally
  cannot: the router is a prompt). On the droplet, with the real model and the real catalog:
  `scripts/router-selftest.mjs` PASS (13/13 — the new manifest steals none of the existing orders),
  and `"change your tag to @assist"` / `"muda sua tag para @assist"` both route to
  `assistant_settings`, not to `feature_request` — which was the one way this feature could have
  shipped silently broken. Persistence proved on the box too: a stored list beat the `SECRETARY_TAG`
  seed across a restart (`tags: … (source: stored setting)`), and `redis-cli DEL
  secretary:settings:tags` + restart put it back on the seed.

- **2026-07-13 — The secretary got slow: a fresh calendar order took 16–23s to reply, as unbroken
  silence (SHIPPED, DEPLOYED — maintenance card 9af6967a).** It used to be ~6.5s. **Two causes,
  both in the SHARED request path** — which is why every skill regressed at once, and why the fix is
  a rails fix. This is a **cure**: both steps remove a cause. There is no retry, no timeout bump, no
  special case anywhere in it.

  **Cause 1 — nobody ever set `thinking`, and we threw the results away.** `claude-sonnet-5` runs
  extended thinking **on by default**, adaptively sized. None of the product's 16 `messages.create`
  call sites passed a `thinking` parameter, and both `readText()` and the router's inline reader
  keep only `text` blocks — so the model reasoned, we waited for it, we paid for it, and we deleted
  it. Measured: **~4.6s of every 16s turn.**
  **Fix:** `lib/llm.js` gains **`withThinkingDefault(client)`**, and `server.js` wraps its **one**
  Anthropic client in it (a `Proxy`, not a spread — the SDK client is a class instance). All 16 call
  sites inherit `thinking: {type:"disabled"}`, **and so does a skill written next month.** A caller
  that genuinely wants reasoning passes its own `thinking` and is left alone. Putting it at the one
  shared door is the whole point: a skill cannot forget to opt in.

  **Cause 2 — the router call and the extraction call were two round-trips reading the same
  transcript.** Per-turn latency is linear in the number of round-trips, and each is 4–8s. A create
  cost three (router → extract → clarify).
  **Fix:** they are now **ONE call**. Every skill **declares its inputs** (`manifest.inputs`); the
  router asks for the chosen skill's inputs in the same call that classifies the order; **plain code
  — no AI** — then validates the payload against the declaration (`lib/inputs.js`), and only if that
  check fails does the skill run its own clarification call. A valid payload reaches the skill as
  **`ctx.info`**. A fresh create is **1 call** (2 if something is genuinely missing), down from 3.

  **The reply format is demanded in the PROMPT, never via `output_config` — and that is a hard
  product constraint, not an optimization.** With `output_config` the orchestrator would have to
  import each skill's JSON Schema to build the merged one: **the router would then know what a
  calendar IS.** It must not. It concatenates each skill's declared inputs as opaque text and
  validates the reply against the declaration. Dropping `output_config` is worth ~0 seconds — it is
  an architectural choice and it costs us the API's shape guarantee (see the risk below).

  **Measured before/after** (median / p90 to first reply): today **19.7s / 28.3s** → thinking-off
  alone **12.0s / 15.8s** → both **8.5s / 11.3s**. **The order is safety-critical:** the merge with
  thinking still ON measures a **p90 of 41.5s — worse than the bug**, because the merged prompt
  looks harder and the model spends its thinking budget on it.
  **The tail is provider-side and this fix does not bound it.** The provider draws 10–44s on a single
  HTTP request, with zero retries, and nothing in our control removes that. Taking ~1.8 draws per turn
  instead of 3 shrinks our exposure to it; it does not cap it. No bounded worst case is promised.

  **The risk this creates, stated out loud:** `manifest.inputs` and `CAL_SCHEMA` are two hand-written
  lists that must stay in lockstep, and nothing in the language enforces it. Add a field to the schema
  and forget the declaration, and the merged prompt silently stops asking for it. **It already happened
  once** (`all_day`, one commit before this card). The mitigation is a static set-equality lint —
  `scripts/turn-latency-selftest.mjs` **T2.10**. **If a future card makes it red, update the
  declaration; never loosen the lint.** Deriving one list from the other is a follow-up card.
  Secondary risk: nothing but a prompt instruction now enforces the reply shape (0/132 unparseable
  when measured; ~4% leak prose and are recovered by the router's brace-scanner, which is therefore
  load-bearing). An unparseable reply degrades to "I didn't understand" **plus a self-learning
  report** — that existing path is the alarm. Watch `Bugs and Malfunctions/inbox/` for a week.

  **New:** `scripts/turn-latency-selftest.mjs` (offline, free — boots the real server and counts the
  round-trips before the first reply), `scripts/calendar-extraction-livetest.mjs` (live, opt-in — the
  accuracy bar, three arms, with a pre-declared STOP rule: *a faster, dumber assistant is a worse
  product*), `scripts/calendar-editdelete-livetest.mjs` (live, human-gated, zero attendees).
  **Not fixed here, and they are a different card:** **(a)** the wrong-recipient bug (the contact's own
  email attached to a *different* person); **(b)** *"na sexta"* does not reliably resolve to Friday.
  Both were measured failing **identically on production code** during build review, so the accuracy
  livetest is **red on both arms and exits 1 by design** — that red is the script working, and the way
  to clear it is to fix the product, never to weaken the expectations. **No improvement is claimed.** An earlier draft of this entry
  claimed "7/8 → 0/8" — that number came from a card-folder experiment and **did not reproduce at
  HEAD** under the build review's live probe. It has been removed rather than qualified, because a
  bogus improvement figure is exactly how the open wrong-recipient card gets closed without a fix.
  **The bug stays open and unmeasured by this card.**

- **2026-07-13 — All-day events on the calendar skill, single day AND multi-day ranges (SHIPPED,
  DEPLOYED — card 0822a8e0).** *"agendar amanhã o dia inteiro biópsia laura"* now produces a **real
  Google all-day event** — the one in the strip at the top of the day — instead of a **24-hour timed
  block starting at midnight**. Ranges work too: *"de segunda a quarta o dia todo"* is ONE all-day
  event covering all three days.
  **The intent layer had no field for it.** `CAL_SCHEMA` (and `REVIEW_SCHEMA`, so *"na verdade, o dia
  todo"* works at the confirm step) gains **`all_day`** and **`all_day_end_iso`** — the latter being
  the **LAST day the event still COVERS, INCLUSIVE**. The model, the draft, the confirm bubble and
  `SKILL.md` all speak **inclusive** days.
  **⚠️ Google's `end.date` is EXCLUSIVE, and that conversion lives in EXACTLY ONE place** —
  `createFromDraft` (`end_date = addDays(last_date, 1)`). A single day on 2026-07-14 is `start.date
  2026-07-14` / `end.date 2026-07-15`; **Mon 13 → Wed 15 is `start.date 2026-07-13` / `end.date
  2026-07-16` — a THURSDAY.** Off by one is a 2-day event, or a zero-day one Google rejects. Both
  shapes are pinned by assertions on the exact payload Google receives, not by a comment.
  **`start_iso` stays REQUIRED** — the day is *derived* from it in `CAL_TZ` — so `missingOf().noTime`
  still guards the null-start → 1970 write, and gathering is untouched (`RESOLVE_SCHEMA` gains
  nothing; the flags are carried across a merge by `mergeDraft`, without which they would be silently
  dropped). The confirm bubble reads **"13 de jul. de 2026 – 15 de jul. de 2026 · Dia todo (3 dias)"**
  — both endpoints **plus the day count**, which is the owner's sanity check against an off-by-one
  range *before* he says "sim". Two clamps, both silent and both visible in that bubble: a backwards
  range collapses to a single day, and a span over `MAX_ALL_DAY_DAYS` (31) is clamped.
  **`findConfirmedDuplicates` was blind to all-day events** (it filters on `e.start?.dateTime`), so
  dedupe-on-create would have silently stopped working for exactly the events this card adds. It now
  matches **both** `start.date` and `end.date` — matching only the start would dedupe a Mon–Wed order
  against a Monday-only event. The delete sweep (its other caller) passes no flag → falsy → today's
  exact behaviour.
  **The edit guard (corruption prevention — it does NOT make all-day events editable).**
  `applyEditDraft` was guarded only by `if (draft.start_iso)`, so *"move a biópsia pra quarta"* would
  patch a `dateTime` start over an all-day event and **silently convert it into a 45-minute block**.
  The guard is now `if (draft.start_iso && !draft.all_day)`: the event is still renamed / re-invited,
  it is simply **not MOVED**. **Rescheduling an all-day event remains an OPEN GAP, deliberately — a
  separate card.** → **CLOSED 2026-07-13 by card 64ff1f1d** (top of this changelog): the guard's write
  now exists, so the refusal is gone. The `if (draft.start_iso && !draft.all_day)` line described here
  no longer exists in the code.
  Skill-local: **no rails, no orchestrator, no `manifest.description`, no router change** — so no
  live router check was owed and no money was spent. `scripts/calendar-create-selftest.mjs` gains
  scenario **`g`** (g1–g6, two drives: single day + range) and the **`a7` tripwire** (a timed event
  stays TIMED — `start.dateTime` present, `start.date` absent), 40 assertions green, Google never
  contacted.
  **STILL OWED — the live check.** The suite pins the model's outputs, so it proves the CODE writes a
  real all-day event when told `all_day: true`. It **cannot** prove a live Claude *says* `all_day:
  true` for "o dia inteiro", nor that it picks the correct **inclusive last day** for "de segunda a
  quarta" — that off-by-one is the single most likely thing for the model to get wrong, and it is
  invisible offline.

- **2026-07-13 — Calendar create: "nobody", "I don't have their email" and "forget it" are now
  ANSWERS (card 33bb6637).** The create flow had three required states that a truthful answer could
  not satisfy. `missingOf`'s **`noAttendees` invariant is gone** — a calendar event has **0–n outside
  guests, and zero is an ordinary event**. The email requirement is **not** gone but is now
  *answerable*: `RESOLVE_SCHEMA` gains `no_email_for[]`, so *"não tenho o e-mail dela"* creates the
  event **without** that guest **and says so** (*"criei sem convidar a Laura"*) — never a silent drop.
  And the gathering loop's `if (sameMissing(before, after)) return;`, which inferred *"was that
  message for me?"* from a field diff and answered an untagged correction with **total silence**, is
  replaced by the same `confirm|modify|cancel|unrelated` decision channel `flight_search` already uses
  while gathering. `await_info` therefore has a **cancel branch for the first time**, and an Anthropic
  error during gathering is now reported instead of swallowed. `mergeDraft` and `applyDraftUpdate` now
  treat the resolver's guest list as **authoritative** (an empty list is an answer, not an absence) —
  which also ends a separate outward harm: *"não é a Laura, é a Ana"* used to invite **both**.
  Skill-local: **no rails, no manifest, no router change.** New: `scripts/calendar-create-selftest.mjs`.

- **2026-07-13 — Name change: "secretária" → "assistente" (SHIPPED — committed 2026-07-13;
  NOT yet deployed to the droplet).** The trigger tag is now **`@assistente`/`@assistant`**. The old
  `@secretaria`/`@secretary` pair **stops working — no alias, no grace period**: `matchedTag()`
  returns `null` for it and nothing starts. The reply header is now **`[Assistente IA do Marcelo]:`
  / `[Marcelo's AI Assistant]:`**. Six value edits, one new self-test; no signature changed, no
  caller edited.
  **Both old headers live on in `LEGACY_HEADERS` forever**, as `${OWNER}`-interpolated template
  literals, not hardcoded strings. This is the safety line and it is not optional: every bot message
  already sitting in WhatsApp history carries an old header, and `5. Feedback/skill.js:94` uses
  `isOwnMessage()` on the *quoted* message to tell "the bot did this wrong" from "here is some
  context". Drop those entries and the feedback skill goes **silently** blind to the entire
  back-catalogue. **The tag is retired from matching; the header is retired from *sending* but kept
  forever in *recognition* — two lists, opposite answers.** `scripts/identity-selftest.mjs`
  assertion `4a` is what stands between them and a future "tidy-up".
  **THE DEPLOY TRAP — read before deploying.** The value production actually runs is baked into
  **`/opt/evolution/docker-compose.yml:58`** on the droplet — a hand-maintained file that is **not
  in git**, so `git pull` does not touch it. Compose's `environment:` beats `env_file`, and
  `identity.js` reads `process.env.SECRETARY_TAG || "<default>"` — so **the live value wins and the
  new code default is never reached**. A deploy that only pulls the repo ships a bot that **replies
  as "Assistente" but still answers only to "@secretaria"**. It will look like the build is broken;
  it isn't. The deploy is: `git pull`, then **hand-edit** that line to
  `SECRETARY_TAG: "@assistente,@assistant"`, then from `/opt/evolution` run
  `docker compose up -d --force-recreate secretary` — **`--force-recreate`, NOT `restart`**: the
  `environment:` block is baked in at container *creation* and `restart` will not pick up the new
  value. Verify with `docker exec secretary printenv SECRETARY_TAG`. `EVOLUTION_INSTANCE: secretaria`
  (line 57) **stays as it is** — renaming the instance means re-scanning the QR and re-registering
  the webhook. The `SECRETARY_TAG` variable *name*, the container, `/opt/secretary` and the Redis
  prefix are all likewise unchanged: this card renames what the owner types and what the bot signs
  itself, nothing infrastructural.
- **2026-07-13 — Flight search via chat (`flight_search`) — the sixth skill (SHIPPED — committed
  2026-07-13; NOT yet deployed to the droplet).** Ask in a sentence (`@secretary find me a flight from São Paulo to Lisbon on the
  14th, back on the 22nd`); it asks for anything missing one field at a time, **confirms before it
  searches**, then shows the **3 cheapest options** and, on a follow-up turn (`link for option 2`,
  tagged or untagged), sends that option's booking link. **It never buys** — "book it" gets the
  link *and* a plain statement that the purchase is the owner's to make.
  **Provider: Kiwi's keyless MCP endpoint** (`https://mcp.kiwi.com` — no API key, no account, no
  handshake; a plain `fetch`, no new npm dependency). Full wire contract in §8: **CRLF-framed SSE**,
  `dd/mm/yyyy` dates, `cabinClass M|W|C|F`, and `isError: true` arriving on an **HTTP 200** with a
  plain non-JSON body.
  **Why there is a client-side result filter, and why it is the point of the card.** Kiwi is a
  virtual-interlining OTA and its API has **no max-stops and no self-transfer parameter** — so the
  filter cannot live at the API. On a real SAO→LIS capture, **the four cheapest results were all
  self-transfer carrier chains** (separate tickets; a missed connection is the passenger's problem).
  A naive "3 cheapest" would have put exactly that in front of the owner. So the skill **drops
  >1-stop and carrier-chained itineraries — judging BOTH legs — and only THEN sorts and takes 3**.
  The filter is deliberately over-strict (it also drops legitimate single-ticket alliance
  connections): **when in doubt, drop.** It shows fewer than three options when that is the truth,
  and says why. **Kiwi's results are VOLATILE** (four identical queries → four disjoint sets), so
  the filter's bite varies wildly between identical searches — that is expected, not a bug (§8).
  **The options live in a sidecar session key** (`` `${remoteJid}|flights` ``), because a *tagged*
  follow-up makes the orchestrator clear the chat session (`server.js:402`) before the router has
  decided whose order it is. Three states, three different facts: **options** → the link;
  **tombstone** → *"I dropped those when the new search started"*; **absent** → *"I have no options
  on hand"*. **A new search writes the tombstone at FLOW START** — before the slot chase, before the
  confirm, before any Kiwi call — so a search that finds nothing can never leave the *previous*
  search's booking links addressable. **No reply ever claims a search "expired"**: the skill cannot
  distinguish an expiry from "already sent" or "never searched", so it says only what it knows.
  New: `secretary/2. Skills/6. Flight Search/{skill,prompt}.js` + `SKILL.md`, and
  `scripts/flights-selftest.mjs` (65 offline assertions; **its Kiwi fixtures are frozen — never
  regenerate them**). `scripts/router-selftest.mjs` gained a per-case `transcript` and the four
  flight cases. One new env var, **`FLIGHT_CURRENCY`** (default `BRL`; there is no provider key).
  **Zero rails changes** — no `server.js`, no `router/`, no `lib/`, no `package.json`.
  **Shipped over a `DO NOT SHIP` review verdict, by explicit owner override.** The build review's
  only blocker was that the **mandatory live router check** (`CONVENTIONS.md` §1: any new/changed
  `manifest.description` requires `ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs`) **was
  never run** — there was no usable API key. The owner overrode it (*"ship this into production, I
  will test it live"*) and accepted the risk: **if the new manifest makes the router misroute, the
  owner gets a wrong reply AND a false bug ticket is filed on his own Bugs board** — `server.js:420-428`
  answers `notUnderstood` *and* calls `fireCapture(phase:"unrouted")`. The riskiest case is
  `link for option 2` → `other`. **The check is still owed**: the four flight cases are already in
  `scripts/router-selftest.mjs` and it costs real money to run. Offline: 65/65 green.
  Plan archived to `Shipped Features/2026-07-13 - flight-search-via-chat.md`.

- **2026-07-12 — Kanban: the open card is lit on the board.**
  Opening a card's chat now gives that card an accent-blue **background** on the board (not just an
  outline), so you can see at a glance which card you are talking about. The card's drawer carries a
  **matching** title block — and the two hexes are deliberately **different**: the card sits *under*
  the `.4` scrim and the drawer *above* it, so the drawer takes the **pre-composited** value
  (`--card-open-seen` = `0.6 × --card-open`) in order to *look* like the same colour. They are a
  **pair**: change one and you must recompute the other. The chips inside `#d-card .dh` were
  restyled — scoped to that drawer only — because accent-blue text on an accent-blue field is
  unreadable; the other four drawers are visually untouched. The highlight is **derived at render
  time** (`renderBoards()` rebuilds every card) and repainted directly on open/close, because
  `closeAll()` does not re-render. New `scripts/card-highlight-selftest.mjs` guards the hex pairing,
  the `#d-card` scoping and the four JS touchpoints; `AI Coding-kanban/tests/ui_test.py` proves in
  real Chrome that exactly one card is lit and that the tint actually applied.

- **2026-07-12 — Tasks: the planner now knows whether it was addressed (BUGFIX).**
  The engaged window keeps listening for 10 minutes after a task exchange, and a continuation is
  **never** tagged — so the planner read *"amanha vou tentar implementar o tenente dentro do
  VsCode"* (the owner talking **to Tony, about Tony's project**) as an order, and silently wrote a
  phantom task to the owner's real Google Tasks list. Thirteen seconds later *"e mandar ele ter
  workers"* was read as an **edit** to the phantom. The planner had no way to know it had not been
  addressed: `ctx.tag` falls back to `TAGS[0]` and is always truthy.
  (`Bugs and Malfunctions/bugfix-task-false-positive.md`.)
  **The fix, in three parts.** (1) **Rails, additive:** `server.js` puts **`isTagged`** on `ctx` —
  the bit was computed and then thrown away before any skill could see it. (2) `planTaskOps` takes
  a **required** `{ addressed }`, and **all three** call sites (`run`, `resumeConfirm`'s
  "unrelated" re-plan, `resumeEngaged`) pass **`ctx.isTagged`** — never a literal, which the
  selftest lints for: a hardcoded `addressed: true` would sail through the live half and silently
  restore the bug. (3) The planner gains a **second posture** for the untagged case. It **asks**
  whether the message was aimed at the secretary — it never **asserts** that it wasn't, because
  *every genuine in-window follow-up is untagged too*, and an asserting prompt would quietly gut
  the shipped window. The bar is uniform across `ops` **and** `list_requested` (reading the list
  aloud would print the owner's to-dos into a third party's chat), with **`owner_done` exempt** —
  it only closes the window. Overheard talk now produces **silence**: no ops, no reply, no re-arm.
  **New self-test:** `scripts/tasks-addressed-selftest.mjs` — a live half (the real logged
  transcript; the two directions) plus an offline wiring lint.
  **Residual risk:** the fix is **probabilistic** — a prompt, not a guarantee. It cannot make a
  false positive impossible. If one recurs, the escalation is **confirm-first on untagged
  creates**, accepting that a confirmation interrupts the owner's conversation with a third party
  (which is why it was not chosen now). **Not fixed here, on purpose:** a *read-only* "what tasks
  do I have?" still arms a 10-minute **write** window — its own card, because un-arming it would
  break "what's on my list? … ok, add milk".

- **2026-07-12 — Self-learning: the secretary reports its own failures (SHIPPED, DEPLOYED).**
  Deployed 2026-07-12 (git pull + `docker compose restart secretary`; boot clean, Redis connected,
  all **five** skills loaded incl. `feedback`). Live behaviour still to be confirmed in the chat —
  see the checklist below.
  New `1. Orchestrator/lib/logbuffer.js` (redacted in-memory log ring) + `lib/selflearning.js`
  (`captureFailure` → a Markdown report in `secretary/improvements/`), wired into the three catch
  blocks, the `notUnderstood` branch, and a soft-failure scan in the `ctx.send` wrapper.
  **Plus a fifth trigger no amount of try/catch could ever provide: the new `feedback` skill.**
  Reply to a wrong message with `@secretary you made a mistake here` and the complaint is filed
  as a human-verified bug report — the only way a *false positive* or a confidently-wrong answer
  ever enters the loop, since the code doesn't know it failed. Say "…and fix it to 5pm" and the
  router returns **both** tasks: file the defect *and* do the fix.
  Reports sync to the Mac (`scripts/self-learning-pull.sh`, pull-based — the droplet's deploy key
  is read-only) and `/triage-failures` turns each into a plan.
  Three things the build got right only because they were checked against the code first:
  `ctx._turn` is an **object** (a boolean would be lost to `callSkill`'s ctx spread); the
  `feedback` skill **writes the report before it asks its clarifying question** (a new tagged
  order clears the session, so asking first would silently lose the complaint); and reports use
  an **exclusive-create write** (a one-second filename stamp meant two notes in the same second
  overwrote each other — caught by the self-test). Owner reports are exempt from dedupe and the
  hourly cap by design. Self-tests: `scripts/selflearning-selftest.mjs` (24 checks, offline,
  green) and `scripts/router-selftest.mjs` (live router; **not yet run — no local API key**).
  Plan: `New Features Plans/self-learning-skill.md`.

- **2026-07-11 — Bold header + italic body on every secretary message (SHIPPED, DEPLOYED, verified live).**
  Deployed to production 2026-07-11 (git pull + `docker compose restart secretary`; boot clean, all
  four skills loaded) and **confirmed working in production** the same day — including the two
  regression guards: the calendar link's `eid` survives framing (reply-to-invite cancel still
  resolves) and the bot does not re-consume its own bolded reply as an owner continuation.
  The secretary replies from the owner's own WhatsApp account, so its messages sit in the same
  thread as the owner's typing, in the same plain text. Now every outgoing message is framed
  **bold header** + blank line + *italic body*, so the two voices are visually distinct. New
  `1. Orchestrator/lib/format.js` exports `frame(header, body, {italic})`; `send()` applies it
  once, at the send boundary — **no skill reply string changed** (this is presentation only).
  Three constraints drove the design: (1) WhatsApp italics **do not span newlines**, so the body
  is wrapped **line by line**, with a leading bullet/indent kept outside the markers
  (`- _Buy milk_`); (2) lines carrying a **URL** or an existing `_ * ~` are left **plain** —
  a trailing `_` is a valid base64url char and would be swallowed into a calendar link's `eid`
  by `findCalendarLink`, silently breaking the reply-to-invite edit/delete flow (emails like
  `bruno_x@…` and verbatim task titles are the other carve-out); (3) markers are applied **after**
  `localizeBody()`, so the translation model never sees them. `isOwnMessage()` now strips leading
  `* _ ~` before matching — the bolded header would otherwise fail its `startsWith` check and the
  bot would read its **own** replies as owner continuations (the one change that could loop it);
  it still recognizes the unbolded headers sitting in chat history. Feature Requests' `sendMedia`
  caption bypasses `send()`, so it calls `frame()` itself. The audio **transcript** is sent with
  `{ italic: false }` — that text is the owner's own words quoted back, not the secretary
  speaking. Plan archived to `Shipped Features/2026-07-11 - feature-italic-secretary-messages.md`.
- **2026-07-11 — Calendar read/list action SHIPPED (DEPLOYED).** Added a fourth, **read-only**
  action to `calendar_action`: the owner can ask what's scheduled ("what's on my calendar
  tomorrow?", "do I have anything Friday afternoon?", "what's my next meeting?") and get an
  immediate, time-ordered, localized (en/pt) reply. Unlike create/edit/delete it is
  **stateless** — no session, no confirm, no write. `CAL_SCHEMA` gained `"list"` plus
  `list_mode` (`"window"` | `"next"`) and `range_start_iso`/`range_end_iso` (all `null` for the
  other actions); `handleList` resolves the window (`list_mode:"next"` → forward-scan to the
  first upcoming event; `"window"` → the LLM's range, defaulting to the rest of today via
  `endOfLocalDay`), fetches with `events.list` (`singleEvents:true`, `orderBy:startTime`,
  confirmed-only, capped at 50) and renders per-day time-only or multi-day full-date lines,
  handling all-day events and an empty state. **Entirely inside the skill folder** — no
  orchestrator/`server.js` change (uses `Date.now()`, not a ctx clock); the router routes it
  via the updated `manifest.description`. Plan archived to
  `Shipped Features/2026-07-11 - feature-calendar-read-query.md`.
- **2026-07-11 — Tasks: batch + edit + stateful, on one resolver (SHIPPED, DEPLOYED, verified live).**
  Deployed to production 2026-07-11 (git pull + `docker compose restart secretary`; boot clean,
  `task_action (capabilities: list)` loaded) and **confirmed working in production** the same day
  (batch complete, edit/delete, disambiguation, untagged follow-ups).
  Reworked `task_action` from a single-item inbox into one that handles **any number of tasks
  per message** and **edits/deletes tasks already on file**, all through a single list-aware
  planner. `planTaskOps` (schema `PLAN_SCHEMA` in `prompt.js`) reads the conversation *and* the
  numbered open list and returns `{ list_requested, owner_done, ops[] }` — one op per distinct
  task (`create|complete|edit|delete`), with list-aware matching (`target_index` /
  `candidate_indices`). This replaces the old `interpret` + `resolveTaskRef` (and retires
  `reviewAdd`), fixing the production bug where "mark A and B done" collapsed to one ref and got
  lost. `dispatchPlan` routes ops: self-creates write immediately (batch); complete/edit/delete
  of stored tasks share **one confirm session** (`resumeConfirm`, per-item ok/fail, unmatched
  refs surfaced not dropped); an edit/delete of a just-touched task is frictionless (the old
  amend window, now an op). New **stateful `engaged` window** (`armEngaged`/`resumeEngaged`,
  TTL 600) keeps follow-ups **tag-free** — verified the orchestrator's generic continuation
  (dispatches by `skill`+`awaitFrom`) needs **no router change**. Third-party reminders remain
  capped at 1/message (a serial queue is out of scope under one-session-per-chat). Reply layer
  refactored to a single `makeReply(vocabulary)` render for en+pt. Files: `secretary/2. Skills/
  3. Tasks/{prompt.js,skill.js,SKILL.md}`. Plan: `New Features Plans/task-improvements.md`.
- **2026-07-11 — AI Brain → AI Secretary rename SHIPPED (DEPLOYED).** Shipped in two layers.
  **Layer 1 (deployed first)** introduced `secretary/1. Orchestrator/lib/identity.js` as the
  single source of truth for trigger tags + the reply header: `SECRETARY_TAG` is now
  **comma-separated** (`@secretaria,@secretary` both trigger; the legacy `@brain` is **retired**
  and silently ignored), the header is **language-aware** (`headerFor(lang)`:
  `[Marcelo's AI Secretary]:` / `[Secretaria IA do Marcelo]:`, owner name from `OWNER_NAME`),
  and self-message detection (`isOwnMessage`, matching all header variants incl. the legacy
  `[AI Brain]:`) replaced the old `isBrainMsg`/fixed-header check. **Layer 2 (this change)**
  removed the "Brain" name everywhere: the `brain/` folder → `secretary/` (git mv), the Redis
  session prefix `brain:session:` → `secretary:session:`, the Docker service/container `brain`
  → `secretary` and host mount `/opt/brain` → `/opt/secretary`, plus all comments/banners/docs.
  Note the live compose that actually runs the stack is the hand-maintained
  `/opt/evolution/docker-compose.yml` (drifted from the repo copy; `EVOLUTION_INSTANCE: secretaria`).
- **2026-07-11 — calendar edit/reschedule SHIPPED (DEPLOYED + verified).** Phase B is done
  and confirmed working in production. Final design is **confirm-first + stays open**: an
  unambiguous edit no longer applies immediately (the first cut did, which meant a *second*
  change had to re-tag `@brain`) — the change is folded into a draft of the event's target
  state, shown for confirmation, and written to Google only on `yes`; the confirm session
  stays open so further changes ("actually 4:30", "also add bruno@x.com") land tagless.
  Reuses create's confirm/modify machinery (`EDIT_REVIEW_SCHEMA`, `reviewEdit`,
  `openEditConfirm`, `applyEditDraft`, `resumeEditClarify` / `resumeEditConfirm`). Commits
  `8036a4b` (initial) → `55891fe` (confirm-first rework), deployed `96850ce`. Plan promoted
  to `Shipped Features/2026-07-11 - calendar-edit-reschedule.md`; behavior in the skill's
  `SKILL.md`. (Supersedes the two 2026-07-11 edit entries below.)
- **2026-07-11 — calendar edit/reschedule, Phase B (initial cut — superseded above).** Reply
  to an event's invite with a change and `@brain` — "move it to 4pm", "make it 30 min",
  "add carlos@x.com", "remove ana@x.com", "rename to Kickoff". `interpret` gains
  `action:"edit"` (classify only); a focused pass (`interpretEdit` / `buildEditSystem` /
  `EDIT_SCHEMA`) reads the request against the event's real state and returns a structured
  patch or a `clarify` question. `applyEdit` builds a minimal `events.patch({sendUpdates:
  "all"})`, carrying current start/duration for correlated time fields and merging
  attendees; ambiguous asks open an `await_clarification` session (`awaitFrom:"owner"`)
  resumed by `resumeEdit`. Not confirm-first (edit isn't destructive). Shipped to
  production the same day (`git pull` + restart). **Not yet marked done** — pending a live
  test. Docs: `2. Skills/1. Calendar Actions/SKILL.md`,
  `New Features Plans/calendar-actions.md`.
- **2026-07-11 — feature-request skill (DEPLOYED).** New `feature_request`
  skill (`2. Skills/4. Feature Requests/`): the owner messages himself a feature idea
  (`@brain I have a feature idea…`); the brain becomes **stateful and interviews him** —
  one structured `clarifyTurn` per message (`CLARIFY_SCHEMA`) that folds each answer into a
  running `draft`, decides `clarifying`/`finalize`/`cancel`, and generates the next
  question in `ctx.lang`. On a done-signal it renders a Markdown feature spec (from the
  user's POV) and delivers it as a real `.md` **document** via the new
  `evolution.sendMedia` (`POST /message/sendMedia`, base64). The conversation follows
  `ctx.lang` but the **document is always English** (destined for the codebase) — a
  documented exception to the localization convention. No new env, no OAuth scope, no
  orchestrator/router edit; only shared change is the additive `sendMedia` on the Evolution
  client. **Shipped WITHOUT probing the `/message/sendMedia` contract** (owner's call) — if
  the field names differ on the running image the conversation still works and `finalize`
  replies `sendFailed()` + logs the HTTP error; verify from logs / fix forward (see §8).
  Docs: `2. Skills/4. Feature Requests/SKILL.md`, ARCHITECTURE flow step 8b + localization
  exception, ORCHESTRATOR external-touchpoints note.
- **2026-07-11 — task capture skill + cross-skill capability registry (DEPLOYED).** New
  `task_action` skill (`2. Skills/3. Tasks/`): a to-do inbox backed by
  Google Tasks. A to-do for **yourself** is created immediately in Google Tasks, then a
  short **amend window** (session) lets you correct/delete it with no confirm step; **list**
  reads open tasks; **complete** is confirm-first. A to-do for **someone else** is delegated
  to `calendar_action` as a 5-min invite (15:00 on the due date) so they're emailed — via a
  new **capability registry**: skills may export a `capabilities` object, and the orchestrator
  injects `ctx.hasSkill`/`ctx.callSkill` (auto-injects ctx, `MAX_SKILL_DEPTH` loop guard) so
  skills compose without importing each other's files. Calendar exposes
  `capabilities.startCreate`. en+pt localized per the convention. **OAuth:** the
  `GOOGLE_REFRESH_TOKEN` was re-minted with the `tasks` scope added alongside `calendar`
  (see §8); the previously-missing `/opt/brain/.env` was also restored from the running
  container (backup at `/root/brain.env.bak`) — a latent break on any future recreate.
  Reply layout: `Added to your list:\n<dd/mmm> - <title>\n\nTell me if you need something to
  change…`; list as `Here are your open tasks:` + one `<dd/mmm> - <title>` per line
  (`localizeDueDate` → localized `dd/mmm`). Self add/list/complete verified in production.
  Known gaps (next): no batch create/complete, no editing an existing task — see
  `New Features Plans/task-improvements.md`. Docs: `2. Skills/3. Tasks/SKILL.md`,
  ARCHITECTURE "Composing skills", ORCHESTRATOR registry note.
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
