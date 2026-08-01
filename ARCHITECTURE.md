# Architecture & data flow

What is sent to each service, with what content, as the system runs. This replaces
the original personal handover document; it describes the current version
(`secretary/`: orchestrator + skills).

## Components

Four containers on one host, talking over the internal Docker network `evolution-net`:

| Container            | Image                                | Port          | Role |
|----------------------|--------------------------------------|---------------|------|
| `evolution_api`      | `evoapicloud/evolution-api:latest`   | `8080` public | WhatsApp gateway |
| `evolution_postgres` | `postgres:15`                        | internal 5432 | Evolution database |
| `evolution_redis`    | `redis:latest`                       | internal 6379 | Evolution cache + secretary session store |
| `secretary`          | `node:20-alpine`                     | internal 3000 | The AI app (orchestrator + skills) |

Only `8080` is published to the internet.

## Flow

```
webhook  ->  filter (start on fromMe + matchedTagNew, or continue an active session)  ->  build context  ->  ROUTER  ->  SKILL(s)
```

### The turn loop (the model holds the conversation)

There is **one** flow. A message summoned by the `@mary` tag (`SECRETARY_TAG_NEW`) runs the
orchestrator's **turn loop**: the model **holds the conversation** and, on each turn, makes ONE
three-part decision, and the orchestrator holds a marker between messages.

```
message → route(ctx, turn) → { say, keepListening, execute, lang, pendingNeed, degraded }
                                │
   execute = []  & keepListening=true  ── ask / propose / stay silent, keep the marker open
   execute = [ids]                     ── run task(s); a task returns a value → a READ-BACK turn
   execute = []  & keepListening=false ── close the conversation (clean close → sign-off)
```

**The unified turn call (card 327be40b).** `route()` is ONE `messages.create` that carries, together:
`output_config: jsonFormat(TURN_DECISION_SCHEMA)` (the reply is schema-locked to the three-decision
envelope), the native toolset (`buildNativeTools`, `lib/nativeTools.js` — `web_search` + `web_fetch`,
plus `code_execution` when `NATIVE_CODE_EXEC` is on; `[]` when `NATIVE_TOOLS` is off), and **adaptive
thinking**. So Mary reasons, may search the web / read a URL / compute **inline** this turn, and then
fills `say` — answering a direct question (a live-web fact, general knowledge no skill covers) is
simply `say=prose`, `keepListening=true`; there is **no separate `answer` pass** any more (it was
folded in and deleted). Server-side tools run an internal sampling loop that can pause
(`stop_reason:"pause_turn"`); `route()` resumes it up to `NATIVE_MAX_TOOL_HOPS`, each call bounded by
`NATIVE_ANSWER_TIMEOUT_MS`. `parseJsonReply` (via `readReply`) is retained as the degrade-to-menu
fallback: a refusal / unparseable / still-paused turn returns `{keepListening:false, execute:[],
degraded:true}`, and only that flag fires the "I didn't understand" menu. (`output_config` composes
with the toolset, adaptive thinking, and the media/vision path — verified live; `max_tokens` is 8192
so adaptive thinking cannot truncate the JSON.)

**Two-phase execute.** Because `output_config` forbids the polymorphic `info`, payload extraction is a
SECOND call: `extract(ctx, turn)` carries `output_config` whose schema is derived **shape-only** from
the chosen task's `manifest.inputs` by `buildExecuteSchema` (`lib/inputs.js`) — the orchestrator still
never imports a skill's schema. `checkPayload` gates the payload; on a validation failure the loop
**RE-RUNS `extract()`** with the `describeProblems` feedback threaded in (repair = re-extraction, NOT a
re-decision), bounded by `MAX_REPAIRS`→`repairGiveUp`. A genuinely-missing detail is caught UPSTREAM —
`route()`'s certainty rule keeps the task out of `execute` and asks for it (`keepListening=true` +
`pendingNeed`) — so repair only fixes fixable mis-parses.

`execute` is **non-terminal**: a task (`manifest.conversation:"orchestrator"`) returns a
JSON-serialisable value which the orchestrator feeds back to the model as a **read-back** turn (the
model reads its own result and usually closes). The loop is bounded by `MAX_TURNS`, `MAX_DISPATCHES`,
`MAX_REPAIRS`, enforces the **write invariant** (a read-back may not execute), and makes deliberate
silence free.

**Stateful conversation + open gate + sign-off (card 327be40b).** The marker carries a `state` object
(goal + decision log + last extraction payload + `pendingNeed` + `didWork`), rendered into every turn
by `renderStateBlock`. The continuation gate is keyed on `session.open` and opens to **any sender**
(the who-lock `awaitFrom` is gone — a guest the owner is scheduling with can answer inline). A **clean
task-completion close** (execute empty, `keepListening=false`, not degraded, `state.didWork` true)
sends a mandatory bilingual **sign-off** (`ORCH_MSG.finishedSignOff`) after any `say`, then closes; a
chatter-ignore turn is `keepListening=true` and never reaches a close, and cap/error closes carry their
own notice instead.

