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
- **A conversation loops without closing (the turn cap) → *"I'm going in circles…"*** (`turnCap`)
- **Too many skills fired in one conversation (the dispatch cap) → *"I've done a few things in a row…"*** (`dispatchCap`)
- **A converted skill's payload failed validation twice → *"I couldn't get that right…"*** (`repairGiveUp`)
- **A second converted skill was asked for in one batch and can't run there → *"…send me the other part on its own."*** (`dispatchSkipped`)

> **Flow diagram note:** the end-to-end flow diagram in `../../ARCHITECTURE.md` and `../../README.md`
> is **pending** — those docs are owned by another card's in-flight work and were not touched here.
> This file is the authoritative description of the new turn loop until they catch up.

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
   **`anthropic` is WRAPPED, once, here:** `withThinkingDefault(new Anthropic({…}))` (`lib/llm.js`).
   It is the **only** `new Anthropic(` in the product, and every call site reaches it through
   `ctx.anthropic` — so all of them send `thinking: {type:"disabled"}`, **and a skill written next
   month inherits that without knowing it exists.** *Why:* extended thinking is **on by default**
   on `claude-sonnet-5`, and both `readText()` and the router's reader keep only `text` blocks —
   the model reasoned, we waited for it, we paid for it, and we deleted it (~4.6s of every 16s
   turn). A call site that genuinely wants reasoning **passes its own `thinking`** and the wrapper
   leaves it alone. The wrapper is a `Proxy`, not a spread — the SDK client is a class instance and
   a spread would drop its prototype. `scripts/turn-latency-selftest.mjs` T1.5 lints that exactly
   one client exists and that it is wrapped.
