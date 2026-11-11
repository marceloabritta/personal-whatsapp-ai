"""Prompt caching — the part of a prompt that repeats is marked so Anthropic can read it back.

Anthropic reuses a prompt only as a prefix: the first byte that differs from the last call ends
what is reusable, and nothing says so when it happens. The calls keep working and cost full price.
So this holds the three things that keep the prefix reusable:

  - the prompt: nothing that belongs to the turn (the clock, the language lock, the address book,
    a waiting skill) comes before the first stable offset, for any skill;
  - the request: the stable prefixes go out as marked blocks that join back into the same prompt,
    the conversation is marked by the top-level field only on a turn that carries server tools
    (the one kind that re-sends it), and with the switch off the request is the one sent before
    caching existed;
  - the counts: `usage.input` stays everything she was sent, with what was read back and what was
    stored beside it.

Whether Anthropic really reads it back is tests/live_cache.py. No network, no Postgres, no key.

    python tests/run_prompt_cache.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app.prompt as prompt_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.prompt import SystemPrompt, called_by_block  # noqa: E402
from app.reasoning.anthropic import AnthropicReasoner, add_usage, system_blocks  # noqa: E402
from app.review.judge import Judge  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, output_schema_for, server_tools_for, system_prompt_for,
)
from run_step3 import _invoke, _upsert, make_toolenv  # noqa: E402

_checks = {"pass": 0, "fail": 0}

SP = ZoneInfo("America/Sao_Paulo")
BOOK = "\n\nAddress book — Felipe Prado <felipe@example.com>"
BOOK_2 = "\n\nAddress book — Ana Lima <ana@example.com>"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def settings(**over) -> Settings:
    """Production's switches: every skill on, every gated block in the prompt."""
    base = dict(evolution_apikey="x", anthropic_api_key="", database_url=None, redis_url=None,
                contacts_enabled=True, contacts_crud_enabled=True,
                google_contacts_refresh_token="x", presets_enabled=True, media_enabled=True)
    return Settings(**{**base, **over})


class at:
    """Freeze the owner's clock for the prompt builder."""

    def __init__(self, when: datetime) -> None:
        self.when = when

    def __enter__(self):
        self._real = prompt_mod.now_in
        prompt_mod.now_in = lambda tz_name: (self.when, tz_name)

    def __exit__(self, *exc):
        prompt_mod.now_in = self._real


def prefixes(p) -> tuple[str, str]:
    one, two = p.stable_ends
    return p[:one], p[:two]


# --- the prompt ---------------------------------------------------------------------------

