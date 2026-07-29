# The Orchestrator

> **For humans — quick read.**
>
> The orchestrator is the secretary's front door. Every WhatsApp message hits it; it decides
> whether the secretary should act, works out *what* you want, and hands the job to the right
> **skill**.
>
> **What it does:**
> - Receives every message from WhatsApp (via the Evolution API webhook).
> - **Starts** a task only when *you* (the owner) write a trigger tag (`@mary`, by default).
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
- The router **degraded** (refused or produced an unparseable reply) on a first turn → *"I didn't
  understand what you want me to do. Available skills: …"* — this menu now fires **only** on a
  `degraded` router reply, not on every empty first-turn close. A legitimate empty close (the model
  deliberately ending chit-chat / a no-op) closes silently, and an `execute` that names no
  dispatchable skill closes silently too — neither speaks up nor files a report.
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
   header live in `lib/identity.js`: `NEW_TAGS` is parsed from `SECRETARY_TAG_NEW` (**comma-separated**,
   lowercased, default `@mary`), and the header
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
   a spread would drop its prototype.
3. **`loadSkills(dir = NEW_SKILLS_DIR)`** — scans `<dir>/*/skill.js` (`NEW_SKILLS_DIR` is
   `3. Mary Skills/`), dynamically `import()`s each, and requires `manifest.id` + `run()`. Returns
   `{ skills, catalog }`: `skills = { [id]: run }` and
   `catalog = [{id, description, inputs, conversation}]` (the router's menu — `inputs` is the
   skill's declared input contract, `manifest.inputs`, or `null`; **`conversation` is
   `"orchestrator"` if the manifest declares it, else `"skill"`** — the safe default, see
   "The conversation loop" below). Logs each `skill loaded: … -> id`. **Drop-in skills:** no edit
   here to add one. Skills export no skill-to-skill API — each is a pure task, and the model chains
   them across turns (see "Composing skills" below).

   Boot calls it **once**: `loadSkills(NEW_SKILLS_DIR)` → `NEW_SKILLS`/`NEW_CATALOG`, which
   `NEW_FLOW.catalog` points at and the turn loop dispatches against. Boot logs `mary skills: …`.
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
   - `gateText = text || attachmentCaption` (a captioned document has `text === ""`, so the tag
     rides its caption); `tag = fromMe ? matchedTagNew(gateText) : null`; `isTagged = !!tag` → a
     **fresh** command (owner only); `matchedTagNew` returns whichever tag in `NEW_TAGS` the message
     starts with (or null).
   - `isContinuation` = there's a `session`, it's not tagged, not one of the secretary's own
     messages, **and** the sender matches `session.awaitFrom`: `owner`→`fromMe`, `contact`→`!fromMe`, `any`→both.
   - If **neither** → `return` (ignored — incl. all non-owner messages with no session for them).
8. **Dedup** by `id` via the `seen` set (capped at 500).
9. `order` = text minus the tag (fresh) or the whole text (continuation);
   `number` = `remoteJid` before `@`.
10. **Build context:** `nowStr` (São Paulo); `conv = combine(buffer + evolution.fetchHistory(remoteJid))`
    → `transcript` via `buildTranscript` (`ME:` / `OTHER:`, last ~30); `contact` =
    last `OTHER` pushName. Logged as `TRANSCRIPT>>>`.
