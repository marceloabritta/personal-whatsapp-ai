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

    # Structural placement, not prompt guidance.
    # `only_self_chat` keeps a skill out of every chat but the owner's chat with himself — the
    # router skips it elsewhere, so no classifier miss can put it in a contact's conversation.
    # `resolve_gate` is the skill's own tool-safety rule, applied by the execute node:
    #   (verb, inputs, state) -> (patched_inputs | None, {"error", "summary"} | None)
    # It answers "may this action run, and against what?" and may PATCH the inputs (that is how
    # an ordinal from a list becomes a resolved id). None = this skill gates nothing.
    only_self_chat: bool = False
    resolve_gate: Optional[Callable] = None

    # Verbs that run OUT OF BAND: stripped from `actions` at the confirm node and dispatched
    # detached. They never gate, never render, never reach `respond`, and nothing in the reply path
    # awaits them. For work whose failure must cost the person in the chat nothing — an address-book
    # write, not a calendar write.
    side_effects: set = field(default_factory=set)

    # (state, ctx) -> str | None. A block appended to this skill's system prompt, built in code from
    # in-memory state on the turn it is used. MUST be pure and synchronous: it runs inside the reply
    # path, so anything that awaits or burns CPU here stalls every chat, not just this one.
    context_provider: Optional[Callable] = None

    # Parts of the skill that only exist when a setting is on. A feature flag that gates only the
    # RUNTIME leaves the model still being told about a capability it does not have — it then
    # reasons against a section of the prompt that is never injected. These make the flag reach the
    # contract itself, so "disabled" really is the pre-feature build.
    #   gated_verbs:    {verb: settings_attr} — absent from the schema when the attr is falsy
    #   gated_guidance: [(settings_attr, text)] — appended to guidance only when truthy
    gated_verbs: dict = field(default_factory=dict)
    gated_guidance: list = field(default_factory=list)

    def enabled_verbs(self, settings=None) -> list:
        """This skill's verbs for the given settings (all of them when settings is None)."""
        if settings is None:
            return list(self.verbs)
        return [v for v in self.verbs
                if not self.gated_verbs.get(v) or getattr(settings, self.gated_verbs[v], False)]

    # Per-skill reason-call runtime. model/effort fall back to the settings default when None, so a
    # skill only names what it wants to differ. `think` turns on adaptive thinking for this skill's
    # reason call — its depth is governed by `effort` (this is how Sonnet 5 exposes thinking under a
    # forced-JSON output_config; there is no separate token budget).
    model: Optional[str] = None                       # override settings.claude_model
    effort: Optional[str] = None                      # override settings.claude_effort
    think: bool = False                               # True = adaptive thinking on; False = off


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
