# Brain v2.0 — Orchestrator + Skills

Evolution of v1.0 (a single scheduling agent) into a **network of skills** with a
router that classifies intent and dispatches to the right skill.

## Structure

```
brain/
├── 1. Orchestrator/         # the Node app that runs (webhook + router + skill loading)
│   ├── server.js            #   receives the webhook, filters the trigger tag (SECRETARY_TAG), builds context,
│   │                        #   DISCOVERS the skills, calls the router and dispatches
│   ├── package.json         #   process dependencies (includes the skills' deps)
│   ├── .env.example
│   ├── lib/                 #   shared utilities
│   │   ├── whatsapp.js      #     extract text, detect quoted audio, buffer, transcript
│   │   ├── evolution.js     #     send/fetch messages and download media (base64) from Evolution
│   │   └── sessions.js      #     per-chat conversation state in Redis (confirmations, clarifications)
│   └── router/
│       ├── prompt.js        #     classification prompt (lists the catalog's skills)
│       └── router.js        #     calls Claude and returns the task(s)
└── 2. Skills/               # one folder per skill; the orchestrator scans this at boot
    ├── 1. Calendar Actions/
    │   ├── skill.js         #   export { manifest, run } — creates or cancels/deletes a Google Calendar event
    │   └── prompt.js        #   extraction rules (action, participants, date, time, duration)
    └── 2. Audio transcriptions/
        ├── skill.js         #   export { manifest, run } — transcribes via AssemblyAI
        └── prompt.js        #   reply texts (this skill does not use an LLM)
```

## How a skill is discovered

At boot, the orchestrator scans `2. Skills/*/skill.js`. Each skill exports:

```js
export const manifest = { id: "my_id", description: "what it does" };
export async function run(ctx) { /* ... */ }
```

The `manifest.id` goes into the catalog the router uses to classify; `run(ctx)` is
called when the router picks that id. **Adding a new skill = create a folder here
with a `skill.js`. You don't edit `server.js` or the router.**

The `ctx` object handed to skills carries everything they need (no imports back to
the orchestrator): `owner, anthropic, model, order, transcript, nowStr, contact,
number, remoteJid, quoted, hasQuotedAudio, catalog, tag, fromMe, sessions, session,
env, evolution, send, lang`. `ctx.quoted` is `{ id, hasAudio, mediaType, text, calendarLink }`.
`ctx.sessions` is the Redis-backed session store and `ctx.session` is the current chat's
state, so a skill can drive a multi-step, stateful flow (confirmations, clarifications).

## Localization

Replies follow `ctx.lang` (the conversation language the router detects). Each skill keeps
its user-facing strings in its `prompt.js` as a per-language map (`{ en, pt }`) selected by
`ctx.lang`, and **must ship both `en` and `pt` for every message** (English is canonical;
dates use `localizeDate(ctx.lang, …)`). A language without a map is auto-translated from the
`en` copy by the orchestrator's `send()` fallback; the `[AI Brain]:` header is never
translated. See the convention in `../ARCHITECTURE.md` and the design in
`../New Features Plans/multilingual-brain.md`.

## Stateful flow (starting vs. continuing)

The brain is **stateful**: it keeps per-chat conversation state in Redis (`lib/sessions.js`).
A flow only **starts** on a message that is from the owner (`fromMe === true`) and begins with
the trigger tag (`SECRETARY_TAG`, default `@brain`). Once a session is active, though, it can
**continue without the tag**: the brain uses the LLM to ignore normal chatter and watch for the
answer it's waiting on. That answer can also come from the **other person** in the chat (e.g.
they type their email), so the old blanket rule "only acts if `fromMe` and the text starts with
the tag" no longer holds — a non-owner message can be a valid continuation of an active session.

## Run / deploy

The app is the contents of the `brain/` folder (that's where `package.json` lives,
and `server.js` looks for the skills at `../2. Skills`). A single `node_modules`
at the `brain/` root is shared by the orchestrator and the skills. Start it with
`npm start` (which runs `node "1. Orchestrator/server.js"`). New `.env` variables:
`ASSEMBLYAI_API_KEY` (and optionally `ASSEMBLYAI_LANGUAGE`), and `REDIS_URL` for the
session store (defaults to `redis://evolution_redis:6379` — the same Redis the stack uses
for Evolution's cache).
