"""The domain router — programmatic-first, an LLM classifier only when it can't be sure.

Decides the domain in CODE where possible:
  1. an obvious matcher "yes" (a clear calendar word) wins with no model call;
  2. a continuation of an already-open loop STICKS to that loop's domain (no model call) — so a
     keyword-less reply like "sim, crie" stays in the calendar conversation it belongs to;
  3. otherwise a cheap classifier decides, reading the RECENT CONVERSATION (not just the last
     line) plus a short preamble about how the assistant works;
  4. no reasoner / classifier error → `settings.default_domain` (web, the read-only general skill)."""
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


# Preamble + domain rules. The classifier reads the RECENT CONVERSATION (passed as messages), not
# just the last line, so a short follow-up ("sim, crie", "adicione a Ana", "muda pra 16h") is
# judged in context. Owner name is substituted (no str.format, to keep the JSON braces literal).
_CLASSIFY_SYSTEM = (
    "You are the routing step of {owner}'s WhatsApp assistant. The assistant takes part in "
    "{owner}'s conversations: it reads the recent messages (below, in order; the assistant's own "
    "past replies were sent on {owner}'s behalf under a header like \"[{owner}'s AI Assistant]:\") "
    "and acts on the LATEST thing {owner} is now asking it to do.\n\n"
    "Classify that latest request into exactly one domain:\n"
    '- "calendar": anything about events on {owner}\'s Google Calendar — creating, listing, '
    "finding, rescheduling, cancelling, or editing an event (its title, time, location, or "
    "guests/attendees; adding or removing a guest; adding a video call). A short follow-up that "
    "acts on an event just discussed in the conversation — \"add Ana as a guest and rename it\", "
    "\"sim, crie\", \"muda pra 16h\" — is calendar.\n"
    '- "web": anything else — general questions, chit-chat, or looking something up online.\n'
    'Respond ONLY as JSON: {"domain": "calendar"} or {"domain": "web"}.'
)


def _classify_system(owner: str) -> str:
    return _CLASSIFY_SYSTEM.replace("{owner}", owner or "the owner")


def _neutral(messages: list) -> list[dict]:
    """The checkpointed thread as a neutral [{role, content}] conversation (same shape reason uses)."""
    out: list[dict] = []
    for m in messages or []:
        if isinstance(m, dict):
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        else:  # a langchain BaseMessage restored from the checkpoint
            role = "assistant" if getattr(m, "type", "") == "ai" else "user"
            out.append({"role": role, "content": getattr(m, "content", "")})
    return out


async def classify_domain(state: dict, domains: list[str], reasoner, settings) -> str:
    """One cheap enforced-JSON call over the recent conversation → a domain in `domains`. Raises on
    any failure so the caller can fall back to the default."""
    n = getattr(settings, "context_window_messages", 30) or 30
    convo = _neutral(state.get("messages"))[-n:]
    if not convo:  # no thread yet (e.g. a unit test) → fall back to the bare text
        convo = [{"role": "user", "content": state.get("text") or ""}]
    data = await reasoner.classify(
        system=_classify_system(settings.owner_name),
        messages=convo,
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

    Only an OBVIOUS calendar signal (a matcher "yes") skips the model. Anything the matcher can't
    affirmatively place — a keyword-less edit, a cancel, a bare confirmation like "sim, crie" — goes
    to the cheap classifier, NOT silently to web. Web is only the fallback when there's no reasoner
    or the classifier errors. (Earlier, a matcher "no" defaulted straight to web, which stranded
    every calendar request the lexicon didn't recognise on the general skill.)

    Imported lazily so this module has no import cycle with the skills registry."""
    from . import SKILLS

    text = state.get("text") or ""
    # 1. An explicit calendar signal always wins — even to switch INTO calendar mid-loop.
    for name, skill in SKILLS.items():
        if skill.matcher is not None and skill.matcher(text) == "yes":
            return name, "matcher"

    # 2. A continuation of an already-open loop STICKS to that loop's domain — deterministically,
    #    no model call. A reply like "sim, crie" or a keyword-less follow-up edit stays in the
    #    conversation it belongs to instead of being re-decided from scratch. A fresh @mary tag
    #    opens a new loop (loop_domain was cleared on the reset), so it re-decides below.
    loop_domain = state.get("loop_domain")
    if loop_domain and loop_domain in SKILLS and not state.get("loop_opened"):
        return loop_domain, "loop"

    # 3. Not obviously calendar and not inside a loop → ask the cheap classifier.
    if reasoner is None:
        return settings.default_domain, "default"
    try:
        return await classify_domain(state, list(SKILLS), reasoner, settings), "classifier"
    except Exception as exc:  # any classifier/transport error → safe default
        log.warning("domain classifier failed (%s); defaulting to %s", exc, settings.default_domain)
        return settings.default_domain, "default"
