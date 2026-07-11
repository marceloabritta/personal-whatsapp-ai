# Architecture & data flow

What is sent to each service, with what content, as the system runs. This replaces
the original personal handover document; it describes the current version
(`brain/`: orchestrator + skills).

## Components

Four containers on one host, talking over the internal Docker network `evolution-net`:

| Container            | Image                                | Port          | Role |
|----------------------|--------------------------------------|---------------|------|
| `evolution_api`      | `evoapicloud/evolution-api:latest`   | `8080` public | WhatsApp gateway |
| `evolution_postgres` | `postgres:15`                        | internal 5432 | Evolution database |
| `evolution_redis`    | `redis:latest`                       | internal 6379 | Evolution cache + brain session store |
| `brain`              | `node:20-alpine`                     | internal 3000 | The AI app (orchestrator + skills) |

Only `8080` is published to the internet.

## Flow

```
webhook  ->  filter (start on fromMe + @brain, or continue an active session)  ->  build context  ->  ROUTER  ->  SKILL(s)
```

### 1. Evolution → brain (incoming webhook)

Configured once via `POST /webhook/set/secretary`. On every message Evolution sends:

```
POST http://brain:3000/webhook
```
Body (`MESSAGES_UPSERT`), example:
```json
{
  "event": "messages.upsert",
  "instance": "secretary",
  "data": {
    "key": { "remoteJid": "5531999...@s.whatsapp.net", "fromMe": true, "id": "3EB0..." },
    "pushName": "User",
    "message": { "conversation": "@brain schedule..." },
    "messageType": "conversation",
    "messageTimestamp": 1751560000
  }
}
```
The brain **buffers every message** (for context). A flow only **starts** when `fromMe === true`
**and** the text starts with `@brain`. But the brain is **stateful** — it keeps per-chat state
in Redis (see `1. Orchestrator/lib/sessions.js`) — so once a session is active it can **continue
without the tag**: the brain uses the LLM to ignore normal chatter and watch for the awaited
answer (a confirmation or clarification). That continuation can also come from the **other person**
in the chat (e.g. they reply with their email), so a non-owner message can be a valid continuation
of an active session. Dedup by `key.id`. Messages that are neither a trigger nor a continuation
pass through but are discarded and never sent to any external API.

### 2. brain → Evolution (fetch history)

```
POST http://api:8080/chat/findMessages/secretary
apikey: <AUTHENTICATION_API_KEY>
```
Body: `{ "where": { "key": { "remoteJid": "..." } } }`. The brain merges this with its
in-memory buffer, dedups, sorts by time and builds a transcript of the last ~30
messages as `ME: ...` / `OTHER: ...`.

### 3. brain → Claude (router)

```
POST https://api.anthropic.com/v1/messages   (via @anthropic-ai/sdk)
```
Sent: the router system prompt (the live skill catalog) plus a user message with the
order, the transcript and whether a quoted audio is present. The router is
schema-enforced (`output_config.format` with `ROUTER_SCHEMA`) and returns:
```json
{ "tasks": ["calendar_action"], "lang": "pt", "reason": "..." }
```
`lang` is the detected conversation language (ISO code; default `"en"`) — it rides in
`ctx.lang` so the whole system replies in that language (see the localization note under
"Adding a skill"). Only the content of that one conversation leaves for Anthropic, and only
at that moment.

### 4. brain → Claude (skill: calendar_action)

A second call, with the calendar skill's own prompt, extracts:
```json
{
  "action": "create",
  "participants": [ { "name": "Alex", "email": "alex@example.com" } ],
  "start_iso": "2026-07-04T14:00:00-03:00",
  "duration_min": null,
  "missing": [],
  "summary": "Meeting with Alex, tomorrow 2pm."
}
```
`action` is `"create"` or `"delete"` — the skill can create a new event or cancel/delete an
existing one (edit/reschedule is planned, not yet built).

### 5. skill → Google Calendar (create or cancel/delete event)

