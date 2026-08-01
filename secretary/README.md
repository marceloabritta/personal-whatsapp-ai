# Secretary v2.0 — Orchestrator + Skills

Evolution of v1.0 (a single scheduling agent) into a **network of skills** with a
router that classifies intent and dispatches to the right skill.

## Structure

```
secretary/
├── 1. Orchestrator/         # the Node app that runs (webhook + router + skill loading)
│   ├── server.js            #   receives the webhook, filters the trigger tags (SECRETARY_TAG_NEW=@mary,
│   │                        #   SECRETARY_TAG=@assistant), selects the flow, builds context, DISCOVERS the
│   │                        #   skills, calls the router and dispatches (the @mary turn loop + runLegacyFlow)
│   ├── package.json         #   process dependencies (includes the skills' deps)
│   ├── .env.example
│   ├── lib/                 #   shared utilities
│   │   ├── whatsapp.js      #     extract text, detect quoted audio, buffer, transcript
│   │   ├── evolution.js     #     sendText/sendMedia (documents), fetch history, download media (base64)
│   │   ├── llm.js           #     jsonFormat/readReply/readText/parseJsonReply + withThinkingDefault
│   │   │                    #     (wraps the ONE Anthropic client: every call defaults thinking:disabled)
│   │   ├── inputs.js        #     the declared-inputs contract: describeInputs (prompt text) +
│   │   │                    #     buildExecuteSchema (shape-only extraction schema) + checkPayload (no-AI gate)
│   │   ├── sessions.js      #     per-chat conversation state in Redis (confirmations, clarifications)
│   │   ├── transcribe.js    #     transcribeAudio(env,buffer,lang): system-side AssemblyAI transcription,
│   │   │                    #     folded into the turn prompt as ctx.audioTranscript (the model can't take audio)
│   │   └── nativeTools.js   #     buildNativeTools(env): the native server-side tool bundle (web_search +
│   │                        #     web_fetch, code_execution when NATIVE_CODE_EXEC), attached to the turn call
│   └── router/
│       ├── prompt.js        #     the turn + extraction prompts: buildRouterSystem (3-decision contract +
│       │                    #     preamble), buildExtractionUser (the per-task payload call, with a repair
│       │                    #     block), renderStateBlock. Both calls carry output_config (see router.js).
│       └── router.js        #     the UNIFIED turn call route() ({say,keepListening,execute} + tools + adaptive
│                            #     thinking + pause_turn loop) and extract() (the schema-locked per-task payload)
│   └── legacy/              #   FROZEN pre-retirement @assistant flow (router/prompt/inputs +
│                            #     assistant-settings) — dispatched only by runLegacyFlow, never by @mary
├── improvements/           # runtime failure-report spool (gitignored; pulled to Bugs and Malfunctions/)
├── specs/                  # runtime feature-spec spool (gitignored; pulled to New Features Plans/)
├── 2. Skills/               # @assistant's FROZEN tree (each skill drives its own dialogue); scanned at boot
│                            #   into SKILLS/CATALOG/CAPS (7 folders: Calendar, Audio, Tasks, Feature Requests,
│                            #   Feedback, Flight Search, Assistant Settings)
└── 3. Mary Skills/          # @mary's tree — the same seven skills as converted pure tasks; scanned at boot
    ├── 1. Calendar Actions/
    │   ├── skill.js         #   export { manifest, run } — create/edit/cancel a Calendar event (READ-then-ACT)
    │   └── prompt.js        #   extraction rules + localized reply() strings
    ├── 2. Audio transcriptions/
    │   ├── skill.js         #   export { manifest, run } — transcribes via AssemblyAI
    │   └── prompt.js        #   reply texts (this skill does not use an LLM)
    ├── 3. Tasks/
    │   ├── skill.js         #   export { manifest, run } — batch add/list/complete/edit/delete; a task-for-others becomes a Calendar invite
    │   └── prompt.js        #   list-aware planner prompt + PLAN_SCHEMA, confirm classifier, localized reply() strings
    ├── 4. Feature Requests/
    │   ├── skill.js         #   export { manifest, run } — clarify conversation → Markdown spec sent as a .md document
    │   └── prompt.js        #   clarify prompt + CLARIFY_SCHEMA, English doc prompt, slugify, localized reply() strings
    ├── 5. Feedback/
    │   ├── skill.js         #   export { manifest, run } — "you got this wrong" → a self-learning failure report
    │   └── prompt.js        #   the complaint prompt + schema, localized reply() strings
    ├── 6. Flight Search/
    │   ├── skill.js         #   export { manifest, run } — confirm-first flight search (Kiwi), 3 cheapest AFTER the junk filter, one link turn
    │   └── prompt.js        #   3 schemas + prompts, the option/confirm renderers, localized reply() strings
    └── 7. Assistant Settings/
        ├── skill.js         #   export { manifest, run } — change the tag the owner summons her with
        └── prompt.js        #   localized reply() strings
```

## Two skill trees (one per flow)

There are **two** skill trees, discovered **per-flow** at boot (`loadSkills(dir)` runs once per
tree):

