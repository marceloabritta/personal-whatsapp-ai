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
from .sessions import InMemorySessions, RedisSessions
from .skills import confirm_policies, handlers, render_policies
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
    tools: dict = None              # {domain: handler_instance} for the execute node
    confirm_policies: dict = None   # {domain: ConfirmPolicy|None} for the confirm node
    render_policies: dict = None    # {domain: RenderPolicy|None} for the respond node


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
        tools=handlers(settings),
        confirm_policies=confirm_policies(),
        render_policies=render_policies(),
    )
