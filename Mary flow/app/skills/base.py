"""The Skill — one domain's whole policy, in one object.

A skill carries everything the graph needs to serve its domain and nothing more:

  - how the model is prompted and what JSON it must emit   → describe / guidance / verbs / schemas
  - how a turn is matched to this domain                    → matcher (for the router)
  - whether / how a pending write is confirmed             → confirm  (skills.confirm)
  - how a tool result becomes a reply                       → render   (skills.render)
  - and either a local handler or native server tools       → kind + handler_cls | server_tools

`kind` is "local" (actions run through a handler behind the execute node) or "native" (the
model uses Anthropic server tools inside the reason call — no local execution). The graph never
imports a concrete skill; it reaches them only through the fan-out in `skills/__init__.py`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Skill:
    name: str
    kind: str                                        # "local" | "native"
    describe: str                                    # one line for the prompt's tool list
    guidance: str                                    # the per-domain prompt block
    verbs: list[str] = field(default_factory=list)
    schemas: dict = field(default_factory=dict)      # local: {verb: {"required":[...], "properties":{...}}}
    handler_cls: Optional[type] = None               # local infra client, built with settings
    confirm: Any = None                              # ConfirmPolicy | None  (skills.confirm)
    render: Any = None                               # RenderPolicy  | None  (skills.render)
    server_tools: Any = None                         # native tool defs, or a builder(settings) -> list
    matcher: Optional[Callable[[str], str]] = None   # text -> "yes" | "no" | "maybe" for this domain


# --- the enforced-schema guards (moved verbatim from the old tools/registry.py) -------------
# Anthropic structured outputs cap a schema at 16 union/array params and 24 optional params.
# Now checked PER DOMAIN (each skill's own schema), which is always <= the old merged count.

def count_unions(schema: Any) -> int:
    """Count every anyOf + every type:array param — Anthropic caps this at 16."""
    n = 0
    if isinstance(schema, dict):
        if "anyOf" in schema:
            n += 1
        if schema.get("type") == "array":
            n += 1
        for v in schema.values():
            n += count_unions(v)
    elif isinstance(schema, list):
        for v in schema:
            n += count_unions(v)
    return n


def count_optionals(schema: Any) -> int:
    """Count optional (not-required) parameters across the whole schema — capped at 24. For each
    object that is len(properties) - len(required); summed through anyOf branches / array items."""
    n = 0
    if isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            req = set(schema.get("required", []))
            n += sum(1 for k in schema["properties"] if k not in req)
        for v in schema.values():
            n += count_optionals(v)
    elif isinstance(schema, list):
        for v in schema:
            n += count_optionals(v)
    return n