OAuth (Client ID + Secret + Refresh Token); the brain exchanges the refresh token for
an access token automatically.
```
POST   https://www.googleapis.com/calendar/v3/calendars/primary/events?sendUpdates=all   (create)
DELETE https://www.googleapis.com/calendar/v3/calendars/primary/events/{eventId}?sendUpdates=all   (cancel/delete)
```
`sendUpdates=all` makes Google send the invite (or cancellation) email to the attendees from your account.

### 5b. skill → Google Tasks (add / list / complete) — task_action

Same OAuth client as Calendar (the refresh token must also carry the
`https://www.googleapis.com/auth/tasks` scope). The Tasks list defaults to `@default`
(override with `GOOGLE_TASKLIST_ID`).
```
POST   https://tasks.googleapis.com/tasks/v1/lists/@default/tasks              (add)
GET    https://tasks.googleapis.com/tasks/v1/lists/@default/tasks?showCompleted=false  (list)
PATCH  https://tasks.googleapis.com/tasks/v1/lists/@default/tasks/{taskId}     (complete: status=completed; or amend title/due)
DELETE https://tasks.googleapis.com/tasks/v1/lists/@default/tasks/{taskId}     (amend-window "cancel that")
```
`due` is **date-only** (stored at UTC midnight). A to-do for **yourself** lands here; a
to-do assigned to **someone else** has no private-list equivalent (Tasks emails no one),
so `task_action` **delegates** to `calendar_action` (step 5) via the capability registry —
a 5-min invite that notifies them by email. See "Composing skills" below.

### 6. skill → Evolution (fetch audio) — transcribe_audio

When you reply to a voice message, the brain reads `contextInfo.stanzaId` (the quoted
message id) and downloads the decrypted bytes:
```
POST http://api:8080/chat/getBase64FromMediaMessage/secretary
apikey: <AUTHENTICATION_API_KEY>
Body: { "message": { "key": { "id": "<stanzaId>" } }, "convertToMp4": false }
```
Returns `{ base64, mimetype }`. (Requires `DATABASE_SAVE_DATA_NEW_MESSAGE=true`.)

### 7. skill → AssemblyAI (transcribe)

```
POST https://api.assemblyai.com/v2/upload            (raw audio bytes)  -> { upload_url }
POST https://api.assemblyai.com/v2/transcript        { audio_url, language_code } -> { id }
GET  https://api.assemblyai.com/v2/transcript/{id}   (poll until status=completed) -> { text }
```

### 8. skill → Evolution (reply to you)

```
POST http://api:8080/message/sendText/secretary
apikey: <AUTHENTICATION_API_KEY>
Body: { "number": "5531999...", "text": "[AI Brain]:\n\n..." }
```
The reply goes to the originating chat. In a group, the confirmation is visible to
everyone (a private-reply option is on the roadmap).

### 8b. skill → Evolution (send a document) — feature_request

The `feature_request` skill holds a stateful clarifying conversation (per-chat session,
`awaitFrom: "owner"`) and, when the owner says he's done, renders a Markdown feature spec
and delivers it as a real, saveable file:
```
POST http://api:8080/message/sendMedia/secretary
apikey: <AUTHENTICATION_API_KEY>
Body: { "number": "5531999...", "mediatype": "document", "mimetype": "text/markdown",
        "media": "<base64 of the .md>", "fileName": "feature-<slug>.md",
        "caption": "[AI Brain]:\n\n..." }
```
The caption carries the `[AI Brain]:` header (media framing is the caller's job, like
`sendText`). The **conversation** follows `ctx.lang`, but the **document body is always
English** by design — it's destined for the owner's (English) codebase; only the caption
localizes (see the localization note below). This is the only skill that sends a file;
`evolution.sendMedia` was added for it (additive to `sendText`/`fetchHistory`/
`getMediaBase64`).

## Environment variables