**Inbound media (card cf60f344).** A turn that carries files (a receipt, an invoice) relays them to
`route()` as Anthropic **multimodal content**, interpreted on the turn they arrive.
`inboundMedia(data, quoted)` (`lib/whatsapp.js`) detects the turn's media **LIST** (attachment +
quoted file, both documented webhook shapes); `mediaBlockFor({mediaType,mimetype,base64})` is the
**extension point** — one "supported? → native block, or defer" decision plus two native handlers
(image, PDF); everything else returns `null` (a localized "can't read that yet"), and a future
format is a single new branch here. The orchestrator downloads each file (per-file + per-turn
caps), builds `ctx.media = { blocks, model: VISION_MODEL }`, and `route()`'s turn call becomes an
**N-block content array (media before text) with the vision model pinned** whenever `ctx.media` is
present and the turn is not a read-back — otherwise the call is the byte-identical text-only
string. `ctx.media` is the one additive `ctx` field and is `null` on every text-only turn.

**Skills are pure tasks.** Skill discovery runs once at boot: `loadSkills(dir = NEW_SKILLS_DIR)`
discovers `secretary/3. Mary Skills/` → `NEW_SKILLS`/`NEW_CATALOG` (the turn loop reads these).
Every skill under that tree is a **pure task** (see "Adding a skill" and the per-skill `SKILL.md`s):
the orchestrator model runs the whole dialogue, and each `run(ctx)` only validates its declared
`inputs`, acts, and **returns** a value. `calendar_action` and `task_action` use a
**READ-then-ACT** contract — a `find`/`list` READ returns id-bearing candidates the model
reads back, and a later ACT targets one by id (which is why calendar/tasks need no in-skill session).
The model **chains skills itself** across turns; a skill never invokes another. The trigger tag list
(`NEW_TAGS`, mutated by `setNewTags`) is durable in `secretary:settings:new:tags`, which wins over
the `SECRETARY_TAG_NEW` seed at boot.

### 1. Evolution → secretary (incoming webhook)

Configured once via `POST /webhook/set/secretaria` (the instance name). On every message Evolution sends:

```
POST http://secretary:3000/webhook
```
Body (`MESSAGES_UPSERT`), example:
```json
{
  "event": "messages.upsert",
  "instance": "secretaria",
  "data": {
    "key": { "remoteJid": "5531999...@s.whatsapp.net", "fromMe": true, "id": "3EB0..." },
    "pushName": "User",
    "message": { "conversation": "@mary schedule..." },
    "messageType": "conversation",
    "messageTimestamp": 1751560000
  }
}
```
The secretary **buffers every message** (for context). A flow only **starts** when `fromMe === true`
**and** the text starts with a trigger tag (`matchedTagNew`, default `@mary`). But the secretary is **stateful** — it keeps per-chat state
in Redis (see `1. Orchestrator/lib/sessions.js`) — so once a session is active it can **continue
without the tag**: the secretary uses the LLM to ignore normal chatter and watch for the awaited
answer (a confirmation or clarification). That continuation can also come from the **other person**
in the chat (e.g. they reply with their email), so a non-owner message can be a valid continuation
of an active session. Dedup by `key.id`. Messages that are neither a trigger nor a continuation
pass through but are discarded and never sent to any external API.

### 2. secretary → Evolution (fetch history)

```
POST http://api:8080/chat/findMessages/secretary
apikey: <AUTHENTICATION_API_KEY>
```
Sent **twice** — once as `{ "where": { "key": { "remoteJid": "…" } } }` and once as
`{ "where": { "key": { "remoteJidAlt": "…" } } }` — and merged. The secretary then merges
*that* with its in-memory buffer, dedups, sorts by time and builds a transcript of the last
~30 messages as `ME: ...` / `OTHER: ...`.

> **Why two queries — WhatsApp LID addressing.** In a **1:1 chat**, Evolution persists
> inbound messages under the contact's **`…@lid`** JID, while the JID the webhook hands us —
> and that we send to — is the phone **`…@s.whatsapp.net`**. Query the phone JID alone and the
> durable history comes back containing **nothing but the secretary's own outbound messages**:
> it reads its own voice back and sees no conversation at all. Evolution records the phone JID
> on those LID rows as `key.remoteJidAlt`, which is the link between the two. **Group chats
> (`@g.us`) are unaffected** — their inbound messages are stored under the same JID the webhook
> delivers, so the second query is a no-op there.
>
> This was a real, silent, high-severity bug (fixed 2026-07-12): the durable read returned
> nothing usable, so the secretary's entire memory of any 1:1 chat silently collapsed onto the
> volatile 50-message in-memory buffer — and **every container restart wiped it**. It looked
> like a deployment problem. It was one wrong lookup key. See
> `Bugs and Malfunctions/bugfix-lid-history-blindness.md`, and
> `scripts/history-selftest.mjs`, which fails if anyone drops back to a single query.
>
> Note `findMessages` paginates at **50 rows/page**, page 1 being the **newest** (descending) —
> which is what makes the merge correct. Those 50 are raw rows, though, including non-text
> protocol noise, so a busy chat's usable transcript can be far thinner than 30 messages.

### 3. secretary → Claude (the unified turn call — DECIDE, then EXTRACT in a second call)

