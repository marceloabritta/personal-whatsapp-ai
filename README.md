# Personal WhatsApp AI — "Lisa"

A personal WhatsApp assistant. Send a message tagged **`@lisa`** in any chat and the
brain wakes, works, and replies. Self-hosted on a single DigitalOcean droplet.

Lisa is built on [LangGraph](https://github.com/langchain-ai/langgraph) (the open-source,
MIT library) + FastAPI, calling the WhatsApp gateway
([Evolution API](https://github.com/EvolutionAPI/evolution-api)) directly through one
internal client. Core capabilities today: **Google Calendar** actions, **audio
transcription** — on request, or automatically for chats you enrol — and reading **PDFs and
images**.

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

## Automatic transcription

Some people send a lot of voice notes. Enrol a chat once and every voice note in it comes back
transcribed, as a reply to the audio, in the same format a manual `@lisa transcribe` produces.

Configure it from the chat with yourself — nowhere else:

    @lisa setup

Add a **contact** by forwarding their contact card (the number is read from the vCard, never
guessed); add a **group** by name (matched against your own chat list, most recently active
first). Each chat is set to **inbound** (what they send), **outbound** (what you send), or
**in & out**.

Two blanket rules cover whole categories: **all contacts** and **all groups**, each with its own
direction. A chat's own rule and the blanket rule for its kind add up, so "all contacts:
outbound" plus "Mãe: in & out" writes out your audio for everyone and brings Mãe's back too. `setup` also lists what is active — contacts and groups titled separately but
numbered in one sequence, so "edit 2 to outbound" or "remove 5" is enough — and every change
is confirmed before it is written.

Off by default. Turn it on with `AUTO_TRANSCRIBE_ENABLED=true` on the flow, plus
`AUTO_TRANSCRIBE_URL` on the dispatcher so untagged voice notes reach it at all
(see `Lisa flow/.env.example`).

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
