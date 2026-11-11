"""Live check — does the real model put the person he is talking to on the meeting?

run_chat_partner.py proves the code around the rule: which chats are one-to-one, and the line that
tells the model so. The rule itself is prose, and only the model can show it is followed: that
"@lisa agendar amanha 13hrs" in his wife's chat books the two of them, that the dentist stays his
alone, and that a group or his own chat is left as it was. Each scenario runs the real graph with
the real reasoner against a fake WhatsApp, a stub calendar and an address book held in memory.
Nothing is booked, nothing is written to his contacts, and no message is sent to anyone.

It needs ANTHROPIC_API_KEY and the model settings, so it runs on the droplet, in a throwaway
container from the lisa image with this folder mounted and the live service's env passed in
(the env never touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_partner.py

LIVE_REPEAT (default 3) runs each scenario that many times, since one run of a model proves
little. LIVE_ONLY=name,name runs just those scenarios. Exits non-zero when any run fails.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import confirm_policies, render_policies, resolve_gates, side_effect_verbs  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.contacts import ContactsService  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_contacts_skill import ChatEvolution, book, person  # noqa: E402
from run_step3 import StubCalendar  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"
HER_NUMBER = "5511994220000"
HER_JID = f"{HER_NUMBER}@s.whatsapp.net"
GROUP_JID = "120363041234567890@g.us"
HER_NAME = "Laura"                       # what WhatsApp shows; her card says something else
ONE = "laura@example.com"
TWO = "laura@work.example.com"
FELIPE = "felipe.rodrigues@example.com"
REPEAT = int(os.environ.get("LIVE_REPEAT", "3"))
ONLY = {n for n in os.environ.get("LIVE_ONLY", "").split(",") if n}
ASK = "@lisa agendar amanha 13hrs"       # the message of 2026-11-10, as he typed it
# With a topic, for the chats where nobody is implied: there the bare message gets "about what?",
# as it always did, and that question is not what these check.
TOPIC = "@lisa agendar revisao do orcamento amanha 13hrs"


def her_card(*emails) -> dict:
    return person("people/laura", "Bbzao", emails=emails, phones=[f"+{HER_NUMBER}"])


FELIPE_CARD = person("people/felipe", "Felipe Rodrigues", emails=[FELIPE])


def settings() -> Settings:
    # The model settings and the key come from the env; the rest of the outside world is off.
    return Settings(evolution_apikey="x", redis_url=None, database_url=None, owner_jid=OWNER_JID,
                    review_enabled=False, media_enabled=False, transcription_enabled=False,
                    auto_transcribe_enabled=False, contacts_enabled=True,
                    contacts_crud_enabled=True)


class Chat:
    def __init__(self, people: list, *, jid: str = HER_JID) -> None:
        s = settings()
        self.jid = jid
        # A few minutes back, so the chat reads as earlier today and "amanhã" is tomorrow.
        self.evo = ChatEvolution(start_ts=int(time.time()) - 900)
        self.cal = StubCalendar()
        self.cal.responses["create"] = lambda i: {"ok": True, "summary": "created", "data": {
            "title": i.get("title"), "start": i.get("start"), "attendees": i.get("attendees") or [],
            "notified": True, "html_link": "http://cal/1"}}
        self.directory, _google, _store = book(people)
        contacts = ContactsService(s)
        contacts.directory = self.directory
        self.deps = Deps(settings=s, evolution=self.evo, sessions=InMemorySessions(ttl=300),
                         echoes=InMemoryEchoes(ttl=3600), trace=build_trace(),
                         reasoner=build_reasoner(s), redis=None,
                         tools={"calendar": self.cal, "contacts": contacts},
                         confirm_policies=confirm_policies(), render_policies=render_policies(),
                         resolve_gates=resolve_gates(), side_effects=side_effect_verbs(),
                         directory=self.directory)
        self.graph = build_graph(self.deps, MemorySaver())
        self._n = itertools.count(1)

    def other(self, text: str, name: str = HER_NAME) -> None:
        self.evo.store(f"o{next(self._n)}", text, from_me=False, name=name)

    def me(self, text: str) -> None:
        self.evo.store(f"e{next(self._n)}", text, from_me=True, name="Marcelo")

    async def say(self, text: str) -> dict:
        mid = f"m{next(self._n)}"
        self.evo.store(mid, text, from_me=True, name="Marcelo")
        body = {"data": {"key": {"remoteJid": self.jid, "fromMe": True, "id": mid},
                         "message": {"conversation": text},
                         "messageTimestamp": int(time.time()), "pushName": "Marcelo"}}
        config = {"configurable": {"thread_id": make_thread_id("secretaria", self.jid)}}
        return await self.graph.ainvoke({"raw": body}, config=config)

    def last(self) -> str:
        return self.evo.sent[-1][1] if self.evo.sent else ""

    def booked(self) -> dict:
        return next((dict(i) for v, i in self.cal.calls if v == "create"), {})


def held(st: dict) -> dict:
    return st.get("pending_action") or {}


def guests(action: dict) -> list:
    return sorted(a.lower() for a in action.get("attendees") or [])


def household(c: Chat) -> None:
    """The chat as it was: about the house, nothing about a meeting."""
    c.other("Bbzao no hall da escada sabe se tem ponto para lampada alem do ponto para spot?")
    c.me("acho que tem, vejo quando chegar")
    c.other("ta bom")


# ============================ one-to-one, her card on file ===================================

async def the_message_that_started_this():
    c = Chat([her_card(ONE)])
    household(c)
    st = await c.say(ASK)
    p = held(st)
    card = (p.get("task") == "calendar.create" and guests(p) == [ONE]
            and "&" in (p.get("title") or "") and ONE in c.last() and c.cal.n("create") == 0)
    await c.say("sim")
    return card and guests(c.booked()) == [ONE], f"held={p} card={c.last()!r} booked={c.booked()}"


async def nobody_else_has_spoken():
    """He opens the chat and tags her at once: her card is all that says who the chat is with."""
    c = Chat([her_card(ONE)])
    st = await c.say(ASK)
    p = held(st)
    return p.get("task") == "calendar.create" and guests(p) == [ONE], f"held={p} said={c.last()!r}"


async def two_addresses_is_a_question():
    c = Chat([her_card(ONE, TWO)])
    household(c)
    st = await c.say(ASK)
    asked = c.last()
    question = not held(st) and ONE in asked and TWO in asked and c.cal.n("create") == 0
    first = ONE if asked.find(ONE) < asked.find(TWO) else TWO
    st = await c.say("1")
    p = held(st)
    return (question and p.get("task") == "calendar.create" and guests(p) == [first],
            f"asked={asked!r} held={p}")


async def pointing_back_at_her_proposal():
    c = Chat([her_card(ONE)])
    c.other("Vamos almocar amanha 12h30 no Mani?")
    c.me("bora")
    st = await c.say("@lisa agendar")
    p = held(st)
    ok = (p.get("task") == "calendar.create" and guests(p) == [ONE]
          and "T12:30" in (p.get("start") or ""))
    return ok, f"held={p} said={c.last()!r}"


# ============================ one-to-one, and the context says otherwise =====================

async def the_dentist_is_his_alone():
    c = Chat([her_card(ONE)])
    household(c)
    st = await c.say("@lisa agenda dentista amanha as 9h")
    p = held(st)
    return p.get("task") == "calendar.create" and not guests(p), f"held={p} said={c.last()!r}"


async def he_says_only_him():
    c = Chat([her_card(ONE)])
    household(c)
    st = await c.say(f"{ASK}, so pra mim")
    p = held(st)
    return p.get("task") == "calendar.create" and not guests(p), f"held={p} said={c.last()!r}"


async def a_day_off_has_no_guest():
    c = Chat([her_card(ONE)])
    household(c)
    st = await c.say("@lisa bloqueia depois de amanha o dia inteiro, vou viajar")
    p = held(st)
    ok = p.get("task") == "calendar.create" and p.get("all_day") is True and not guests(p)
    return ok, f"held={p} said={c.last()!r}"


async def he_names_someone_else():
    """A meeting with Felipe, asked for in her chat, is with Felipe. A full name on file is matched
    by name alone, so she may confirm who he is before the card: either way her address stays off."""
    c = Chat([her_card(ONE), FELIPE_CARD])
    household(c)
    st = await c.say("@lisa marca uma reuniao com o Felipe Rodrigues amanha as 15h")
    p = held(st)
    said = c.last()
    about_felipe = guests(p) == [FELIPE] if p else "felipe" in said.lower()
    return about_felipe and ONE not in guests(p) and ONE not in said, f"held={p} said={said!r}"


# ============================ not one-to-one: as before =======================================

async def a_group_names_nobody():
    c = Chat([her_card(ONE)], jid=GROUP_JID)
    c.other("Alguem viu o jogo ontem?")
    c.other("Vi, que jogo ruim", name="Felipe Rodrigues")
    st = await c.say(TOPIC)
    p = held(st)
    ok = (p.get("task") == "calendar.create" and not guests(p)
          and not int(st.get("skill_calls") or 0))
    return ok, f"held={p} calls={st.get('skill_calls')} said={c.last()!r}"


async def his_own_chat():
    c = Chat([her_card(ONE)], jid=OWNER_JID)
    st = await c.say(TOPIC)
    p = held(st)
    return p.get("task") == "calendar.create" and not guests(p), f"held={p} said={c.last()!r}"


# ============================ one-to-one, nobody on file ======================================

async def no_card_for_the_number():
    """Someone his contacts do not have. She cannot invite an address she does not hold, and he
    offered none, so she does not ask for one: the card names the two of them, with no guest."""
    c = Chat([FELIPE_CARD])
    c.other("Oi Marcelo, aqui e o Joao Pereira da marcenaria", name="Joao Pereira")
    c.other("Podemos falar amanha sobre o orcamento?", name="Joao Pereira")
    c.me("podemos")
    st = await c.say(ASK)
    p = held(st)
    ok = (p.get("task") == "calendar.create" and not guests(p)
          and "T13:00" in (p.get("start") or ""))
    return ok, f"held={p} calls={st.get('skill_calls')} said={c.last()!r}"


async def nobody_to_go_by():
    """No card for the number and nothing from them in the chat: nothing says who the person is.
    It is booked as it was before this rule, without a made-up name or "who is this with?"."""
    c = Chat([FELIPE_CARD])
    st = await c.say(TOPIC)
    p = held(st)
    ok = (p.get("task") == "calendar.create" and not guests(p)
          and not int(st.get("skill_calls") or 0))
    return ok, f"held={p} calls={st.get('skill_calls')} said={c.last()!r}"


async def her_card_has_no_address():
    c = Chat([her_card()])
    household(c)
    st = await c.say(ASK)
    p = held(st)
    ok = (p.get("task") == "calendar.create" and not guests(p)
          and "&" in (p.get("title") or ""))
    return ok, f"held={p} calls={st.get('skill_calls')} said={c.last()!r}"


SCENARIOS = [
    the_message_that_started_this, nobody_else_has_spoken, two_addresses_is_a_question,
    pointing_back_at_her_proposal, the_dentist_is_his_alone, he_says_only_him,
    a_day_off_has_no_guest, he_names_someone_else, a_group_names_nobody, his_own_chat,
    no_card_for_the_number, nobody_to_go_by, her_card_has_no_address,
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
    print(f"contacts: enabled={s.contacts_enabled} crud_on={s.contacts_crud_on} "
          f"prompt={s.prompt_version} (the address book is in memory)")
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
