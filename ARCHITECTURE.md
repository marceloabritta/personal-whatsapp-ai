# Architecture & data flow

What is sent to each service, with what content, as the system runs. This replaces
the original personal handover document; it describes the current version
(`brain/v2.0`: orchestrator + skills).

## Components

Four containers on one host, talking over the internal Docker network `evolution-net`:

| Container            | Image                                | Port          | Role |
|----------------------|--------------------------------------|---------------|------|
| `evolution_api`      | `evoapicloud/evolution-api:latest`   | `8080` public | WhatsApp gateway |
| `evolution_postgres` | `postgres:15`                        | internal 5432 | Evolution database |
| `evolution_redis`    | `redis:latest`                       | internal 6379 | Evolution cache |
| `brain`              | `node:20-alpine`                     | internal 3000 | The AI app (orchestrator + skills) |

Only `8080` is published to the internet.

## Flow

```
webhook  ->  filter (fromMe + @secretary)  ->  build context  ->  ROUTER  ->  SKILL(s)
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
    "message": { "conversation": "@secretary schedule..." },
    "messageType": "conversation",
    "messageTimestamp": 1751560000
  }
}
```
The brain **buffers every message** (for context) but only continues if `fromMe === true`
**and** the text starts with `@secretary`. Dedup by `key.id`. Messages from other
people pass through but are discarded and never sent to any external API.

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
order, the transcript and whether a quoted audio is present. The router returns:
```json
{ "tasks": ["schedule_meeting"], "reason": "..." }
```
Only the content of that one conversation leaves for Anthropic, and only at that moment.

### 4. brain → Claude (skill: schedule_meeting)

A second call, with the scheduling skill's own prompt, extracts:
```json
{
  "intent": "create_event",
  "participants": [ { "name": "Alex", "email": "alex@example.com" } ],
  "start_iso": "2026-07-04T14:00:00-03:00",
  "duration_min": null,
  "missing": [],
  "summary": "Meeting with Alex, tomorrow 2pm."
}
```

### 5. skill → Google Calendar (create event)

OAuth (Client ID + Secret + Refresh Token); the brain exchanges the refresh token for
an access token automatically.
```
POST https://www.googleapis.com/calendar/v3/calendars/primary/events?sendUpdates=all
```
`sendUpdates=all` makes Google send the invite email to the attendees from your account.

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
Body: { "number": "5531999...", "text": "[AI Secretary]:\n...\n(sent by AI)" }
```
The reply goes to the originating chat. In a group, the confirmation is visible to
everyone (a private-reply option is on the roadmap).

## Environment variables

**brain (`/opt/brain/.env`)** — `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID`, `ASSEMBLYAI_API_KEY`,
`ASSEMBLYAI_LANGUAGE`, `OWNER_NAME`. Injected by compose: `EVOLUTION_URL`,
`EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE`, `SECRETARY_TAG`.

**Evolution (`/opt/evolution/.env`)** — `AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`,
`DATABASE_CONNECTION_URI`, `CACHE_REDIS_URI`, etc.

## Adding a skill

Create `brain/v2.0/2. Skills/<Your Skill>/skill.js`:
```js
export const manifest = { id: "unique_id", description: "what it does (the router reads this)" };
export async function run(ctx) { /* use ctx.send, ctx.evolution, ctx.anthropic, ... */ }
```
The orchestrator discovers it at boot; the router starts routing to it. No other changes.
