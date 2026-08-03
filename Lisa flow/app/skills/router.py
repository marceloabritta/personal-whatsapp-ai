"""The domain router — programmatic-first, an LLM classifier only on ambiguity.

The orchestrator decides the domain in CODE. Each skill offers a cheap matcher over the turn
text; the first that says "yes" wins with no model call. If none says yes and at least one says
"maybe", one lightweight classifier call resolves it. On no signal at all — or any classifier
error — the router falls back to `settings.default_domain` (web, the read-only general skill)."""
from __future__ import annotations

import logging

log = logging.getLogger("mary.router")


def _classify_schema(domains: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["domain"],
        "properties": {"domain": {"type": "string", "enum": domains}},
    }


_CLASSIFY_SYSTEM = (
    "Classify the user's request into exactly one domain.\n"
    '- "calendar": creating, listing, finding, rescheduling, or cancelling calendar events.\n'
    '- "web": anything else — general questions, chit-chat, or looking something up online.\n'
    'Respond ONLY as JSON: {"domain": "calendar"} or {"domain": "web"}.'
)


async def classify_domain(text: str, domains: list[str], reasoner, settings) -> str:
    """One cheap enforced-JSON call → a domain in `domains`. Raises on any failure so the
    caller can fall back to the default."""
    data = await reasoner.classify(
        system=_CLASSIFY_SYSTEM,
        text=text,
        schema=_classify_schema(domains),
        max_tokens=32,
        effort=getattr(settings, "router_effort", "low"),
    )
    domain = (data or {}).get("domain")
    if domain not in domains:
        raise ValueError(f"classifier returned {domain!r}, not in {domains}")
    return domain


async def route_domain(state: dict, settings, *, reasoner=None) -> tuple[str, str]:
    """Return (domain, how) where `how` is "matcher" | "classifier" | "default" — for the trace.

    Imported lazily so this module has no import cycle with the skills registry."""
    from . import SKILLS

    text = state.get("text") or ""
    any_maybe = False
    for name, skill in SKILLS.items():
        if skill.matcher is None:
            continue
        verdict = skill.matcher(text)
        if verdict == "yes":
            return name, "matcher"
        if verdict == "maybe":
            any_maybe = True

    if not any_maybe or reasoner is None:
        return settings.default_domain, "default"

    try:
        return await classify_domain(text, list(SKILLS), reasoner, settings), "classifier"
    except Exception as exc:  # any classifier/transport error → safe default
        log.warning("domain classifier failed (%s); defaulting to %s", exc, settings.default_domain)
        return settings.default_domain, "default"
