"""Live check — does Anthropic really read the prompt back from its cache?

run_prompt_cache.py proves the requests are marked where the prompt repeats. Only Anthropic can
show that a marked prefix is long enough to be stored, that the request is accepted at all with
each skill's tools, thinking and enforced JSON, and that the second call is billed as a read. A
prompt that is never read back raises no error, so this is the only place it shows.

For every skill that is on, three calls straight to the reasoner, the way one activation makes
them (nothing goes through the graph, WhatsApp or Google, and nothing is sent to anyone):

  1. the first pass after a tag: language not locked yet.
  2. a later pass, a minute on, language locked, another address-book block. Only the skill's
     own instructions repeat, so those must be read back.
  3. the next pass of that same activation: same minute, same block, one more message. Now the
     prompt must be read back through the JSON contract, and nothing stored: the conversation
     is only marked on a turn with server tools (web), which must read all of it back.

Then the review judge, twice. It needs ANTHROPIC_API_KEY and the model settings, so it runs on the
droplet, in a throwaway container from the lisa image with this folder mounted and the live
service's env passed in (the env never touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_cache.py

Fourteen small calls, a few cents. LIVE_WEB_SEARCH=1 adds one web turn that searches, to show
what a server-tool turn reads back between its own hops. Exits non-zero when any check fails.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import app.prompt as prompt_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from app.review.judge import Judge  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, output_schema_for, reason_runtime_for, server_tools_for, skill_enabled,
    system_prompt_for,
)

_checks = {"pass": 0, "fail": 0}
_rows: list = []

BOOK = ("\n\nAddress book — the people this conversation points at:\n"
        "- Felipe Prado <felipe.prado@example.com>")
BOOK_2 = ("\n\nAddress book — the people this conversation points at:\n"
          "- Ana Lima <ana.lima@example.com>")

# What he asks each skill. Nothing here needs a tool to have run: the reasoner is called on its
# own, so an action she proposes is never executed.
ASK = {
    "calendar": "@lisa quais são meus compromissos de amanhã?",
    "contacts": "@lisa qual é o email do Felipe Prado?",
    "setup": "@lisa setup quais conversas têm transcrição automática?",
    "web": "@lisa quanto é 17 vezes 3?",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""),
          flush=True)
    _checks["pass" if cond else "fail"] += 1


def row(what: str, u: dict) -> None:
    total = u.get("input") or 0
    read = u.get("cache_read") or 0
    _rows.append((what, total, read, u.get("cache_write") or 0, u.get("output") or 0,
                  f"{100 * read // total}%" if total else "-"))


class at:
    """The owner's clock, as the prompt builder reads it."""

    def __init__(self, when: datetime) -> None:
        self.when = when

    def __enter__(self):
        self._real = prompt_mod.now_in
        prompt_mod.now_in = lambda tz_name: (self.when.astimezone(self._real(tz_name)[0].tzinfo),
                                             tz_name)

    def __exit__(self, *exc):
        prompt_mod.now_in = self._real


async def skill_checks(name: str, s: Settings, reasoner, run_id: str) -> None:
    runtime = reason_runtime_for(name, s)
    kw = dict(output_schema=output_schema_for(name, settings=s),
              server_tools=server_tools_for(name, s), **runtime)
    now = prompt_mod.now_in(s.calendar_timezone)[0]
    # The run id makes this conversation new to the cache, so what is read back on call 2 can
    # only be the instructions, never a transcript left over from an earlier run.
    opened = {"role": "user", "content":
              f"── {now:%A, %d %b %Y} ──\n[{now:%H:%M}] {s.owner_name}: {ASK[name]} (ref {run_id})"}

    with at(now):
        p1 = system_prompt_for(name, s, context_block=BOOK)
    r1 = await reasoner.respond(system=p1, messages=[opened], **kw)
    u1 = r1.get("usage") or {}
    row(f"{name} 1 first pass", u1)
    check(f"[{name}] the request is accepted with its breakpoints",
          r1.get("error_category") == "none" and r1.get("stop_reason") != "error",
          str(r1.get("stop_reason")))
    check(f"[{name}] the prompt is long enough to be stored or already was",
          (u1.get("cache_write") or 0) + (u1.get("cache_read") or 0) > 0, str(u1))

    # A minute on, locked, another block: only the skill's own instructions still match.
    convo = [opened, {"role": "user", "content": "[observation] Nothing ran yet."}]
    with at(now + timedelta(minutes=1)):
        p2 = system_prompt_for(name, s, session_lang="pt", context_block=BOOK_2)
        r2 = await reasoner.respond(system=p2, messages=convo, **kw)
        u2 = r2.get("usage") or {}
        row(f"{name} 2 later pass", u2)
        check(f"[{name}] a later pass reads the skill's instructions back",
              (u2.get("cache_read") or 0) > 0, str(u2))

        # The same minute, lock and block, one more message: everything before it is read back.
        convo = convo + [{"role": "user", "content": "[observation] Still nothing ran."}]
        p3 = system_prompt_for(name, s, session_lang="pt", context_block=BOOK_2)
        r3 = await reasoner.respond(system=p3, messages=convo, **kw)
    u3 = r3.get("usage") or {}
    row(f"{name} 3 next pass", u3)
    check(f"[{name}] the next pass reads this turn's block and the contract back too",
          (u3.get("cache_read") or 0) > (u2.get("cache_read") or 0), f"{u2} then {u3}")
    if kw["server_tools"]:
        check(f"[{name}] a turn with server tools reads its conversation back as well",
              (u3.get("cache_read") or 0) >= 0.95 * (u3.get("input") or 1), str(u3))
    else:
        check(f"[{name}] most of the prompt is read back",
              (u3.get("cache_read") or 0) >= 0.8 * (u3.get("input") or 1), str(u3))
        check(f"[{name}] and nothing is stored: its conversation is not marked",
              (u3.get("cache_write") or 0) == 0, str(u3))
    check(f"[{name}] she still answers in the contract", r3.get("error_category") == "none"
          and r3.get("state") in ("keep_listening", "awaiting_answer", "close"),
          str(r3.get("stop_reason")))


