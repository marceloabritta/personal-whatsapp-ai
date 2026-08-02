"""Wire the runtime dependencies once, from settings. Injected into the graph so
nodes stay pure and tests can swap any piece for a stub."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clients.evolution import Evolution
from .config import Settings, load_settings
from .reasoning import build_reasoner
from .sessions import InMemorySessions, RedisSessions
from .trace import Trace, build_trace


@dataclass
class Deps:
    settings: Settings
    evolution: Any
    sessions: Any
    trace: Trace
    reasoner: Any
    redis: Any = None


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or load_settings()

    store = None
    redis_client = None
    # The listening window uses the loop TTL (default 60s), not the old session TTL.
    sessions: Any = InMemorySessions(ttl=settings.loop_ttl_seconds)
    if settings.redis_url:
        import redis  # local import so the dep is optional

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        sessions = RedisSessions(redis_client, ttl=settings.loop_ttl_seconds)
        store = redis_client

    return Deps(
        settings=settings,
        evolution=Evolution(
            settings.evolution_url,
            settings.evolution_apikey,
            settings.evolution_instance,
        ),
        sessions=sessions,
        trace=build_trace(store=store),
        reasoner=build_reasoner(settings),
        redis=redis_client,
    )