11. **Build `ctx`** (handed to router + skills): `owner, tag, tags, anthropic, model, order,
    transcript, nowStr, contact, remoteJid, number, fromMe, isTagged, quoted, hasQuotedAudio,
    catalog, env, evolution, sessions, settings, session, lang, send, sendFailure, info, media,
    _turn`.
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
    falls back to `NEW_TAGS[0]` and is therefore always truthy. A skill reads it to tell an order
    *addressed to it* from talk it merely overheard while a window was open (Tasks does —
    `3. Mary Skills/3. Tasks/SKILL.md`). `ctx.lang` is the
    conversation language, and it is **PINNED to the opening language for the life of the
    conversation** (card 3ec5be77). The first turn adopts the router's detection and persists it
    on the session marker as `openingLang`; every later turn — continuation or a fresh `@mary`
    that the marker still owns — HOLDS that pinned value via `resolveTurnLang(pinnedLang,
    reply.lang)` (`lib/lang.js`), so the router's per-turn `lang` is subordinate and can no longer
    drift the reply language EN↔PT mid-conversation. Default `"en"`; the pin lives only as long as
    the marker (a `done`/expiry drops it, so a genuinely new conversation re-detects). `ctx.send`
    is bound to it (see the localizing `send` above); the router's free-form `say` prose is
    force-translated onto the pin for the en↔pt residual (`localizeBody({force})`). `_turn` is the
    self-learning per-turn object (see below).
12. **Dispatch — the turn loop (see "The conversation loop" below).** Both a fresh tagged order
    and an untagged follow-up on a conversation the orchestrator owns feed the **same** multi-turn
    loop: a fresh order first `sessions.clear`s any stale session (a new `@mary` overrides), a
    continuation rebuilds its counters from the marker. Each turn is **`route(ctx, turn)`** — one
    Claude call that both classifies AND extracts — returning `{say, next, skills, info, lang,
    awaitFrom, degraded}`; the model drives a three-state cycle and the orchestrator runs each
    `NEW_SKILLS[task](ctx)` on an `execute`. A **degraded** router reply (refused/unparseable) on a
    first turn → "I didn't understand… Available skills: …" plus an `unrouted` capture; an `execute`
    that names no dispatchable skill closes silently (no menu, no report); per-skill errors → "I
    failed to run that task."

      **THE PAYLOAD GATE, and it is plain code — no AI.** The turn call also returns `info`: the
      **first** skill's declared inputs, as the model filled them. Before any skill sees it,
      `checkPayload(primary.inputs, info)` (`lib/inputs.js`) checks it against the *declaration*:
      is it an object, are the declared fields present, are the types right?
      - **shape-VALID** → it is handed to the primary skill as `ctx.info`. That skill skips its own
        extraction call. If the payload is valid but *incomplete* (no email for Laura) it is
        **still handed over** — the skill's own clarification pass fills the gap exactly as it
        does today. That is the "only if the check fails do we ask again" call.
      - **shape-INVALID, or the task declared no inputs** → `ctx.info` is `null` and the skill
        extracts for itself. Today's path, unchanged.

      So the worst case of the merge is **correct but slow**, never **fast and wrong** — and note
      that a *declared field that is absent* is INVALID, not defaulted. That distinction is the
      whole safety net: a skill that adds a schema field and forgets its declaration gets a slow
      turn, not a silently un-shipped feature.

### The conversation loop — the orchestrator holds the conversation

The turn loop is the **only** path: a **fresh tagged order, and every untagged follow-up on a
conversation the orchestrator itself owns, go through a MULTI-TURN LOOP** in which the model drives a
three-state cycle. The whole loop runs inside one `POST /webhook` request; only counters cross a
message boundary. The payload gate above (`checkPayload`) is applied on each dispatch inside it.

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

**The conversation marker — and yielding the key.** The orchestrator's own open session is a
**conversation marker** (`{ open, awaitFrom, lang, turns, dispatches, expiresAt }`) with **no
`skill` field** — it carries the loop's counters between messages. Before the orchestrator clears
**or** writes the marker it **re-reads the key** and leaves it alone if a dispatched skill has taken
it (a session that skill opened, carrying a `skill` field — its confirmation outranks the marker;
`sessions.set` is a full overwrite).

**Orchestrator-owned failures** each fire a `fireCapture` (existing plumbing): `turn_cap`,
`dispatch_cap`, `repair_giveup`, `readback_execute`, and `throw:readback` (a read-back call that
threw — the orchestrator stays **silent**, because the skill already wrote and already told him).

