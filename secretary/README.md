# Secretary v2.0 — Orchestrator + Skills

Evolution of v1.0 (a single scheduling agent) into a **network of skills** with a
router that classifies intent and dispatches to the right skill.

## Structure

```
secretary/
├── 1. Orchestrator/         # the Node app that runs (webhook + router + skill loading)
│   ├── server.js            #   receives the webhook, filters the trigger tag (SECRETARY_TAG), builds context,
│   │                        #   DISCOVERS the skills, calls the router and dispatches
│   ├── package.json         #   process dependencies (includes the skills' deps)
│   ├── .env.example
│   ├── lib/                 #   shared utilities
│   │   ├── whatsapp.js      #     extract text, detect quoted audio, buffer, transcript
│   │   ├── evolution.js     #     sendText/sendMedia (documents), fetch history, download media (base64)
│   │   ├── llm.js           #     jsonFormat/readReply/readText/parseJsonReply + withThinkingDefault
│   │   │                    #     (wraps the ONE Anthropic client: every call defaults thinking:disabled)
│   │   ├── inputs.js        #     the declared-inputs contract: describeInputs (prompt text) +
│   │   │                    #     checkPayload (the plain-code, no-AI gate). Knows declarations, not skills.
│   │   └── sessions.js      #     per-chat conversation state in Redis (confirmations, clarifications)
│   └── router/
│       ├── prompt.js        #     the MERGED prompt: classifies AND asks for the chosen skill's declared
│       │                    #     inputs. No output_config — the format is demanded in the prompt, which is
│       │                    #     what keeps the orchestrator from having to know what a calendar is.
│       └── router.js        #     ONE Claude call; returns { tasks, lang, info }
├── improvements/           # runtime failure-report spool (gitignored; pulled to Bugs and Malfunctions/)
├── specs/                  # runtime feature-spec spool (gitignored; pulled to New Features Plans/)
└── 2. Skills/               # one folder per skill; the orchestrator scans this at boot
    ├── 1. Calendar Actions/
    │   ├── skill.js         #   export { manifest, run, capabilities.startCreate } — create/cancel a Calendar event
    │   └── prompt.js        #   extraction rules + localized reply() strings
    ├── 2. Audio transcriptions/
    │   ├── skill.js         #   export { manifest, run } — transcribes via AssemblyAI
    │   └── prompt.js        #   reply texts (this skill does not use an LLM)
    ├── 3. Tasks/
    │   ├── skill.js         #   export { manifest, run, capabilities } — batch add/list/complete/edit/delete; delegates a task-for-others to Calendar
    │   └── prompt.js        #   list-aware planner prompt + PLAN_SCHEMA, confirm classifier, localized reply() strings
    ├── 4. Feature Requests/
    │   ├── skill.js         #   export { manifest, run } — clarify conversation → Markdown spec sent as a .md document
    │   └── prompt.js        #   clarify prompt + CLARIFY_SCHEMA, English doc prompt, slugify, localized reply() strings
    ├── 5. Feedback/
    │   ├── skill.js         #   export { manifest, run } — "you got this wrong" → a self-learning failure report
    │   └── prompt.js        #   the complaint prompt + schema, localized reply() strings
    └── 6. Flight Search/
        ├── skill.js         #   export { manifest, run } — confirm-first flight search (Kiwi), 3 cheapest AFTER the junk filter, one link turn
        └── prompt.js        #   3 schemas + prompts, the option/confirm renderers, localized reply() strings
```

## Two skill trees (one per flow)

As of 2026-07-15 there are **two** skill trees, discovered **per-flow** at boot
(`loadSkills(dir)` runs once per tree):

- **`2. Skills/`** — the OLD (`@assistant`) tree. Each skill drives its own
  propose/confirm/clarify dialogue (`conversation:"skill"`) and may export `capabilities`.
- **`3. Mary Skills/`** — the NEW (`@mary`) tree: the **same seven skills as converted PURE
  TASKS** (`conversation:"orchestrator"`, declared `inputs`, a `run(ctx)` that validates →
  acts → sends one outcome → **returns** a JSON value the model reads back). No new-tree skill
  imports `lib/confirm.js`, opens a session, or exports `capabilities`. `calendar_action`,
  `task_action` and `flight_search` use a READ-then-ACT contract; the `startCreate` coupling
  exists only in the old tree.

The two trees load into separate maps (`SKILLS`/`CATALOG`/`CAPS` vs `NEW_SKILLS`/`NEW_CATALOG`)
and are byte-isolated copies, so a bug in one cannot reach the other. When the migration's
default flip lands (a later card), the old tree is retired.

## How a skill is discovered

At boot, the orchestrator scans each tree's `*/skill.js`. Each skill exports:

```js
export const manifest = { id: "my_id", description: "what it does" };
export async function run(ctx) { /* ... */ }
```

The `manifest.id` goes into that flow's catalog the router uses to classify; `run(ctx)` is
called when the router picks that id. **Adding a new skill = create a folder in the right tree
with a `skill.js`. You don't edit `server.js` or the router.** (A converted skill also sets
`manifest.conversation:"orchestrator"` and declares its `inputs`.)

A skill **in the old tree** may also export an optional `capabilities` object — an internal API
other skills can call via `ctx.callSkill(id, name, …)` (never seen by the router). This is how
one skill composes another without importing its file: e.g. `task_action` turns a to-do
assigned to someone else into a calendar invite by calling `calendar_action.startCreate`. Guard
with `ctx.hasSkill(id, name)` for a friendly fallback when a capability isn't loaded. See
"Composing skills" in `ORCHESTRATOR.md`. The converted tree drops this — the orchestrator model
chains skills itself.

The `ctx` object handed to skills carries everything they need (no imports back to
the orchestrator): `owner, anthropic, model, order, transcript, nowStr, contact,
number, remoteJid, quoted, hasQuotedAudio, catalog, tag, fromMe, sessions, session,
env, evolution, send, lang, hasSkill, callSkill`. `ctx.quoted` is
`{ id, hasAudio, mediaType, text, calendarLink }`. `ctx.sessions` is the Redis-backed
session store and `ctx.session` is the current chat's state, so a skill can drive a
multi-step, stateful flow (confirmations, clarifications).

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
A flow only **starts** on a message that is from the owner (`fromMe === true`) and begins with
a trigger tag (`SECRETARY_TAG` is **comma-separated**, default `@secretaria,@secretary`; the old
`@brain` is retired). Once a session is active, though, it can
**continue without the tag**: the secretary uses the LLM to ignore normal chatter and watch for the
answer it's waiting on. That answer can also come from the **other person** in the chat (e.g.
they type their email), so the old blanket rule "only acts if `fromMe` and the text starts with
the tag" no longer holds — a non-owner message can be a valid continuation of an active session.

## Run / deploy

The app is the contents of the `secretary/` folder (that's where `package.json` lives,
and `server.js` looks for the skills at `../2. Skills`). A single `node_modules`
at the `secretary/` root is shared by the orchestrator and the skills. Start it with
`npm start` (which runs `node "1. Orchestrator/server.js"`). New `.env` variables:
`ASSEMBLYAI_API_KEY` (and optionally `ASSEMBLYAI_LANGUAGE`), and `REDIS_URL` for the
session store (defaults to `redis://evolution_redis:6379` — the same Redis the stack uses
for Evolution's cache).