def prompt_checks() -> None:
    print("\nThe prompt — what repeats comes first")
    s = settings()
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=SP)
    # Another minute, another day, another weekday, another year: every part of the date line.
    later = [noon + timedelta(minutes=1), noon + timedelta(days=1), noon + timedelta(days=400)]

    for name in SKILLS:
        with at(noon):
            first = system_prompt_for(name, s)
        check(f"[{name}] the prompt says where its two reusable prefixes end",
              isinstance(first, SystemPrompt) and len(first.stable_ends) == 2
              and 0 < first.stable_ends[0] < first.stable_ends[1] < len(first),
              repr(getattr(first, "stable_ends", None)))
        one, two = prefixes(first)

        # The clock. The whole of both prefixes must survive it, not just the first.
        moved = []
        for when in later:
            with at(when):
                moved.append(prefixes(system_prompt_for(name, s)))
        check(f"[{name}] neither prefix changes with the minute, the day or the year",
              all(m == (one, two) for m in moved))
        check(f"[{name}] the date line is after both", "Current date and time:" not in two
              and "Current date and time:" in first)

        # The language lock: unlocked on the first pass after a tag, locked on every later one.
        with at(noon):
            locked = system_prompt_for(name, s, session_lang="pt")
        check(f"[{name}] locking the language changes neither prefix", prefixes(locked) == (one, two))
        lock_at = locked.find('This conversation is in "pt"')
        check(f"[{name}] the lock comes right after the contract, before the date lines",
              lock_at == locked.stable_ends[1] + 2
              and lock_at < locked.index("Current date and time:"))
        check(f"[{name}] locked or not, the prompt ends on the same date rules",
              locked[locked.index("Current date and time:"):]
              == first[first.index("Current date and time:"):])
        check(f"[{name}] unlocked, there is no lock anywhere",
              "This conversation is in" not in first)
        check(f"[{name}] the rule it locks is there either way",
              "speaks several languages" in one and "speaks several languages" in locked)

        # This turn's block (the address book, his presets) and a waiting skill.
        with at(noon):
            with_book = system_prompt_for(name, s, context_block=BOOK)
            other_book = system_prompt_for(name, s, context_block=BOOK_2)
            called = system_prompt_for(name, s, called_by={"domain": "calendar",
                                                           "goal": "Find Felipe's email"})
        check(f"[{name}] the turn's block leaves the first prefix alone",
              prefixes(with_book)[0] == one and prefixes(other_book)[0] == one)
        check(f"[{name}] the turn's block is inside the second prefix, before the contract",
              BOOK in prefixes(with_book)[1] and BOOK not in one
              and prefixes(with_book)[1].index(BOOK) < prefixes(with_book)[1].index("Respond ONLY as JSON"))
        check(f"[{name}] the same block again gives the same second prefix",
              prefixes(with_book)[1] == prefixes(system_prompt_for(name, s, context_block=BOOK))[1])
        check(f"[{name}] another block gives another second prefix",
              prefixes(with_book)[1] != prefixes(other_book)[1])
        check(f"[{name}] a waiting skill leaves the first prefix alone",
              prefixes(called)[0] == one
              and called_by_block(s.owner_name, {"domain": "calendar", "goal": "x"})[:30] not in one)
        check(f"[{name}] the JSON contract ends the second prefix",
              "Respond ONLY as JSON" in two and "Respond ONLY as JSON" not in one)

    # It is still the prompt: a plain string to everything that reads one.
    with at(noon):
        p = system_prompt_for("calendar", s)
    check("it reads as the string it is", isinstance(p, str) and "calendar" in p.lower())
    check("a copy of it carries no offsets, so it is sent whole",
          not getattr(p.lower(), "stable_ends", ()) and not getattr(p + "", "stable_ends", ()))
    check("an offset outside the text is dropped",
          SystemPrompt("abc", stable_ends=(0, 2, 3, 9)).stable_ends == (2,))


# --- the request --------------------------------------------------------------------------

class _Usage(SimpleNamespace):
    pass


class FakeMessages:
    """Records each create() call and plays back scripted responses."""

    def __init__(self, responses: list) -> None:
        self.calls: list = []
        self._responses = list(responses)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _resp(text: str, usage: _Usage, stop_reason: str = "end_turn", content=None):
    blocks = content if content is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage, _request_id="req_1")


_OK = '{"reasoning": "r", "state": "keep_listening", "message": "Oi", "lang": "pt"}'


def _reasoner(s: Settings, responses: list) -> tuple[AnthropicReasoner, FakeMessages]:
    r = AnthropicReasoner(s)
    fake = FakeMessages(responses)
    r._client = SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))
    return r, fake


def _marks(system) -> list:
    return [b.get("cache_control") for b in system] if isinstance(system, list) else []