async def web_search_check(s: Settings, reasoner, run_id: str) -> None:
    runtime = reason_runtime_for("web", s)
    now = prompt_mod.now_in(s.calendar_timezone)[0]
    msg = {"role": "user", "content":
           f"[{now:%H:%M}] {s.owner_name}: @lisa pesquisa na web: qual foi o resultado do último "
           f"jogo do Cruzeiro? (ref {run_id})"}
    r = await reasoner.respond(system=system_prompt_for("web", s), messages=[msg],
                               output_schema=output_schema_for("web", settings=s),
                               server_tools=server_tools_for("web", s), **runtime)
    u = r.get("usage") or {}
    row(f"web search ({len(r.get('tool_calls') or [])} tool calls)", u)
    check("[web] a turn that searches is accepted with its breakpoints",
          r.get("error_category") == "none", str(r.get("stop_reason")))
    if r.get("tool_calls"):
        check("[web] its hops read each other's results back", (u.get("cache_read") or 0) > 0, str(u))


async def judge_checks(s: Settings, run_id: str) -> None:
    j = Judge(s)
    when = prompt_mod.now_in(s.calendar_timezone)[0]
    # The judge reports one number, everything it was sent, so the split is read off the
    # responses themselves.
    client = j._client_or_make()
    create, seen = client.messages.create, []

    async def spy(**kwargs):
        resp = await create(**kwargs)
        seen.append(resp.usage)
        return resp

    client.messages.create = spy
    lines = [{"who": s.owner_name, "text": f"@lisa que horas são? (ref {run_id})",
              "wa_ts": int(time.time()) - 60}]
    a = await j.judge(lines=lines, reply_text="São 12:00.", turn_ts=when)
    b = await j.judge(lines=lines + [{"who": s.owner_name, "text": "obrigado",
                                      "wa_ts": int(time.time())}],
                      reply_text=None, turn_ts=when)
    check("[judge] both verdicts came back", a.get("error") is None and b.get("error") is None,
          f"{a.get('error')} / {b.get('error')}")
    reads = [getattr(u, "cache_read_input_tokens", 0) or 0 for u in seen]
    writes = [getattr(u, "cache_creation_input_tokens", 0) or 0 for u in seen]
    check("[judge] the second verdict reads the instructions back",
          len(reads) == 2 and reads[1] > 0, f"read {reads}, written {writes}")
    check("[judge] its input count is still the whole prompt both times",
          len(reads) == 2 and min(a.get("input_tokens") or 0, b.get("input_tokens") or 0) > reads[1],
          f"{a.get('input_tokens')} / {b.get('input_tokens')}")
    for n, (v, r, w) in enumerate(zip((a, b), reads, writes), 1):
        row(f"judge {n}", {"input": v.get("input_tokens"), "cache_read": r, "cache_write": w,
                           "output": v.get("output_tokens")})


async def main() -> None:
    s = Settings()
    if not s.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set — run this on the droplet (see the docstring).")
        sys.exit(2)
    if not s.prompt_cache_enabled:
        print("PROMPT_CACHE_ENABLED is off in this env — nothing to check.")
        sys.exit(2)
    reasoner = build_reasoner(s)
    run_id = f"{int(time.time())}"
    for name in SKILLS:
        if skill_enabled(SKILLS[name], s):
            await skill_checks(name, s, reasoner, run_id)
    if os.environ.get("LIVE_WEB_SEARCH") == "1":
        await web_search_check(s, reasoner, run_id)
    if getattr(s, "review_model", None):
        await judge_checks(s, run_id)

    print("\ncall                        input   read  written  output  read%")
    for what, total, read, write, out, pct in _rows:
        print(f"{what:26s} {total!s:>6} {read!s:>6} {write!s:>8} {out!s:>7} {pct:>6}")
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