```
POST https://api.anthropic.com/v1/messages   (via @anthropic-ai/sdk)
```
Sent: the router system prompt — the orientation preamble (native-app persona + "use your tools
inline"), the live skill catalog, **each skill's declared inputs (`manifest.inputs`) and its own
extraction rulebook**, the rendered conversation `state` — plus a user message with the order, the
transcript, the current date/time, the contact, any quoted message, and (when present) a system-side
**audio transcript** folded in as inline text. The call carries `output_config:
jsonFormat(TURN_DECISION_SCHEMA)` + the native toolset + adaptive thinking, and returns the
three-decision envelope:
```json
{ "say": "…" , "keepListening": true, "execute": ["calendar_action"], "lang": "pt", "pendingNeed": null }
```
`lang` is the detected conversation language (ISO code; default `"en"`) — it rides in `ctx.lang` so the
whole system replies in that language. There is **no inline `info`**: when `execute` names a task, a
SECOND call `extract()` produces that task's payload.

**Why `output_config` is now safe here.** The old merged call sent NO `output_config` to avoid
importing each skill's JSON Schema — *the router would then know what a calendar is.* The two-phase
design keeps that invariant a different way: the turn call's schema is the generic `TURN_DECISION_SCHEMA`
(it names no skill), and the extraction call's schema is derived **shape-only** from the declaration by
`buildExecuteSchema` (`lib/inputs.js`) — so the orchestrator still never imports a skill's own schema.
`parseJsonReply` (via `readReply`) is retained as the degrade fallback; an unparseable/refused/still-paused
turn returns `{keepListening:false, execute:[], degraded:true}` → "I didn't understand" **+ a self-learning
report**, which is the alarm.

**Extraction (`extract`) + repair.** For an `execute`, `extract()` sends the SAME system prompt with a
per-task extraction user message and `output_config: jsonFormat(buildExecuteSchema(spec))` (no tools).
Then **plain code — no AI** — checks the payload (`checkPayload`): is it an object, are the declared
fields present and well-typed? An orchestrator-tier task that FAILS validation is re-extracted with the
`describeProblems` feedback threaded into `extract()` (bounded by `MAX_REPAIRS`→`repairGiveUp`); a valid
payload reaches the skill as **`ctx.info`**. Only the content of that one conversation leaves for
Anthropic, and only at that moment.

### 4. secretary → Claude (skill: calendar_action) — now the FALLBACK, not the norm

This call only runs when the merged call above did **not** hand the skill a usable payload (a
shape-invalid `info`, or a dual-intent turn where the payload belonged to another skill). With
the skill's own prompt, it extracts:
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
— **the same field names the merged call returns**, which is exactly what makes the merged
payload a drop-in and leaves `handleCreate`/`handleDelete`/`handleEdit`/`handleList` untouched.
`action` is `"create"`, `"delete"`, `"edit"`, or `"list"` — the skill can create a new
event, cancel/delete an existing one, edit/reschedule one (reply to the invite **or** the
summary/confirm bubble with a change — the target is matched like delete, by decoded link
or start-time + attendee-email; confirm-first and stays open until you save), or **read/list**
what's on the calendar. `list` is **read-only** (no session, no confirm, no write): the LLM
also fills `list_mode` (`"window"` | `"next"`) and `range_start_iso`/`range_end_iso`, and the
skill just formats and replies (e.g. "what's on tomorrow?", "what's my next meeting?").

### 5. skill → Google Calendar (create, cancel/delete, edit, or read event)

OAuth (Client ID + Secret + Refresh Token); the secretary exchanges the refresh token for
an access token automatically.
```
POST   https://www.googleapis.com/calendar/v3/calendars/primary/events?sendUpdates=all   (create)
DELETE https://www.googleapis.com/calendar/v3/calendars/primary/events/{eventId}?sendUpdates=all   (cancel/delete)
GET    https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin=…&timeMax=…&singleEvents=true&orderBy=startTime   (list/read — no write)
```
`sendUpdates=all` makes Google send the invite (or cancellation) email to the attendees from
your account. The `list` GET is read-only and sends no email; `singleEvents=true` expands
recurring events into concrete instances inside the window.

### 5b. skill → Google Tasks (add / list / complete / edit / delete) — task_action

Same OAuth client as Calendar (the refresh token must also carry the
`https://www.googleapis.com/auth/tasks` scope). The Tasks list defaults to `@default`
(override with `GOOGLE_TASKLIST_ID`). One list-aware planner (`planTaskOps`) enumerates the
tasks a message refers to — so add / complete / edit / delete all work in **batch** — and the
same HTTP surface is called once per task:
```
POST   https://tasks.googleapis.com/tasks/v1/lists/@default/tasks              (add, one per created task)
GET    https://tasks.googleapis.com/tasks/v1/lists/@default/tasks?showCompleted=false  (list; also read before every plan, to match refs)
PATCH  https://tasks.googleapis.com/tasks/v1/lists/@default/tasks/{taskId}     (complete: status=completed; or edit/amend title/due)
DELETE https://tasks.googleapis.com/tasks/v1/lists/@default/tasks/{taskId}     (delete / amend-window "cancel that")
```
`due` is **date-only** (stored at UTC midnight). A to-do for **yourself** lands here; a
to-do assigned to **someone else** has no private-list equivalent (Tasks emails no one),
so a to-do for someone else is **not** a task op — the model **chains** a `calendar_action` create
(step 5) instead: a 5-min invite that notifies them by email.

### 5c. skill → Google People / Contacts (look up & save a guest email) — calendar_action

Same OAuth client as Calendar/Tasks (the refresh token must **also** carry the
`https://www.googleapis.com/auth/contacts` scope — read **and** write, not `contacts.readonly`; and
the People API must be enabled). On the calendar **create** path (1:1 chats only), `calendar_action`
resolves a guest's missing email from the owner's address book by phone number, and saves a
freshly-supplied one back:
```
GET   https://people.googleapis.com/v1/people/me/connections?personFields=names,emailAddresses,phoneNumbers,metadata   (paged; match a phone → collect emails)
PATCH https://people.googleapis.com/v1/{resourceName}:updateContact?updatePersonFields=emailAddresses   (additively append a second email + etag)
POST  https://people.googleapis.com/v1/people:createContact   (new contact when the number is unknown)
```
One match → the email is filled onto the invite silently; several → the model asks the owner which;
none / any error → today's book-without-invite fallback (both seams **never throw**, so a Contacts
outage never blocks a booking). The save-back is **additive** — a second email, never an overwrite.
Nothing here 401s the booking: without the scope every People call fails and the lookup degrades to
no-match. Detail: `3. Mary Skills/1. Calendar Actions/SKILL.md` §CONTACTS + §Setup.

