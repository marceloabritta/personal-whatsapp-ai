# Plan — Router reliability & stateful @mary conversations

## Context

The `@mary` WhatsApp flow fails more than it should, and the causes trace almost entirely to the
**router layer** — the single Claude call (`route()` in `secretary/1. Orchestrator/router/router.js`)
that classifies every turn and extracts skill inputs, plus the prompt that drives it
(`router/prompt.js`). Four problems, each independently confirmed against the code:

1. **The reply shape is not enforced.** `route()` calls `messages.create({...})` with **no
   `output_config`** — the JSON contract lives only in the prompt text, and a brace-scanner
   (`parseJsonReply`) salvages replies that leak prose. When it can't, the turn silently degrades to
   *"I didn't understand."* This is self-inflicted: every skill and `lib/confirm.js` already use
   structured output (`output_config: jsonFormat(SCHEMA)`); the router is the one caller that opted out.
2. **The extracted payload can't be schema-locked in one call** because it's polymorphic (each
   task's inputs differ), so today it rides in a loose `info` object.
3. **The prompt never orients the assistant** on how the system works (WhatsApp, the tag, the
   messages+files she receives, inline replies stamped with a header, the system that drives her),
   and it carries only a thin task list rather than real per-task operating instructions.
4. **The router is stateless per turn.** It sees the conversation *messages* but not the
   **decisions it already made** in this context, so across loops it loses its own thread.

**Outcome:** every model call trades a perfect, schema-locked JSON; the assistant reasons from a
correct mental model of its environment and from real per-task instructions; and the interaction is
**stateful from the moment the tag fires**. All changes live in the orchestrator "rails"
(`secretary/1. Orchestrator/`); see **Rails changes** for the authorization the build review needs.

## Summary of changes

1. **Orientation preamble** — a data-filled "how the system works" section atop the main prompt.
2. **Enforced JSON on the decision call** — structured output on the classification turn.
3. **Two-phase execute** — decision call signals *what* to run; a second, per-task schema-enforced
   call produces the *perfect* payload.
4. **Per-task prompt sections + execute schemas** — author one rich section + one input schema for
   **all seven** tasks.
5. **Stateful conversation** — a conversation-state object opened at tag-start, carried through every
   loop and message, fed into every call.
6. **Drop `awaitFrom`** — track *what* the assistant is waiting for (held in state), not *who* may
   reply next; any participant can supply the missing info.

---

## 1 — Orientation preamble (data-filled) in the main prompt

Prepend a "how the system works" section to `buildRouterSystem()` (`router/prompt.js`), **above** the
existing states/contract and the task catalog, so it frames them. Intent (wording ours to refine):

- You are an AI assistant operating **inside WhatsApp**, in conversations between your boss and his
  contacts / group chats.
- The boss summons you with a **tag** (e.g. `@mary`); that starts your flow.
- When called you receive the **last N messages** of that conversation; any files exchanged in them
  are given to you too.
- You interact by **posting messages inline into the chat** — they appear as if from the boss, marked
  with a header like `[Boss's AI Assistant]`. Through them you talk to the boss **and** to whoever
  else he is talking to.
- You run **inside a system that calls you and executes your decisions.** Each turn it expects exactly
  one decision: send a message · keep listening · execute a function · done.
- Beyond general help you can call **functions (tasks)**; each has its own instructions (section 4).

**Data-filled, not hardcoded** — interpolate from live state: owner name, current trigger tag(s) and
reply header (`lib/identity.js`: `NEW_TAGS`, `headerFor`), the task list (`describeInputs(catalog)`,
already used), and **N** = the real transcript window (the `combine()` limit in `lib/whatsapp.js`).

**Files:** `router/prompt.js` (a small `buildSystemPreamble(...)` helper called by `buildRouterSystem`).

## 2 — Enforce JSON on the decision call

Adopt the pattern already proven codebase-wide (`lib/llm.js` → `jsonFormat`, used by `confirm.js` and
every skill). Define `ROUTER_REPLY_SCHEMA` for the decision **envelope** and pass
`output_config: jsonFormat(ROUTER_REPLY_SCHEMA)` on the classification `messages.create` (the normal,
read-back, and repair turns); read via `readReply(msg, "router")`.

Envelope fields: `say` (string|null), `next` (enum `listen|execute|done|answer`, required),
`skills` (array of string), `lang` (string). **No `info`** (payload comes from the two-phase
extraction, section 3) and **no `awaitFrom`** (dropped — see section 6). Do **not** touch `answer()`
(prose). Keep `parseJsonReply` as the documented fallback (it already notes it's for a model without
structured output).

**Files:** `router/router.js` (the three create calls + `readReply`); schema in `router.js` or
`router/prompt.js`. Reuse `lib/llm.js`.

## 3 — Two-phase execute (decision → schema-enforced per-task extraction)

Because the execute payload is polymorphic, split execution into two schema-locked calls instead of
one loose `info`:

- **Phase 1 — Decision call** (section 2): enforced envelope; when the AI wants to act it returns
  `next:"execute"` + the function id in `skills`. No inputs produced here.
- **Phase 2 — Extraction call** (new; only on execute): the system sees the function and makes a
  **new** `messages.create` with the **complete history/state** and
  `output_config: jsonFormat(<that task's INPUT schema>)` — the task's response format is *appended* to
  the call. The AI returns the perfect payload, e.g. `{ "say": …, <CALENDAR_CREATE input JSON> }`,
  which is dispatched to the skill. `checkPayload` stays a light safety net (types are schema-
  guaranteed; it still confirms completeness/consistency so "ask for what's missing" still works).

**Turn-loop change** (`server.js`): on `next:"execute"`, insert an extraction round before dispatch.
This restores a per-task extraction pass (like the pre-"pure-task" `EDIT RAW` call) — now schema-
enforced.

**Tradeoff (flag for reviewer):** +1 LLM call per execute, reversing part of the single-call merge —
a deliberate trade of one hop for perfect per-task JSON. Read/`listen`/`done` turns are unaffected.

**Files:** `router/router.js` (extraction-call builder), `router/prompt.js` (extraction user prompt),
`server.js` (loop: decision → extraction → dispatch), reuse `lib/llm.js`.

## 4 — Per-task prompt sections + execute schemas (all seven tasks)

The prompt is **one main prompt + a separate authored section per task, appended** — extending the
existing `rulebook()` mechanism (`describeInputs` already appends each skill's `rulebook()`;
calendar's is `buildExtractionRules()`). Enrich it from "extraction rules" to full operating orders,
and author one for **every** task: `calendar_action, task_action, flight_search, feature_request,
feedback, assistant_settings, transcribe_audio`.

**Per-task section template** — each section covers: (1) scope/when; (2) operating context (e.g.
calendar actions go out from the boss's connected Gmail account); (3) certainty rule (only call the
function once the full scope is correct — otherwise send messages asking for what's missing);
(4) defaults; (5) sub-operations (e.g. CRUD); (6) per-operation input spec + format.

**Draft all seven** to this template from each skill's current `rulebook()` + real behavior in its
`skill.js` (the calendar example the user gave is an **indication of the desired flow, not final
content**). Wording is ours; input specs must match each skill's real `manifest.inputs`.

**Each task also owns its machine-readable execute-JSON schema(s)** — one per operation whose inputs
differ (calendar create vs edit vs delete). This is the response format section 3's extraction call
appends. The prose input spec (item 6) is the human-readable twin, derived from the same
`manifest.inputs`, so the two can't drift. Each task ships: (a) its authored prompt section, (b) its
execute-JSON schema(s).

**Files:** each `3. Mary Skills/*/prompt.js` (its section, like `buildExtractionRules`) and
`skill.js`/`manifest.inputs` (its schema); `lib/inputs.js` (`describeInputs` assembly) and
`router/prompt.js` (composition) if the shape changes.

## 5 — Stateful conversation from the first tag (governing principle)

*Every interaction, from the moment the tag starts, must be stateful.* Reverse the current stateless-
router philosophy: when the tag first fires, open a **conversation state** that lives until the
conversation is done; every loop reads and writes it, and every call (decision, extraction, read-back,
repair) is given it — so on loop 3 the AI sees what it decided/did on loops 1–2 (the read-back only
carried the *immediately* prior result).

**State carries:** a **decision log** (per loop: index/time, `next`, `skills`, short `say` summary,
and for an execute the function + outcome); the **payload accumulated so far** (from the section-3
extraction calls, so filled slots aren't re-extracted); and the current **goal/intent**, the
**pending need** (what it's waiting for, e.g. "the guest's email" — see section 6), and phase.

**Recommended mechanism (implementer's to design):** grow the existing foundation — `lib/sessions.js`
+ the conversation **marker** in `server.js` already persist `awaitFrom`/`turns`/`dispatches`/`lang`
across messages in Redis (`MARKER_TTL`). Extend that into a real conversation-state object created at
tag-start; cap to recent-K + truncated summaries (same discipline as the transcript window /
`READBACK_CAP`).

**Prompt:** add a "Where you are in this conversation" block (goal + decision log) to the router user
prompts. **Files:** `server.js` (open/accumulate/persist state; pass into every call),
`router/prompt.js` (render the block).

---

## 6 — Drop `awaitFrom`: track WHAT it's waiting for, not WHO may reply

`awaitFrom` (owner|contact|any) is a **who-gate**, and the stateful model (section 5) makes it
useless. Once the tag is called the assistant reads the **entire conversation**, and the missing info
can legitimately come from *anyone*: if it asked the guest for their email, Marcelo may reply that it
isn't needed, or supply the email himself. Locking the next turn to a specific sender throws that away.

**Do:**
- **Remove `awaitFrom`** from the router reply (section 2 envelope) and from the state (section 5).
- **Open the continuation gate** (`server.js`, Stage 1): while a conversation is open, accept the next
  message from **any participant** (owner or contact) — stop discriminating by sender.
- The state instead holds the **pending need** ("waiting on the guest's email"). On each new message
  (from anyone) the assistant re-reads the stateful context and decides: it satisfies the need /
  changes it ("don't invite them") / is unrelated chatter (stay silent via `next:listen`, say:null).
  Relevance becomes the assistant's judgment over state, not a mechanical sender lock.

**Files:** `router/prompt.js` (drop `awaitFrom` from the contract; add the "pending need" to the state
block); `router/router.js` (drop it from the schema/return); `server.js` (open the continuation gate to
any sender; carry the pending need on state instead of `awaitFrom`).

## Rails changes (explicit authorization for the build review)

All edits are in `secretary/1. Orchestrator/` and are **authorized and additive**:
- `router/router.js` — add `output_config`/`readReply` to `route()`; add the extraction-call builder;
  decision call drops `info`. `route()`'s callers in `server.js` adapt to the two-phase flow.
- `router/prompt.js` — prepend the orientation preamble; add the extraction user prompt; render the
  state block. Additive text; the existing states/contract are preserved.
- `server.js` — turn loop gains an extraction round on execute; opens/persists conversation state on
  the marker; and the continuation gate accepts any sender (`awaitFrom` removed). **Blast radius:** the
  dispatched skill still receives the same declared inputs; skills themselves are untouched; `answer()`
  untouched.
- Each `3. Mary Skills/*/prompt.js` — richer per-task section (extends the existing `rulebook()`); each
  `manifest.inputs` gains/uses its execute schema. No change to skill *execution* logic.
- Reuses `lib/llm.js` (`jsonFormat`, `readReply`) — no new shared machinery.

## Risks & mitigations

- **Schema too strict rejects a valid reply** → constrain only the envelope; keep `parseJsonReply` as
  fallback so a schema/SDK surprise degrades to today's behavior, not worse.
- **Read-back/repair turns** must carry `output_config` too — they're exactly the turns that also
  degrade today.
- **The +1 execute call** adds latency — accepted; scoped to execute turns only.
- **State growth** → cap recent-K + truncate (per `READBACK_CAP` precedent).
- **`next` enum must keep `answer`** — don't drop a state.
- **Open continuation gate wakes the AI on all chatter** while a conversation is open → rely on the
  existing deliberate-silence path (`next:listen`, `say:null`, which is free) to ignore irrelevant
  messages; the pending-need state keeps it focused on what it's actually waiting for.

## Verification

- **Live router** (the real safeguard): `ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node
  scripts/router-selftest.mjs` — confirms classification + per-task extraction under the schemas.
  NOTE: hits the live API and costs money; per repo convention the reviewer **escalates** it rather
  than running it unprompted.
- **Turn loop:** `node scripts/turn-latency-selftest.mjs` + the standard regressions
  (`selflearning-selftest.mjs`, `history-selftest.mjs`).
- **End-to-end @mary:** reproduce the documented calendar-create flow (clarify → ask contact →
  execute → read-back); confirm each turn returns schema-valid JSON, the extraction call produces the
  exact create payload, the event is created, and the decision log shows all prior loops.
- **Failure-path regression:** feed a prose-prone order and confirm it no longer degrades to "I didn't
  understand" from a parse failure.

## Out of scope

- The `answer()` prose pass (no JSON there).
- Changing what each skill actually *does* (Google/Kiwi/Tasks logic) — only how it's prompted and how
  its payload is produced.