**One converted skill ships in this card: `assistant_settings`.** The other six are unchanged; each
gains only a redundant explicit `conversation: "skill"` line (except Feature Requests, whose absent
declaration correctly defaults to `"skill"`).

### Inbound media relay (card cf60f344) — files reach the turn call as multimodal content

A `@mary` turn that carries files (a receipt, an invoice) relays them to the turn call as Anthropic
**multimodal content**, interpreted **on the turn they arrive** — no cross-turn persistence.

- **Detection — `inboundMedia(data, quoted)` (`lib/whatsapp.js`).** At intake (step 4) it returns
  the turn's media **LIST** (attachment first, then the quoted file). It handles **both** documented
  webhook shapes defensively — bare `documentMessage` and the `documentWithCaptionMessage` wrapper —
  and always reads the media id from `data.key.id`. `video` is detected only so it can be
  **deferred**, never relayed. **`audio` is NOT relayed at all — it is OMITTED from the media list**
  (neither a direct `audioMessage` attachment nor a quoted audio produces an entry). The AI can't
  take audio natively; audio is handled by `transcribe_audio` via **normal routing**
  (`ctx.hasQuotedAudio`, which this detector leaves untouched), never intercepted by the relay — so
  replying to a voice note still reaches `transcribe_audio`.
- **Gate open (captioned document).** A captioned document's `text` is `""` (`extractText` has no
  document branch), so the tag matcher and the order derivation also read the **attachment caption**
  (the `gateText`/`attachmentCaption` locals in the gate). This lets a captioned PDF *start* the flow
  and carries its caption instruction as the order on both the first (tagged) turn and a mid-session
  (untagged) continuation.
- **The extension point — `mediaBlockFor({ mediaType, mimetype, base64 })` (`lib/whatsapp.js`).**
  The single "is this type supported? → native block, or defer" decision, plus the two ship-now
  native handlers: **image** (`image/jpeg|png|gif|webp`) → an `image` block; **document**
  (`application/pdf`) → a `document` block; **everything else → `null`** (deferred). `media_type`
  comes from the **real** mime, never a hard-coded default, so `getMediaBase64`'s `audio/ogg`
  fallback can never be trusted onto an image/PDF block. **A future file type is added HERE (one new
  branch) + its converter — no other rails file changes.** *Audio is the exception:* it is **not**
  relayed and does **not** flow through `mediaBlockFor` — `inboundMedia` omits it from the media list
  entirely, so the sibling audio-input card handles audio through the **skill/routing path**
  (`transcribe_audio`), not this relay.
- **Media prep (before the turn loop, `server.js`).** Enforce `MAX_FILES_PER_TURN` (10; over it →
  `fileTooMany` and close). For each file: download via `evolution.getMediaBase64`, enforce the
  per-file byte cap (`IMAGE_MAX_BYTES` 5 MB / `PDF_MAX_BYTES` 32 MB), then route through
  `mediaBlockFor`. Per-file failures accumulate into **one consolidated note per distinct reason**
  (`fileDownloadFailed` / `fileTooLarge` / `fileUnsupported`, fixed order, each at most once — never
  a silent drop). If **nothing** is readable, the notes are sent and the turn closes without routing.
  Otherwise `ctx.media = { blocks, model: VISION_MODEL }`.
- **Third media source — reference-gated recent-history fallback (card beba8beb).** Besides the
  attachment and the quote, MEDIA-PREP has a third source: when a turn carries **no** on-turn media
  **and** its words refer to a file (`mentionsFile(order)`, an en+pt keyword heuristic), it sources
  the **single most-recent relayable file from the last hour** (`historyMediaFile`, `lib/whatsapp.js`)
  and relays it exactly like an on-turn file. An unrelated turn (calendar/time/chit-chat) pulls
  nothing. This is **best-effort**: a history-sourced file that fails to download is silently dropped
  (no problem-note, no early return) and the turn routes text-only, so an inferred file never hijacks
  a turn the owner didn't mean about a file. To support it, the Evolution client's `fetchHistory` now
  carries **per-row media** (`mediaId`/`mediaType`/`mimetype`; additive — text-only rows are unchanged
  and still dropped from the transcript by `combine`).
