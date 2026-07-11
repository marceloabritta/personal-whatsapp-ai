# personal-whatsapp-ai

Connect your WhatsApp to the [Evolution API](https://github.com/EvolutionAPI/evolution-api) and you get an AI secretary wired to your own frontier-model API keys. The project is deployed on DigitalOcean. The layout is an **orchestrator** called into action by a message you send in any chat (`@brain`). The orchestrator detects the requested task and hands it off to one of many task-specific agents (**skills**).

Everything is **self-hosted**: no third party sits between WhatsApp and you. Your conversations only ever leave your server when a skill deliberately calls an external API (Claude for reasoning, Google Calendar for invites, AssemblyAI for transcription) — and only for the one message where you invoked `@brain`.

## How it works

You type `@brain <order>` in any WhatsApp chat (your own, a 1:1, or a group). The system:

1. receives the message through the Evolution API webhook;
2. a flow only **starts** on a message that is **from you** and begins with `@brain`;
3. reads the recent context of that chat;
4. asks the **router** (an LLM call) which task you're requesting;
5. dispatches to the matching **skill**, which does the work and replies to you on WhatsApp.

The brain is **stateful**: it keeps per-chat conversation state in Redis, so once a flow is
running it can **continue without the tag** (for confirmations and clarifications). The brain
uses the LLM to ignore normal chatter and watch for the answer it's waiting on — and that
answer can come from the **other person** in the chat too (e.g. they type their email), so a
non-owner message can be a valid continuation of an active session.

```
You type "@brain ..." in a chat
              │
              ▼
     WhatsApp (your phone, linked device)
              │
              ▼
     Evolution API  ──webhook──►  brain (orchestrator)
                                    │  1. filter: start on fromMe + @brain
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
                              └─────────────┘
                                    │
                                    ▼
                          reply back on WhatsApp
```

## Skills (today)

- **`calendar_action`** — reads the chat and either **creates** or **cancels/deletes** a Google Calendar event. On create it extracts participants, date, time and duration, creates the event and fires the invite email to the attendees. (Edit/reschedule is planned, not yet built.)
- **`transcribe_audio`** — reply to a voice message and type `@brain transcribe`; it downloads the audio from WhatsApp, transcribes it with AssemblyAI and sends you the text.

Adding a skill is a drop-in: create a folder under `brain/v2.0/2. Skills/` with a `skill.js` that exports `{ manifest, run }`. The orchestrator discovers it at boot and the router starts offering it — no changes to the orchestrator or the router. See `brain/v2.0/README.md`.

## Repository layout

```
.
├── README.md              # this file
├── ARCHITECTURE.md        # detailed data flow: what is sent to each service
├── LICENSE
├── .gitignore
├── brain/                 # the "brain" (Node.js) — the AI app
│   └── v2.0/              #   current: orchestrator + skills  ← run this
│       ├── 1. Orchestrator/
│       └── 2. Skills/     #   (the earlier single-agent version lives in git history)
└── evolution/             # the WhatsApp gateway (Docker)
    ├── docker-compose.yml #   Evolution API + Postgres + Redis + brain
    └── .env.example
```

## Prerequisites

- A server to host it (this project uses a DigitalOcean droplet, Ubuntu, ~US$12/mo, 2 GB) with Docker + Docker Compose.
- A WhatsApp account to link as a "linked device".
- **Anthropic API key** (the reasoning model).
- **Google OAuth credentials** (Client ID, Secret, Refresh Token) for the Calendar skill.
- **AssemblyAI API key** for the transcription skill.

## Setup

1. **Bring up the stack.** Copy `evolution/` to `/opt/evolution` on the server, create `.env` from `.env.example`, and generate the two secrets with `openssl rand -hex 16` (`AUTHENTICATION_API_KEY`, `POSTGRES_PASSWORD`). Then `docker compose up -d`.
2. **Deploy the brain.** Put the **contents of `brain/v2.0/`** in `/opt/brain` (so `/opt/brain/package.json`, `/opt/brain/1. Orchestrator/`, `/opt/brain/2. Skills/` exist). Create `/opt/brain/.env` from `brain/v2.0/.env.example` and fill in your keys. The `brain` service in the compose file runs `npm install && npm start`.
3. **Link WhatsApp.** In the Evolution manager (`http://YOUR_IP:8080`), create an instance named `secretary` and scan the QR code with WhatsApp → Linked devices.
4. **Point the webhook** at the brain for `MESSAGES_UPSERT` events:

   ```bash
   API_KEY=$(grep AUTHENTICATION_API_KEY /opt/evolution/.env | cut -d= -f2)
   curl -sS -X POST http://localhost:8080/webhook/set/secretary \
     -H "Content-Type: application/json" -H "apikey: $API_KEY" \
     -d '{"webhook":{"enabled":true,"url":"http://brain:3000/webhook","byEvents":false,"base64":false,"events":["MESSAGES_UPSERT"]}}'
   ```

5. **Test.** In any chat, type `@brain schedule a 30-min call with me tomorrow at 2pm, my email is you@example.com`.

> Note on code vs. secret changes: `docker compose restart brain` re-reads code, but **not** `.env`. After changing secrets use `docker compose up -d --force-recreate brain`.

## Language

The default reply strings and LLM prompts are in English. Each skill keeps its language in its own `prompt.js` (and the transcription reply strings in `2. Audio transcriptions/prompt.js`), so switching a skill to another language is a single-file edit. Set `ASSEMBLYAI_LANGUAGE` for the audio language.

## Security

- The Evolution API port (`8080`) is exposed to the internet, protected only by the API key. Lock it down with a firewall (`ufw`), allowing only your own access.
- Never commit real `.env` files — `.gitignore` blocks them; only `.env.example` files are tracked.

## Roadmap

- More skills (reminders, lookups, etc.) — each a new folder under `2. Skills/`.
- Reply privately when `@brain` is called in a group.
- Smarter scheduling (name events by topic; detect & collect missing attendee emails, including asking the other person).
- Edit/reschedule existing events by replying.

## Contributing

Issues and pull requests are welcome. New skills are the easiest contribution: follow the `{ manifest, run }` contract in `brain/v2.0/2. Skills/` and open a PR.

## License

MIT — see [LICENSE](LICENSE).