async def request_checks() -> None:
    print("\nThe request — marked where the prompt repeats")
    s = settings()
    convo = [{"role": "user", "content": "[12:00] Marcelo: @lisa marca um almoço amanhã"}]

    for name in SKILLS:
        prompt = system_prompt_for(name, s, session_lang="pt", context_block=BOOK)
        r, fake = _reasoner(s, [_resp(_OK, _Usage(input_tokens=10, output_tokens=5))])
        await r.respond(system=prompt, messages=convo,
                        output_schema=output_schema_for(name, settings=s),
                        server_tools=server_tools_for(name, s), model="claude-sonnet-5",
                        effort="medium")
        sent = fake.calls[0]
        system = sent["system"]
        check(f"[{name}] the system prompt goes out as three text blocks",
              isinstance(system, list) and len(system) == 3
              and all(b.get("type") == "text" and b.get("text", "").strip() for b in system))
        check(f"[{name}] joined, the blocks are the prompt, byte for byte",
              isinstance(system, list) and "".join(b["text"] for b in system) == str(prompt))
        check(f"[{name}] the two stable blocks are marked, the per-turn one is not",
              _marks(system) == [{"type": "ephemeral"}, {"type": "ephemeral"}, None])
        check(f"[{name}] the blocks end where the prompt says its prefixes end",
              isinstance(system, list)
              and len(system[0]["text"]) == prompt.stable_ends[0]
              and len(system[0]["text"]) + len(system[1]["text"]) == prompt.stable_ends[1])
        # A turn with server tools re-sends its conversation on every hop; no other turn does,
        # and marking theirs stored a third of every prompt that nothing ever read back.
        tools = bool(server_tools_for(name, s))
        check(f"[{name}] the conversation is {'marked: this turn can search' if tools else 'not marked: one request, nothing re-sent'}",
              sent.get("cache_control") == ({"type": "ephemeral"} if tools else None))
        check(f"[{name}] {'three' if tools else 'two'} breakpoints in all, of the four allowed",
              sum(1 for m in _marks(system) if m) + (1 if sent.get("cache_control") else 0)
              == (3 if tools else 2))
        check(f"[{name}] nothing in the conversation itself is touched", sent["messages"] == convo)

    # A prompt with no offsets (a plain str, from anywhere) is sent whole, with nothing marked.
    r, fake = _reasoner(s, [_resp(_OK, _Usage(input_tokens=1, output_tokens=1))])
    await r.respond(system="You are a test.", messages=convo)
    check("a plain str prompt is sent as the string it is, with no cache field",
          fake.calls[0]["system"] == "You are a test." and "cache_control" not in fake.calls[0])

    # An MCP connector runs its tools on Anthropic's side too, hop after hop, like web search.
    r = AnthropicReasoner(s, mcp_servers=[{"type": "url", "url": "https://mcp.example.com", "name": "x"}])
    fake = FakeMessages([_resp(_OK, _Usage(input_tokens=1, output_tokens=1))])
    r._client = SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))
    await r.respond(system=system_prompt_for("calendar", s), messages=convo)
    check("a turn with an MCP connector marks its conversation too",
          fake.calls[0].get("cache_control") == {"type": "ephemeral"})

    # The switch. Off, the request is the one sent before caching existed.
    off = settings(prompt_cache_enabled=False)
    prompt = system_prompt_for("calendar", off, session_lang="pt", context_block=BOOK)
    r, fake = _reasoner(off, [_resp(_OK, _Usage(input_tokens=1, output_tokens=1))])
    await r.respond(system=prompt, messages=convo)
    sent = fake.calls[0]
    check("off: the system prompt is one plain string", type(sent["system"]) is str
          and sent["system"] == str(prompt))
    check("off: no cache field anywhere in the request",
          "cache_control" not in sent and "cache_control" not in repr(sent["messages"]))
    check("the switch ships on", Settings(evolution_apikey="x").prompt_cache_enabled is True)

    # A server-tool turn (web): every hop re-sends the same prefix, so every hop is marked alike.
    paused = _resp("", _Usage(input_tokens=40, output_tokens=7, cache_creation_input_tokens=6000,
                              cache_read_input_tokens=0),
                   stop_reason="pause_turn",
                   content=[SimpleNamespace(type="server_tool_use", name="web_search")])
    done = _resp(_OK, _Usage(input_tokens=25, output_tokens=30, cache_creation_input_tokens=900,
                             cache_read_input_tokens=6000))
    prompt = system_prompt_for("web", s)
    r, fake = _reasoner(s, [paused, done])
    out = await r.respond(system=prompt, messages=convo, server_tools=server_tools_for("web", s),
                          think=True)
    check("a paused turn is sent again with the same marked prefix",
          len(fake.calls) == 2 and fake.calls[0]["system"] == fake.calls[1]["system"]
          and fake.calls[1].get("cache_control") == {"type": "ephemeral"})
    check("the second hop only adds to the conversation",
          fake.calls[1]["messages"][:len(convo)] == convo
          and len(fake.calls[1]["messages"]) == len(convo) + 1)

    print("\nThe counts — input stays everything she was sent")
    u = out["usage"]
    check("input is uncached + read + written, over every hop",
          u["input"] == (40 + 6000 + 0) + (25 + 900 + 6000), str(u))
    check("what was read back and what was stored are counted beside it",
          u["cache_read"] == 6000 and u["cache_write"] == 6900 and u["output"] == 37, str(u))
    plain = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    add_usage(plain, SimpleNamespace(input_tokens=8000, output_tokens=100))
    check("a response with no cache counts adds none",
          plain == {"input": 8000, "output": 100, "cache_read": 0, "cache_write": 0})
    add_usage(plain, SimpleNamespace(input_tokens=None, output_tokens=None,
                                     cache_read_input_tokens=None, cache_creation_input_tokens=None))
    check("missing counts are zero, not an error", plain["input"] == 8000)
    check("a prompt with one offset is two blocks, the first marked",
          [b.get("cache_control") for b in system_blocks(SystemPrompt("abcdef", (2,)))]
          == [{"type": "ephemeral"}, None])