- **The turn call — `route()` (`router/router.js`).** When `ctx.media` is present **and the turn is
  not a read-back**, `route()` builds an **N-block** `content` array (**media before text**, the
  empty text block omitted) and pins the create call's model to `media.model`. A read-back carries
  no file (Edge 15); a repair keeps the media. With no media, `content` is the byte-identical
  `buildRouterUser` string and the model is `ctx.model`. `buildRouterUser` also gains one conditional
  model-facing line when files are attached.
- **The vision-model pin — `VISION_MODEL`** (env `VISION_MODEL`, default `claude-sonnet-5`;
  `claude-haiku-4-5` also supports vision + PDF). A file-carrying turn uses this model **independent
  of `CLAUDE_MODEL`**, so the droplet's model choice never disables file reading. Non-file turns are
  unaffected.
- **The four plumbing notices** — `fileDownloadFailed`, `fileTooLarge`, `fileTooMany`,
  `fileUnsupported` — are new `ORCH_MSG` keys (`en`+`pt`), sent via the bare `send()`. They are the
  orchestrator's own informational notices (the `turnCap`/`dispatchCap` category), **not**
  `*Failed`/`*Error` failure replies.

### Tag settings — the durable summon list

The accepted tag list (`NEW_TAGS`, default `@mary`) is durable: the owner can change it at runtime
by asking (the `assistant_settings` skill), which mutates `NEW_TAGS` via `setNewTags` and persists it
to a namespaced settings key (`createSettings({ ns: "new" })` → `secretary:settings:new:tags`). At
**boot** the stored list is loaded over the `SECRETARY_TAG_NEW` seed: `await newSettings.ready` →
`newSettings.loadTags()` → `setNewTags`, so a stored value wins and the boot log prints `new-tags:`
with its source.

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
| the degraded `done` branch | `unrouted` (a *missing capability*, not a bug — the highest-signal machine report) |

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
report per webhook turn, but it is also the turn's shared scratch: `ctx.sendFailure` and the
read-back both read and write it (`_turn.said`, `_turn.captured`, `_turn.skill`). A boolean flag
would be copied by value at each read/write and the writers would never see each other's updates, so
the state has to live on one shared object whose *reference* every reader holds.
`scripts/selflearning-selftest.mjs` pins this so a refactor can't quietly reintroduce a flag.

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

### `ctx.dmOwner(text)` — an additive private note to the owner
An **additive** `ctx` field: it sends a framed, localized message (through the same `send()`
choke point, on `ctx.lang`) to the owner's **own** number — `OWNER_NUMBER = process.env.OWNER_JID
|| process.env.OWNER_NUMBER || null` — rather than to the current chat. It is a **no-op when
`OWNER_NUMBER` is unset** (returns without sending), and it does **not** record onto
`ctx._turn.said` (a side note, not this chat's outbound reply). It exists because the orchestrator
only ever holds the *current* chat's number, which in an `awaitFrom:"contact"` booking is the
**guest's**; originating a note to the owner needs a distinct send path. Additive — no existing
`ctx` field or `send()` caller changes; today only `calendar_action` calls it (the Contacts
"email saved" note). Skills guard the call with `typeof ctx.dmOwner === "function"` so they stay
safe where the field is absent.

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

### Composing skills — the model chains them
Skills never import or call each other; there is no in-code skill-to-skill registry. Each skill is a
**pure task** that runs, sends one outcome message, and returns. Composition happens in the **turn
loop**: the model can name more than one skill for a single `execute` batch, and a converted skill's
**read-back** (its return value, shown back to the model on the next turn) lets the model decide a
follow-up `execute`. So a job that needs two skills is chained by the model across turns — not by one
skill reaching into another.

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
