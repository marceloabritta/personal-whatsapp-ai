"""The web skill — the general / default assistant that can also read the live web.

`kind="native"`: it takes no local actions. Instead it declares Anthropic's server-side
`web_search` and `web_fetch` tools, which the reason node attaches to the model call and which
run inside that call (the reasoner's pause_turn loop already handles the hops). So the web skill
is "pass the context to the model with web tools loaded" — it answers questions, stays silent
when nothing is directed at it, and searches/reads the web when a good answer needs live info.

Because it has no verbs, its enforced-output schema is the lean base (reasoning/state/message/
lang) with no `actions` field, and it has no confirm or render policy — the reply is whatever
its single reason call writes."""
from __future__ import annotations

from .base import Skill

# The prose is templated with {owner_name} the same way the calendar guidance is.
DESCRIBE = "Search and read the live web to answer general questions for {owner_name}."

GUIDANCE = """You have two tools available — web search and web fetch. Use them when a good answer needs current or external information: news, prices, schedules, facts you are not sure of, or a page {owner_name} or the other person linked. Prefer searching over guessing; then answer plainly from what you found, in your own words. For things you already know, or ordinary conversation, just answer — do not search needlessly, and do not narrate that you are searching.

You take no other actions in this conversation. You cannot change {owner_name}'s calendar, send anything, or act on his accounts — if you are asked to, say briefly that it is not something you can do here, and stop. Keep every message short and direct, and write in the language of the message that called you in."""


def web_server_tools(settings) -> list[dict]:
    """The native Anthropic server-tool defs, built with the configured usage cap. Exact type
    strings match the ones this repo ran before web tools were globally disabled."""
    n = settings.web_search_max_uses
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": n},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": n},
    ]


WEB = Skill(
    name="web",
    kind="native",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    server_tools=web_server_tools,   # a builder(settings) -> list; server_tools_for resolves it
    # no verbs/schemas/handler, no confirm, no render, no matcher — web is the default skill.
    # Long, expensive server-tool turns were degenerating the forced-JSON output into silence, so
    # this path gets a real thinking channel (adaptive, depth set by effort). Sonnet / medium /
    # thinking-on; bump to opus + high here if the web answers need more muscle.
    model="claude-sonnet-5",
    effort="medium",
    think=True,
)
