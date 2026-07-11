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
   `CATALOG = [{id, description}]` (the router's menu). Logs each `skill loaded: … -> id`.
   **Drop-in skills:** no edit here to add one.
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
    catalog, env, evolution, send, sessions, session`. `session` is set **only** on a
    continuation (else `null`).
12. **Dispatch:**
    - **Continuation** → **bypass the router**, run `SKILLS[session.skill](ctx)` directly
      (the skill reads `ctx.session` and decides). Missing skill → `sessions.clear`. Errors
      → "I failed to continue that."
    - **Fresh** → first `sessions.clear` any stale session (a new `@brain` overrides), then
      **`route(ctx)`** (one Claude call via the router) → `tasks[]`, validated against the
      catalog. Empty/unknown → "I didn't understand… Available skills: …". Otherwise run
      each `SKILLS[task](ctx)` in order; per-skill errors → "I failed to run that task."

### `send(number, text)`
Prepends `HEADER` + a blank line and calls `evolution.sendText`. The single choke point
for every user-facing message (skills call `ctx.send`).

### State the orchestrator holds
- **`sessions`** (Redis / in-memory) — per-chat pending actions; skills open/clear them,
  the orchestrator only reads them to decide start-vs-continue. TTLs are set by skills.
- **In-memory buffer** (`remember`/`combine` in `whatsapp.js`) — recent messages per chat,
  merged with Evolution history to build the transcript. Lost on restart.
- **`seen`** — message-id dedup set (last 500).

### External touchpoints, timeouts, completion
- **Evolution:** `fetchHistory` (context) and `sendText` (replies) per handled message.
- **Anthropic:** one router call per **fresh** command (continuations skip it; the skill
  does its own LLM work).
- **Redis:** one `get` per inbound message; `set`/`clear` are driven by skills.
- **Timeouts:** the orchestrator loop has none of its own — it `await`s the router/skill
  and returns. Conversation timeouts live in the **session TTLs** the skills set.
- **Completes:** the HTTP 200 is sent up front; the handler finishes when the chosen
  skill(s) return (or an early `return` ignores the message).