### 6. skill → Evolution (fetch audio) — transcribe_audio

When you reply to a voice message, the secretary reads `contextInfo.stanzaId` (the quoted
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
Body: { "number": "5531999...", "text": "*[Marcelo's AI Secretary]:*\n\n_..._" }
```
The reply header is **language-aware** — `headerFor(ctx.lang)` from `1. Orchestrator/lib/identity.js`
stamps `[Marcelo's AI Secretary]:` (en) or `[Secretaria IA do Marcelo]:` (pt), derived from
`OWNER_NAME`. The reply goes to the originating chat. In a group, the confirmation is visible to
everyone (a private-reply option is on the roadmap).

**Message framing (`1. Orchestrator/lib/format.js`).** Because the secretary replies from the
owner's own WhatsApp account, its messages sit in the same thread as the owner's typing. `frame()`
makes the two voices visually distinct: **bold header** (`*...*`), blank line, **italic body**
(`_..._`). Three rules the implementation depends on:
- WhatsApp italics **do not span newlines**, so the body is wrapped **line by line**, never as a
  whole. A leading bullet/indent stays outside the markers (`- _Buy milk_`).
- A line is left **plain** when wrapping would corrupt it: it carries a **URL** (a trailing `_` is a
  valid base64url char and would be swallowed into a calendar link's `eid` by `findCalendarLink`,
  silently breaking reply-to-invite edit/delete) or it already contains `_ * ~` (emails like
  `bruno_x@…`, verbatim task titles). Plain-but-correct beats italic-but-broken.
- Markers are applied **after** `localizeBody()`, so the translation model never sees them.

Framing happens once, in `send()` — skills never write markup. `ctx.send(number, text, { italic:
false })` opts a body out entirely; no skill needs it today (the audio transcript used to, and is
now italic like every other reply). Because the header now ships bolded, `isOwnMessage()` strips leading `* _ ~` before matching
— it must keep recognizing both the bold header and the unbolded ones still in chat history, or the
bot reads its own replies as owner continuations.

### 8b. skill → Evolution (send a document) — feature_request

The `feature_request` skill holds a stateful clarifying conversation (per-chat session,
`awaitFrom: "owner"`) and, when the owner says he's done, renders a Markdown feature spec
and delivers it as a real, saveable file:
```
POST http://api:8080/message/sendMedia/secretary
apikey: <AUTHENTICATION_API_KEY>
Body: { "number": "5531999...", "mediatype": "document", "mimetype": "text/markdown",
        "media": "<base64 of the .md>", "fileName": "feature-<slug>.md",
        "caption": "*[Marcelo's AI Secretary]:*\n\n_..._" }
```
**Before the send, the same markdown is spooled to `secretary/specs/`** — a timestamped file
(`feature-<slug>-<YYYY-MM-DDTHH-MM-SS>.md`) opening with a YAML-shaped frontmatter header
(`title` / `one_liner` / `when`). This happens *first*, so a failed send never loses the spec;
that spooled copy is what the Mac later pulls and turns into a card on the kanban backlog (see
"Self-learning" below). The **attachment is byte-for-byte unchanged** — its `fileName` is still
`feature-<slug>.md`, no timestamp, no header — only the spooled copy carries them. If the spool
write fails but the send succeeds, the owner gets one extra reply (`specFileFailed`, via
`ctx.sendFailure`) telling him it will not reach the board.

The caption carries the language-aware header (`headerFor(ctx.lang)`; media framing is the caller's job, like
`sendText`, so it calls `frame()` itself to get the same bold-header/italic-body treatment). The **conversation** follows `ctx.lang`, but the **document body is always
English** by design — it's destined for the owner's (English) codebase; only the caption
localizes (see the localization note below). `evolution.sendMedia` was added for this skill
(additive to `sendText`/`fetchHistory`/`getMediaBase64`) and is now also used by
`transcribe_audio`, which delivers the transcript of an audio longer than **2 minutes** as a
`.txt` document (`mimetype: "text/plain"`) instead of a wall of inline text — same shape as
above, same caller-frames-the-caption rule.

### 8c. skill → Kiwi (search flights) — flight_search

The `flight_search` skill confirms the trip with the owner first, then makes **one** call to
Kiwi's public MCP endpoint. It is **keyless** — no API key, no `initialize` handshake, no
`Mcp-Session-Id`:
```
POST https://mcp.kiwi.com
Content-Type: application/json
Accept: application/json, text/event-stream        <-- BOTH (json alone -> HTTP 406)
Body: {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
         "name":"search-flight",
         "arguments":{"flyFrom":"SAO","flyTo":"LIS","departureDate":"14/08/2026",
                      "returnDate":"22/08/2026","adults":1,"cabinClass":"M",
                      "currency":"BRL","locale":"pt"}}}
```
The answer is an **SSE frame with CRLF terminators** (`event: message\r\ndata: {…}\r\n\r\n`);
the payload is `result.structuredContent`. **Dates on the wire are `dd/mm/yyyy`, not ISO**, and
`cabinClass` is the enum `M|W|C|F`. A bad argument comes back on an **HTTP 200** with
`isError: true` and a plain, non-JSON body — checked before anything is parsed. Timeout 20s;
no interim ack (the search lands in ~1.5–4s). Only the trip's parameters leave for Kiwi — no
conversation, no personal data. `locale` is fixed at `pt` (it drives Kiwi's booking page) and is
deliberately **not** tied to `ctx.lang`, which controls only our reply. Full contract, including
the volatility warning, in `PROJECT_LOG.md` §8; the mandatory client-side result filter — Kiwi
has no max-stops or self-transfer parameter — in the skill's `SKILL.md`.

## Environment variables

**secretary (`/opt/secretary/.env`)** — `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `TRANSLATE_MODEL`
(cheap model for the long-tail reply-translation fallback; default `claude-haiku-4-5`),
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (needs **both** the
`calendar` **and** `tasks` scopes), `GOOGLE_CALENDAR_ID`, `GOOGLE_TASKLIST_ID` (optional,
default `@default`; Skill: `task_action`),
`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_LANGUAGE` (now only a *fallback* for the transcription
language — the transcription follows the detected `ctx.lang` first; it does **not** set the
reply language, which follows `ctx.lang`), `FLIGHT_CURRENCY` (optional, default `BRL`; Skill:
`flight_search` — the currency asked of Kiwi. **There is no flight-provider API key**: the Kiwi
endpoint is keyless), `OWNER_NAME`, `REDIS_URL` (session store **and** the durable settings
store; defaults to `redis://evolution_redis:6379`). Injected by compose: `EVOLUTION_URL`,
`EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE`, and `SECRETARY_TAG_NEW` (the trigger tags —
**comma-separated**, default `@mary`; a message starting with one of these runs the orchestrator
turn loop. Its own stored list wins over the seed at `secretary:settings:new:tags`).

`SECRETARY_TAG_NEW` is the **SEED, not the last word**. The owner can change the tags by asking
her (`assistant_settings`); the confirmed list is stored in Redis under
`secretary:settings:new:tags` (**no TTL**, `lib/settings.js`) and **wins over the env var at boot**
— `server.js` awaits the store's `ready` before reading it (an un-awaited read would race the
Redis connect and silently fall back to the seed) and logs which source won. **A restart does
not revert a changed tag**; the store outlives it. The recovery path — a tag the owner cannot
type, or has forgotten — is to clear the key and restart, which falls back to the seed:

```bash
docker exec evolution_redis redis-cli DEL secretary:settings:new:tags
```

**Evolution (`/opt/evolution/.env`)** — `AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`,
`DATABASE_CONNECTION_URI`, `CACHE_REDIS_URI`, etc.

## Adding a skill

Skills live under one tree: `secretary/3. Mary Skills/`. Discovery is `server.js` running
`loadSkills(NEW_SKILLS_DIR)` once at boot, so a skill folder is picked up simply by sitting in that
tree. **No `server.js` or router edit is needed to add a skill; it is a drop-in.**

**A skill is a PURE TASK** — `secretary/3. Mary Skills/<Your Skill>/skill.js`:
```js
export const manifest = {
  id: "unique_id",
  conversation: "orchestrator",   // the MODEL runs the dialogue; the skill never asks/confirms
  description: "what it does (no 'she proposes/asks' — the orchestrator does)",
  inputs: { /* a declaration — see below; or null, e.g. transcribe_audio */ },
};
// validate ctx.info defensively → act → send ONE outcome → RETURN a JSON value (the read-back)
export async function run(ctx) { /* ...; return { ok, ... }; */ }
// NO `capabilities` export; NO lib/confirm.js; NO sessions.set — the model runs the dialogue.
```
For a read-then-act skill (calendar/tasks/flights), a discriminator value that only READS carries
**no** `requiredWhen`, every non-discriminator field is `nullable`, and the READ step **returns**
structured candidates carrying a stable id which the model reads back before dispatching the ACT.
A converted skill with **no** declared inputs (`inputs:null`, e.g. `transcribe_audio`) is dispatched
directly — the orchestrator's dispatch gate treats `inputs == null` as "nothing to validate, run it"
rather than trapping it in the repair loop.

### Declaring your inputs (`manifest.inputs`) — one fewer round-trip

A skill that needs data extracted from the order can **declare** it. The router then fills that
declaration in the **same call** that classifies the order, plain code validates the reply
against it (`lib/inputs.js`), and a valid payload arrives on **`ctx.info`**:

```js
inputs: {
  discriminator: "action",                       // the field whose value picks the required set
  fields: { action: { type: "enum", enum: [...], desc: "…" }, /* … */ },
  requiredWhen: { create: ["start_iso"] },       // "must be non-null before we can act"
  consistency: [{ name: "…", test: (i) => true }],  // your own plain-code sanity rules
  rulebook: () => buildExtractionRules(owner),   // your extraction prose, carried VERBATIM
}
export async function run(ctx) {
  let info = ctx.info ?? null;                   // the router already extracted it
  if (!info) info = await interpret(ctx);        // …or it didn't: fall back to your own call
}
```

`desc` is not documentation — **it is the prompt**, and so is `rulebook()`. Both are rendered
straight into the merged system prompt. **`inputs: null` is a perfectly good answer** ("no
inputs; I read the conversation myself"), and such a skill is never handed a payload. A skill
that ignores `ctx.info` behaves exactly as it did before any of this existed.

**Two rules, and both have already bitten:**
- **`ctx.info` is scoped to `tasks[0]`, and to nobody else.** On a dual-intent turn
  (`["feedback","calendar_action"]`) the payload belongs to *feedback*; every other skill is
  handed `null` and extracts for itself. Handing a skill someone else's payload is how you book
  the wrong meeting.
- **If your `fields` mirror a JSON Schema you also send elsewhere, they must stay in lockstep
  forever, and nothing in the language enforces it.** Add a field to the schema and forget the
  declaration and the merged prompt silently stops asking for it — the feature dies with no test
  going red. There is no automated set-equality lint guarding this today. **Write one.**

**Send failures with `ctx.sendFailure`, not `ctx.send`.** A reply that means *"you asked me to
do something and I did not do it"* — an API error, "something went wrong", a batch that only
half-applied — goes through `ctx.sendFailure(number, text)`. It sends exactly like `ctx.send`
**and** files a self-learning failure report (see "Self-learning" below), which is how the bug
reaches the owner instead of dying in the chat.

Everything else stays on `ctx.send`: successes, confirmations, **questions** ("which task did
you mean?"), and empty-but-true answers ("your list is empty"). Asking for more information is
not failing. The test is not whether the message *sounds* apologetic — it's whether the owner
asked for something and didn't get it. A lint in `scripts/selflearning-selftest.mjs` fails the
test run if a reply named `*Error`/`*Failed`/`noAction` is sent with plain `send()`.

**A private note to the owner with `ctx.dmOwner(text)`.** Additive `ctx` field (constructed in
`server.js`) that sends a framed, localized message to the owner's **own** number
(`OWNER_JID`/`OWNER_NUMBER`) rather than to the current chat — a **no-op when that env var is
unset**. It exists because the orchestrator only ever knows the *current* chat's number, which in
a booking driven from the guest's chat is the guest's number; a side note to the owner needs its own send path.
Today only `calendar_action` uses it (the Contacts "email saved" note). It is an **outcome** note,
so it rides `ctx.dmOwner`/`ctx.send`, never `ctx.sendFailure`. Guard the call with a `typeof`
check so a skill stays safe in a deployment where the field is absent.

### The shared lib (`1. Orchestrator/lib/`) — don't re-implement these

Skills import these directly (`../../1. Orchestrator/lib/<x>.js`). Each one existed as a
copy-paste in two or three skills before it was lifted here; a bug fixed in a copy was a bug
still live in the others. Reach for them before writing your own:

| Module | Exports | Use it for |
| --- | --- | --- |
| `llm.js` | `jsonFormat`, `readReply`, `readText`, `parseJsonReply`, `withThinkingDefault` | Any Claude call that must return JSON. `jsonFormat(SCHEMA)` → `output_config`; `readReply(msg, "<skill>")` → the parsed object, or `null` on a refusal/truncated reply (it logs `stop_reason` + size). Never hand-parse a model reply. `withThinkingDefault(client)` wraps the SDK client so every call defaults to `thinking: {type:"disabled"}` — **`server.js` already applies it to the one shared client, so a skill inherits it and never calls this itself.** (Extended thinking is on by default and we discard every thinking block; we were paying latency for output nobody reads. A call site that genuinely wants reasoning passes its own `thinking`, and the wrapper leaves it alone.) |
| `inputs.js` | `describeInputs`, `checkPayload`, `describeProblems`, `buildExecuteSchema` | The **declared-inputs contract**. `describeInputs(catalog)` renders each skill's declaration as prompt text; `buildExecuteSchema(spec)` derives the extraction call's `output_config` schema **shape-only** from the declaration (never naming a skill); `checkPayload(inputs, info)` is the **plain-code, no-AI** gate — `{ shapeOk, ok, problems }`; `describeProblems` renders its failures for a repair re-extraction. It knows about *declarations*, never about skills. You almost never call these directly: declare `manifest.inputs` and read `ctx.info`. |
| `confirm.js` | `classifyConfirmation`, `CONFIRM_SCHEMA`, `buildConfirmSystem/User` | **Confirm-first writes.** `await classifyConfirmation(ctx, { action: "cancel the 15:00 meeting", who: "<skill>" })` → `confirm \| decline \| unrelated`. Any doubt or API error returns `unrelated` (the safe no-op), so an unclear message can never fire an irreversible write. The *session* stays yours — this only reads the latest message. |
| `google.js` | `googleAuth(env)` | The OAuth2 client from `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`. Build your own service on top: `google.tasks({ version: "v1", auth: googleAuth(env) })`. Adding a Google API means adding its **scope** to the refresh token (re-consent), not new auth code. |
| `identity.js` | `NEW_TAGS`, `headerFor(lang)`, `isOwnMessage`, `matchedTagNew` | The trigger tags and the reply header. |
| `whatsapp.js` | `extractText`, `getQuoted`, `inboundMedia`, `mediaBlockFor`, `remember`, `combine`, `buildTranscript`, `buildLabeledTranscript`, `HISTORY_WINDOW` | Message-shape utilities. **`inboundMedia(data, quoted)`** → the `@mary` turn's inbound media LIST (detection only; audio is omitted — it is handled by system-side transcription). **`mediaBlockFor({mediaType,mimetype,base64})`** is the media **extension point**: image → an image block, document (pdf) → a document block, everything else → `null` (defer). `HISTORY_WINDOW` (=30) is `combine`'s default window, named so the router preamble can interpolate the real N. `media_type` comes from the real mime, never trusted from a default. |
| `transcribe.js` | `transcribeAudio(env, buffer, lang)` | **System-side audio transcription** (AssemblyAI), lifted out of the audio skill so the orchestrator never imports a skill. The model can't ingest audio, so `server.js` MEDIA PREP transcribes a quoted audio and folds the text into the turn prompt on `ctx.audioTranscript`. Reads `ASSEMBLYAI_API_KEY`/`ASSEMBLYAI_LANGUAGE`. |
| `format.js` | `frame` | Bold-header/italic-body framing — normally applied for you in `send()`; import it only if you bypass `ctx.send` (as `feature_request` does for a media caption). |
| `nativeTools.js` | `buildNativeTools(env)` | The native server-side tool bundle attached to the **unified turn call** (`route()`), built off env toggles: `web_search_20260209` + `web_fetch_20260209`, plus `code_execution_20260521` when `NATIVE_CODE_EXEC` is on; `[]` when `NATIVE_TOOLS` is off. Rails-only — no skill imports it; the orchestrator attaches it inside `route()` (the separate answer pass is gone). |
| `logbuffer.js` | `installLogBuffer`, `getRecentLogs`, `redact` | The secretary's own recent logs, in memory. Installed once by `server.js`; you almost never call this directly. |
| `selflearning.js` | `captureFailure`, `appendToReport`, `looksLikeFailure` | **Failure capture** — writes a Markdown report to `secretary/improvements/`. Wired into the orchestrator's catch blocks for you; a skill only calls it directly to report a failure the code *can't see* (as `feedback` does). See "Self-learning" below. |

Everything else a skill needs (`send`, `lang`, `sessions`, `anthropic`, `evolution`, `env`)
arrives on **`ctx`** — see `server.js`. If you find yourself editing the orchestrator to add a
skill, that's the signal `ctx` or this lib is missing something: fix it **once**, here, rather
than reaching around it.

### Chaining skills (the model does it)

Skills never import or invoke each other. When a request needs more than one skill — a to-do for
someone else that must go out as a calendar invite, a search whose result feeds a later action —
the **model chains them itself** across the turn loop: it dispatches one skill, reads back the
value that skill returns, and dispatches the next. There is no skill-to-skill API, no capability
registry, and no `capabilities` export. E.g. a "task for Ana" is routed by the model to
`calendar_action` (not `task_action`), which opens the confirm-first create; Ana's email or your
`yes` continues that same conversation on the next turn.

### Localization convention (applies to every skill)

Replies follow `ctx.lang` (detected by the router). **Every user-facing string a skill
sends lives in that skill's `prompt.js` as a per-language map (`{ en, pt }`), selected at
send time with `ctx.lang` (fall back to `en`); every new message must ship its `en` *and*
`pt` entries.** English is the canonical source — do not write user-facing prose inline in
`skill.js`. Dates use a `localizeDate(ctx.lang, …)` helper (always 3-letter month + AM/PM;
the locale sets day/month order). Any language you did *not* write a map for is produced
from the `en` copy by the orchestrator's `send()` translation fallback — a safety net for
unmaintained languages, **not** a substitute for authoring `en`/`pt`. The reply header is
not translated by `send()` — it is produced per-language by `headerFor(lang)`; internal/classification prompts (router + skill system prompts) stay
English. Maintained languages today: **en + pt-BR**. The map is **per-skill** (in each
skill's `prompt.js`) — deliberately *not* a central `i18n.js` catalog; prose stays with the
skill that owns it. Live in production since 2026-07-11.

**One deliberate exception — generated artifacts.** A skill may pin a *generated
document* to a fixed language even though its chat replies follow `ctx.lang`.
`feature_request` writes its `.md` spec **always in English** (the artifact is for the
owner's English codebase) while the clarifying conversation and the file's caption still
follow `ctx.lang`. The rule stands for user-facing chat prose; a saved artifact can opt
out.

## Self-learning — how the secretary reports its own failures

The secretary writes **failure reports about itself** to `secretary/improvements/`; the Mac
pulls them and turns each into an implementation plan. The capture layer is
`1. Orchestrator/lib/{logbuffer,selflearning}.js` — **infrastructure, not a skill** (every
loaded skill lands in `NEW_CATALOG`, the router's menu, so a skill the router must never pick is
a misroute hazard with no upside).

**Six triggers — five the machine sees, one only the owner can.**

| Trigger | Fires when | Wired at |
|---|---|---|
| `throw:continuation` / `throw:router` / `throw:skill` | a hard exception | the three catch blocks in `server.js` |
| `unrouted` | the router understood nothing — a **missing capability**, not a bug | the `notUnderstood` branch |
| `soft` | **a skill says it failed without throwing** — the biggest category by far | **`ctx.sendFailure()`**, explicitly, at ~29 call sites across the skills |
| **`reported`** | **the owner says the secretary was wrong** | the **`feedback` skill** |

### A malfunction is exactly three things

1. **A code error** — something threw (`throw:*`).
2. **A soft landing of an uncompleted task** — the owner asked for something and did not get
   it. **Declared by the skill** with `ctx.sendFailure` (`soft`). This also covers *"I didn't
   understand"*: the `unrouted` branch and the skills' own `noAction` ("I didn't identify a
   calendar action"). It reads like guidance, but he asked and got nothing — and it is the
   clearest signal the system has of a **missing capability**, which is what tells you what to
   build next. **Deliberate call, 2026-07-12: keep filing these.**
3. **The owner saying it got something wrong** (`reported`).

**Everything else the secretary says is GUIDANCE, and guidance is not a malfunction.**
"Reply to the audio you want transcribed." "Which task did you mean?" "What should the task
say?" "Your list is empty." "Nothing on your calendar." A secretary asking a question, or
truthfully reporting an empty result, is a secretary **working** — filing that as a defect
would bury the real ones.

**The test is not whether the message sounds apologetic. It is whether the owner asked for
something and didn't get it.** "I couldn't find: buy milk. *Which one did you mean?*" sounds
like a failure and is a question. "Done — but couldn't do these: call Ana" sounds like a
success and is a failure. Read the outcome, not the tone.

**#2 is the common case, and it is DECLARED, never inferred.** Most failures never reach a
catch block: *"I understood the request but failed to create it in Google."* *"I hit an error
while thinking."* *"Something went wrong with your tasks."* So the skill says which is which,
at the call site:

```js
await ctx.sendFailure(number, reply(ctx.lang).createGoogleError());  // sends AND files a report
await ctx.send(number, reply(ctx.lang).whichOne(ref));               // a question — not a failure
```

**There is no runtime text scanning, by design.** An earlier version regex-scanned every
outgoing message and was wrong in *both* directions: it **missed** half the real failures
(`thinkingError` — "I hit an error while thinking" — contains no failure word) and it **fired
on guidance** ("I couldn't find: X. *Which one did you mean?*" is a clarifying question, not a
defect). Prose can't be classified by keyword. Only the skill knows whether it just failed the
owner or just asked him something, so only the skill decides. The guard against a skill
*forgetting* is a **lint over the call sites** in `scripts/selflearning-selftest.mjs`: a reply
key named `*Error`/`*Failed`/`*NoMatch`/`noAction` that is sent with plain `send()` fails the
test run, naming the file and line.

Note this includes **partial** failures: Tasks' "Couldn't do these:" after a batch half-applied
goes through `sendFailure`, because the two to-dos that didn't happen are two to-dos the owner
asked for and didn't get.

The first five triggers only fire when the code *knows* it failed. The failures that matter
most are invisible even to `sendFailure`: a **false positive**, a confidently wrong answer, an
event on the wrong day. The secretary reports *success*; nothing looks broken; the only
detector is the owner. `reported` is therefore the only **human-verified** report in the
system, and triage takes it first.

**What a report contains:** the error + stack (or the owner's note + the offending message he
replied to), the recent logs from the ring buffer, the chat transcript, and a cheap-model
"likely cause" guess kept in its own clearly-labelled, discardable section — never mixed with
the owner's testimony.

**Invariants worth not breaking:**
- **Capture never throws** and never masks the original error; it runs *after* the user has
  their reply.
- **One report per webhook turn** (`ctx._turn`), which is an **object, not a boolean**:
  `ctx.sendFailure` and the read-back both read and write it across the turn loop, so a bare
  boolean flag couldn't carry that shared state.
- **Machine failures dedupe (10 min) and are capped (~20/h)** — a crash loop must not fill the
  droplet's disk. **Owner reports do neither**: a human can't loop, two notes are two
  complaints, and a silently dropped note is the worst failure this system has.
- **Secrets are redacted on the way *into* the ring buffer**, and the whole report again on the
  way out — these files live in a git repo.
- Reports are written **inside** `secretary/` because the container only mounts the app dir,
  and they are **gitignored** because `/opt/secretary` symlinks into the production git tree.

**The loop:** `scripts/self-learning-daily.sh` runs at 09:00 daily (launchd): it pulls the
reports **and the feature specs** (Mac → droplet over SSH; pull-based because the droplet's
deploy key is read-only) into `Bugs and Malfunctions/inbox/` and `New Features Plans/`, then
runs `/triage-failures` headless, which writes a `Bugs and Malfunctions/bugfix-<slug>.md` plan
per report and commits it. **It never pushes and never deploys** — `git push`/`ssh`/`docker`
are denied to it. The owner reviews and ships.

**The loop now gains an end: the plan (and the spec) becomes a card on the kanban backlog by
itself.** After triage, the daily job runs `scripts/board-ingest.mjs enqueue` then `drain`
(and a launchd timer drains every 5 min): each new feature spec (`feature-*.md`), each triaged
bugfix plan (`bugfix-*.md`), and each **owner-reported** failure no plan claims becomes one card
on the board's **backlog**, typed (`kind: feature|maintenance`) and unrouted. The staging state
lives in `Board Inbox/` — a `queue/`, a **tracked** `ledger.tsv` (which stops a card being opened
twice, and stops anything predating the feature from ever becoming a card), and a `delivered/`
archive. **The board is consumed over its existing HTTP API and is never modified**, and a card
created with a valid `kind` costs the board nothing (no LLM triage call). A down board is a clean
no-op — the queue is retained and retried. See `Board Inbox/README.md`.

Self-tests: `scripts/selflearning-selftest.mjs` (capture invariants, offline),
`scripts/board-ingest-selftest.mjs` (the exactly-once / nothing-dropped ingest, offline against a
stub board), `scripts/pull-archive-selftest.mjs` (the restructured pull's archive fix and funnel
independence, offline against stub `ssh`/`rsync`) and `scripts/router-selftest.mjs` (that a
*complaint* is filed, not executed — needs an API key).
