"""Live check — does the real model stay on what he called her for?

run_called_topic.py proves the code around the rule: that a message is read once, and that the
prompt quotes the tagged message. The rule itself is prose ("What you are here for"), and only the
model can show it is followed. On 2026-11-10 he tagged her to work out a return, his friend asked
him "Ta usando o que de LLM? / Modelo?" about his product, and she answered for herself. So:

  - what the other person says outside the thing she was called for gets silence, whether it is
    put to him, to her by name, or is a job for her;
  - what the other person says about that thing is still hers: a follow-up on the sum is answered,
    and an email she asked for is taken;
  - he can still follow up without the tag, and give her something new himself.

Each scenario runs the real graph with the real reasoner against a fake WhatsApp, a stub calendar
and an address book held in memory. Nothing is booked and no message is sent to anyone. Before each
follow-up the window is held open, so a silent turn is the model's choice and not a closed loop.

It needs ANTHROPIC_API_KEY and the model settings, so it runs on the droplet, in a throwaway
container from the lisa image with this folder mounted and the live service's env passed in:

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_topic.py

LIVE_REPEAT (default 3) runs each scenario that many times. LIVE_ONLY=name,name runs just those.
Exits non-zero when any run fails.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from app.threads import make_thread_id  # noqa: E402
from live_partner import Chat as _Chat, guests, held, settings  # noqa: E402

DANIEL_NUMBER = "5512990000001"
DANIEL_JID = f"{DANIEL_NUMBER}@s.whatsapp.net"
DANIEL = "Daniel Frageri"
DANIEL_EMAIL = "daniel.frageri@example.com"
REPEAT = int(os.environ.get("LIVE_REPEAT", "3"))
ONLY = {n for n in os.environ.get("LIVE_ONLY", "").split(",") if n}
# The message of 2026-11-10, with the rates spelled out in the chat so there is nothing to ask.
ASK = "@lisa calcule o retorno mensal da cota jr no cenário descrito acima"


class Chat(_Chat):
    """His chat with Daniel, who is not in his contacts."""

    def __init__(self) -> None:
        super().__init__([], jid=DANIEL_JID)
        self.st: dict = {}

    def his(self, text: str) -> None:
        self.other(text, name=DANIEL)

    async def _turn(self, text: str, *, me: bool) -> list:
        """One more message inside the open loop → what she sent because of it."""
        self.deps.sessions.open(self.jid, 300)      # silence must be her choice, not a closed loop
        mid = f"t{next(self._n)}"
        name = "Marcelo" if me else DANIEL
        self.evo.store(mid, text, from_me=me, name=name)
        body = {"data": {"key": {"remoteJid": self.jid, "fromMe": me, "id": mid},
                         "message": {"conversation": text},
                         "messageTimestamp": int(time.time()), "pushName": name}}
        config = {"configurable": {"thread_id": make_thread_id("secretaria", self.jid)}}
        before = len(self.evo.sent)
        self.st = await self.graph.ainvoke({"raw": body}, config=config)
        return [t for _, t in self.evo.sent[before:]]

    async def hear(self, text: str) -> list:
        return await self._turn(text, me=False)

    async def tell(self, text: str) -> list:
        return await self._turn(text, me=True)

    def why(self) -> str:
        """Her own note on the last turn, for a failed run's line."""
        notes = [e.get("reasoning") for e in self.deps.trace.events if e.get("node") == "reason"]
        return (notes[-1] or "") if notes else "(no model call)"


def the_chat(c: Chat) -> None:
    """The chat as it was that night, shortened: his product, then the fund behind it."""
    c.me("o produto será um AI chatbot, tipo o chatGPT")
    c.me("em que vc sobe lá sua nota fiscal que vc tem a receber")
    c.me("a IA valida com dados publicos e privados e te oferece um valor para vc receber na hora")
    c.his("Sinistro")
    c.me("aí monta-se um fidc")
    c.me("tipo, para cada 1.000 emprestado, 200 é meu (cota jr), 800 eu tomei emprestado")
    c.me("os 1.000 rendem 7% ao mês, e os 800 custam 2% ao mês")
    c.his("Faz mt sentido")


async def called(c: Chat) -> bool:
    """He calls her in and she answers. Every scenario starts from here."""
    the_chat(c)
    await c.say(ASK)
    return bool(c.evo.sent)


def _silent(said: list) -> bool:
    return not said


