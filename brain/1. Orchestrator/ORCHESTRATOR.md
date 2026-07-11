# The Orchestrator

> **For humans — quick read.**
>
> The orchestrator is the brain's front door. Every WhatsApp message hits it; it decides
> whether the brain should act, works out *what* you want, and hands the job to the right
> **skill**.
>
> **What it does:**
> - Receives every message from WhatsApp (via the Evolution API webhook).
> - **Starts** a task only when *you* (the owner) write the `@brain` tag.
> - Once a task is mid-conversation (e.g. a cancel awaiting your "yes"), it lets the
>   follow-up through **without** the tag — and can even pick up the *other person's*
>   reply — while ignoring normal chatter.
> - Figures out the intent (the **router**) and runs the matching skill.
> - Adds the `[AI Brain]:` header to every reply it or a skill sends.
>
> You never call the orchestrator directly — you call skills, and it routes you there.

## Messages the orchestrator itself sends

Most replies come from skills. The orchestrator only speaks up on routing/plumbing
problems (all prefixed with `[AI Brain]:`):
- Couldn't classify the order → *"I didn't understand what you want me to do. Available
  skills: …"*
- The router call failed → *"I hit an error understanding the request. Try again?"*
- A skill threw while running → *"I failed to run that task. Error in the log."*
- A continuation's skill threw → *"I failed to continue that. Error in the log."*

---

## For AI / maintainers — detailed

File: `server.js`. Helpers: `lib/evolution.js`, `lib/whatsapp.js`, `lib/sessions.js`,
`router/router.js` (+ `router/prompt.js`). One long-running Node/Express process.

### Boot (once, at startup)
1. **Config from env:** `EVOLUTION_URL`, `EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE`
   (default `secretary`; prod overrides to `secretaria`), `SECRETARY_TAG` (→ `TAG`,
   default `@brain`, lowercased), `CLAUDE_MODEL` (default `claude-sonnet-5`),
   `OWNER_NAME`, `ANTHROPIC_API_KEY`, `REDIS_URL` (default
   `redis://evolution_redis:6379`; set empty to force in-memory). `HEADER = "[AI Brain]:"`.
2. **Clients:** `anthropic` (SDK), `evolution` (`createEvolution`), `sessions`
   (`createSessions` — Redis or in-memory fallback).
