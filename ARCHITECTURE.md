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
webhook  ->  filter (start on fromMe + @secretary, or continue an active session)  ->  build context  ->  ROUTER  ->  SKILL(s)
```

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
    "message": { "conversation": "@secretary schedule..." },
    "messageType": "conversation",
    "messageTimestamp": 1751560000
  }
}
```
The secretary **buffers every message** (for context). A flow only **starts** when `fromMe === true`
**and** the text starts with a trigger tag (`@secretaria`/`@secretary`). But the secretary is **stateful** — it keeps per-chat state
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

### 3. secretary → Claude (router)

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

### 4. secretary → Claude (skill: calendar_action)

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
so `task_action` **delegates** to `calendar_action` (step 5) via the capability registry —
a 5-min invite that notifies them by email. See "Composing skills" below.

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
The caption carries the language-aware header (`headerFor(ctx.lang)`; media framing is the caller's job, like
`sendText`, so it calls `frame()` itself to get the same bold-header/italic-body treatment). The **conversation** follows `ctx.lang`, but the **document body is always
English** by design — it's destined for the owner's (English) codebase; only the caption
localizes (see the localization note below). `evolution.sendMedia` was added for this skill
(additive to `sendText`/`fetchHistory`/`getMediaBase64`) and is now also used by
`transcribe_audio`, which delivers the transcript of an audio longer than **2 minutes** as a
`.txt` document (`mimetype: "text/plain"`) instead of a wall of inline text — same shape as
above, same caller-frames-the-caption rule.

## Environment variables

**secretary (`/opt/secretary/.env`)** — `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `TRANSLATE_MODEL`
(cheap model for the long-tail reply-translation fallback; default `claude-haiku-4-5`),
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (needs **both** the
`calendar` **and** `tasks` scopes), `GOOGLE_CALENDAR_ID`, `GOOGLE_TASKLIST_ID` (optional,
default `@default`; Skill: `task_action`),
`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_LANGUAGE` (now only a *fallback* for the transcription
language — the transcription follows the detected `ctx.lang` first; it does **not** set the
reply language, which follows `ctx.lang`), `OWNER_NAME`, `REDIS_URL` (session store; defaults to
`redis://evolution_redis:6379`). Injected by compose: `EVOLUTION_URL`,
`EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE`, `SECRETARY_TAG` (the trigger tags —
**comma-separated**, default `@secretaria,@secretary`; both trigger the secretary. The old
`@brain` tag is **retired** — a message using it is silently ignored).

**Evolution (`/opt/evolution/.env`)** — `AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`,
`DATABASE_CONNECTION_URI`, `CACHE_REDIS_URI`, etc.

## Adding a skill

Create `secretary/2. Skills/<Your Skill>/skill.js`:
```js
export const manifest = { id: "unique_id", description: "what it does (the router reads this)" };
export async function run(ctx) { /* use ctx.send, ctx.evolution, ctx.anthropic, ctx.lang, ... */ }
export const capabilities = { doThing: (ctx, args) => ... };  // OPTIONAL — see "Composing skills"
```
The orchestrator discovers it at boot; the router starts routing to it. No other changes.

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

### The shared lib (`1. Orchestrator/lib/`) — don't re-implement these

Skills import these directly (`../../1. Orchestrator/lib/<x>.js`). Each one existed as a
copy-paste in two or three skills before it was lifted here; a bug fixed in a copy was a bug
still live in the others. Reach for them before writing your own:

| Module | Exports | Use it for |
| --- | --- | --- |
| `llm.js` | `jsonFormat`, `readReply`, `readText`, `parseJsonReply` | Any Claude call that must return JSON. `jsonFormat(SCHEMA)` → `output_config`; `readReply(msg, "<skill>")` → the parsed object, or `null` on a refusal/truncated reply (it logs `stop_reason` + size). Never hand-parse a model reply. |
| `confirm.js` | `classifyConfirmation`, `CONFIRM_SCHEMA`, `buildConfirmSystem/User` | **Confirm-first writes.** `await classifyConfirmation(ctx, { action: "cancel the 15:00 meeting", who: "<skill>" })` → `confirm \| decline \| unrelated`. Any doubt or API error returns `unrelated` (the safe no-op), so an unclear message can never fire an irreversible write. The *session* stays yours — this only reads the latest message. |
| `google.js` | `googleAuth(env)` | The OAuth2 client from `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`. Build your own service on top: `google.tasks({ version: "v1", auth: googleAuth(env) })`. Adding a Google API means adding its **scope** to the refresh token (re-consent), not new auth code. |
| `identity.js` | `headerFor`, `TAGS`, `isOwnMessage`, `matchedTag` | The trigger tags and the reply header. |
| `format.js` | `frame` | Bold-header/italic-body framing — normally applied for you in `send()`; import it only if you bypass `ctx.send` (as `feature_request` does for a media caption). |
| `logbuffer.js` | `installLogBuffer`, `getRecentLogs`, `redact` | The secretary's own recent logs, in memory. Installed once by `server.js`; you almost never call this directly. |
| `selflearning.js` | `captureFailure`, `appendToReport`, `looksLikeFailure` | **Failure capture** — writes a Markdown report to `secretary/improvements/`. Wired into the orchestrator's catch blocks for you; a skill only calls it directly to report a failure the code *can't see* (as `feedback` does). See "Self-learning" below. |

Everything else a skill needs (`send`, `lang`, `sessions`, `anthropic`, `evolution`, `env`,
`hasSkill`/`callSkill`) arrives on **`ctx`** — see `server.js`. If you find yourself editing
the orchestrator to add a skill, that's the signal `ctx` or this lib is missing something:
fix it **once**, here, rather than reaching around it.

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
loaded skill lands in `CATALOG`, the router's menu, so a skill the router must never pick is
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
  `ctx.callSkill` spreads the ctx, so a boolean flag set by a callee would mutate a copy and
  never reach the caller.
- **Machine failures dedupe (10 min) and are capped (~20/h)** — a crash loop must not fill the
  droplet's disk. **Owner reports do neither**: a human can't loop, two notes are two
  complaints, and a silently dropped note is the worst failure this system has.
- **Secrets are redacted on the way *into* the ring buffer**, and the whole report again on the
  way out — these files live in a git repo.
- Reports are written **inside** `secretary/` because the container only mounts the app dir,
  and they are **gitignored** because `/opt/secretary` symlinks into the production git tree.

**The loop:** `scripts/self-learning-daily.sh` runs at 09:00 daily (launchd): it pulls the
reports (Mac → droplet over SSH; pull-based because the droplet's deploy key is read-only) into
`Bugs and Malfunctions/inbox/`, then runs `/triage-failures` headless, which writes a
`Bugs and Malfunctions/bugfix-<slug>.md` plan per report and commits it. **It never pushes and
never deploys** — `git push`/`ssh`/`docker` are denied to it. The owner reviews and ships.

Self-tests: `scripts/selflearning-selftest.mjs` (capture invariants, offline) and
`scripts/router-selftest.mjs` (that a *complaint* is filed, not executed — needs an API key).
