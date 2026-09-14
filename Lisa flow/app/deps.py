"""Wire the runtime dependencies once, from settings. Injected into the graph so
nodes stay pure and tests can swap any piece for a stub."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cache import TranscriptionService
from .clients.evolution import Evolution
from .config import Settings, load_settings
from .echoes import InMemoryEchoes, RedisEchoes
from .reasoning import build_reasoner
from .roster import DailyCap, Roster
from .sessions import InMemorySessions, RedisSessions
from .directory import Directory
from .skills import (
    confirm_policies, handlers, render_policies, resolve_gates, side_effect_verbs,
)
from .transcription import build_transcriber
from .trace import Trace, build_trace


@dataclass
class Deps:
    settings: Settings
    evolution: Any
    sessions: Any
    echoes: Any
    trace: Trace
    reasoner: Any
    transcription: Any = None  # TranscriptionService (download + transcribe + cache)
    redis: Any = None
    # Skills framework — the registry fanned out (see skills/__init__.py).
    # Session review sink — attached in the FastAPI lifespan when REVIEW_ENABLED. act_node
    # offers a closed loop id to it; None (dev, tests, review off) makes that a no-op.
    reaper: Any = None
    tools: dict = None              # {domain: handler_instance} for the execute node
    confirm_policies: dict = None   # {domain: ConfirmPolicy|None} for the confirm node
    render_policies: dict = None    # {domain: RenderPolicy|None} for the respond node
    resolve_gates: dict = None      # {domain: gate} — the execute node's tool-safety rules
    side_effects: dict = None       # {domain: {verb}} — the confirm node's strip list
    directory: Any = None           # the address book (app/directory.py); None = contacts off
    # Auto-transcription. `roster` is read by the gate on every voice note (pure, no I/O) and
    # written by the setup skill; `caps` is the per-chat daily ceiling.
    roster: Any = None
    caps: Any = None


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or load_settings()

    store = None
    redis_client = None
    # The listening window uses the loop TTL (default 60s), not the old session TTL.
    sessions: Any = InMemorySessions(ttl=settings.loop_ttl_seconds)
    echoes: Any = InMemoryEchoes(ttl=settings.echo_ttl_seconds)
    if settings.redis_url:
        import redis  # local import so the dep is optional

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        sessions = RedisSessions(redis_client, ttl=settings.loop_ttl_seconds)
        echoes = RedisEchoes(redis_client, ttl=settings.echo_ttl_seconds)
        store = redis_client

    evolution = Evolution(
        settings.evolution_url,
        settings.evolution_apikey,
        settings.evolution_instance,
    )
    # Transcription service: download + transcribe + cache. The durable cache tier (a
    # TranscriptStore) is attached best-effort in the FastAPI lifespan when a DB is present.
    transcription = TranscriptionService(
        evolution, build_transcriber(settings), settings
    )

    # The roster starts empty and in-memory; the durable tier is attached in the FastAPI
    # lifespan when a DB is present, exactly like the transcript cache.
    roster = Roster(ttl=settings.roster_cache_ttl)
    caps = DailyCap(settings.auto_transcribe_daily_cap)

    tools = handlers(settings)

    # The address book. Built only when the feature is on AND its own refresh token is present —
    # contacts never borrows the calendar token, so a bad mint cannot take the calendar down.
    directory = None
    if settings.contacts_enabled and settings.google_contacts_refresh_token:
        from .tools.people import GooglePeople

        directory = Directory(settings, people=GooglePeople(settings))
        cal = tools.get("calendar")
        if cal is not None:
            cal.directory = directory
    # The setup handler needs the roster it edits and the client it searches chats with. The
    # skills fan-out builds handlers from settings alone, so they are attached here — the same
    # way the transcript store is attached to the transcription service.
    setup_tool = tools.get("setup")
    if setup_tool is not None:
        setup_tool.roster = roster
        setup_tool.evolution = evolution

    # The reasoner builds its default (calendar) schema itself; the reason node passes the
    # routed skill's per-call schema each turn. No merged schema, no MCP tools in v1.
    return Deps(
        settings=settings,
        evolution=evolution,
        sessions=sessions,
        echoes=echoes,
        trace=build_trace(store=store),
        reasoner=build_reasoner(settings),
        transcription=transcription,
        redis=redis_client,
        tools=tools,
        confirm_policies=confirm_policies(),
        render_policies=render_policies(),
        resolve_gates=resolve_gates(),
        side_effects=side_effect_verbs(),
        directory=directory,
        roster=roster,
        caps=caps,
    )
