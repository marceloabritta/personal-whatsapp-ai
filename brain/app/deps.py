"""Wire the runtime dependencies once, from settings. Injected into the graph so
nodes stay pure and tests can swap any piece for a stub."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clients.evolution import Evolution
from .config import Settings, load_settings
from .sessions import InMemorySessions, RedisSessions
from .trace import Trace, build_trace


@dataclass
class Deps:
    settings: Settings
    evolution: Any
    sessions: Any
    trace: Trace


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or load_settings()

    store = None
    sessions: Any = InMemorySessions(ttl=settings.session_ttl)
    if settings.redis_url:
        import redis  # local import so the dep is optional

        client = redis.from_url(settings.redis_url, decode_responses=True)
        sessions = RedisSessions(client, ttl=settings.session_ttl)
        store = client

    return Deps(
        settings=settings,
        evolution=Evolution(
            settings.evolution_url,
            settings.evolution_apikey,
            settings.evolution_instance,
        ),
        sessions=sessions,
        trace=build_trace(store=store),
    )
