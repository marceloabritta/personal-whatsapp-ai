# Personal WhatsApp AI — "Mary"

A personal WhatsApp assistant. Send a message tagged **`@mary`** in any chat and the
brain wakes, works, and replies. Self-hosted on a single DigitalOcean droplet.

The project is being **rebuilt from scratch** on [LangGraph](https://github.com/langchain-ai/langgraph)
(the open-source, MIT library) + FastAPI. Only the WhatsApp gateway
([Evolution API](https://github.com/EvolutionAPI/evolution-api)) carried over from the
previous version.

## Layout

    brain/       The AI brain — LangGraph OSS + FastAPI. Calls Evolution's HTTP API
                 directly through one internal client. See brain/README.md.
    evolution/   The Evolution API stack (WhatsApp gateway): API + Postgres + Redis,
                 via docker-compose. The brain runs alongside it.

## How it works

```
WhatsApp ──▶ Evolution API ──MESSAGES_UPSERT──▶ brain /webhook ──▶ LangGraph graph ──▶ reply ──▶ Evolution ──▶ WhatsApp
```

A run starts only when the owner sends a message carrying `@mary` (or a session for
that chat is already open). Every run leaves a two-level trace: a code-level event
stream and a user-level transcript, sharing one trace id.

## Run it

Bring up the Evolution stack and the brain together (see `evolution/docker-compose.yml`
and `brain/README.md`), then register the webhook so Evolution delivers messages to the
brain:

    POST http://localhost:8080/webhook/set/<instance>
    { "webhook": { "enabled": true, "url": "http://brain:8000/webhook",
                   "events": ["MESSAGES_UPSERT"] } }

## History

The previous Node.js implementation (the "secretary" orchestrator + skills, plus the
kanban dev-tooling and project spools) is preserved on the git branches
**`archive/secretary-v1`** and **`wip-snapshot-2026-08-02`**, and remains in this
repo's history.

## License

See [LICENSE](LICENSE).