3. **`loadSkills()`** — scans `../2. Skills/*/skill.js`, dynamically `import()`s each,
   and requires `manifest.id` + `run()`. Builds `SKILLS = { [id]: run }` and
   `CATALOG = [{id, description}]` (the router's menu). Also collects each skill's
   **optional** `capabilities` export into `CAPS = { [id]: { [name]: fn } }` — the
   internal skill-to-skill API (see "Composing skills" below). Logs each
   `skill loaded: … -> id` (with its capabilities, if any). **Drop-in skills:** no edit
   here to add one.
4. **Express:** `GET /` health check; `POST /webhook`; `listen(3000)`.

### The webhook pipeline — `POST /webhook` (per message)
1. **`res.sendStatus(200)` immediately** so Evolution doesn't resend; everything else runs
   after, wrapped in try/catch (`"Webhook error"` on throw).
2. Read `data.key` → `{ fromMe, remoteJid, id }`; `text = extractText(data.message)`.
3. **`remember(remoteJid, …)`** — buffer **every** message (owner and contact) in the
   in-memory short-term buffer, even ones that won't trigger the brain. (Context.)
4. **`getQuoted(data)`** → `quoted = { id, hasAudio, mediaType, text, calendarLink } | null`
   (the replied-to message; Evolution puts a plain-text reply's context at the *sibling*
   `data.contextInfo`). Logged as `QUOTED>>>`.
5. **`isBrainMsg`** = text starts with `HEADER` — the brain's own sends are never acted on.
6. **`session = await sessions.get(remoteJid)`** — any open per-chat state.
7. **The gate (start vs continue vs ignore):**
   - `isTagged = fromMe && text.startsWith(TAG)` → a **fresh** command (owner only).
   - `isContinuation` = there's a `session`, it's not tagged, not a brain message, **and**
     the sender matches `session.awaitFrom`: `owner`→`fromMe`, `contact`→`!fromMe`, `any`→both.
   - If **neither** → `return` (ignored — incl. all non-owner messages with no session for them).
8. **Dedup** by `id` via the `seen` set (capped at 500).
9. `order` = text minus the tag (fresh) or the whole text (continuation);
   `number` = `remoteJid` before `@`.
10. **Build context:** `nowStr` (São Paulo); `conv = combine(buffer + evolution.fetchHistory(remoteJid))`
    → `transcript` via `buildTranscript` (`ME:` / `OTHER:`, last ~30); `contact` =
    last `OTHER` pushName. Logged as `TRANSCRIPT>>>`.
11. **Build `ctx`** (handed to router + skills): `owner, tag, anthropic, model, order,
    transcript, nowStr, contact, remoteJid, number, fromMe, quoted, hasQuotedAudio,
    catalog, env, evolution, send, sessions, session, lang, hasSkill, callSkill`.
    `session` is set **only** on a continuation (else `null`). `ctx.lang` is the
    conversation language — from the session on a continuation, from the router on a fresh
    command (set after `route()` returns), default `"en"`; `ctx.send` is bound to it (see
    the localizing `send` above). `ctx.hasSkill`/`ctx.callSkill` are the capability-registry
    helpers (see "Composing skills" below).
12. **Dispatch:**
    - **Continuation** → **bypass the router**, run `SKILLS[session.skill](ctx)` directly
      (the skill reads `ctx.session` and decides). Missing skill → `sessions.clear`. Errors
      → "I failed to continue that."
    - **Fresh** → first `sessions.clear` any stale session (a new `@brain` overrides), then
      **`route(ctx)`** (one Claude call via the router) → `tasks[]`, validated against the
      catalog. Empty/unknown → "I didn't understand… Available skills: …". Otherwise run
      each `SKILLS[task](ctx)` in order; per-skill errors → "I failed to run that task."

### `send(number, text)` — the localizing choke point
Prepends `HEADER` + a blank line and calls `evolution.sendText`. The single choke point
for every user-facing message (skills call `ctx.send`). It also **localizes**: skills and
the orchestrator author each message in `en`/`pt` (the maintained languages), so those
pass through untouched; for any **other** detected language (`ctx.lang`) it LLM-translates
the **body only** (a cheap model, `TRANSLATE_MODEL`) — the `HEADER` is added afterwards and
is never translated. English never calls the model. Skills receive a `ctx.send` already
bound to the conversation's `ctx.lang`, so their call sites don't pass a language.

The orchestrator's own strings ("I didn't understand…", router/continuation/skill errors)
live in an `en`/`pt` map (`ORCH_MSG` + `orch(lang, key, …)`); a non-en/pt language is
produced from the English copy by the same fallback. See the "Localization convention" in
`../../ARCHITECTURE.md`.

### State the orchestrator holds
- **`sessions`** (Redis / in-memory) — per-chat pending actions; skills open/clear them,
  the orchestrator only reads them to decide start-vs-continue. TTLs are set by skills.
- **In-memory buffer** (`remember`/`combine` in `whatsapp.js`) — recent messages per chat,
  merged with Evolution history to build the transcript. Lost on restart.
- **`seen`** — message-id dedup set (last 500).

### Sessions — shape & skill contract
A **session** is a short-lived pending action, keyed by `remoteJid`, that lets a flow
continue without the `@brain` tag. Shape:
```jsonc
{
  "skill": "calendar_action",     // which skill owns the follow-up (dispatch target)
  "intent": "delete",             // delete | create | edit …
  "stage": "await_confirmation",  // await_confirmation | await_info | await_clarification …
  "awaitFrom": "owner",           // who may continue: owner (fromMe) | contact (!fromMe) | any
  "lang": "pt",                   // conversation language — so the continuation replies in-language
  "data": { "eventId": "…", "title": "…", "when": "…" },  // skill-specific payload
  "expiresAt": 1720000900         // TTL — the skill sets it (e.g. 10–15 min)
}
```
The orchestrator only **reads** `awaitFrom`/`skill` (to gate + dispatch) and `lang` (to set
`ctx.lang` on the continuation, since continuations bypass the router that detects it);
**skills own the rest.** A skill opts into multi-turn via the store on `ctx`, persisting
`ctx.lang` so a later bare "yes" answers in the language the flow started in:
```js
// open a follow-up (in a fresh run)
await ctx.sessions.set(remoteJid, { skill:"calendar_action", intent:"delete",
  stage:"await_confirmation", awaitFrom:"owner", lang: ctx.lang,
  data:{ eventId, title, when } });

// on resume, ctx.session is the stored object; read ctx.session.data, then when done/cancelled:
await ctx.sessions.clear(remoteJid);
```
`ctx.session` is set **only** on a continuation (else `null`), and `ctx.sessions` exposes
`get / set / clear`. A fresh `@brain` command clears any stale session first (starting over
always wins). Skills that never call `ctx.sessions.set` behave statelessly, exactly as before.

### Composing skills — the capability registry
Skills compose without importing each other. `loadSkills()` collects each skill's optional
`capabilities` export into `CAPS = { [id]: { [name]: fn } }`, and the orchestrator injects
two helpers into every `ctx`:
```js
ctx.hasSkill = (id, name) => typeof CAPS[id]?.[name] === "function";
ctx.callSkill = async (id, name, ...args) => {          // auto-injects THIS ctx
  const fn = CAPS[id]?.[name];
  if (!fn) throw new Error(`capability ${id}.${name} unavailable`);
  const depth = (ctx._skillDepth || 0) + 1;             // loop guard
  if (depth > MAX_SKILL_DEPTH) throw new Error(`skill-call depth exceeded at ${id}.${name}`);
  return fn({ ...ctx, _skillDepth: depth }, ...args);
};
```
The callee receives the caller's `ctx` (shared `owner`/`lang`/`sessions`/`send`), so a
session it opens is tagged with the **callee's** `skill` id and its continuations route
back to the callee — the caller only initiates. A missing capability throws and is caught
by the per-skill try/catch (or the caller guards with `hasSkill` for a friendlier reply).
`capabilities` are **not** in the router catalog — they're internal, addressed by skill id
(rename-safe). Example: `task_action` turns a to-do assigned to another person into a
calendar invite by calling `calendar_action.startCreate`, never re-implementing create.

### External touchpoints, timeouts, completion
- **Evolution:** `fetchHistory` (context) and `sendText` (replies) per handled message.
  A skill may also call `evolution.sendMedia({ mediatype, mimetype, media, fileName,
  caption })` (`POST /message/sendMedia`, base64 `media`) to deliver a file — the caller
  frames the `[AI Brain]:` header inside `caption`, exactly as `send()` does for text.
  Used by `feature_request` to send its generated `.md` spec as a document.
- **Anthropic:** one router call per **fresh** command (continuations skip it; the skill
  does its own LLM work).
- **Redis:** one `get` per inbound message; `set`/`clear` are driven by skills.
- **Timeouts:** the orchestrator loop has none of its own — it `await`s the router/skill
  and returns. Conversation timeouts live in the **session TTLs** the skills set.
- **Completes:** the HTTP 200 is sent up front; the handler finishes when the chosen
  skill(s) return (or an early `return` ignores the message).
