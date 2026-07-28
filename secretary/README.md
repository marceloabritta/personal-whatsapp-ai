# Secretary v2.0 — Orchestrator + Skills

Evolution of v1.0 (a single scheduling agent) into a **network of skills** with a
router that classifies intent and dispatches to the right skill.

## Structure

```
secretary/
├── 1. Orchestrator/         # the Node app that runs (webhook + router + skill loading)
│   ├── server.js            #   receives the webhook, filters the trigger tag (SECRETARY_TAG_NEW), builds context,
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
└── 3. Mary Skills/          # one folder per skill; the orchestrator scans this at boot
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

## The skill tree

The skills live under `3. Mary Skills/`, discovered at boot (`loadSkills(dir)` scans it
once). Each skill is a **PURE TASK** (`conversation:"orchestrator"`, declared `inputs`, a
`run(ctx)` that validates → acts → sends one outcome → **returns** a JSON value the model
reads back). No skill imports `lib/confirm.js` or opens a session of its own; the
orchestrator holds the conversation. `calendar_action`, `task_action` and `flight_search`
use a READ-then-ACT contract. Skills never call one another — the model chains them itself.

The tree loads into the orchestrator's skill maps (`NEW_SKILLS`/`NEW_CATALOG`).

## How a skill is discovered

At boot, the orchestrator scans the tree's `*/skill.js`. Each skill exports:

```js
export const manifest = { id: "my_id", description: "what it does" };
export async function run(ctx) { /* ... */ }
```

The `manifest.id` goes into the catalog the router uses to classify; `run(ctx)` is
called when the router picks that id. **Adding a new skill = create a folder in the tree
with a `skill.js`. You don't edit `server.js` or the router.** (A skill also sets
`manifest.conversation:"orchestrator"` and declares its `inputs`.)

Skills don't compose one another directly — there is no cross-skill call mechanism. The
orchestrator model chains skills itself: e.g. a to-do assigned to someone else becomes a
calendar invite because the model dispatches `calendar_action`, not because `task_action`
reaches into it.

The `ctx` object handed to skills carries everything they need (no imports back to
the orchestrator): `owner, anthropic, model, order, transcript, nowStr, contact,
number, remoteJid, quoted, hasQuotedAudio, catalog, tag, fromMe, sessions, session,
env, evolution, send, lang`. `ctx.quoted` is
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
the trigger tag (`SECRETARY_TAG_NEW`, default `@mary`; a stored value set by the owner wins over
the seed). Once a session is active, though, it can
**continue without the tag**: the secretary uses the LLM to ignore normal chatter and watch for the
answer it's waiting on. That answer can also come from the **other person** in the chat (e.g.
they type their email), so the old blanket rule "only acts if `fromMe` and the text starts with
the tag" no longer holds — a non-owner message can be a valid continuation of an active session.

## Run / deploy

The app is the contents of the `secretary/` folder (that's where `package.json` lives,
and `server.js` looks for the skills at `../3. Mary Skills`). A single `node_modules`
at the `secretary/` root is shared by the orchestrator and the skills. Start it with
`npm start` (which runs `node "1. Orchestrator/server.js"`). New `.env` variables:
`ASSEMBLYAI_API_KEY` (and optionally `ASSEMBLYAI_LANGUAGE`), and `REDIS_URL` for the
session store (defaults to `redis://evolution_redis:6379` — the same Redis the stack uses
for Evolution's cache).
