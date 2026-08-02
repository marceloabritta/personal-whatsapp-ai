"""Wire the runtime dependencies once, from settings. Injected into the graph so
nodes stay pure and tests can swap any piece for a stub."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clients.evolution import Evolution
from .config import Settings, load_settings
from .echoes import InMemoryEchoes, RedisEchoes
from .reasoning import build_reasoner
from .sessions import InMemorySessions, RedisSessions
from .tools.registry import (
    build_mcp_servers,
    build_output_schema,
    build_task_prompts,
    build_tools_prompt,
    confirm_first,
    local_handlers,
)
from .trace import Trace, build_trace


@dataclass
class Deps:
    settings: Settings
    evolution: Any
    sessions: Any
    echoes: Any
    trace: Trace
    reasoner: Any
    redis: Any = None
    # Tool framework — the registry fanned out (see tools/registry.py).
    tools: dict = None            # {domain: handler_instance} for the execute node
    tools_prompt: str = ""        # the tool list injected into the system prompt
    task_prompts: str = ""        # per-tool guidance blocks appended to the prompt
    confirm_first: dict = None    # {domain: {verbs needing a go-ahead}}


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

    # Fan the tool registry out into every seam, once.
    output_schema = build_output_schema()
    mcp_servers = build_mcp_servers(settings=settings)

    return Deps(
        settings=settings,
        evolution=Evolution(
            settings.evolution_url,
            settings.evolution_apikey,
            settings.evolution_instance,
        ),
        sessions=sessions,
        echoes=echoes,
        trace=build_trace(store=store),
        reasoner=build_reasoner(settings, output_schema=output_schema, mcp_servers=mcp_servers),
        redis=redis_client,
        tools=local_handlers(settings=settings),
        tools_prompt=build_tools_prompt(owner_name=settings.owner_name),
        task_prompts=build_task_prompts(owner_name=settings.owner_name),
        confirm_first=confirm_first(),
    )
