"""Run the history agent from a shell: one question, or a short conversation.

    python -m app.history.cli "who did I talk to about a demo day in the US?"
    python -m app.history.cli --fake "quem me mandou o deck do Kestrel?"
    python -m app.history.cli --fake -i                     # a conversation, notes carried along
    python -m app.history.cli --notes '{"looking_for": "..."}' "what else did he say?"
    python -m app.history.cli --json "..."                  # the AgentResult, for eval scripts

The real store is PgHistoryStore over DATABASE_URL (or HISTORY_DATABASE_URL); the media is
EvolutionHistoryMedia over EVOLUTION_* and the transcription service. --fake uses the invented
dataset in fake.py instead, with no database and no Evolution. Files the agent picks are LISTED,
never sent. The trace shows each tool call's input and timing, never what it returned.

Cost is estimated at Claude Opus 5.5 prices: $4/M input, $20/M output, $0.20/M cache reads,
$5/M cache writes (5-minute entries)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any, Optional

PRICES = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}  # $ per million


def cost_of(usage: dict) -> float:
    """Dollars for a turn's usage, as add_usage counts it: `input` is everything sent, of which
    `cache_read` was read back and `cache_write` was stored."""
    u = {k: int(usage.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")}
    uncached = max(0, u["input"] - u["cache_read"] - u["cache_write"])
    return (uncached * PRICES["input"] + u["cache_write"] * PRICES["cache_write"]
            + u["cache_read"] * PRICES["cache_read"] + u["output"] * PRICES["output"]) / 1e6


async def build(settings: Any, fake: bool):
    """(store, media, closers)."""
    if fake:
        from .fake import FakeHistoryMedia, FakeHistoryStore

        store = FakeHistoryStore(owner_name=settings.owner_name)
        return store, FakeHistoryMedia(store), []

    dsn = settings.history_database_url or settings.database_url
    if not dsn:
        raise SystemExit("DATABASE_URL is not set: use --fake, or run where the database is.")
    from ..cache import TranscriptionService, TranscriptStore
    from ..clients.evolution import Evolution
    from ..transcription import build_transcriber
    from .media import EvolutionHistoryMedia
    from .store import PgHistoryStore

    closers: list = []
    store = PgHistoryStore(dsn, schema=settings.history_schema)
    await store.open()
    closers.append(store)
    evolution = Evolution(settings.evolution_url, settings.evolution_apikey,
                          settings.evolution_instance)
    transcription = TranscriptionService(evolution, build_transcriber(settings), settings)
    if settings.database_url:
        tstore = TranscriptStore(settings.database_url, schema=settings.log_schema)
        try:
            await tstore.open()
            transcription.store = tstore
            closers.append(tstore)
        except Exception as exc:  # the cache is a saving, not a requirement
            print(f"(transcript cache unavailable: {type(exc).__name__})", file=sys.stderr)
    return store, EvolutionHistoryMedia(evolution, transcription, settings), closers


def _settings(args) -> Any:
    from ..config import Settings

    s = Settings()
    update = {k: v for k, v in (("history_effort", args.effort), ("history_model", args.model),
                                ("history_max_tool_calls", args.max_calls),
                                ("history_max_seconds", args.max_seconds)) if v is not None}
    return s.model_copy(update=update) if update else s


def _print_event(ev: dict) -> None:
    err = f" ERROR {ev['error']}" if ev.get("error") else ""
    print(f"  · {ev['tool']} {json.dumps(ev.get('args'), ensure_ascii=False)} → n={ev.get('n')} "
          f"{ev.get('ms')} ms{err}", file=sys.stderr, flush=True)


async def _describe_files(store: Any, ids: list) -> list[str]:
    out = []
    for mid in ids:
        try:
            h = await store.get_message(mid) or {}
        except Exception:
            h = {}
        name = h.get("file_name") or h.get("kind") or "?"
        out.append(f"{mid} — {name} ({h.get('chat_name') or h.get('chat') or '?'})")
    return out


def _report(res: dict, elapsed: float, files: list[str]) -> str:
    u = res.get("usage") or {}
    lines = [f"── answer · state={res.get('state')} lang={res.get('lang')} "
             f"stop={res.get('stop_reason')} error={res.get('error_category')} · {elapsed:.1f} s ──",
             res.get("message") or "(no message)"]
    if files:
        lines.append("files to send: " + "; ".join(files))
    lines.append(f"── {len(res.get('tool_calls') or [])} tool calls ──")
    for i, ev in enumerate(res.get("tool_calls") or [], 1):
        err = f" ERROR {ev['error']}" if ev.get("error") else ""
        lines.append(f"{i:>2}. {ev['tool']} {json.dumps(ev.get('args'), ensure_ascii=False)} "
                     f"→ n={ev.get('n')} {ev.get('ms')} ms{err}")
    uncached = max(0, int(u.get("input") or 0) - int(u.get("cache_read") or 0)
                   - int(u.get("cache_write") or 0))
    lines.append(
        f"── tokens: input {u.get('input', 0):,} (cache read {u.get('cache_read', 0):,}, cache "
        f"write {u.get('cache_write', 0):,}, uncached {uncached:,}) · output {u.get('output', 0):,} "
        f"· ≈ ${cost_of(u):.4f} · {res.get('model')} · {res.get('provider_request_id')} ──")
    if res.get("reasoning"):
        lines.append(f"reasoning: {res['reasoning']}")
    lines.append("notes: " + json.dumps(res.get("notes") or {}, ensure_ascii=False))
    return "\n".join(lines)


async def run(args) -> int:
    import anthropic

    from .agent import run_history_agent

    settings = _settings(args)
    store, media, closers = await build(settings, args.fake)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    notes: Optional[dict] = json.loads(args.notes) if args.notes else None
    lang: Optional[str] = args.lang
    convo: list[dict] = []
    question = args.question
    status = 0
    try:
        while True:
            if not question:
                if not args.interactive:
                    print("a question is required (or -i)", file=sys.stderr)
                    return 2
                try:
                    question = input("\nyou> ").strip()
                except EOFError:
                    break
                if not question:
                    break
            convo.append({"role": "user", "content": question})
            t0 = time.monotonic()
            res = await run_history_agent(
                convo=convo, store=store, media=media, settings=settings, notes=notes,
                session_lang=lang, client=client,
                on_event=None if args.json else _print_event)
            elapsed = time.monotonic() - t0
            if args.json:
                print(json.dumps({**res, "elapsed_s": round(elapsed, 2),
                                  "cost_usd": round(cost_of(res.get("usage") or {}), 5)},
                                 ensure_ascii=False))
            else:
                print(_report(res, elapsed, await _describe_files(store, res.get("files") or [])))
            if res.get("error_category") not in (None, "none", "budget"):
                status = 1
            if not args.interactive or res.get("state") == "close":
                break
            if res.get("message"):
                convo.append({"role": "assistant", "content": res["message"]})
            notes = res.get("notes") or notes
            lang = lang or res.get("lang")
            question = None
    finally:
        await client.close()
        for c in reversed(closers):
            try:
                await c.aclose()
            except Exception:
                pass
    return status


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.history.cli",
                                description="Ask Lisa's history search one question.")
    p.add_argument("question", nargs="?", help="the owner's question, as he would write it")
    p.add_argument("--fake", action="store_true", help="use the invented dataset (fake.py)")
    p.add_argument("--notes", help="notes JSON from a previous turn")
    p.add_argument("--lang", help="lock the conversation's language (ISO 639-1)")
    p.add_argument("--json", action="store_true", help="print the AgentResult as JSON")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="keep the conversation going, carrying notes forward")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model")
    p.add_argument("--max-calls", dest="max_calls", type=int)
    p.add_argument("--max-seconds", dest="max_seconds", type=int)
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