3. **`loadSkills(dir = SKILLS_DIR)`** — scans `<dir>/*/skill.js`, dynamically `import()`s each,
   and requires `manifest.id` + `run()`. Builds `SKILLS = { [id]: run }` and
   `CATALOG = [{id, description, inputs, conversation}]` (the router's menu — `inputs` is the
   skill's declared input contract, `manifest.inputs`, or `null`; **`conversation` is
   `"orchestrator"` if the manifest declares it, else `"skill"`** — the safe default, see
   "The conversation loop" below). Also collects each skill's
   **optional** `capabilities` export into `CAPS = { [id]: { [name]: fn } }` — the
   internal skill-to-skill API (see "Composing skills" below). Logs each
   `skill loaded: … -> id` (with its capabilities, if any). **Drop-in skills:** no edit
   here to add one.

   **RAILS CHANGE (a) — per-flow discovery (2026-07-15).** `loadSkills` is now **parametrized**
   (`dir` defaults to `SKILLS_DIR = "2. Skills/"`, so the existing zero-arg call is unchanged), and
   boot calls it **twice**: `loadSkills()` → `SKILLS`/`CATALOG`/`CAPS` for `@assistant`, and
   `loadSkills(NEW_SKILLS_DIR = "3. Mary Skills/")` → `NEW_SKILLS`/`NEW_CATALOG` for `@mary`
   (`NEW_FLOW.catalog = NEW_CATALOG`, and the six NEW-loop `SKILLS`/`CATALOG` references are
   repointed to the NEW maps). Boot logs both `available skills:` (old) and `mary skills:` (new).
   **`CAPS` is discovered only on the OLD tree and is NOT repointed** — its sole consumers are the
   shared `ctx.hasSkill`/`ctx.callSkill` closure (built before the flow split), which the legacy
   Tasks→Calendar `startCreate` delegation depends on; the converted tree exports no capabilities
   and needs none (the model chains skills itself).
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
    catalog, env, evolution, send, sendFailure, sessions, session, lang, info, hasSkill,
    callSkill, _turn`.
    `session` is set **only** on a continuation (else `null`).
    **`ctx.info`** — the skill's **declared inputs**, already extracted by the router in the same
    call that classified the order (see step 12), and already checked by plain code. It is set on
    the dispatch loop and it is **scoped to `tasks[0]`: every other skill on the turn is handed
    `null`** and extracts for itself. A skill reads it as *"my extraction may already be done"*:
    `let info = ctx.info ?? null; if (!info) info = await interpret(ctx);`. It is `null` on a
    continuation, on a shape-invalid payload, and for any skill that declared no `inputs` — in
    every one of those cases the skill falls back to its own call, which is the old behaviour,
    unchanged. **A skill that ignores `ctx.info` is untouched by any of this.**
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
      **`route(ctx)`** — **ONE Claude call that both classifies AND extracts** → `{tasks, lang,
      info}`. `tasks[]` is validated against the catalog; empty/unknown → "I didn't understand…
      Available skills: …". Otherwise run each `SKILLS[task](ctx)` in order; per-skill errors →
      "I failed to run that task."

      **THE GATE, and it is plain code — no AI.** The merged call also returns `info`: the
      **first** task's declared inputs, as the model filled them. Before any skill sees it,
      `checkPayload(primary.inputs, info)` (`lib/inputs.js`) checks it against the *declaration*:
      is it an object, are the declared fields present, are the types right?
      - **shape-VALID** → it is handed to `tasks[0]` as `ctx.info`. That skill skips its own
        extraction call. If the payload is valid but *incomplete* (no email for Laura) it is
        **still handed over** — the skill's own clarification pass fills the gap exactly as it
        does today. That is the "only if the check fails do we ask again" call.
      - **shape-INVALID, or the task declared no inputs** → `ctx.info` is `null` and the skill
        extracts for itself. Today's path, unchanged.

      So the worst case of the merge is **correct but slow**, never **fast and wrong** — and note
      that a *declared field that is absent* is INVALID, not defaulted. That distinction is the
      whole safety net: a skill that adds a schema field and forgets its declaration gets a slow
      turn, not a silently un-shipped feature.

### The conversation loop (card 55e00052) — the orchestrator holds the conversation

The dispatch above is the **legacy path**, and it stays live for skills that run their own
dialogue (`conversation: "skill"` — six of seven skills today). What changed is that a **fresh
tagged order, and every untagged follow-up on a conversation the orchestrator itself owns, now go
through a MULTI-TURN LOOP** in which the model drives a three-state cycle. The whole loop runs
inside one `POST /webhook` request; only counters cross a message boundary.

**`manifest.conversation` — a new, additive skill-contract field.** `"skill"` (default; absent ⇒
`"skill"`) means the skill asks/confirms for itself, exactly as today. `"orchestrator"` means the
skill has handed its conversation over: the model proposes/confirms; the skill just **acts, sends
one outcome message, and returns**. It is rendered into the router prompt as **opaque text** (a
`CONVERSATION:` line, `lib/inputs.js` `describeSkill`) and read in code for exactly two decisions:
which `checkPayload` tier gates the dispatch, and whether a read-back happens.

**`run(ctx)` return contract — additive.** `run()` may now **return** a JSON-serializable value.
`undefined` (today's shape, all six unconverted skills) ⇒ no read-back, the cycle ends. Any other
value ⇒ the orchestrator serializes it (truncated to `READBACK_CAP` bytes) and makes **one more
turn call** — the *read-back* — showing the model the result and the prose the skill already sent.

**`route(ctx, turn)` — the turn call.** `route` gained a second argument
`turn = { labeledTranscript, readback? }` and now returns the control signal
`{ say, next, skills, info, lang, awaitFrom }` (was `{ tasks, lang, info }`). Still **no
`output_config`** — the reply shape is demanded in the prompt (`router/prompt.js`), and the
read-back turn reuses the **same** system prompt (only the user message differs), so both calls stay
on the generic path. The model reads a **labelled** transcript (`buildLabeledTranscript` —
`OWNER`/`SECRETARY`/`CONTACT`, so it can tell her own past words from his); `ctx.transcript` (the
unlabelled `ME:`/`OTHER:` string) is **unchanged**, so the six unconverted skills' own extractors
see today's exact bytes. The labelled transcript is a plain webhook-handler local passed as the
`route()` argument — **not** a `ctx` field, so the `ctx` surface is unchanged.

**The three states, crossed with `say` (prose | null):**
- **`listen`** — reply (or stay silent) and keep the conversation open; the model declares
  `awaitFrom` (`owner`/`contact`/`any`) for who to listen to next.
- **`execute`** — run `skills` now with `info` (the first skill's payload). Dispatch is the same
  dual-intent batch as today: deduped, order preserved, **only `skills[0]` receives `info`**.
- **`done`** — the conversation is over.

**The tier is chosen by `conversation`:** an `"orchestrator"` primary is gated on **`ok`** (all
three `checkPayload` tiers) — a failure is the **repair loop**, *not* a dispatch: the problems are
rendered back to the model (`describeProblems`), which retries; after `MAX_REPAIRS` consecutive
failures it gives up (`repairGiveUp`). A `"skill"` primary keeps today's **`shapeOk`** gate.

**RAILS CHANGE (b) — `inputs:null ⇒ dispatch-without-validation` (2026-07-15).** An `"orchestrator"`
primary that declares **no** inputs (`manifest.inputs == null`, e.g. `transcribe_audio`) is
dispatched **directly** (`infoFor = null`) instead of being gated on `ok`. Without it,
`checkPayload(null, …).ok === false` would trap such a skill in the repair loop forever. The
declared-inputs path is unchanged — the existing `checkPayload` gate is simply moved verbatim into
the `else` branch, so `assistant_settings` and every declared skill behave exactly as before.

**Read-back vs repair — two different follow-up turns, two different prompts.** A read-back
(`turn.readback`) shows the model a dispatch's result and **forbids** executing again (the write
invariant); a repair (`turn.repair`) shows the model its validation problems and **invites** a
corrected execute. They are mutually exclusive and each has its own user prompt
(`buildReadbackUser` vs `buildRepairUser` in `router/prompt.js`), sharing the same system prompt so
both stay on the generic no-`output_config` path. (An earlier build reused the read-back prompt for
the repair turn, so the prompt told the model it may NOT execute on the exact turn the repair loop
needs it to — fixed here.)

**The caps (module-locals in `server.js`) — the model can loop on skills, so the bound is code:**
- **`MAX_TURNS = 10`** — *productive* turns only. **A deliberate-silence turn
  (`{say:null, next:"listen"}`) is FREE** and does not count: the secretary listens to a real
  human thread and must stay silent on chatter without the conversation dying.
- **`MAX_DISPATCHES = 3`** — a **DISPATCH ceiling, NOT "3 writes".** Under a read-back design a
  dispatch can be a *read* (a future calendar delete costs two dispatches for one write). Do not
  re-document this as a write ceiling, and do not size the next card's constant against a pilot
  that never reads.
- **≤ 1 successful dispatch per incoming message — the WRITE INVARIANT.** A **read-back turn may
  not `execute`**: the orchestrator refuses it, treats it as `done`, and files a report
  (`readback_execute`). An autonomous write-loop is structurally impossible — a second write needs
  a new owner message.

**`ctx.send` / `ctx.sendFailure` now also record the body they sent onto `ctx._turn.said`** —
additive, invisible to every caller — because that is the outcome message the read-back shows the
model. `sendFailure` records too, so a *failing* read-back does not re-narrate.

**Two kinds of open session — the coexistence gate.** A session with a **`skill` field** is a
legacy skill session → the bypass at step 12, byte-for-byte unchanged. A session with **no `skill`
field** is the orchestrator's own **conversation marker** (`{ open, awaitFrom, lang, turns,
dispatches, expiresAt }`) → the turn loop. Before the orchestrator clears **or** writes the marker
it **re-reads the key** and leaves it alone if a dispatched skill has taken it (its confirmation
outranks the marker; `sessions.set` is a full overwrite). **This two-kinds gate is temporary
machinery with a named end date: it is deleted by the last skill-conversion card.**

**Orchestrator-owned failures** each fire a `fireCapture` (existing plumbing): `turn_cap`,
`dispatch_cap`, `repair_giveup`, `readback_execute`, and `throw:readback` (a read-back call that
threw — the orchestrator stays **silent**, because the skill already wrote and already told him).

**One converted skill ships in this card: `assistant_settings`.** The other six are unchanged; each
gains only a redundant explicit `conversation: "skill"` line (except Feature Requests, whose absent
declaration correctly defaults to `"skill"`).

### Dual-tag parallel run (@assistant = OLD, @mary = NEW) — temporary scaffolding

The turn loop above does **not** replace the legacy dispatch in place. Both run in one process,
chosen by the **summon tag** on each message, so the new architecture can be tested live without
risking the owner's daily driver. The branch is made **as early as possible** in the webhook
handler (server.js), before any flow-specific logic:

- **`@assistant` (`SECRETARY_TAG`) → the LEGACY flow** (`runLegacyFlow`): the pre-card `route →
  dispatch`, run on **frozen copies** of the pre-card code under `1. Orchestrator/legacy/`
  (`router.js`, `prompt.js`, `inputs.js`, `assistant-settings.js`, `assistant-settings-prompt.js` —
  the deleted propose/`classifyConfirmation` flow). None of it is imported by the NEW flow. This is
  **byte-for-byte the committed (HEAD) behaviour.**
- **`@mary` (`SECRETARY_TAG_NEW`) → the NEW flow**: the turn loop above.

**How the branch is decided.** A *tagged* message: `matchedTagNew` hit → NEW, else `matchedTag` hit
→ LEGACY (if a message somehow matched both disjoint lists, LEGACY wins, so @assistant is never
starved). A *continuation*: a session **with a `skill` field** → LEGACY bypass (every skill-session
continuation, including a NEW-flow-dispatched skill's own confirmation, is handed off through the
shared run and behaves identically); a **marker** (no `skill` field) → the NEW turn loop. The NEW
flow's converted `assistant_settings` never opens a skill session, so a `skill:"assistant_settings"`
session can only be the legacy propose/confirm flow — the split is unambiguous.

**The isolation is the whole point, and it is structural.** The NEW flow's `assistant_settings`
mutates a **separate** tag list (`NEW_TAGS` via `setNewTags`, not `TAGS`/`setTags`) persisted to a
**separate** settings key (`createSettings({ ns: "new" })` → `secretary:settings:new:tags`). `ctx`
is built **per flow** — `tags`, `catalog`, `settings` all point at the active flow's own state. So a
tag change (or any bug) in the `@mary` path is **incapable** of altering what `@assistant` answers
to. The two flows share only the invariant rails (Evolution I/O, `sessions`, `format`, the wrapped
Anthropic client, `logbuffer`, `selflearning`) — exactly what the legacy path used at HEAD.

**Boot** loads each tag list over its own seed independently: `settings.loadTags()` → `setTags`
(legacy), `newSettings.loadTags()` → `setNewTags` (new). The boot log prints both (`tags:` and
`new-tags:`). **This whole dual-tag apparatus is temporary:** when the migration completes, the
`legacy/` subtree, `NEW_TAGS`/`newSettings`, and the branch are removed and only the turn loop stays.

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
