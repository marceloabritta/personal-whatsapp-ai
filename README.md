# personal-whatsapp-ai

Connect your WhatsApp to the [Evolution API](https://github.com/EvolutionAPI/evolution-api) and you get an AI secretary wired to your own frontier-model API keys. The project is deployed on DigitalOcean. The layout is an **orchestrator** called into action by a message you send in any chat (`@secretary`). The orchestrator detects the requested task and hands it off to one of many task-specific agents (**skills**).

Everything is **self-hosted**: no third party sits between WhatsApp and you. Your conversations only ever leave your server when a skill deliberately calls an external API (Claude for reasoning, Google Calendar for invites, AssemblyAI for transcription) — and only for the one message where you invoked `@secretary`.

## How it works

You type `@secretary <order>` in any WhatsApp chat (your own, a 1:1, or a group). The system:

1. receives the message through the Evolution API webhook;
2. a flow only **starts** on a message that is **from you** and begins with `@secretary`;
3. reads the recent context of that chat;
4. asks the **router** (an LLM call) which task you're requesting;
5. dispatches to the matching **skill**, which does the work and replies to you on WhatsApp.

The secretary is **stateful**: it keeps per-chat conversation state in Redis, so once a flow is
running it can **continue without the tag** (for confirmations and clarifications). The secretary
uses the LLM to ignore normal chatter and watch for the answer it's waiting on — and that
answer can come from the **other person** in the chat too (e.g. they type their email), so a
non-owner message can be a valid continuation of an active session.

```
You type "@secretary ..." in a chat
              │
              ▼
     WhatsApp (your phone, linked device)
              │
              ▼
     Evolution API  ──webhook──►  secretary (orchestrator)
                                    │  1. filter: start on fromMe + @secretary
                                    │     (or continue an active session)
                                    │  2. build context (chat history)
                                    │  3. ROUTER: classify the intent
                                    │  4. dispatch to the chosen SKILL
                                    ▼
                              ┌─────────────┐
                              │   Skills    │
                              ├─────────────┤
                              │ calendar_action   → Google Calendar
                              │ transcribe_audio  → AssemblyAI
                              │ task_action       → Google Tasks (self)
                              │                     ↳ Calendar (task for others)
                              │ feature_request   → .md spec document
                              └─────────────┘
                                    │
                                    ▼
                          reply back on WhatsApp
```

## Skills (today)

- **`calendar_action`** — reads the chat and **creates**, **edits/reschedules**, or **cancels/deletes** a Google Calendar event. On create it extracts participants, date, time and duration, creates the event and fires the invite email to the attendees. On edit you reply to the invite with a change ("move it to 4pm", "add carlos@example.com", "rename to Kickoff") — it's confirm-first and stays open so you can keep refining before saving.
- **`transcribe_audio`** — reply to a voice message and type `@secretary transcribe`; it downloads the audio from WhatsApp, transcribes it with AssemblyAI and sends you the text.
- **`task_action`** — your to-do inbox. Add todos (one or several at once — `@secretary add buy flight, book the dentist`), hear your list, check them off ("I bought the pizza and got my flights" checks off both), and **edit or delete** a task already on your list ("change the contract task's due to Monday", "delete the pizza one"). A single list-aware planner reads the chat and matches your words to tasks by meaning — asking which you mean, by name, when it's ambiguous. Completions and edits are **confirm-first** (`yes`); once an exchange is underway, follow-ups need **no re-tag** for a short window. A todo for **yourself** goes to your private **Google Tasks** list (created instantly); a todo assigned to **someone else** becomes a 5-minute **Calendar** invite so they're notified by email. Google Tasks due dates are date-only.
- **`feature_request`** — capture a new feature idea by talking it through. Start with `@secretary I have a feature idea…`; the secretary becomes stateful and **interviews you** until the feature is clear, then writes a **Markdown spec** (from the user's point of view) and sends it as a saveable `.md` document you can drop into your repo. The conversation follows your language; the document is always written in English.

Adding a skill is a drop-in: create a folder under `secretary/2. Skills/` with a `skill.js` that exports `{ manifest, run }`. The orchestrator discovers it at boot and the router starts offering it — no changes to the orchestrator or the router. A skill can also export an optional `capabilities` object to be reused by other skills (e.g. `task_action` calls `calendar_action`'s create flow for a task assigned to someone else). See `secretary/README.md`.

## Repository layout

```
.
├── README.md              # this file
├── ARCHITECTURE.md        # detailed data flow: what is sent to each service
├── LICENSE
├── .gitignore
├── secretary/             # the "secretary" (Node.js) — orchestrator + skills  ← run this
│   ├── 1. Orchestrator/
│   └── 2. Skills/         #   (the earlier single-agent version lives in git history)
└── evolution/             # the WhatsApp gateway (Docker)
    ├── docker-compose.yml #   Evolution API + Postgres + Redis + secretary
    └── .env.example
```

## Prerequisites

- A server to host it (this project uses a DigitalOcean droplet, Ubuntu, ~US$12/mo, 2 GB) with Docker + Docker Compose.
- A WhatsApp account to link as a "linked device".
- **Anthropic API key** (the reasoning model).
- **Google OAuth credentials** (Client ID, Secret, Refresh Token) for the Calendar and Tasks skills — the refresh token must be minted with **both** the `calendar` and `tasks` scopes.
- **AssemblyAI API key** for the transcription skill.

## Setup

1. **Bring up the stack.** Copy `evolution/` to `/opt/evolution` on the server, create `.env` from `.env.example`, and generate the two secrets with `openssl rand -hex 16` (`AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`). Then `docker compose up -d`.
2. **Deploy the secretary.** Put the **contents of `secretary/`** in `/opt/secretary` (so `/opt/secretary/package.json`, `/opt/secretary/1. Orchestrator/`, `/opt/secretary/2. Skills/` exist). Create `/opt/secretary/.env` from `secretary/.env.example` and fill in your keys. The `secretary` service in the compose file runs `npm install && npm start`.
3. **Link WhatsApp.** In the Evolution manager (`http://YOUR_IP:8080`), create an instance named `secretaria` (must match `EVOLUTION_INSTANCE`) and scan the QR code with WhatsApp → Linked devices.
4. **Point the webhook** at the secretary for `MESSAGES_UPSERT` events:

   ```bash
   API_KEY=$(grep AUTHENTICATION_API_KEY /opt/evolution/.env | cut -d= -f2)
   curl -sS -X POST http://localhost:8080/webhook/set/secretaria \
     -H "Content-Type: application/json" -H "apikey: $API_KEY" \
     -d '{"webhook":{"enabled":true,"url":"http://secretary:3000/webhook","byEvents":false,"base64":false,"events":["MESSAGES_UPSERT"]}}'
   ```

5. **Test.** In any chat, type `@secretary schedule a 30-min call with me tomorrow at 2pm, my email is you@example.com`.

> Note on code vs. secret changes: `docker compose restart secretary` re-reads code, but **not** `.env`. After changing secrets use `docker compose up -d --force-recreate secretary`.

## Language

The secretary **detects the language you're writing in** (the router sets `ctx.lang`) and **replies in that same language**, system-wide across every skill. English and Portuguese (PT-BR) are maintained natively — each skill keeps its user-facing strings as a per-language `{ en, pt }` map in its own `prompt.js` (dates via `localizeDate`). Any other language is produced from the English copy by a cheap translation fallback in the orchestrator's `send()`. The reply header itself is **language-aware** — produced by `headerFor(lang)` from `OWNER_NAME` (English → `[Marcelo's AI Secretary]:`, Portuguese → `[Secretaria IA do Marcelo]:`); internal classification prompts always stay English. Audio transcription follows the detected language too, with `ASSEMBLYAI_LANGUAGE` as a fallback. See the "Localization convention" in [ARCHITECTURE.md](ARCHITECTURE.md). (Live since 2026-07-11.)

## Security

- The Evolution API port (`8080`) is exposed to the internet, protected only by the API key. Lock it down with a firewall (`ufw`), allowing only your own access.
- Never commit real `.env` files — `.gitignore` blocks them; only `.env.example` files are tracked.

## Roadmap

- More skills (reminders, lookups, etc.) — each a new folder under `2. Skills/`.
- Reply privately when `@secretary` is called in a group.
- Calendar backlog: conflict/availability check on create, read/query events ("what's on my calendar tomorrow?"), and recurring events.

## Contributing

Issues and pull requests are welcome. New skills are the easiest contribution: follow the `{ manifest, run }` contract in `secretary/2. Skills/` and open a PR.

## License

MIT — see [LICENSE](LICENSE).
