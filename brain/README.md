# Mary — the WhatsApp AI brain

A self-hosted assistant that lives inside a WhatsApp conversation. The owner calls it
by tagging a message `@mary`; the brain then reads the chat, reasons with Claude, can
run real tools (today: Google Calendar), and posts back into the same conversation.

Built on **LangGraph OSS (MIT) + FastAPI**. It talks to the [Evolution API](https://github.com/EvolutionAPI/evolution-api)
WhatsApp gateway over plain HTTP through one internal client — no LangGraph Platform,
no `langgraph-api` server, no third-party MCP for transport.

> 📖 **Full technical documentation** — architecture maps, call flow, JSON contracts, the
> system prompt, logging, and the graph logic in plain language — lives in
> [`docs/documentation.html`](docs/documentation.html) (open it in a browser).

---

## How it works

Evolution delivers every message in the connected chats to the brain's `/webhook`.
The brain answers `200` immediately and runs the graph in the background, scoped to
that chat's persistent thread.

1. **Trigger.** A message from the owner (`fromMe`) carrying `@mary` opens a
   *listening window* for that chat (a TTL marker, default 60s).
2. **Listen.** While the window is open, **every** message in the chat — from anyone —
   is fed to the model, so the owner can keep talking without re-tagging. The model
   decides per message whether to speak or stay silent.
3. **Reason.** Claude receives the recent transcript (labeled by speaker) plus any tool
   results, and returns a single **enforced-JSON** decision: what to say, whether to
   keep listening, and which tools to run.
4. **Act.** Tool actions run locally; a read or a failure loops back to the model for a
   read-back; a clean result is posted to the chat under the owner's assistant header.
5. **Close.** The model can end the window (`close_loop`), or it simply lapses after the
   TTL of quiet — no message is sent on a timeout.

Replies are posted from the owner's own number, so the brain stamps a header
(`*[<owner>'s AI Assistant]:*`) on every outgoing message. That header is also how it
recognizes its own echoes and never replies to itself. **Only the owner** may direct it
or authorize actions; other participants' messages are treated as information.

---

## The graph

```
Evolution ──MESSAGES_UPSERT──▶ POST /webhook
                                    │  (200 immediately, run in background)
                                    ▼
                                  parse ──▶ gate ──┬─(no trigger)─▶ END   (silent)
                                                   │
                                              (owner @mary,
                                            or window open)
                                                   ▼
                                                context ──▶ reason ──┬─(no actions)──────▶ act ──▶ END
                                                                     │                     ▲
                                                                (has actions)              │
                                                                     ▼                     │
                                                                  execute ──┬─(read/fail)──┘ (read-back → reason)
                                                                            │
                                                                            └─(clean write)──▶ act ──▶ END
```

| Node | Responsibility |
|------|----------------|
| **parse**   | Normalize the Evolution payload → `{from_me, jid, text, msg_id, …}`; detect the tag and own-echoes. |
| **gate**    | Open the window on an owner `@mary`; continue while a window is open; otherwise stop silently. |
| **context** | Assemble the turn. First activation seeds the last *N* messages from Evolution; later turns fetch only messages after a stored cursor. Own-origin messages are filtered. Produces one labeled transcript turn. |
| **reason**  | Call the provider-neutral reasoner; get the enforced-JSON decision (message / loop_state / actions / workflow) plus metadata. |
| **execute** | Run each action in order via its local tool. A `list`/search or any failure triggers a **read-back** (loop to `reason`, bounded by `MAX_TOOL_ACTIONS`); a clean write goes straight to `act`. |
| **act**     | Post `next_message` (header-framed) to the chat; apply `loop_state`; emit the one-row activation record. This is the single place a sent message enters history. |

The graph is compiled with a **checkpointer**, so each chat's message history, ingestion
cursor, session language, and in-flight workflow persist across activations.

### What the model returns each turn (enforced JSON)

The response format is constrained by a JSON Schema, so every turn yields exactly:

```jsonc
{
  "lang": "pt",                       // ISO 639-1 of the reply language
  "next_message": "Marquei a call!",  // text to post, or null to stay silent
  "loop_state": "keep_listening",     // or "close_loop" — independent of any action
  "actions": [                        // 0..N tools to run now, may span domains
    { "task": "calendar.create", "inputs": { "title": "Call", "start": "2026-08-03T15:00:00-03:00" } }
  ],
  "workflow": {                       // memory of an in-flight goal while gathering, or null
    "task": "calendar.create",
    "known_inputs":  [{ "field": "title", "value": "Call" }],
    "open_questions":[{ "field": "attendee_email", "reason": "to send the invite" }]
  }
}
```

Running an action does **not** close the window, and closing does not require an action —
they are independent decisions. The model never claims success before it sees a result.

---

## Tools

Tools are declared once in [`app/tools/registry.py`](app/tools/registry.py). Each entry is
either:

- **`local`** — a handler we run inside the `execute` node. It contributes its per-verb
  input schema to the enforced-JSON `actions` contract, and its result is fed back to the
  model on the read-back.
- **`anthropic_mcp`** — a remote MCP server Claude calls inline via the connector (wired,
  none registered yet).

At startup the registry fans out into: the output schema (local tools), the MCP server
config (mcp tools), the prompt's tool list (both), and the handler instances (local).
Add a tool to the registry and every seam updates.

### Calendar (local, Google Calendar API)

Runs on the owner's calendar via an OAuth2 refresh-token client. Verbs:

| Task | Required inputs | Notes |
|------|-----------------|-------|
| `calendar.create` | `title`, `start` | 45-min default duration; location **XOR** virtual (video wins → Meet link); `attendees` emailed unless `send_invites:false`. |
| `calendar.list`   | *(none)* | `query` / `time_min` / `time_max`; returns upcoming events, each with its `event_id`. |
| `calendar.update` | `event_id` | Fetch → patch; resends invites. |
| `calendar.delete` | `event_id` | Cancels and notifies attendees. |

To change or cancel, the model must **search first** (`calendar.list`) to resolve the
`event_id`, then confirm before acting. Times are `America/Sao_Paulo`. Every path returns
a structured result (`ok` / `summary` / `data` / `error`) and never raises into the graph.

---

## Project layout

```
app/
  main.py            FastAPI app, lifespan (checkpointer), POST /webhook, GET / (health)
  graph.py           the StateGraph wiring above
  state.py           MessageState (persisted memory + per-turn scratch)
  config.py          settings from env (pydantic-settings)
  deps.py            builds every dependency from settings; fans out the tool registry
  prompt.py          the versioned system prompt (header + tool list injected)
  identity.py        @mary matching, own-message detection, the reply header
  whatsapp.py        Evolution payload → text; labeled transcript
  sessions.py        listening-window markers (Redis or in-memory)
  threads.py         stable per-chat checkpointer thread id
  trace.py           two-level trace: code events + user transcript
  nodes/             parse · gate · context · reason · execute · act
  reasoning/         base.py (Reasoner protocol) · anthropic.py (Claude behind the seam)
  tools/             base.py · registry.py · schemas.py · calendar.py
  clients/
    evolution.py     the ONE internal Evolution HTTP client (httpx)
tests/run_step3.py   end-to-end graph verification (stub reasoner + stub calendar)
```

**Two seams keep the core provider- and vendor-neutral:** the graph depends only on the
`Reasoner` protocol (Claude sits behind it), and on the tool registry (backends swap
without touching nodes).

---

## Dependencies

Python **3.11+**. Installed from [`requirements.txt`](requirements.txt):

- `langgraph`, `langgraph-checkpoint-postgres`, `psycopg[binary,pool]` — the graph + Postgres memory
- `anthropic` — the reasoning provider
- `google-api-python-client`, `google-auth` — the calendar tool
- `fastapi`, `uvicorn[standard]` — the web server
- `httpx` — the Evolution client
- `pydantic-settings` — config
- `redis` — session markers + durable transcript (optional at runtime)
- `structlog` — structured trace logging

---

## Configuration

All config comes from the environment (or a local `.env` — see [`.env.example`](.env.example)).

### Keys a new deployment must supply

| Variable | Required? | What it is |
|----------|-----------|------------|
| `ANTHROPIC_API_KEY` | **Yes** | Claude API key (the reasoning provider). |
| `EVOLUTION_URL` | **Yes** | Base URL of your Evolution API (e.g. `http://api:8080`). |
| `EVOLUTION_APIKEY` | **Yes** | Evolution's `AUTHENTICATION_API_KEY`. |
| `EVOLUTION_INSTANCE` | **Yes** | The Evolution instance name (the connected WhatsApp number). |
| `DATABASE_URL` | Recommended | Postgres DSN for the checkpointer. **Unset → in-memory** (memory is lost on restart). |
| `GOOGLE_CLIENT_ID` | Calendar only | OAuth client id (Google Cloud Console). |
| `GOOGLE_CLIENT_SECRET` | Calendar only | OAuth client secret. |
| `GOOGLE_REFRESH_TOKEN` | Calendar only | Refresh token for the owner's account, scope `.../auth/calendar`. |
| `GOOGLE_CALENDAR_ID` | Calendar only | Target calendar; `primary` for the account's main one. |
| `REDIS_URL` | Optional | Session-window + transcript store. **Unset → in-memory** (fine for a single process). |

### Tunables (sensible defaults)

| Variable | Default | Meaning |
|----------|---------|---------|
| `MARY_TRIGGER_TAG` | `@mary` | Wake tag (comma-separated list allowed). |
| `OWNER_NAME` | `Marcelo` | Name in the reply header + prompt. |
| `LOOP_TTL_SECONDS` | `60` | Listening-window lifetime after each message. |
| `CONTEXT_WINDOW_MESSAGES` | `30` | Messages seeded on a chat's first activation. |
| `CLAUDE_MODEL` | `claude-opus-4-8` | Model id. |
| `CLAUDE_EFFORT` | `high` | Reasoning effort. |
| `CLAUDE_MAX_TOKENS` | `8192` | Output cap. |
| `WEB_SEARCH_MAX_USES` | `5` | Cap on native web search/fetch per turn. |
| `MAX_TOOL_ACTIONS` | `4` | Cap on actions + read-backs per activation. |
| `DEFAULT_MEETING_MINUTES` | `45` | Fallback event duration. |
| `PROMPT_VERSION` | *(set)* | Stamped on every activation record for auditing. |

> **Getting the Google refresh token:** create an OAuth 2.0 client in the Google Cloud
> Console with the Calendar API enabled, then do a one-time consent for the owner's
> account requesting scope `https://www.googleapis.com/auth/calendar` and capture the
> returned refresh token (e.g. via the OAuth Playground or a short local script). The
> refresh token carries the scope; the brain exchanges it for access tokens as needed.

---

## Install & run

### Locally

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY, EVOLUTION_*, and GOOGLE_* if using calendar
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check: `GET /` → `{ "ok": true, "service": "mary-brain", "tags": ["@mary"], "model": "…" }`.

With no `DATABASE_URL`/`REDIS_URL` it runs fully in-memory — good for a quick local try;
memory and windows reset on restart.

### With Docker (alongside Evolution)

The included [`Dockerfile`](Dockerfile) builds a slim image running Uvicorn on `:8000`.
Add it as a service next to the Evolution stack on the same network, e.g.:

```yaml
brain:
  build: { context: ./brain }
  environment:
    EVOLUTION_URL: http://api:8080
    EVOLUTION_APIKEY: ${AUTHENTICATION_API_KEY}
    EVOLUTION_INSTANCE: secretaria
    ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    DATABASE_URL: postgresql://evolution:${POSTGRES_PASSWORD}@postgres:5432/evolution
    REDIS_URL: redis://redis:6379/1
    GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
    GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
    GOOGLE_REFRESH_TOKEN: ${GOOGLE_REFRESH_TOKEN}
    GOOGLE_CALENDAR_ID: ${GOOGLE_CALENDAR_ID}
  depends_on: [api, redis, postgres]
```

The checkpointer can reuse the Evolution Postgres; its tables are created automatically on
first boot. Redis db `1` keeps the brain's markers clear of Evolution's cache.

### Point Evolution at the brain

Register the webhook once so Evolution forwards messages:

```
POST {EVOLUTION_URL}/webhook/set/{instance}
{ "webhook": { "enabled": true,
               "url": "http://brain:8000/webhook",
               "events": ["MESSAGES_UPSERT"] } }
```

---

## Verify

```bash
cd brain && python tests/run_step3.py
```

Drives the compiled graph with a stubbed reasoner and calendar (no network, no Postgres,
no keys) and asserts the full loop: clean-write execution, list read-back, failure
read-back, execution-independent-of-closure, workflow gathering, multi-action turns, the
gate (own-echo + non-owner ignored), and a well-formed output schema.

---

## Tracing

Every activation leaves a **two-level trace** under one id:

- **code** — structured events for the builder: node I/O, provider/tool calls with args
  and replies, decision variables, timings. JSON to stdout (mirrored to LangSmith if
  configured).
- **user** — the human transcript: what was received vs. what Mary posted back.

Each run also emits a one-row **activation record**: activation message id, chat id,
context message ids, provider/model, prompt version, request id, token usage, latency,
stop reason, actions taken, workflow task, the response, delivery result, and error
category. Gated-out messages log a single "ignored" line and nothing else.
```