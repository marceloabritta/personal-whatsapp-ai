# Personal WhatsApp AI — "Lisa"

A personal WhatsApp assistant. Send a message tagged **`@lisa`** in any chat and the
brain wakes, works, and replies. Self-hosted on a single DigitalOcean droplet.

Lisa is built on [LangGraph](https://github.com/langchain-ai/langgraph) (the open-source,
MIT library) + FastAPI, calling the WhatsApp gateway
([Evolution API](https://github.com/EvolutionAPI/evolution-api)) directly through one
internal client. Core capabilities today: **Google Calendar** actions, **audio
transcription**, and reading **PDFs and images**.

## Layout

    Lisa flow/        The AI brain — LangGraph OSS + FastAPI. Calls Evolution's HTTP API
                      directly through one internal client. See "Lisa flow/README.md".
    dispatcher/       The ONE webhook Evolution posts to. Routes each turn to a flow by
                      @tag (and per-chat window ownership), forwarding the raw payload.
    evolution/        The Evolution API stack (WhatsApp gateway): API + Postgres + Redis,
                      via docker-compose. The flows run alongside it.
    AI Coding-kanban/ The kanban dev-tooling / board spools.

## How it works

```
WhatsApp ─▶ Evolution API ─MESSAGES_UPSERT─▶ dispatcher ─by @tag─▶ Lisa /webhook ─▶ LangGraph ─▶ reply ─▶ Evolution ─▶ WhatsApp
```

A run starts only when the owner sends a message carrying `@lisa` (or a session for that
chat is already open). Every run leaves a two-level trace: a code-level event stream and a
user-level transcript, sharing one trace id.

## Adding a new flow without risking the core

The dispatcher fans out by tag, so a new/experimental feature ships as its **own tag → its
own agent**, leaving the core `@lisa` flow untouched. Add one `(tags, url)` route in
`dispatcher/app.py` (or via `*_TAGS` / `*_URL` env), point a fresh `@tag` at a new service,
and iterate there — a new feature can never break `@lisa`.

## Run it

Bring up the Evolution stack, the dispatcher, and Lisa together (see
`evolution/docker-compose.yml` and `Lisa flow/README.md`), then register the **dispatcher**
as Evolution's webhook so it can route by tag:

    POST http://localhost:8080/webhook/set/<instance>
    { "webhook": { "enabled": true, "url": "http://dispatcher:8090/webhook",
                   "events": ["MESSAGES_UPSERT"] } }

## History

The previous Node.js implementation — the "secretary"/"Mary" flow, a single process serving
two tag-selected designs (`@assistant`/`@assistente` → the legacy skills stack, `@mary` → the
newer Mary skills) — has been retired now that Lisa covers the core. It is preserved on the git
branches **`archive/secretary-v1`** and **`wip-snapshot-2026-08-02`**, and remains in this
repo's history.

## License

See [LICENSE](LICENSE).
