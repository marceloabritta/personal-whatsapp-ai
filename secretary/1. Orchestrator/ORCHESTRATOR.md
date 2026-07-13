# The Orchestrator

> **For humans — quick read.**
>
> The orchestrator is the secretary's front door. Every WhatsApp message hits it; it decides
> whether the secretary should act, works out *what* you want, and hands the job to the right
> **skill**.
>
> **What it does:**
> - Receives every message from WhatsApp (via the Evolution API webhook).
> - **Starts** a task only when *you* (the owner) write a trigger tag (`@assistente`/`@assistant`).
> - Once a task is mid-conversation (e.g. a cancel awaiting your "yes"), it lets the
>   follow-up through **without** the tag — and can even pick up the *other person's*
>   reply — while ignoring normal chatter.
> - Figures out the intent (the **router**) and runs the matching skill.
> - Adds the language-aware header (`[Marcelo's AI Assistant]:` / `[Assistente IA do Marcelo]:`) to every reply it or a skill sends.
>
> You never call the orchestrator directly — you call skills, and it routes you there.

## Messages the orchestrator itself sends

Most replies come from skills. The orchestrator only speaks up on routing/plumbing
problems (all prefixed with the language-aware header from `headerFor(lang)`):
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
   (default `secretary`; prod overrides to `secretaria`), `CLAUDE_MODEL` (default
   `claude-sonnet-5`), `OWNER_NAME`, `ANTHROPIC_API_KEY`, `REDIS_URL` (default
   `redis://evolution_redis:6379`; set empty to force in-memory). The trigger tags and reply
   header live in `lib/identity.js`: `TAGS` is parsed from `SECRETARY_TAG` (**comma-separated**,
   lowercased, default `@assistente,@assistant`; the old `@brain` is retired), and the header
   is produced per-language by `headerFor(lang)` (en → `[Marcelo's AI Assistant]:`, pt →
   `[Assistente IA do Marcelo]:`, from `OWNER_NAME`) — there is no single `HEADER` const anymore.
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
   in-memory short-term buffer, even ones that won't trigger the secretary. (Context.)
4. **`getQuoted(data)`** → `quoted = { id, hasAudio, mediaType, text, calendarLink } | null`
   (the replied-to message; Evolution puts a plain-text reply's context at the *sibling*
   `data.contextInfo`).
5. **`isOwnMsg`** = `isOwnMessage(text)` (from `lib/identity.js`) — true when the text starts
   with **any** header variant the secretary could have emitted (both languages **plus** the
   legacy `[AI Brain]:` for its own older messages), so the secretary's own sends are never
   acted on.
6. **`session = await sessions.get(remoteJid)`** — any open per-chat state.
7. **The gate (start vs continue vs ignore):**
   - `isTagged = fromMe && !!matchedTag(text)` → a **fresh** command (owner only); `matchedTag`
     returns whichever tag in `TAGS` the message starts with (or null).
   - `isContinuation` = there's a `session`, it's not tagged, not one of the secretary's own
     messages, **and** the sender matches `session.awaitFrom`: `owner`→`fromMe`, `contact`→`!fromMe`, `any`→both.
   - If **neither** → `return` (ignored — incl. all non-owner messages with no session for them).
8. **Dedup** by `id` via the `seen` set (capped at 500).
9. `order` = text minus the tag (fresh) or the whole text (continuation);
   `number` = `remoteJid` before `@`.
10. **Build context:** `nowStr` (São Paulo); `conv = combine(buffer + evolution.fetchHistory(remoteJid))`
    → `transcript` via `buildTranscript` (`ME:` / `OTHER:`, last ~30); `contact` =
    last `OTHER` pushName. Logged as `TRANSCRIPT>>>`.
11. **Build `ctx`** (handed to router + skills): `owner, tag, anthropic, model, order,
    transcript, nowStr, contact, remoteJid, number, fromMe, isTagged, quoted, hasQuotedAudio,
    catalog, env, evolution, send, sendFailure, sessions, session, lang, hasSkill,
    callSkill, _turn`.
    `session` is set **only** on a continuation (else `null`).
    `isTagged` — did **THIS** message carry a tag? `true` on a fresh command, and **always
    `false` on a continuation** (a tagged message is never a continuation — see the gate at
    step 7). It is the only honest source of that bit: **`ctx.tag` is not a substitute**, it
    falls back to `TAGS[0]` and is therefore always truthy. A skill reads it to tell an order
    *addressed to it* from talk it merely overheard while a window was open (Tasks does —
    `2. Skills/3. Tasks/SKILL.md`). `ctx.lang` is the
    conversation language — from the session on a continuation, from the router on a fresh
    command (set after `route()` returns), default `"en"`; `ctx.send` is bound to it (see
    the localizing `send` above). `ctx.hasSkill`/`ctx.callSkill` are the capability-registry
    helpers (see "Composing skills" below). `_turn` is the self-learning per-turn flag (see
    below).
