"""Wire the runtime dependencies once, from settings. Injected into the graph so
nodes stay pure and tests can swap any piece for a stub."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .clients.evolution import Evolution
from .config import Settings, load_settings
from .reasoning import build_reasoner
from .sessions import InMemorySessions, RedisSessions
from .tools.registry import (
    TOOLS,
    build_mcp_servers,
    build_output_schema,
    build_tools_prompt,
    local_handlers,
)
from .trace import Trace, build_trace


@dataclass
class Deps:
    settings: Settings
    evolution: Any
    sessions: Any
    trace: Trace
    reasoner: Any
    tools: dict = field(default_factory=dict)  # {domain: local handler}
    tools_prompt: str = ""
    redis: Any = None


def build_deps(settings: Settings | None = None) -> Deps:
    settings = settings or load_settings()

    store = None
    redis_client = None
    sessions: Any = InMemorySessions(ttl=settings.loop_ttl_seconds)
    if settings.redis_url:
        import redis

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        sessions = RedisSessions(redis_client, ttl=settings.loop_ttl_seconds)
        store = redis_client

    # Fan the tool registry out into schema (local) + MCP servers (mcp) + prompt (both).
    output_schema = build_output_schema(TOOLS)
    mcp_servers = build_mcp_servers(TOOLS, settings)
    tools_prompt = build_tools_prompt(TOOLS)
    handlers = local_handlers(TOOLS, settings)

    return Deps(
        settings=settings,
        evolution=Evolution(settings.evolution_url, settings.evolution_apikey,
                            settings.evolution_instance),
        sessions=sessions,
        trace=build_trace(store=store),
        reasoner=build_reasoner(settings, output_schema=output_schema, mcp_servers=mcp_servers),
        tools=handlers,
        tools_prompt=tools_prompt,
        redis=redis_client,
    )
