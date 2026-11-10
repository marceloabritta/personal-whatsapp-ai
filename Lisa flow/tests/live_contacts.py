"""Live check — the real Google Contacts, and the real model, on a throwaway card.

The selftest (run_contacts_skill.py) proves what the code decides, against a fake of the People API.
Only this can show that Google agrees with the fake, and that the model chooses to route, search, call
and propose as its prompts ask.

WHAT IT TOUCHES. It creates one card, "Zeferino Testolino", with an example.com address, and every
write in this file targets that card. It deletes the card at the end, even after a failure. It reads
the rest of the book to search it, and writes nothing else. The calendar is a stub, so nothing is booked,
and WhatsApp is a fake, so nothing is sent. The delete's copy goes to an in-memory store.

It needs the contacts token and ANTHROPIC_API_KEY, so it runs on the droplet, in a throwaway container
from the NEW lisa image with this folder mounted and the live service's env passed in (the env never
touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_contacts.py

Exits non-zero when a check fails. One run proves little about the model, which varies: repeat a
failing scenario against the old and new image before calling it a regression.
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
from app.directory import Directory  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import confirm_policies, render_policies, resolve_gates, side_effect_verbs  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.contacts import ContactsService  # noqa: E402
from app.tools.people import GooglePeople, card_view  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_contacts_skill import ChatEvolution, FakeStore  # noqa: E402
from run_step3 import StubCalendar  # noqa: E402

CHAT_JID = "5511900000001@s.whatsapp.net"
STAMP = int(time.time())
NAME = "Zeferino Testolino"
OLD = f"zeferino.{STAMP}@example.com"
NEW = f"zeferino.novo.{STAMP}@example.com"

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  — {detail}" if detail else ""))


def settings() -> Settings:
    # The Google and model settings come from the env; the rest of the outside world is off.
    return Settings(evolution_apikey="x", redis_url=None, database_url=None, review_enabled=False,
                    media_enabled=False, transcription_enabled=False,
                    auto_transcribe_enabled=False, contacts_enabled=True,
                    contacts_crud_enabled=True)


def fresh(gp: GooglePeople, rn: str) -> dict:
    return card_view(gp.read_card(rn))


# --- Google, with no model in the way --------------------------------------------------------

def google_checks(gp: GooglePeople) -> None:
    print("\nGoogle agrees with the fake")
    person = gp.create_card(NAME, [
        {"field": "email", "op": "add", "old": "", "new": OLD},
        {"field": "phone", "op": "add", "old": "", "new": "+5511900000002"},
        {"field": "company", "op": "add", "old": "", "new": "Testes Ltda"},
    ])
    rn = person["resourceName"]
    try:
        card = fresh(gp, rn)
        check("a create lands with everything on it",
              card["emails"] == [OLD] and card["company"] == "Testes Ltda" and card["phones"], str(card))

        gp.apply_changes(rn, [{"field": "email", "op": "replace", "old": OLD, "new": NEW}])
        check("a replace swaps the address", fresh(gp, rn)["emails"] == [NEW], str(fresh(gp, rn)))
        check("and leaves the other fields alone", fresh(gp, rn)["company"] == "Testes Ltda")
        _p, report = gp.apply_changes(rn, [{"field": "email", "op": "replace", "old": OLD, "new": NEW}])
        check("a replay is a no-op", report["noop"], str(report))

        gp.apply_changes(rn, [{"field": "title", "op": "add", "old": "", "new": "Sócio"},
                              {"field": "birthday", "op": "replace", "old": "", "new": "1985-03-14"},
                              {"field": "notes", "op": "replace", "old": "", "new": "card de teste"},
                              {"field": "address", "op": "add", "old": "", "new": "Rua de Teste 1"},
                              {"field": "nickname", "op": "add", "old": "", "new": "Zefa"}])
        card = fresh(gp, rn)
        check("every other field writes and reads back",
              card["title"] == "Sócio" and card["birthday"] == "1985-03-14"
              and card["notes"] == "card de teste" and card["addresses"] == ["Rua de Teste 1"]
              and card["nicknames"] == ["Zefa"], str(card))
        check("a title did not cost the company", card["company"] == "Testes Ltda", str(card))

        gp.apply_changes(rn, [{"field": "notes", "op": "remove", "old": "", "new": ""},
                              {"field": "birthday", "op": "remove", "old": "", "new": ""},
                              {"field": "address", "op": "remove", "old": "Rua de Teste 1", "new": ""}])
        card = fresh(gp, rn)
        check("removals stick", not card["notes"] and not card["birthday"] and not card["addresses"], str(card))

        gp.apply_changes(rn, [{"field": "name", "op": "replace", "old": "", "new": f"{NAME} Junior"}])
        check("a rename lands", fresh(gp, rn)["name"] == f"{NAME} Junior", str(fresh(gp, rn)))

        batch = gp.read_cards([rn])
        check("the batch read returns the whole card", batch and batch[0]["resourceName"] == rn)
    finally:
        gp.delete_contact(rn)
    try:
        gp.read_card(rn)
        check("a deleted card is gone", False)
    except Exception as exc:
        check("a deleted card is gone", getattr(getattr(exc, "resp", None), "status", None) == 404, str(exc))


# --- the real model, on the real graph -------------------------------------------------------

class Chat:
    def __init__(self, s: Settings, gp: GooglePeople, directory: Directory) -> None:
        self.evo = ChatEvolution()
        self.cal = StubCalendar()
        contacts = ContactsService(s)
        contacts.directory = directory
        self.deps = Deps(settings=s, evolution=self.evo, sessions=InMemorySessions(ttl=300),
                         echoes=InMemoryEchoes(ttl=3600), trace=build_trace(),
                         reasoner=build_reasoner(s), redis=None,
                         tools={"calendar": self.cal, "contacts": contacts},
                         confirm_policies=confirm_policies(), render_policies=render_policies(),
                         resolve_gates=resolve_gates(), side_effects=side_effect_verbs(),
                         directory=directory)
        self.graph = build_graph(self.deps, MemorySaver())
        self._n = itertools.count(1)

    def other(self, text: str) -> None:
        self.evo.store(f"o{next(self._n)}", text, from_me=False, name="Zeferino Testolino")

    async def say(self, text: str, *, from_me: bool = True) -> dict:
        mid = f"m{next(self._n)}"
        name = "Marcelo" if from_me else "Zeferino Testolino"
        self.evo.store(mid, text, from_me=from_me, name=name)
        body = {"data": {"key": {"remoteJid": CHAT_JID, "fromMe": from_me, "id": mid},
                         "message": {"conversation": text},
                         "messageTimestamp": int(time.time()), "pushName": name}}
        config = {"configurable": {"thread_id": make_thread_id("secretaria", CHAT_JID)}}
        return await self.graph.ainvoke({"raw": body}, config=config)

    def last(self) -> str:
        return self.evo.sent[-1][1] if self.evo.sent else ""


async def model_checks(s: Settings, gp: GooglePeople) -> None:
    print("\nthe real model, on the conversation that started this")
    person = gp.create_card(NAME, [
        {"field": "email", "op": "add", "old": "", "new": OLD},
        {"field": "phone", "op": "add", "old": "", "new": "+5511900000002"},
    ])
    rn = person["resourceName"]
    store = FakeStore()
    directory = Directory(s, store=store, people=gp)
    await directory.refresh(full=True)
    check("the whole book loads", directory.ready and directory.get(rn) is not None)
    deleted = False
    try:
        # 1. The request Lisa refused, with the address in the chat.
        c = Chat(s, gp, directory)
        c.other("Só uma obs: o meu e-mail foi atualizado, ok?")
        c.other(f"Agora é {NEW}")
        st = await c.say("@lisa atualize o email do zeferino testolino nos contatos")
        check("she routes it to contacts", st.get("domain") == "contacts", str(st.get("domain")))
        check("she does not refuse: the card goes out with the new address",
              NEW in c.last() and (st.get("pending_action") or {}).get("task") == "contacts.update",
              c.last())
        check("the old address is masked on the card", OLD not in c.last(), c.last())
        check("nothing is written yet", fresh(gp, rn)["emails"] == [OLD])
        await c.say("sim")
        check("his yes writes it to Google", fresh(gp, rn)["emails"] == [NEW], str(fresh(gp, rn)))
        check("and she reports it", NEW in c.last(), c.last())

        # 2. A booking that needs an address only the book has, and that the block does not list.
        c = Chat(s, gp, directory)
        st = await c.say("@lisa marca uma reunião com o Zeferino Testolino sexta às 15h")
        pending = st.get("pending_action") or {}
        check("the booking reaches a card", pending.get("task") == "calendar.create", c.last())
        check("with the address from the book", NEW in (pending.get("attendees") or []), str(pending))
        check("back in calendar, with nobody left waiting",
              st.get("domain") == "calendar" and not st.get("skill_stack"), str(st.get("skill_stack")))
        check("nothing is booked before his yes", c.cal.n("create") == 0)

        # 3. Someone else asking for a number.
        c = Chat(s, gp, directory)
        await c.say("@lisa salva o contato do joão, depois te passo o número")
        await c.say("me passa o telefone do zeferino testolino", from_me=False)
        check("someone else gets no number out of her",
              not any("900000002" in t for _n, t in c.evo.sent), str(c.evo.sent))

        # 4. Deleting: "cancela" keeps it, her card's own word removes it.
        c = Chat(s, gp, directory)
        st = await c.say("@lisa apaga o contato do zeferino testolino")
        check("a delete gets a card", (st.get("pending_action") or {}).get("task") == "contacts.delete", c.last())
        await c.say("cancela")
        check("'cancela' keeps the card", fresh(gp, rn)["emails"] == [NEW])
        st = await c.say("@lisa apaga o contato do zeferino testolino")
        await c.say("apaga")
        deleted = directory.get(rn) is None
        check("'apaga' deletes it", deleted, c.last())
        check("after keeping a copy", store.tombstones and store.tombstones[0][0] == rn)
    finally:
        if not deleted:
            gp.delete_contact(rn)
            print("  (the throwaway card was removed in cleanup)")


async def main() -> int:
    s = settings()
    if not s.contacts_crud_on or not s.anthropic_api_key:
        print("Needs GOOGLE_CONTACTS_REFRESH_TOKEN and ANTHROPIC_API_KEY in the env. See the top of "
              "this file.")
        return 2
    gp = GooglePeople(s)
    ok, detail = gp.check_scopes()
    check("the token can write contacts", ok, detail)
    if not ok:
        print(f"\n{PASS} passed, {FAIL} failed")
        return 1
    await asyncio.to_thread(google_checks, gp)
    await model_checks(s, gp)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