**brain (`/opt/brain/.env`)** — `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `TRANSLATE_MODEL`
(cheap model for the long-tail reply-translation fallback; default `claude-haiku-4-5`),
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (needs **both** the
`calendar` **and** `tasks` scopes), `GOOGLE_CALENDAR_ID`, `GOOGLE_TASKLIST_ID` (optional,
default `@default`; Skill: `task_action`),
`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_LANGUAGE` (now only a *fallback* for the transcription
language — the transcription follows the detected `ctx.lang` first; it does **not** set the
reply language, which follows `ctx.lang`), `OWNER_NAME`, `REDIS_URL` (session store; defaults to
`redis://evolution_redis:6379`). Injected by compose: `EVOLUTION_URL`,
`EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE`, `SECRETARY_TAG` (the trigger tag, default `@brain`).

**Evolution (`/opt/evolution/.env`)** — `AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`,
`DATABASE_CONNECTION_URI`, `CACHE_REDIS_URI`, etc.

## Adding a skill

Create `brain/2. Skills/<Your Skill>/skill.js`:
```js
export const manifest = { id: "unique_id", description: "what it does (the router reads this)" };
export async function run(ctx) { /* use ctx.send, ctx.evolution, ctx.anthropic, ctx.lang, ... */ }
export const capabilities = { doThing: (ctx, args) => ... };  // OPTIONAL — see "Composing skills"
```
The orchestrator discovers it at boot; the router starts routing to it. No other changes.

### Composing skills (the capability registry)

A skill has **two faces**. The *routable* face (`manifest` + `run`) is what the router
sees and dispatches to. The optional *internal* face — an exported `capabilities`
object — is a private **skill-to-skill API** the router never sees. Skills never import
each other's files; the orchestrator collects every skill's `capabilities` at boot into a
registry and injects two helpers into `ctx`:

```js
ctx.hasSkill(id, name)              // is capability id.name available?
await ctx.callSkill(id, name, ...args)  // invoke it; THIS ctx is auto-injected as the first arg
```

`callSkill` passes the caller's `ctx` (so the callee shares `owner`/`lang`/`sessions`/
`send`) and enforces `MAX_SKILL_DEPTH` as a loop guard; a missing capability throws (caught
by the orchestrator's per-skill try/catch). **Decoupled by id, not path** — renaming a
skill's folder never breaks a caller. Today: `calendar_action` exposes `startCreate` (the
confirm-first create flow), and `task_action` calls it for a to-do assigned to someone else.

**Session ownership on delegation.** A session the callee opens is tagged with the
callee's `skill` id, so its continuations (the `yes`, a modify, an email chase) route back
to the **callee** — the caller initiates and steps out. E.g. a "task for Ana" opens a
`calendar_action` session; Ana's email or your `yes` is handled by Calendar, not Tasks.

### Localization convention (applies to every skill)

Replies follow `ctx.lang` (detected by the router). **Every user-facing string a skill
sends lives in that skill's `prompt.js` as a per-language map (`{ en, pt }`), selected at
send time with `ctx.lang` (fall back to `en`); every new message must ship its `en` *and*
`pt` entries.** English is the canonical source — do not write user-facing prose inline in
`skill.js`. Dates use a `localizeDate(ctx.lang, …)` helper (always 3-letter month + AM/PM;
the locale sets day/month order). Any language you did *not* write a map for is produced
from the `en` copy by the orchestrator's `send()` translation fallback — a safety net for
unmaintained languages, **not** a substitute for authoring `en`/`pt`. Never translate the
`[AI Brain]:` header; internal/classification prompts (router + skill system prompts) stay
English. Maintained languages today: **en + pt-BR**. The map is **per-skill** (in each
skill's `prompt.js`) — deliberately *not* a central `i18n.js` catalog; prose stays with the
skill that owns it. Live in production since 2026-07-11.

**One deliberate exception — generated artifacts.** A skill may pin a *generated
document* to a fixed language even though its chat replies follow `ctx.lang`.
`feature_request` writes its `.md` spec **always in English** (the artifact is for the
owner's English codebase) while the clarifying conversation and the file's caption still
follow `ctx.lang`. The rule stands for user-facing chat prose; a saved artifact can opt
out.