- **`3. Mary Skills/`** — the `@mary` tree (the PRIMARY, actively-developed flow): the **seven
  skills as PURE TASKS** (`conversation:"orchestrator"`, declared `inputs`, a `run(ctx)` that
  validates → acts → sends one outcome → **returns** a JSON value the model reads back). No
  Mary-tree skill imports `lib/confirm.js`, opens a session, or exports `capabilities`.
  `calendar_action`, `task_action` and `flight_search` use a READ-then-ACT contract; the model
  chains skills itself.
- **`2. Skills/`** — the FROZEN `@assistant` tree (the legacy fallback). Each skill drives its own
  propose/confirm/clarify dialogue (`conversation:"skill"`) and may export `capabilities`; the
  `startCreate` coupling (Tasks→Calendar) exists only in this tree.

The two trees load into separate maps (`NEW_SKILLS`/`NEW_CATALOG` vs `SKILLS`/`CATALOG`/`CAPS`) and
are byte-isolated copies, so a bug in one cannot reach the other. The legacy tree is restored
frozen at its retirement state and runs side-by-side with `@mary`.

## How a skill is discovered

At boot, the orchestrator scans each tree's `*/skill.js`. Each skill exports:

```js
export const manifest = { id: "my_id", description: "what it does" };
export async function run(ctx) { /* ... */ }
```

The `manifest.id` goes into that flow's catalog the router uses to classify; `run(ctx)` is
called when the router picks that id. **Adding a new skill = create a folder in the right tree
with a `skill.js`. You don't edit `server.js` or the router.** (A converted `@mary` skill also
sets `manifest.conversation:"orchestrator"` and declares its `inputs`.)

A skill **in the frozen `@assistant` tree** may also export an optional `capabilities` object — an
internal API other skills can call via `ctx.callSkill(id, name, …)` (never seen by the router).
This is how one skill composes another without importing its file: e.g. `task_action` turns a
to-do assigned to someone else into a calendar invite by calling `calendar_action.startCreate`.
Guard with `ctx.hasSkill(id, name)` for a friendly fallback when a capability isn't loaded. See
"Composing skills" in `ORCHESTRATOR.md`. The converted `@mary` tree drops this — the orchestrator
model chains skills itself.

The `ctx` object handed to skills carries everything they need (no imports back to
the orchestrator): `owner, anthropic, model, order, transcript, nowStr, contact,
number, remoteJid, quoted, hasQuotedAudio, catalog, tag, fromMe, sessions, session,
env, evolution, send, dmOwner, lang, hasSkill, callSkill`. `ctx.quoted` is
`{ id, hasAudio, mediaType, text, calendarLink }`. `ctx.sessions` is the Redis-backed
session store and `ctx.session` is the current chat's state, so a skill can drive a
multi-step, stateful flow (confirmations, clarifications). `ctx.dmOwner(text)` is an
**additive** helper that sends a framed, localized note to the owner's **own** number
(`OWNER_JID`/`OWNER_NUMBER`) instead of the current chat — a **no-op when that var is
unset**; today only `calendar_action` uses it (a Contacts "email saved" note). Guard
calls with `typeof ctx.dmOwner === "function"`.

## Localization

Replies follow `ctx.lang` (the conversation language the router detects). Each skill keeps
its user-facing strings in its `prompt.js` as a per-language map (`{ en, pt }`) selected by
`ctx.lang`, and **must ship both `en` and `pt` for every message** (English is canonical;
dates use `localizeDate(ctx.lang, …)`). A language without a map is auto-translated from the
`en` copy by the orchestrator's `send()` fallback; the reply header is never translated — it
is produced per-language by `headerFor(lang)` (en → `[Marcelo's AI Secretary]:`, pt →
`[Secretaria IA do Marcelo]:`). See the "Localization convention" in `../ARCHITECTURE.md`.

## Stateful flow (starting vs. continuing)

The secretary is **stateful**: it keeps per-chat conversation state in Redis (`lib/sessions.js`).
A flow only **starts** on a message that is from the owner (`fromMe === true`) and begins with a
trigger tag — `SECRETARY_TAG_NEW` (default `@mary`, the primary flow) or `SECRETARY_TAG` (default
`@assistente,@assistant`, the frozen legacy flow); a stored value set by the owner wins over the
seed, per flow. The summon tag selects the flow. Once a session is active, though, it can
**continue without the tag**: the secretary uses the LLM to ignore normal chatter and watch for the
answer it's waiting on. That answer can also come from the **other person** in the chat (e.g.
they type their email), so the old blanket rule "only acts if `fromMe` and the text starts with
the tag" no longer holds — a non-owner message can be a valid continuation of an active session.

## Run / deploy

The app is the contents of the `secretary/` folder (that's where `package.json` lives,
and `server.js` looks for the skills at `../3. Mary Skills` for `@mary` and `../2. Skills`
for the frozen `@assistant` flow). A single `node_modules`
at the `secretary/` root is shared by the orchestrator and the skills. Start it with
`npm start` (which runs `node "1. Orchestrator/server.js"`). New `.env` variables:
`ASSEMBLYAI_API_KEY` (and optionally `ASSEMBLYAI_LANGUAGE`), and `REDIS_URL` for the
session store (defaults to `redis://evolution_redis:6379` — the same Redis the stack uses
for Evolution's cache).
