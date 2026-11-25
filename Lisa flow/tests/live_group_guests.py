"""Live check — in a small group, does the real model invite the people in it?

run_group_guests.py proves the code around the rule: who has written in the chat, which card is
theirs, and the lines that tell the model so. The rule itself is prose (calendar GUIDANCE, "WHO it is
with"), and only the model can show it is followed. The first scenario replays 2026-11-25 with
made-up people: three people agree a call time in a group, and "@lisa agendar google meet" must
book the other two, from addresses she holds, without asking him for them. Each scenario runs the
real graph with the real reasoner against a fake WhatsApp, a stub calendar and an address book held
in memory. Nothing is booked, nothing is written to his contacts, and no message is sent to anyone.

On the droplet, in a throwaway container from the lisa image (see live_partner.py):

    docker run --rm --env-file <(docker exec lisa_brain env) --env-file /opt/evolution/tests.env \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_group_guests.py

LIVE_REPEAT (default 3) runs each scenario that many times. LIVE_ONLY=name,name runs just those.
Exits non-zero when any run fails.
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from live_partner import Chat as PartnerChat, guests, held, settings  # noqa: E402
from run_contacts_skill import person  # noqa: E402

GROUP_JID = "120363000000000000@g.us"
BRUNO = "5511988880001"            # a Google card holds his number
CAIO = "5512977770002"             # only a learned card, linked to his number by a booking
DORA = "5521966660003"             # nobody on file
BRUNO_MAIL = "bruno@example.com"
CAIO_MAIL = "caio@example.org"
REPEAT = int(os.environ.get("LIVE_REPEAT", "3"))
ONLY = {n for n in os.environ.get("LIVE_ONLY", "").split(",") if n}

BRUNO_CARD = person("people/bruno", "Bruno Almeida Prado", emails=[BRUNO_MAIL], phones=[f"+{BRUNO}"])
# Strangers whose names hold ordinary words of the conversation.
STRANGERS = [person("people/x1", "Paula Grupo Capilar", phones=["+5511955550001"]),
             person("people/x2", "Eva Fundos Imobiliarios", phones=["+5511955550002"]),
             person("people/x3", "Rita Hoje Doces", phones=["+5511955550003"])]


class Chat(PartnerChat):
    """A group: each line carries its sender, as Evolution's rows do (an @lid, and the phone)."""

    def __init__(self, people: list, *, caio_card: bool = True) -> None:
        super().__init__(people, jid=GROUP_JID)
        if caio_card:
            learned = {"resource_name": "learned/caio@example.org", "name": "Caio Teixeira",
                       "emails": [CAIO_MAIL], "phones": [], "nicknames": [], "source": "learned",
                       "preferred": CAIO_MAIL, "used_at": None}
            d = self.directory
            d.load(list(d._contacts.values()) + [learned], links={f"+{CAIO}": learned["resource_name"]})

    def from_(self, phone: str, name: str | None, text: str) -> None:
        self.other(text, name=name)
        rec = self.evo.history[-1]
        rec["participant"] = f"7560000000{phone[-4:]}@lid"
        rec["participant_alt"] = f"{phone}@s.whatsapp.net"


def the_afternoon(c: Chat, *, third=(CAIO, "Caio Teixeira")) -> None:
    """2026-11-25 with made-up people: a nickname, an @-mention, and nobody named in the text."""
    phone, name = third
    c.me("oi pessoal, criando grupo com nós 3")
    c.me("temos que falar antes de falarmos com os fundos")
    c.from_(phone, name, "com certeza, temos que estar alinhados")
    c.from_(BRUNO, "Bruno", "Hoje à tarde eu tô livre. A gente pode encontrar mais cedo também")
    c.me("tbm to livre hj a tarde. 15hrs?")
    c.from_(phone, name, "15h eu tenho uma agenda, podemos fazer 15h30?")
    c.me("posso")
    c.from_(phone, None, "boa, bora as 15h30")
    c.me("blz Brunão?")
    c.me(f"@{7560000000}{BRUNO[-4:]} 15:30 blz?")
    c.from_(BRUNO, "Bruno", "Beleza")


async def the_message_that_started_this():
    c = Chat([BRUNO_CARD, *STRANGERS])
    the_afternoon(c)
    st = await c.say("@lisa agendar google meet")
    p = held(st)
    card = (p.get("task") == "calendar.create" and guests(p) == sorted([BRUNO_MAIL, CAIO_MAIL])
            and "T15:30" in (p.get("start") or ""))
    await c.say("sim")
    return card and guests(c.booked()) == sorted([BRUNO_MAIL, CAIO_MAIL]), \
        f"held={p} said={c.last()!r} booked={c.booked()}"


async def one_of_them_has_no_card():
    """Dora is not on file: she asks him for Dora's address, and invents none."""
    c = Chat([BRUNO_CARD, *STRANGERS], caio_card=False)
    the_afternoon(c, third=(DORA, "Dora Lima"))
    st = await c.say("@lisa agendar google meet")
    p = held(st)
    said = c.last()
    extra = [g for g in guests(p) if g != BRUNO_MAIL]
    asked = "dora" in said.lower() and ("email" in said.lower() or "e-mail" in said.lower())
    return (not extra and asked and c.cal.n("create") == 0), f"held={p} said={said!r}"


async def his_own_appointment():
    c = Chat([BRUNO_CARD, *STRANGERS])
    the_afternoon(c)
    st = await c.say("@lisa agenda dentista amanha as 9h")
    p = held(st)
    return p.get("task") == "calendar.create" and not guests(p), f"held={p} said={c.last()!r}"


async def he_names_one_of_them():
    c = Chat([BRUNO_CARD, *STRANGERS])
    the_afternoon(c)
    st = await c.say("@lisa marca amanha 10h so eu e o Bruno, revisao do deck")
    p = held(st)
    return (p.get("task") == "calendar.create" and guests(p) == [BRUNO_MAIL],
            f"held={p} said={c.last()!r}")


SCENARIOS = [the_message_that_started_this, one_of_them_has_no_card, his_own_appointment,
             he_names_one_of_them]


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
    from live_key import require_tests_key

    require_tests_key()
    sys.exit(asyncio.run(main()))
