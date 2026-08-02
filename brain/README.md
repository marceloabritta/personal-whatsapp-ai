# Mary — brain (Step 1: the `@mary` trigger skeleton)

The rebuilt WhatsApp AI brain. **LangGraph OSS (MIT) + FastAPI**, self-hosted next to
the Evolution API stack, calling Evolution's HTTP API **directly** through one internal
client. No LangGraph Platform, no `langgraph-api` server, no third-party MCP.

## What Step 1 does

A message tagged `@mary` from the owner — and nothing else — wakes the graph, which
replies with a fixed acknowledgement. Everything else is silently ignored.

```
Evolution ──MESSAGES_UPSERT──▶ POST /webhook ──▶ parse ──▶ gate ──▶ ack ──▶ send ──▶ Evolution
                                                            │
                                                            └─(no trigger)─▶ stop
```

The gate opens only when: (a) the message is from the owner (`fromMe`) **and** carries
`@mary`, or (b) a session for that chat is already open. Every run leaves a **two-level
trace**: a code-level event stream and a user-level transcript, sharing one trace id.

## Layout

    app/
      main.py            FastAPI app + POST /webhook
      graph.py           LangGraph StateGraph: parse -> gate -> ack -> send
      state.py           MessageState
      config.py          env settings (pydantic-settings)
      deps.py            wires evolution client + sessions + trace
      trace.py           two-level trace (code events + user transcript)
      identity.py        @mary matching + own-message detection + header
      whatsapp.py        Evolution payload -> text
      sessions.py        Redis / in-memory session markers
      nodes/             parse.py, gate.py, ack.py, send.py
      clients/
        evolution.py     the ONE internal Evolution HTTP client (httpx)
    tests/run_step1.py   end-to-end verification (no live Evolution)

## Run it

    cp .env.example .env      # fill in EVOLUTION_APIKEY etc.
    pip install -r requirements.txt
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Register the webhook once (Evolution -> brain):

    POST http://localhost:8080/webhook/set/secretaria
    { "webhook": { "enabled": true, "url": "http://brain:8000/webhook",
                   "events": ["MESSAGES_UPSERT"] } }

## Verify

    cd brain && python tests/run_step1.py

Drives the compiled graph with simulated payloads and asserts: `@mary` replies, plain
messages stay silent, non-owner and Mary's own echoes are ignored (no loop), sessions
continue untagged, and `@maryland` does **not** trigger.