12. **Dispatch:**
    - **Continuation** → **bypass the router**, run `SKILLS[session.skill](ctx)` directly
      (the skill reads `ctx.session` and decides). Missing skill → `sessions.clear`. Errors
      → "I failed to continue that."
    - **Fresh** → first `sessions.clear` any stale session (a new `@secretary` overrides), then
      **`route(ctx)`** (one Claude call via the router) → `tasks[]`, validated against the
      catalog. Empty/unknown → "I didn't understand… Available skills: …". Otherwise run
      each `SKILLS[task](ctx)` in order; per-skill errors → "I failed to run that task."

### Self-learning — the orchestrator's failure capture
`installLogBuffer()` (`lib/logbuffer.js`) runs **first**, above everything that logs: it wraps
`console` so stdout is unchanged (`docker logs` still works) while every line also enters a
redacted, truncated 500-entry ring the secretary can read back about itself.

`fireCapture(ctx, info)` → `captureFailure` (`lib/selflearning.js`) writes a Markdown report to
`secretary/improvements/`. It's wired into **four** places, always **after** the user has
already received their error reply:

| Where | Phase |
|---|---|
| the continuation catch | `throw:continuation` |
| the router catch | `throw:router` |
| the per-skill catch | `throw:skill` |
| the `notUnderstood` branch | `unrouted` (a *missing capability*, not a bug — the highest-signal machine report) |

**Plus `ctx.sendFailure(number, text)` — the `soft` phase, and the one that fires most.** Most
failures never reach a catch block: the skill understands the order, fails to execute it, and
*says so* ("I understood the request but failed to create it in Google", "I hit an error while
thinking", "Something went wrong with your tasks"). `sendFailure` sends exactly like `ctx.send`
and **always** files a report; 29 call sites across the four skills use it. `ctx._turn.skill`
(set before each dispatch) names the culprit skill in that report.

**A malfunction is exactly three things:** a code error, a soft landing of an *uncompleted
task* (declared via `sendFailure`), and the owner reporting a mistake. Everything else is
**guidance** — "reply to the audio you want", "which task did you mean?", "your list is empty" —
and guidance is the secretary *working*, so it stays on plain `ctx.send` and files nothing.

**`ctx.send` is never scanned.** No regex, no sniffing. Text can't be classified by keyword:
the version that tried missed "I hit an error while thinking" (no failure word in it) *and*
flagged "I couldn't find: X. Which one did you mean?" — a question — as a defect. Only the
skill knows which it just sent. A **lint** in `scripts/selflearning-selftest.mjs` catches a
skill that forgets, at test time, with the file and line.

The orchestrator's own `ORCH_MSG` replies go through the **bare `send()`**, not `ctx.send` —
they're already covered by the catch block or the `unrouted` branch that produced them.

The fifth trigger, **`reported`**, is the only one a human pulls: the `feedback` skill, when the
owner says the secretary got something wrong.

**`ctx._turn` is an object, not a boolean, and that is load-bearing.** It caps capture at one
report per webhook turn — but `ctx.callSkill` hands the callee `{ ...ctx, _skillDepth }`, a
**spread**. A boolean flag set by a callee would mutate a *copy* and never be seen by the
caller, so the flag has to live on a shared object whose *reference* the spread copies.
`scripts/selflearning-selftest.mjs` pins this so a refactor can't quietly reintroduce it.

### `send(number, text)` — the localizing choke point
Prepends the language-aware header (`headerFor(ctx.lang)` from `lib/identity.js`) + a blank
line and calls `evolution.sendText`. The single choke point
for every user-facing message (skills call `ctx.send`). It also **localizes**: skills and
the orchestrator author each message in `en`/`pt` (the maintained languages), so those
pass through untouched; for any **other** detected language (`ctx.lang`) it LLM-translates
the **body only** (a cheap model, `TRANSLATE_MODEL`) — the header is added afterwards and
is never translated (it comes from `headerFor(lang)`, which falls back to the English header for
unmaintained languages). English never calls the model. Skills receive a `ctx.send` already
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
continue without the `@secretary` tag. Shape:
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
`get / set / clear`. A fresh `@secretary` command clears any stale session first (starting over
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
  frames the language-aware header (`headerFor(lang)`) inside `caption`, exactly as `send()` does for text.
  Used by `feature_request` to send its generated `.md` spec as a document.
- **Anthropic:** one router call per **fresh** command (continuations skip it; the skill
  does its own LLM work).
- **Redis:** one `get` per inbound message; `set`/`clear` are driven by skills.
- **Timeouts:** the orchestrator loop has none of its own — it `await`s the router/skill
  and returns. Conversation timeouts live in the **session TTLs** the skills set.
- **Completes:** the HTTP 200 is sent up front; the handler finishes when the chosen
  skill(s) return (or an early `return` ignores the message).
