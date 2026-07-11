# Brain v2.0 — Orchestrator + Skills

Evolution of v1.0 (a single scheduling agent) into a **network of skills** with a
router that classifies intent and dispatches to the right skill.

## Structure

```
v2.0/
├── 1. Orchestrator/         # the Node app that runs (webhook + router + skill loading)
│   ├── server.js            #   receives the webhook, filters the trigger tag (SECRETARY_TAG), builds context,
│   │                        #   DISCOVERS the skills, calls the router and dispatches
│   ├── package.json         #   process dependencies (includes the skills' deps)
│   ├── .env.example
│   ├── lib/                 #   shared utilities
│   │   ├── whatsapp.js      #     extract text, detect quoted audio, buffer, transcript
│   │   └── evolution.js     #     send/fetch messages and download media (base64) from Evolution
│   └── router/
│       ├── prompt.js        #     classification prompt (lists the catalog's skills)
│       └── router.js        #     calls Claude and returns the task(s)
└── 2. Skills/               # one folder per skill; the orchestrator scans this at boot
    ├── 1. Calendar Actions/
    │   ├── skill.js         #   export { manifest, run } — creates the Google Calendar event
    │   └── prompt.js        #   extraction rules (participants, date, time, duration)
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
number, remoteJid, quoted, env, evolution, send`.

## Run / deploy

The app is the contents of the `v2.0/` folder (that's where `package.json` lives,
and `server.js` looks for the skills at `../2. Skills`). A single `node_modules`
at the `v2.0/` root is shared by the orchestrator and the skills. Start it with
`npm start` (which runs `node "1. Orchestrator/server.js"`). New `.env` variables:
`ASSEMBLYAI_API_KEY` (and optionally `ASSEMBLYAI_LANGUAGE`).