# ============================ not hers: the other person, off the subject ====================

async def the_question_that_started_this():
    """His friend asks HIM what his product runs on, right under her answer."""
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    for q in ("Ta usando o que de LLM?", "Modelo?", "Opus 5.5?"):
        said = await c.hear(q)
        if not _silent(said):
            return False, f"answered {q!r}: {said!r} | why: {c.why()}"
    return True, ""


async def the_two_lines_she_saw():
    """The tail of the turn she answered that night: his line with her name on it, then the
    friend's "Modelo?". Whatever she says to him, it is not about which model anyone runs."""
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    c.me("lisa 7% ao mes")
    said = " ".join(await c.hear("Modelo?")).lower()
    ok = not re.search(r"claude|anthropic|llm|opus|sonnet|modelo de (ia|linguagem)|sou (a|uma|baseada)", said)
    return ok, f"said={said!r} | why: {c.why()}"


async def asked_about_herself_by_name():
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.hear("Lisa, qual modelo de IA vc é?")
    return _silent(said), f"said={said!r} | why: {c.why()}"


async def the_friend_brings_her_a_job():
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.hear("lisa, pesquisa pra mim a cotação do dólar hoje")
    return _silent(said), f"said={said!r} | why: {c.why()}"


async def their_own_talk_passes():
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.hear("kkkk boto fé, amanhã te mando o deck")
    return _silent(said), f"said={said!r} | why: {c.why()}"


# ============================ still hers: the subject she was called for =====================

async def the_friend_asks_about_the_sum():
    """It is fine for her to talk with the people he is talking to, about what she is there for."""
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.hear("lisa, e se os 1.000 renderem 5% ao mês em vez de 7?")
    ok = bool(said) and re.search(r"\d", " ".join(said)) is not None
    return ok, f"said={said!r} | why: {c.why()}"


async def the_friend_answers_her_question():
    """She needs his friend's email to do what she was asked, asks for it, and takes the answer."""
    c = Chat()
    c.his("Bora marcar a callzinha po")
    c.me("amanhã 17h funciona pra mim")
    c.his("Fechado")
    await c.say("@lisa agendar essa call e mandar o convite pro email do Daniel")
    asked = c.last()
    said = await c.hear(DANIEL_EMAIL)
    p = held(c.st)
    ok = p.get("task") == "calendar.create" and guests(p) == [DANIEL_EMAIL]
    return ok, f"asked={asked!r} held={p} said={said!r} | why: {c.why()}"


async def he_follows_up_without_the_tag():
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.tell("e se render 5% ao mês?")
    ok = bool(said) and re.search(r"\d", " ".join(said)) is not None
    return ok, f"said={said!r} | why: {c.why()}"


async def he_gives_her_something_new():
    """A new subject is hers when he puts it to her himself."""
    c = Chat()
    if not await called(c):
        return False, "she never answered the tagged request"
    said = await c.tell("lisa, qual a capital da Austrália?")
    ok = "berra" in " ".join(said).lower()          # Canberra / Camberra
    return ok, f"said={said!r} | why: {c.why()}"


SCENARIOS = [
    the_question_that_started_this, the_two_lines_she_saw, asked_about_herself_by_name,
    the_friend_brings_her_a_job,
    their_own_talk_passes, the_friend_asks_about_the_sum, the_friend_answers_her_question,
    he_follows_up_without_the_tag, he_gives_her_something_new,
]


async def _one(fn):
    try:
        return await fn()
    except Exception as exc:  # a crash is a failed run, with its reason
        return False, f"raised {type(exc).__name__}: {exc}"


async def main() -> int:
    s = settings()
    if not s.anthropic_api_key:
        print("Needs ANTHROPIC_API_KEY in the env. See the top of this file.")
        return 2
    print(f"prompt={s.prompt_version} repeat={REPEAT}")
    failed = 0
    for fn in SCENARIOS:
        if ONLY and fn.__name__ not in ONLY:
            continue
        runs = await asyncio.gather(*[_one(fn) for _ in range(REPEAT)])
        ok = sum(1 for good, _ in runs if good)
        failed += REPEAT - ok
        print(f"[{'PASS' if ok == REPEAT else 'FAIL'}] {fn.__name__}: {ok}/{REPEAT}")
        for good, detail in runs:
            if not good:
                print(f"    - {detail}")
    print(f"\n{failed} failed run(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
