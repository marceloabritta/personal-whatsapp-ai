"""The history agent: the tool loop, its tools, its prompt and the media seam.

A scripted Anthropic client (tool_use turns, then the final JSON) drives run_history_agent over
the invented dataset in app/history/fake.py. No network, no Postgres, no key.

    cd "Lisa flow" && python tests/run_history_agent.py
Exits non-zero if any check fails.

Covers: the tool schemas and the strict-schema budget; dates in and out on the owner's clock; tool
input checks; every tool against the fake store; parallel tool calls answered in ONE message;
error results; the tool-call and time bounds and the forced final answer; refusal and provider
errors; pause_turn and max_tokens; notes carried from one turn to the next; files checked and
capped; that on_event and the log never carry a message's contents; the request's shape (model,
thinking, effort, enforced JSON, caching, fallbacks); and EvolutionHistoryMedia over a fake
Evolution and transcription service."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.history import fake  # noqa: E402
from app.history.agent import clean_notes, run_history_agent  # noqa: E402
from app.history.fake import FakeHistoryMedia, FakeHistoryStore, ts_of  # noqa: E402
from app.history.media import EvolutionHistoryMedia, mediatype_for  # noqa: E402
from app.history.prompt import OUTPUT_SCHEMA, system_blocks  # noqa: E402
from app.history.tools import (  # noqa: E402
    TOOLS, ToolBox, ToolError, fmt_ts, parse_when, snippet, zone,
)

logging.getLogger("mary").addHandler(logging.NullHandler())

_checks = {"pass": 0, "fail": 0}
SP = zone("America/Sao_Paulo")
NOW = ts_of("2026-11-16 12:00")
OWNER_JID = "5511900000000@s.whatsapp.net"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def settings(**over) -> Settings:
    base = dict(evolution_apikey="x", anthropic_api_key="", database_url=None, redis_url=None,
                owner_name="Owner", owner_jid=OWNER_JID, calendar_timezone="America/Sao_Paulo",
                history_enabled=True)
    return Settings(**{**base, **over})


# ============================ a scripted Anthropic client ====================================

def blk_tool(tid: str, name: str, args: dict):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=args)


def blk_text(text: str):
    return SimpleNamespace(type="text", text=text)


def usage(i=100, o=40, cr=0, cw=0, iterations=None):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
                           cache_creation_input_tokens=cw, iterations=iterations)


def resp(content, stop="tool_use", u=None, model="claude-opus-5-5", rid="req_test"):
    r = SimpleNamespace(content=list(content), stop_reason=stop, usage=u or usage(), model=model,
                        stop_details=None)
    r._request_id = rid
    return r


def tools_turn(*calls, **kw):
    return resp([blk_tool(f"tu_{i}_{c[0]}", c[0], c[1]) for i, c in enumerate(calls)], **kw)


def answer(message="Found it.", *, state="keep_listening", lang="en", files=(), notes=None,
           stop="end_turn", **kw):
    data = {"reasoning": "test", "state": state, "message": message, "lang": lang,
            "files": list(files),
            "notes": notes if notes is not None else {
                "looking_for": "x", "established": "", "candidates": [], "files": [],
                "searched": [], "open_question": ""}}
    return resp([blk_text(json.dumps(data, ensure_ascii=False))], stop=stop, **kw)


class Sleep:
    def __init__(self, seconds: float, then=None) -> None:
        self.seconds, self.then = seconds, then


class Client:
    """Pops one scripted step per request and records what it was sent."""

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.requests: list[dict] = []
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.beta = SimpleNamespace(messages=endpoint)

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        step = self.steps.pop(0) if self.steps else answer("default")
        if isinstance(step, Sleep):
            await asyncio.sleep(step.seconds)
            step = step.then
        if isinstance(step, BaseException):
            raise step
        return step


async def turn(steps, *, s=None, store=None, media=None, notes=None, convo=None, lang=None):
    store = store or FakeHistoryStore("Owner")
    media = media or FakeHistoryMedia(store)
    client = Client(steps)
    events: list[dict] = []
    t0 = time.monotonic()
    res = await run_history_agent(
        convo=convo or [{"role": "user", "content": "@lisa quem me falou do demo day nos EUA?"}],
        store=store, media=media, settings=s or settings(), notes=notes, session_lang=lang,
        now_ts=NOW, client=client, on_event=events.append)
    return res, client, events, time.monotonic() - t0, store, media


def last_user(req: dict) -> dict:
    return req["messages"][-1]


def results_of(req: dict) -> list[dict]:
    return [b for b in last_user(req)["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


# ============================ schemas =======================================================

def _optionals(schema) -> int:
    n = 0
    if isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            req = set(schema.get("required", []))
            n += sum(1 for k in schema["properties"] if k not in req)
        n += sum(_optionals(v) for v in schema.values())
    elif isinstance(schema, list):
        n += sum(_optionals(v) for v in schema)
    return n


def _unions(schema) -> int:
    n = 0
    if isinstance(schema, dict):
        if "anyOf" in schema or isinstance(schema.get("type"), list):
            n += 1
        n += sum(_unions(v) for v in schema.values())
    elif isinstance(schema, list):
        n += sum(_unions(v) for v in schema)
    return n


def _closed(schema) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            if schema.get("additionalProperties") is not False:
                return False
            if not set(schema.get("required", [])) <= set(schema.get("properties", {})):
                return False
        return all(_closed(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_closed(v) for v in schema)
    return True


async def p1_schemas():
    print("p1 — tool schemas and the strict budget")
    check("seven tools, all strict", len(TOOLS) == 7 and all(t.get("strict") is True for t in TOOLS))
    check("every object closed, required within properties",
          all(_closed(t["input_schema"]) for t in TOOLS) and _closed(OUTPUT_SCHEMA))
    opt = sum(_optionals(t["input_schema"]) for t in TOOLS) + _optionals(OUTPUT_SCHEMA)
    uni = sum(_unions(t["input_schema"]) for t in TOOLS) + _unions(OUTPUT_SCHEMA)
    check("optional parameters across the request within 24", opt <= 24, f"{opt}")
    check("union parameters across the request within 16", uni <= 16, f"{uni}")
    check("answer schema: everything required, only message nullable",
          set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"]) and
          _unions(OUTPUT_SCHEMA) == 1)
    box = ToolBox(FakeHistoryStore(), FakeHistoryMedia(), tz=SP, owner_name="Owner")
    check("every declared tool has a handler", all(t["name"] in box._handlers for t in TOOLS))


# ============================ dates =========================================================

async def p2_dates():
    print("p2 — dates on the owner's clock")
    check("a date is the start of that day in São Paulo",
          parse_when("2026-10-20", SP, end=False) == ts_of("2026-10-20 00:00"))
    check("as an end, the first second of the next day",
          parse_when("2026-10-20", SP, end=True) == ts_of("2026-10-21 00:00"))
    check("a date and time, to the minute",
          parse_when("2026-10-20T14:30", SP, end=False) == ts_of("2026-10-20 14:30") and
          parse_when("2026-10-20T14:30", SP, end=True) == ts_of("2026-10-20 14:31"))
    for bad in ("2026-02-30", "20/10/2026", "", "yesterday"):
        try:
            parse_when(bad, SP, end=False)
            ok = False
        except ToolError as e:
            ok = e.code == "invalid_input"
        check(f"{bad!r} is refused", ok)
    from datetime import datetime, timezone
    midnight_utc = int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp())
    check("timestamps are shown on the owner's clock (UTC midnight is 21:00 the day before)",
          fmt_ts(midnight_utc, SP) == "Thu 2026-10-22 21:00", fmt_ts(midnight_utc, SP))

    store = FakeHistoryStore()
    box = ToolBox(store, FakeHistoryMedia(store), tz=SP, owner_name="Owner")
    out = await box.run("search_messages", {"terms": ["kestrel"], "match": "all",
                                            "after": "2026-10-01", "before": "2026-10-31"})
    sent = store.calls[-1][1]
    check("search gets the range as unix seconds, both days whole",
          sent["after"] == ts_of("2026-10-01 00:00") and sent["before"] == ts_of("2026-11-01 00:00"))
    check("and finds only October", out.n == 2 and "2026-10-20" in out.content
          and "2026-11-12" not in out.content, out.content[:200])
    out = await box.run("search_messages", {"terms": ["x"], "match": "all",
                                            "after": "2026-10-31", "before": "2026-10-01"})
    check("after past before is refused", out.error == "invalid_input")
    out = await box.run("search_messages", {"terms": ["demo day"], "match": "all"})
    check("result lines carry weekday, date and time",
          "Fri 2026-09-18 21:40" in out.content, out.content[:200])


# ============================ tool inputs ===================================================

async def p3_inputs():
    print("p3 — tool input checks")
    store = FakeHistoryStore()
    media = FakeHistoryMedia(store)
    box = ToolBox(store, media, tz=SP, owner_name="Owner")
    cases = [
        ("unknown parameter", "search_messages", {"terms": ["a"], "match": "all", "foo": 1}, "invalid_input"),
        ("terms not a list", "search_messages", {"terms": "kestrel", "match": "all"}, "invalid_input"),
        ("empty terms", "search_messages", {"terms": ["  "], "match": "all"}, "invalid_input"),
        ("too many terms", "search_messages", {"terms": [f"t{i}" for i in range(13)], "match": "any"}, "invalid_input"),
        ("bad match", "search_messages", {"terms": ["a"], "match": "some"}, "invalid_input"),
        ("bad regex", "search_messages", {"terms": ["(unclosed"], "match": "all", "regex": True}, "invalid_input"),
        ("bad kind", "search_messages", {"terms": ["a"], "match": "all", "kinds": ["pdf"]}, "invalid_input"),
        ("bad from_me", "search_messages", {"terms": ["a"], "match": "all", "from_me": "yes"}, "invalid_input"),
        ("one topic", "topics_together", {"topics": [["kestrel"]]}, "invalid_input"),
        ("six topics", "topics_together", {"topics": [["a"]] * 6}, "invalid_input"),
        ("unknown tool", "delete_everything", {}, "unknown_tool"),
        ("input not an object", "get_message", "3EB0BRU00001", "invalid_input"),
        ("missing msg_id", "get_message", {}, "invalid_input"),
        ("unknown message", "get_message", {"msg_id": "NOPE"}, "not_found"),
        ("unknown chat key", "read_chat", {"chat": "5511000000000@s.whatsapp.net"}, "unknown_chat"),
        ("message not in that chat", "read_chat",
         {"chat": "5511999990001@s.whatsapp.net", "around_msg_id": "3EB0CAR00001"}, "not_found"),
    ]
    for label, name, args, code in cases:
        out = await box.run(name, args)
        check(f"{label} → {code}", out.error == code, f"{out.error}: {out.content}")

    out = await box.run("read_chat", {"chat": "Bruno"})
    check("a name for a chat → the key it could mean",
          out.error == "unknown_chat" and "5511999990001@s.whatsapp.net" in out.content, out.content)
    out = await box.run("read_chat", {"chat": "zzzz qqqq"})
    check("a name that matches nothing → how to find a key",
          out.error == "unknown_chat" and "list_chats" in out.content)
    out = await box.run("search_messages", {"terms": ["kestrel"], "match": "all",
                                            "chat": "+55 11 99999-0001", "limit": 500})
    sent = store.calls[-1][1]
    check("a phone number becomes its chat key",
          sent["chat"] == "5511999990001@s.whatsapp.net" and out.n == 2, str(sent))
    check("limit 500 reaches the store as 50", sent["limit"] == 50, str(sent))
    await box.run("search_messages", {"terms": ["a"], "match": "any"})
    check("no limit → 30", store.calls[-1][1]["limit"] == 30)


# ============================ each tool =====================================================

async def p4_tools():
    print("p4 — every tool over the fake store")
    store = FakeHistoryStore()
    media = FakeHistoryMedia(store)
    box = ToolBox(store, media, tz=SP, owner_name="Owner", max_transcriptions=2, max_open_files=2)

    out = await box.run("search_messages", {"terms": ["kestrel"], "match": "all"})
    check("search: total, best hits, and the chats with most", out.n == 9 and
          "By chat: Diego Ramos (5521999990003@s.whatsapp.net) ×4" in out.content, out.content[:300])
    out = await box.run("search_messages", {"terms": ["KÉSTREL", "acelerad"], "match": "all"})
    check("case/accent-insensitive, terms inside words, all in one message",
          out.n == 0 and out.content.startswith("No matches"))
    out = await box.run("search_messages", {"terms": ["Kestrel", "aceleradora"], "match": "any"})
    check("match any", out.n == 11, str(out.n))
    out = await box.run("search_messages", {"terms": ["anotado"], "match": "all"})
    check("Lisa's own messages are left out", out.n == 0)
    out = await box.run("search_messages", {"terms": ["anotado"], "match": "all", "include_lisa": True})
    check("unless asked for", out.n == 1 and "Lisa (for Owner)" in out.content, out.content)
    out = await box.run("search_messages", {"terms": ["pitch"], "match": "all", "kinds": ["document"]})
    check("file names are searched; kind filter", out.n == 1 and "Kestrel_pitch_v3.pdf" in out.content)

    out = await box.run("topics_together", {"topics": [["kestrel"], ["eua", "usa", "estados unidos", "acelerad", "yc"]]})
    check("topics_together: chats where the clues meet, tightest first",
          out.n == 2 and "Bruno Teixeira (5511999990001@s.whatsapp.net)" in out.content, out.content)
    out = await box.run("topics_together", {"topics": [["kestrel"], ["demo day"]]})
    check("no chat has Kestrel and 'demo day' in text", out.n == 0)

    out = await box.run("list_chats", {"name": "familia"})
    check("list_chats: accent-insensitive name", out.n == 1 and "120363000000000002@g.us" in out.content)
    out = await box.run("list_chats", {"kind": "group"})
    check("list_chats: by kind", out.n == 2)
    out = await box.run("list_chats", {"name": "diego"})
    check("list_chats: a first name shared by two people lists both", out.n == 2 and
          "Diego Ramos" in out.content and "Diego Martins" in out.content)

    out = await box.run("read_chat", {"chat": "5511999990001@s.whatsapp.net",
                                      "around_msg_id": "3EB0BRU00005", "limit": 6})
    check("read_chat around a message, in order, with the voice notes marked",
          out.n == 6 and out.content.index("3EB0BRU00004") < out.content.index("3EB0BRU00005")
          and "[voice 1:24: transcript pending]" in out.content, out.content)
    check("and says where more is", "earlier messages exist" in out.content and
          "later messages exist" in out.content)

    out = await box.run("transcribe_voice_note", {"msg_id": "3EB0DIE00004"})
    check("an already transcribed note costs nothing", out.n == 1 and not media.calls and
          "Already transcribed" in out.content)
    out = await box.run("transcribe_voice_note", {"msg_id": "3EB0BRU00005"})
    check("a pending note is transcribed through the media seam",
          out.n == 1 and "demo day da YC" in out.content and media.calls == [("transcribe", "3EB0BRU00005")])
    out = await box.run("transcribe_voice_note", {"msg_id": "3EB0GAB00001"})
    check("an expired note is refused without a call", out.error == "expired" and len(media.calls) == 1)
    out = await box.run("transcribe_voice_note", {"msg_id": "3EB0BRU00003"})
    check("a text message is not a voice note", out.error == "not_voice")
    await box.run("transcribe_voice_note", {"msg_id": "3EB0BRU00006"})
    out = await box.run("transcribe_voice_note", {"msg_id": "3EB0BRU00005"})
    check("the per-turn transcription cap holds", out.error == "limit" and box.transcribed == 2)

    out = await box.run("open_file", {"msg_id": "3EB0BRU00009"})
    doc = out.content[1] if isinstance(out.content, list) else {}
    check("open_file: a PDF comes back as a document block",
          doc.get("type") == "document" and doc["source"]["media_type"] == "application/pdf"
          and doc.get("title") == "Kestrel_pitch_v3.pdf" and "1 page" in out.content[0]["text"])
    out = await box.run("open_file", {"msg_id": "3EB0FEL00001"})
    check("a .docx cannot be read, but can be sent", out.error == "unsupported" and "sent" in out.content)
    out = await box.run("open_file", {"msg_id": "3EB0DIE00005"})
    check("the per-turn open cap holds", out.error == "limit")
    box2 = ToolBox(store, media, tz=SP, owner_name="Owner")
    check("an expired file says so", (await box2.run("open_file", {"msg_id": "3EB0DIE00005"})).error == "expired")
    check("a voice note is not opened", (await box2.run("open_file", {"msg_id": "3EB0BRU00005"})).error == "not_file")

    png = fake.make_png()
    fake.FILES["3EB0IMGTEST1"] = ("image/png", lambda: png)
    store2 = FakeHistoryStore(rows=fake._M + [("3EB0IMGTEST1", "carla", "2026-11-10 10:00", None,
                                              "image", "", {"mimetype": "image/png"})])
    box3 = ToolBox(store2, FakeHistoryMedia(store2), tz=SP, owner_name="Owner")
    out = await box3.run("open_file", {"msg_id": "3EB0IMGTEST1"})
    check("an image comes back as an image block",
          isinstance(out.content, list) and out.content[1]["type"] == "image"
          and out.content[1]["source"]["media_type"] == "image/png")
    fake.FILES.pop("3EB0IMGTEST1")

    out = await box.run("get_message", {"msg_id": "3EB0BRU00009"})
    check("get_message: the whole message and its file",
          "file: Kestrel_pitch_v3.pdf (application/pdf)" in out.content)
    long = "palavra " * 200 + "Kestrel no meio " + "fim " * 200
    s = snippet(long, ["kestrel"], 120)
    check("a long text is cut around the term", "Kestrel" in s and len(s) <= 122 and s.startswith("…"))


# ============================ the loop ======================================================

async def p5_parallel():
    print("p5 — parallel tool calls, one result message, the request's shape")
    store = FakeHistoryStore(delay=0.3)
    steps = [
        tools_turn(("search_messages", {"terms": ["kestrel"], "match": "all"}),
                   ("list_chats", {"name": "bruno"}),
                   ("read_chat", {"chat": "5511999990001@s.whatsapp.net", "limit": 10}),
                   u=usage(i=50, o=30, cw=4000)),
        answer("It was *Bruno Teixeira*.", u=usage(i=20, o=60, cr=4000, cw=900), rid="req_last"),
    ]
    res, client, events, elapsed, *_ = await turn(steps, store=store)
    req1, req2 = client.requests
    msgs = req2["messages"]
    check("two requests", len(client.requests) == 2)
    check("assistant turn echoed, then ONE user message with every result",
          [m["role"] for m in msgs] == ["user", "assistant", "user"] and len(results_of(req2)) == 3)
    check("results answer their calls, in order",
          [b["tool_use_id"] for b in results_of(req2)] == ["tu_0_search_messages", "tu_1_list_chats",
                                                           "tu_2_read_chat"])
    check("the three ran at once", elapsed < 0.8, f"{elapsed:.2f}s")
    check("the answer", res["message"] == "It was *Bruno Teixeira*." and res["state"] == "keep_listening"
          and res["stop_reason"] == "end_turn" and res["error_category"] == "none")
    check("usage summed over hops, cache counted",
          res["usage"] == {"input": 50 + 4000 + 20 + 4000 + 900, "output": 90, "cache_read": 4000,
                           "cache_write": 4900}, str(res["usage"]))
    check("model and request id", res["model"] == "claude-opus-5-5" and res["provider_request_id"] == "req_last")
    check("one event per call", len(events) == 3 and [e["tool"] for e in res["tool_calls"]] ==
          [e["tool"] for e in events])
    check("model and limits", req1["model"] == "claude-opus-5-5" and req1["max_tokens"] >= 8000)
    check("adaptive thinking, effort from settings",
          req1["thinking"] == {"type": "adaptive"} and req1["output_config"]["effort"] == "medium")
    check("enforced JSON on every hop", req1["output_config"]["format"] == {
        "type": "json_schema", "schema": OUTPUT_SCHEMA} and req2["output_config"] == req1["output_config"])
    check("strict tools, tool_choice left to the model",
          [t["name"] for t in req1["tools"]] == [t["name"] for t in TOOLS] and "tool_choice" not in req1)
    check("server-side refusal fallback opted in",
          req1.get("betas") == ["server-side-fallback-2026-07-01"] and req1.get("fallbacks") == "default")
    check("top-level cache mark; stable system block marked, turn block not",
          req1.get("cache_control") == {"type": "ephemeral"} and
          req1["system"][0].get("cache_control") == {"type": "ephemeral"} and
          "cache_control" not in req1["system"][1])
    check("the turn block holds the clock and the coverage",
          "Now: Monday, 2026-11-16 12:00 (America/Sao_Paulo)" in req1["system"][1]["text"] and
          "144 messages in 11 chats" in req1["system"][1]["text"], req1["system"][1]["text"][:300])
    check("the system prompt is the same on every hop", req1["system"] == req2["system"])

    res2, client2, *_ = await turn([answer("ok")], s=settings(prompt_cache_enabled=False))
    r = client2.requests[0]
    check("cache switch off: no marks", "cache_control" not in r and
          "cache_control" not in r["system"][0])


async def p6_errors():
    print("p6 — failed tools come back as errors, never dropped")

    class Boom(FakeHistoryStore):
        async def list_chats(self, **kw):
            raise RuntimeError("SECRET-CONTENT should not travel")

    steps = [tools_turn(("search_messages", {"terms": [], "match": "all"}),
                        ("no_such_tool", {}),
                        ("list_chats", {"name": "x"}),
                        ("get_message", {"msg_id": "3EB0BRU00001"})),
             answer("ok")]
    res, client, events, *_ = await turn(steps, store=Boom())
    rs = results_of(client.requests[1])
    check("four results for four calls", len(rs) == 4)
    check("bad input, unknown tool and a crash are is_error",
          [bool(r.get("is_error")) for r in rs] == [True, True, True, False])
    check("a crash names its class, not its message",
          "RuntimeError" in rs[2]["content"] and "SECRET" not in rs[2]["content"])
    check("events carry the error codes",
          [e["error"] for e in events] == ["invalid_input", "unknown_tool", "failed", None])
    check("the turn still answers", res["message"] == "ok")


async def p7_call_bound():
    print("p7 — the tool-call bound and the forced final answer")
    s = settings(history_max_tool_calls=3)
    steps = [tools_turn(("search_messages", {"terms": ["kestrel"], "match": "all"}),
                        ("list_chats", {})),
             tools_turn(("get_message", {"msg_id": "3EB0BRU00001"}),
                        ("get_message", {"msg_id": "3EB0BRU00002"})),
             answer("Partial: probably Bruno. I stopped before finishing; want me to keep looking?")]
    res, client, events, *_ = await turn(steps, s=s)
    final = client.requests[-1]
    rs = results_of(final)
    check("three requests: two hops and the final", len(client.requests) == 3)
    check("the call past the bound is not run, and says so",
          len(rs) == 2 and not rs[0].get("is_error") and rs[1].get("is_error")
          and "limit of 3" in rs[1]["content"])
    check("the final request disables tools but keeps them declared",
          final.get("tool_choice") == {"type": "none"} and len(final["tools"]) == len(TOOLS))
    note = last_user(final)["content"][-1]
    check("and tells the model it was cut short, after the results",
          note.get("type") == "text" and "cut short" in note["text"] and "3 tool calls" in note["text"])
    check("everything the previous request sent is sent again unchanged",
          final["messages"][:3] == client.requests[1]["messages"])
    check("the note is appended after the tool results, not inserted before them",
          last_user(final)["content"][:-1] == rs and len(final["messages"]) == 5)
    check("stop_reason budget, with an answer", res["stop_reason"] == "budget" and
          res["message"].startswith("Partial") and res["error_category"] == "none")
    check("an answer cut short that offers more holds the window", res["state"] == "awaiting_answer")
    check("every call has an event, the skipped one marked budget",
          len(events) == 4 and events[-1]["error"] == "budget" and events[-1]["ms"] == 0)


async def p8_time_bound():
    print("p8 — the time bound")
    s = settings(history_max_seconds=2)
    steps = [tools_turn(("list_chats", {})),
             Sleep(10, tools_turn(("list_chats", {}))),
             answer("I ran out of time; want me to keep looking?")]
    res, client, events, elapsed, *_ = await turn(steps, s=s)
    check("a hop past the clock is stopped", elapsed < 3.0, f"{elapsed:.2f}s")
    final = client.requests[-1]
    check("then one final request, tools off, saying the time ran out",
          len(client.requests) == 3 and final.get("tool_choice") == {"type": "none"}
          and "time limit of 2 seconds" in last_user(final)["content"][-1]["text"])
    check("stop_reason budget", res["stop_reason"] == "budget" and res["message"])

    slow = FakeHistoryStore(delay=10)
    res, client, events, elapsed, *_ = await turn(
        [tools_turn(("list_chats", {})), answer("cut")], s=s, store=slow)
    check("a tool past the clock is stopped and reported", elapsed < 3.0 and
          events and events[0]["error"] == "timeout" and results_of(client.requests[-1])[0].get("is_error"),
          f"{elapsed:.2f}s {events}")

    res, client, events, elapsed, *_ = await turn(
        [tools_turn(("list_chats", {})), Sleep(10, None), Sleep(10, answer("late"))], s=s)
    check("a final answer that cannot come in time → a plain line and an offer, not silence",
          res["message"] and "Quer que eu continue" in res["message"] and
          res["state"] == "awaiting_answer" and res["error_category"] == "budget" and
          res["stop_reason"] == "budget" and elapsed < 4.0,
          f"{res['message']} {res['error_category']} {elapsed:.2f}s")
    res, *_ = await turn([tools_turn(("search_messages", {"terms": ["kestrel"], "match": "all"})),
                          Sleep(10, None), Sleep(10, None)], s=s,
                         convo=[{"role": "user", "content": "who sent me the deck?"}])
    check("in his language, with what was searched kept for the next turn",
          res["lang"] == "en" and "keep looking" in res["message"] and
          res["notes"]["searched"] == ["kestrel"], str(res["notes"]))


async def p9_stops():
    print("p9 — refusal, provider errors, pause_turn, max_tokens")
    notes_in = {"looking_for": "o deck", "established": "", "candidates": [], "files": [],
                "searched": [], "open_question": ""}
    res, *_ = await turn([resp([], stop="refusal")], notes=notes_in)
    check("a refusal: no message, no error, notes kept",
          res["message"] is None and res["stop_reason"] == "refusal" and
          res["error_category"] == "none" and res["notes"] == notes_in)

    res, *_ = await turn([RuntimeError("upstream exploded")])
    check("a provider error never raises: error provider, no message",
          res["message"] is None and res["error_category"] == "provider" and res["stop_reason"] == "error")

    res, *_ = await turn([tools_turn(("list_chats", {})), RuntimeError("overloaded")])
    check("an error mid-loop keeps the trace", res["error_category"] == "provider"
          and len(res["tool_calls"]) == 1)

    res, client, *_ = await turn([resp([blk_text("{not json")], stop="end_turn"), answer("second try")])
    check("an unparsable answer gets one forced retry",
          res["message"] == "second try" and client.requests[-1].get("tool_choice") == {"type": "none"})

    res, *_ = await turn([resp([blk_text("{not json")], stop="end_turn"),
                          resp([blk_text("still not json")], stop="end_turn")])
    check("and then gives up as a provider error", res["error_category"] == "provider" and res["message"] is None)

    res, client, *_ = await turn([resp([SimpleNamespace(type="server_tool_use", id="s1", name="x", input={})],
                                       stop="pause_turn"), answer("after pause")])
    check("pause_turn continues the same turn",
          res["message"] == "after pause" and client.requests[1]["messages"][-1]["role"] == "assistant")

    res, client, *_ = await turn([tools_turn(("list_chats", {})),
                                  resp([blk_tool("tu_x", "list_chats", {})], stop="max_tokens"),
                                  answer("best so far")])
    final = client.requests[-1]
    check("a cut-off hop is dropped and the answer is forced",
          res["message"] == "best so far" and final.get("tool_choice") == {"type": "none"} and
          final["messages"][-1]["role"] == "user" and len(final["messages"]) == 3)

    res, client, *_ = await turn([answer("x")], convo=[{"role": "assistant", "content": "hi"}])
    check("no question to answer → no call", res["stop_reason"] == "no_question" and not client.requests)

    fb = resp([SimpleNamespace(type="fallback", **{"from": {"model": "claude-opus-5-5"}},
                               to={"model": "claude-opus-4-8"}), blk_text(json.dumps({
                                   "reasoning": "r", "state": "close", "message": "via fallback",
                                   "lang": "en", "files": [], "notes": {}}))],
              stop="end_turn", model="claude-opus-4-8",
              u=usage(i=10, o=5, iterations=[usage(i=100, o=0), usage(i=10, o=5)]))
    res, *_ = await turn([fb])
    check("a fallback-served answer: its model, every attempt's tokens",
          res["message"] == "via fallback" and res["model"] == "claude-opus-4-8"
          and res["usage"]["input"] == 110 and res["state"] == "close")


async def p10_notes():
    print("p10 — notes from one turn to the next")
    notes = {"looking_for": "who talked about a demo day in the US",
             "established": "Bruno Teixeira, in a voice note on 2026-10-20",
             "candidates": [{"chat": "5511999990001@s.whatsapp.net", "name": "Bruno Teixeira",
                             "msg_ids": ["3EB0BRU00005"], "why": "voice note: demo day da YC"}],
             "files": [{"msg_id": "3EB0BRU00009", "name": "Kestrel_pitch_v3.pdf",
                        "chat": "5511999990001@s.whatsapp.net"}],
             "searched": ["kestrel", "demo day"], "open_question": ""}
    res1, *_ = await turn([answer("It was Bruno.", notes=notes)])
    check("the turn returns the notes the model wrote", res1["notes"] == notes)
    res2, client, *_ = await turn([answer("Sending it.", files=["3EB0BRU00009"])],
                                  notes=res1["notes"],
                                  convo=[{"role": "user", "content": "@lisa who was it?"},
                                         {"role": "assistant", "content": "It was Bruno."},
                                         {"role": "user", "content": "send me that PDF"}])
    turn_text = client.requests[0]["system"][1]["text"]
    check("the next turn sees them, ids and all",
          "3EB0BRU00009" in turn_text and "Kestrel_pitch_v3.pdf" in turn_text and
          "previous turn" in turn_text)
    check("and can resolve 'that PDF' without a search",
          res2["files"] == ["3EB0BRU00009"] and not res2["tool_calls"])
    check("the conversation is passed in order",
          [m["role"] for m in client.requests[0]["messages"]] == ["user", "assistant", "user"])
    big = {"looking_for": "x" * 1000, "candidates": [{"chat": "c", "name": "n", "msg_ids": ["a"] * 20,
                                                       "why": "w"}] * 10, "files": "nope",
           "searched": ["s"] * 40, "junk": 1}
    c = clean_notes(big)
    check("notes are cut to size and shape",
          len(c["looking_for"]) == 300 and len(c["candidates"]) == 6 and
          len(c["candidates"][0]["msg_ids"]) == 8 and c["files"] == [] and len(c["searched"]) == 15
          and "junk" not in c)
    check("garbage notes become empty", clean_notes("x") == {})
    s1 = system_blocks(owner="Owner", max_files=3, tz_name="America/Sao_Paulo", now_ts=NOW,
                       coverage=None, notes=None, session_lang=None, cache=True)
    s2 = system_blocks(owner="Owner", max_files=3, tz_name="America/Sao_Paulo", now_ts=NOW + 7200,
                       coverage={"n_messages": 5}, notes=notes, session_lang="pt", cache=True)
    check("the stable prompt does not change with the clock, coverage, notes or language",
          s1[0] == s2[0] and s1[1] != s2[1])
    check("a locked language is stated", '"pt"' in s2[1]["text"])


async def p11_files():
    print("p11 — files: real file messages only, capped")
    res, *_ = await turn([answer("here", files=["3EB0BRU00009", "3EB0BRU00003", "NOPE",
                                                "3EB0BRU00009", "3EB0BRU00005", "3EB0CAR00004",
                                                "3EB0FEL00001"])])
    check("text and unknown ids dropped, duplicates once, at most 3",
          res["files"] == ["3EB0BRU00009", "3EB0BRU00005", "3EB0CAR00004"], str(res["files"]))
    res, *_ = await turn([answer("here", files=["3EB0BRU00009"])], s=settings(history_max_files=0))
    check("max_files 0 sends nothing", res["files"] == [])
    res, *_ = await turn([answer("here", files="3EB0BRU00009")])
    check("files that are not a list are ignored", res["files"] == [])


async def p12_privacy():
    print("p12 — on_event and the log never carry a message's contents")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger("mary")
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        steps = [tools_turn(("search_messages", {"terms": ["kestrel"], "match": "all"}),
                            ("read_chat", {"chat": "5511999990001@s.whatsapp.net"}),
                            ("transcribe_voice_note", {"msg_id": "3EB0BRU00005"}),
                            ("open_file", {"msg_id": "3EB0BRU00009"}),
                            ("topics_together", {"topics": [["kestrel"], ["acelerad"]]})),
                 answer("ok")]
        res, client, events, *_ = await turn(steps)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    contents = [r[5] for r in fake._M if len(r[5]) >= 12] + list(fake.PENDING_TRANSCRIPTS.values())
    dumped = json.dumps(events, ensure_ascii=False) + json.dumps(res["tool_calls"], ensure_ascii=False)
    check("events have exactly tool, args, n, ms, error",
          all(set(e) == {"tool", "args", "n", "ms", "error"} for e in events) and len(events) == 5)
    check("no message text, transcript or file content in the events",
          not any(c in dumped for c in contents) and "JVBERi" not in dumped)
    logged = buf.getvalue()
    check("nor in the log", logged and not any(c in logged for c in contents), logged[:200])
    check("but the model did get them", any("demo day da YC" in json.dumps(r.get("content"), ensure_ascii=False)
                                            for r in results_of(client.requests[1])))


async def p13_convo():
    print("p13 — the conversation as Anthropic messages")
    convo = [{"role": "assistant", "content": "stale"},
             {"role": "user", "content": "first"},
             {"role": "user", "content": [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}},
                                          {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                       "data": "AAAA"}}]},
             {"role": "assistant", "content": ""},
             {"role": "assistant", "content": "reply"},
             {"role": "user", "content": "third"}]
    res, client, *_ = await turn([answer("ok")], convo=convo)
    m = client.requests[0]["messages"]
    check("leading assistant dropped, same-role turns merged",
          [x["role"] for x in m] == ["user", "assistant", "user"] and len(m[0]["content"]) == 2)
    check("the chat's images and PDFs stay out of the search; stray cache marks are stripped",
          all(b["type"] == "text" for x in m for b in x["content"])
          and all("cache_control" not in b for b in m[0]["content"]))

    # Lisa's thread with media on: the turn is the labelled transcript plus the newest photos. The
    # search gets the transcript (which marks each photo) and none of the photos, on every hop.
    photo = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "B" * 4000}}
    pdf = {"type": "document", "title": "x.pdf",
           "source": {"type": "base64", "media_type": "application/pdf", "data": "C" * 4000}}
    convo = [{"role": "user", "content": [{"type": "text", "text": "Marcelo: [image]\nMarcelo: [PDF: x.pdf]\n"
                                                                    "Marcelo: @lisa quem falou de Kestrel?"},
                                          photo, photo, pdf]}]
    res, client, *_ = await turn([tools_turn(("t1", "search_messages", {"terms": ["kestrel"]})),
                                  answer("ok")], convo=convo)
    sent = [b for req in client.requests for x in req["messages"] if x["role"] == "user"
            for b in (x["content"] if isinstance(x["content"], list) else [])
            if b.get("type") in ("image", "document")]
    check("no photo or PDF from the chat on any hop", len(client.requests) == 2 and not sent,
          f"{len(client.requests)} requests, {len(sent)} media blocks")
    q = client.requests[0]["messages"][0]["content"]
    check("the transcript's markers are still there", "[PDF: x.pdf]" in q[0]["text"] and "Kestrel" in q[0]["text"])
    only_media = [{"role": "user", "content": [photo]}, {"role": "user", "content": "@lisa e isso?"}]
    res, client, *_ = await turn([answer("ok")], convo=only_media)
    check("a turn that was only a photo is skipped, the question still goes",
          client.requests and client.requests[0]["messages"][-1]["content"][-1]["text"] == "@lisa e isso?")


# ============================ the media seam ================================================

class FakeEvolution:
    def __init__(self, records: dict, media: dict) -> None:
        self.records, self.media = records, media
        self.sent: list[dict] = []
        self.fail_send = False

    async def _find_messages(self, where):
        mid = where["key"]["id"]
        rec = self.records.get(mid)
        return [rec] if rec else []

    async def get_media_base64(self, message_id, *, convert_to_mp4=False):
        return self.media.get(message_id)

    async def send_media_id(self, number, *, mediatype, mimetype, media_b64, filename, caption=""):
        if self.fail_send:
            return None
        self.sent.append(dict(number=number, mediatype=mediatype, mimetype=mimetype,
                              filename=filename, caption=caption, size=len(media_b64)))
        return f"SENT{len(self.sent)}"


class FakeTranscription:
    def __init__(self, results: dict) -> None:
        self.results = results

    async def get(self, wa_id, *, language=None):
        return self.results.get(wa_id, {"text": "", "error": "download"})


def _rec(mid, message, age_days):
    return {"key": {"id": mid}, "message": message,
            "messageTimestamp": int(time.time() - age_days * 86400)}


async def p14_media():
    print("p14 — EvolutionHistoryMedia")
    import base64 as b64m

    pdf = b64m.b64encode(fake.make_pdf(["x"])).decode()
    records = {
        "DOC1": _rec("DOC1", {"documentMessage": {"mimetype": "application/pdf", "fileName": "Deck v3.pdf"}}, 2),
        "IMG1": _rec("IMG1", {"imageMessage": {"mimetype": "image/jpeg"}}, 1),
        "AUD1": _rec("AUD1", {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}}, 1),
        "VID1": _rec("VID1", {"videoMessage": {"mimetype": "video/mp4"}}, 1),
        "TXT1": _rec("TXT1", {"conversation": "oi"}, 1),
        "OLD1": _rec("OLD1", {"documentMessage": {"mimetype": "application/pdf", "fileName": "old.pdf"}}, 40),
        "NEW1": _rec("NEW1", {"documentMessage": {"mimetype": "application/pdf", "fileName": "new.pdf"}}, 1),
        "AUDOLD": _rec("AUDOLD", {"audioMessage": {}}, 40),
    }
    media = {
        "DOC1": {"base64": pdf, "mimetype": "audio/ogg"},   # the client's default when none came back
        "IMG1": {"base64": "AAAA", "mimetype": "image/jpeg"},
        "AUD1": {"base64": "AAAA", "mimetype": "audio/ogg; codecs=opus"},
        "VID1": {"base64": "AAAA", "mimetype": "video/mp4"},
        "BIG1": {"base64": "A" * 4000, "mimetype": "application/pdf"},
    }
    evo = FakeEvolution(records, media)
    tr = FakeTranscription({"AUD1": {"text": "olá mundo", "error": None},
                            "EMPTY": {"text": "", "error": "empty"}})
    m = EvolutionHistoryMedia(evo, tr, settings())

    check("transcribe: ok", await m.transcribe("AUD1") == {"ok": True, "text": "olá mundo", "error": None})
    check("transcribe: silent audio", (await m.transcribe("EMPTY"))["error"] == "empty")
    check("transcribe: an old note that will not download is expired",
          (await m.transcribe("AUDOLD"))["error"] == "expired")
    check("transcribe: an unknown one failed", (await m.transcribe("NEW1"))["error"] == "failed")

    f = await m.fetch("DOC1")
    check("fetch: the declared mimetype beats the client's audio default, and the file name is kept",
          f["ok"] and f["mimetype"] == "application/pdf" and f["file_name"] == "Deck v3.pdf")
    check("fetch: a text message is not media", (await m.fetch("TXT1"))["error"] == "not_media")
    check("fetch: too large", (await m.fetch("BIG1", max_bytes=1000))["error"] == "too_large")
    check("fetch: an old file that will not download is expired",
          (await m.fetch("OLD1"))["error"] == "expired")
    check("fetch: a recent one failed", (await m.fetch("NEW1"))["error"] == "failed")
    f = await m.fetch("IMG1")
    check("fetch: a name is made up when WhatsApp has none",
          f["file_name"].startswith("whatsapp-IMG1") and f["file_name"].endswith(".jpg"), f["file_name"])

    r = await m.send_to_owner("DOC1", caption="aqui")
    sent = evo.sent[-1]
    check("send: to the owner's own chat, as a bare number",
          r["ok"] and sent["number"] == "5511900000000" and r["sent_id"] == "SENT1")
    check("send: a PDF goes as a document with its name",
          sent["mediatype"] == "document" and sent["filename"] == "Deck v3.pdf" and sent["caption"] == "aqui")
    await m.send_to_owner("IMG1")
    await m.send_to_owner("AUD1")
    await m.send_to_owner("VID1")
    check("send: photo as image, voice as audio, video as video",
          [x["mediatype"] for x in evo.sent[1:]] == ["image", "audio", "video"])
    check("send: an expired file is not sent", (await m.send_to_owner("OLD1"))["error"] == "expired")
    evo.fail_send = True
    check("send: Evolution failing → failed", (await m.send_to_owner("IMG1"))["error"] == "failed")
    m2 = EvolutionHistoryMedia(evo, tr, settings(owner_jid=""))
    check("send: no owner configured → nothing sent", (await m2.send_to_owner("IMG1"))["error"] == "failed")
    import inspect
    check("send_to_owner takes no destination",
          list(inspect.signature(EvolutionHistoryMedia.send_to_owner).parameters) == ["self", "msg_id", "caption"])
    check("mediatype table", [mediatype_for(x) for x in (
        "image/jpeg", "image/webp", "video/mp4", "video/quicktime", "audio/mpeg", "application/zip", "")]
          == ["image", "document", "video", "document", "audio", "document", "document"])
    m3 = EvolutionHistoryMedia(evo, None, settings())
    check("no transcription service → failed, no raise", (await m3.transcribe("AUD1"))["error"] == "failed")


async def main() -> int:
    for part in (p1_schemas, p2_dates, p3_inputs, p4_tools, p5_parallel, p6_errors,
                 p7_call_bound, p8_time_bound, p9_stops, p10_notes, p11_files, p12_privacy,
                 p13_convo, p14_media):
        await part()
        print()
    print(f"{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