# --- the graph hands the prompt over whole --------------------------------------------------

async def graph_checks() -> None:
    print("\nThe graph — the offsets reach the reasoner, on every pass")
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["find"] = {"ok": True, "summary": "1 event", "data": {"items": [
        {"event_id": "e1", "title": "Call", "start": "2026-11-12T15:00:00-03:00",
         "end": "2026-11-12T15:30:00-03:00"}]}}
    stub.script = [
        {"message": None, "lang": "en",
         "actions": [{"task": "calendar.find", "query": "call"}]},
        {"message": "You have a call on Thursday at 3pm.", "lang": "en"},
    ]
    await _invoke(graph, _upsert("@mary do I have a call on my calendar this week?", mid="pc1"))
    systems = [c["system"] for c in stub.calls]
    check("a find and its read-back are two passes", len(systems) == 2, str(len(systems)))
    check("both passes hand the reasoner a prompt with its offsets",
          all(isinstance(x, SystemPrompt) and len(x.stable_ends) == 2 for x in systems))
    if len(systems) == 2:
        a, b = systems
        check("the read-back's first prefix is the first pass's, byte for byte",
              a[:a.stable_ends[0]] == b[:b.stable_ends[0]])
        check("the read-back is locked to the language, the first pass is not",
              "This conversation is in" not in a and 'This conversation is in "en"' in b)
        first, second = stub.calls[0]["messages"], stub.calls[1]["messages"]
        check("the read-back's conversation only adds to the first pass's",
              len(second) > len(first) and [getattr(m, "content", m) for m in second[:len(first)]]
              == [getattr(m, "content", m) for m in first])


# --- the judge ----------------------------------------------------------------------------

_VERDICT = ('{"verdict": "pass", "confidence": "high", "rationale": "ok", "task_class": null, '
            '"gaps": [], "proposed_gap": ""}')


async def judge_checks() -> None:
    print("\nThe review judge — its instructions are marked, the turn is not")
    s = settings()
    lines = [{"who": "Marcelo", "text": "@lisa que horas são?", "wa_ts": 1762790400}]
    when = datetime(2026, 11, 10, 12, 1, tzinfo=SP)

    j = Judge(s)
    fake = FakeMessages([_resp(_VERDICT, _Usage(input_tokens=300, output_tokens=40,
                                                cache_read_input_tokens=2700,
                                                cache_creation_input_tokens=0))])
    j._client = SimpleNamespace(messages=fake)
    out = await j.judge(lines=lines, reply_text="São 12:00.", turn_ts=when)
    sent = fake.calls[0]
    check("the instructions go out as one marked block",
          sent["system"] == [{"type": "text", "text": j._system,
                              "cache_control": {"type": "ephemeral"}}])
    check("the turn being graded is not marked",
          "cache_control" not in sent and isinstance(sent["messages"][0]["content"], str))
    check("the judge was not thrown by the cache counts", out.get("error") is None, str(out))
    check("its input count is everything it was sent", out.get("input_tokens") == 3000,
          str(out.get("input_tokens")))

    off = settings(prompt_cache_enabled=False)
    j = Judge(off)
    fake = FakeMessages([_resp(_VERDICT, _Usage(input_tokens=3000, output_tokens=40))])
    j._client = SimpleNamespace(messages=fake)
    out = await j.judge(lines=lines, reply_text=None, turn_ts=when)
    check("off: the instructions are one plain string",
          type(fake.calls[0]["system"]) is str and fake.calls[0]["system"] == j._system)
    check("off: the input count is the API's own", out.get("input_tokens") == 3000)


async def main() -> None:
    prompt_checks()
    await request_checks()
    await graph_checks()
    await judge_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
