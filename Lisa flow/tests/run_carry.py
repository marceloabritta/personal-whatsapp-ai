"""Her confirmation cards, answered — the yes that never reached the write, and the re-tag after it.

In the log since August, seven "sim" replies to a card were dropped. Six arrived after the 60s
window had run out (run_window.py). The seventh, on 2026-11-02, arrived five seconds after a card
whose turn the model had marked "close", which shut the window under it. Each time he tagged Lisa
again, and the fresh tag threw the held write away: the model re-proposed it and the same card
went out a second time. These checks pin the three fixes:

  * a card turn keeps the window open, whatever state the model wrote;
  * a fresh tag finds his yes under the card in the chat above and runs the write (carry.py) —
    and when anything is unclear, it does what it did before: one more card;
  * the resolver reads every message of his an activation ingested, once.

Drives the real compiled graph against a fake chat that stores every message and reply the way
Evolution does, so a dropped "sim" is still there when the next tag reads the chat. No network,
no Postgres, no Anthropic key.

    python tests/run_carry.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.carry import carried_yes, kept_on_carry  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.identity import frame  # noqa: E402
from app.intent import classify_confirmation  # noqa: E402
from app.nodes.confirm import action_fingerprint  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import confirm_policies, render_policies, resolve_gates  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_step3 import OWNER_JID, StubCalendar, StubReasoner, _invoke  # noqa: E402

_checks = {"pass": 0, "fail": 0}

START = "2027-03-05T15:00:00-03:00"  # far enough ahead that no run of this file sees it start
CREATE = {"task": "calendar.create", "title": "Marcelo & Lucas Santos", "start": START,
          "location": "Arlene Menna Barreto 253", "attendees": ["lucas@example.com"]}
CREATED = {"ok": True, "summary": "created", "data": {
    "title": "Marcelo & Lucas Santos", "start": START, "html_link": "http://cal/1",
    "attendees": ["lucas@example.com"], "notified": True}}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class ChatEvolution:
    """A chat that keeps everything: each message is stored when it is sent, whether or not Lisa
    is listening, and each of her replies is stored with the id she was handed back."""
    instance = "secretaria"

    def __init__(self) -> None:
        self.sent: list = []
        self.history: list = []
        self._ts = itertools.count(1793630000)

    def store(self, mid: str, text: str, *, from_me: bool, name: str = "Marcelo") -> None:
        self.history.append({"id": mid, "from_me": from_me, "text": text, "push_name": name,
                             "ts": next(self._ts)})

    async def send_text(self, number, text, *, quoted=None):
        mid = f"lisa{len(self.sent)}"
        self.sent.append((number, text))
        self.store(mid, text, from_me=True)
        return mid

    async def fetch_history(self, jid):
        return list(self.history)

    def cards(self) -> int:
        return sum(1 for _, t in self.sent if "Confirm" in t)


class Chat:
    """One conversation with Lisa, driven message by message."""

    def __init__(self) -> None:
        self.settings = Settings(evolution_apikey="x", loop_ttl_seconds=60,
                                 context_window_messages=30, max_tool_actions=4)
        self.evo = ChatEvolution()
        self.stub = StubReasoner()
        self.cal = StubCalendar()
        self.cal.responses["create"] = CREATED
        self.deps = Deps(settings=self.settings, evolution=self.evo,
                         sessions=InMemorySessions(ttl=60), echoes=InMemoryEchoes(ttl=3600),
                         trace=build_trace(), reasoner=self.stub, redis=None,
                         tools={"calendar": self.cal}, confirm_policies=confirm_policies(),
                         render_policies=render_policies(), resolve_gates=resolve_gates())
        self.graph = build_graph(self.deps, MemorySaver())
        self._n = itertools.count(1)

    async def say(self, text: str, *, from_me: bool = True, name: str = "Marcelo",
                  store: bool = True, mid: str | None = None) -> dict:
        mid = mid or f"m{next(self._n)}"
        if store:
            self.evo.store(mid, text, from_me=from_me, name=name)
        body = {"data": {"key": {"remoteJid": OWNER_JID, "fromMe": from_me, "id": mid},
                         "message": {"conversation": text},
                         "messageTimestamp": int(time.time()), "pushName": name}}
        return await _invoke(self.graph, body)

    def lucas(self, text: str, mid: str | None = None) -> str:
        """Lucas says something in the chat without a turn running yet (it is queued)."""
        mid = mid or f"m{next(self._n)}"
        self.evo.store(mid, text, from_me=False, name="Lucas Santos")
        return mid

    def mine(self, text: str) -> str:
        """He says something in the chat without a turn running yet (it is queued)."""
        mid = f"m{next(self._n)}"
        self.evo.store(mid, text, from_me=True)
        return mid

    def close_window(self) -> None:
        self.deps.sessions.close(OWNER_JID)

    def left(self) -> int:
        return self.deps.sessions.ttl_left(OWNER_JID)

    async def card(self, action: dict | None = None, ask: str = "@mary agendar") -> dict:
        """Lisa proposes the write and the card goes out — the start of every scenario."""
        self.evo.store("x0", "sim, pode vir aqui às 15hrs?", from_me=True)
        self.evo.store("x1", "lucas@example.com", from_me=False, name="Lucas Santos")
        self.stub.script = [{"actions": [dict(action or CREATE)]}]
        return await self.say(ask)


async def close_override_checks() -> None:
    print("\nThe card turn keeps the window open (2026-11-02, Lucas)")

    # The production turn, verbatim in shape: a booking AND state "close" in one reply.
    c = Chat()
    c.evo.store("x1", "lucas@example.com", from_me=False, name="Lucas Santos")
    c.stub.script = [{"state": "close", "actions": [dict(CREATE)]}]
    st = await c.say("@mary agendar")
    check("the card went out and the write is held", c.evo.cards() == 1 and bool(st.get("pending_action")))
    check("the model's close did not shut the window", c.deps.sessions.is_open(OWNER_JID))
    check("it is the 5-minute waiting window", 295 <= c.left() <= 300, f"{c.left()}s")
    check("the turn is recorded as awaiting his answer", st.get("llm_state") == "awaiting_answer")
    check("the card's message is remembered with the write it asks about",
          (st.get("pending_card") or {}).get("id") == "lisa0"
          and st["pending_card"]["sig"] == action_fingerprint(st["pending_action"]))
    calls = len(c.stub.calls)
    st = await c.say("sim")  # five seconds later, in production — and dropped
    check("his sim five seconds later books it", c.cal.n("create") == 1)
    check("with no model call", len(c.stub.calls) == calls)
    check("and the card is forgotten with the write", st.get("pending_card") is None)

    # The repeat-suppressed card is a wait too: the model re-proposed the same card and said close.
    c = Chat()
    await c.card()
    c.stub.script = [{"state": "close", "actions": [dict(CREATE)]}]
    st = await c.say("hmm deixa eu ver")
    check("[repeat] the identical card is not sent twice", c.evo.cards() == 1)
    check("[repeat] and the window stays open for his answer",
          c.deps.sessions.is_open(OWNER_JID) and c.left() >= 295, f"{c.left()}s")
    await c.say("pode seguir")
    check("[repeat] his 'pode seguir' then books it", c.cal.n("create") == 1)


async def carry_checks() -> None:
    print("\nA fresh tag under a card he already said yes to — the shapes in the log")

    async def carried(c: Chat, tag: str) -> dict:
        calls = len(c.stub.calls)
        st = await c.say(tag)
        check(f"  ...{tag!r} booked it once", c.cal.n("create") == 1, f"{c.cal.n('create')}")
        check(f"  ...{tag!r} sent no second card", c.evo.cards() == 1, f"{c.evo.cards()} cards")
        check(f"  ...{tag!r} never asked the model", len(c.stub.calls) == calls)
        check(f"  ...{tag!r} replied with the booking", "Scheduled" in c.evo.sent[-1][1],
              c.evo.sent[-1][1][:60])
        return st

    # 11-02 Lucas: sim (dropped) → Lucas laughs → "@lisa pode seguir".
    c = Chat()
    await c.card()
    c.close_window()
    await c.say("sim")
    c.lucas("kkkkkkkkkkkkkkk que legal!")
    st = await carried(c, "@mary pode seguir")
    check("[lucas] the log says why", (st.get("carried_yes") or {}).get("card_id") == "lisa0")
    check("[lucas] the new loop waits the usual 60s after the booking", 55 <= c.left() <= 60,
          f"{c.left()}s")
    c.stub._classify_domain = "web"  # if the loop forgot its skill, the classifier sends it here
    st = await c.say("e o link do meet?")
    check("[lucas] a keyword-less follow-up stays on the calendar", st.get("domain") == "calendar",
          str(st.get("domain")))

    # 10-19 HSG / 10-24 Igor: sim (dropped, window ran out) → "@lisa agendar".
    c = Chat()
    await c.card()
    c.deps.sessions._exp[OWNER_JID] -= 400
    await c.say("sim")
    await carried(c, "@mary agendar")

    # 10-18 Abner: Abner says "Pode", then his sim → "@lisa pode agendar".
    c = Chat()
    await c.card()
    c.close_window()
    c.lucas("Pode")
    await c.say("sim")
    await carried(c, "@mary pode agendar")

    # The tag is the yes itself: card → "@lisa pode agendar", nothing in between.
    c = Chat()
    await c.card()
    c.close_window()
    await carried(c, "@mary pode agendar")

    # A change carries too — the one event id the resolved-id gate needs comes along.
    c = Chat()
    c.cal.responses["find"] = {"ok": True, "summary": "1 [id=E1]", "data": {"items": [
        {"event_id": "E1", "title": "Reunião", "start": START}]}}
    c.cal.responses["update"] = {"ok": True, "summary": "updated", "data": {
        "event_id": "E1", "title": "Reunião", "start": "2027-03-05T16:00:00-03:00"}}
    c.stub.script = [
        {"actions": [{"task": "calendar.find", "query": "reuniao"}]},
        {"actions": [{"task": "calendar.update", "event_id": "E1",
                      "start": "2027-03-05T16:00:00-03:00"}]}]
    await c.say("@mary mude a reunião para 16h")
    check("[update] the change card went out", c.evo.cards() == 1, str(c.evo.sent))
    c.close_window()
    await c.say("sim")
    calls = len(c.stub.calls)
    st = await c.say("@mary altera")
    check("[update] '@lisa altera' ran the change", c.cal.n("update") == 1, str(st.get("action_results")))
    check("[update] past the resolved-id gate, with no model call",
          (st.get("action_results") or [{}])[-1].get("ok") is True and len(c.stub.calls) == calls)


async def no_carry_checks() -> None:
    print("\nWhen the chat above is not a clean yes, the tag does what it did before")

    async def card_again(c: Chat, tag: str, why: str) -> None:
        c.stub.script = [{"actions": [dict(CREATE)]}]
        st = await c.say(tag)
        check(f"[{why}] {tag!r} booked nothing", c.cal.n("create") == 0)
        check(f"[{why}] the model read the chat and the card went out again", c.evo.cards() == 2,
              f"{c.evo.cards()} cards")
        check(f"[{why}] nothing was carried", st.get("carried_yes") is None)

    c = Chat()
    await c.card()
    c.close_window()
    await c.say("sim, mas às 16h")
    await card_again(c, "@mary agendar", "a correction")

    c = Chat()
    await c.card()
    c.close_window()
    await card_again(c, "@mary adicione o Zen", "a new detail in the tag")

    c = Chat()
    await c.card()
    c.close_window()
    c.lucas("sim")
    await card_again(c, "@mary agendar", "only Lucas said yes")

    c = Chat()
    await c.card()
    c.close_window()
    c.lucas("pode ser 16h?")
    await c.say("sim")
    await card_again(c, "@mary agendar", "Lucas spoke before his sim")

    c = Chat()
    await c.card()
    c.close_window()
    await card_again(c, "@mary agendar", "no yes anywhere")

    c = Chat()
    await c.card()
    c.close_window()
    await c.say("não precisa")
    await card_again(c, "@mary agendar", "he declined")


async def burst_checks() -> None:
    print("\nMessages that arrive together")

    # 10-28 Anibal: his sim landed in the same activation as a contact's "Boa !".
    c = Chat()
    await c.card()
    boa = c.lucas("Boa !")
    sim = c.mine("sim")
    calls = len(c.stub.calls)
    await c.say("Boa !", from_me=False, name="Lucas Santos", store=False, mid=boa)
    check("[together] his sim, ingested with Lucas's message, booked it", c.cal.n("create") == 1)
    check("[together] without a model call", len(c.stub.calls) == calls)
    await c.say("sim", store=False, mid=sim)  # the sim's own activation, a moment later
    check("[together] its own activation books nothing twice", c.cal.n("create") == 1)
    check("[together] and sends no card", c.evo.cards() == 1)

    # The dangerous shape: sim, then a correction, both read by a contact's activation. The model
    # re-proposes 16h; the sim's own activation must not approve a card he has not seen.
    c = Chat()
    await c.card()
    m = c.lucas("blz")
    sim = c.mine("sim")
    fix = c.mine("mas às 16h")
    c.stub.script = [{"actions": [{**CREATE, "start": "2027-03-05T16:00:00-03:00"}]}]
    await c.say("blz", from_me=False, name="Lucas Santos", store=False, mid=m)
    check("[sim + correction] read as a correction: a new card", c.evo.cards() == 2 and c.cal.n("create") == 0)
    await c.say("sim", store=False, mid=sim)
    await c.say("mas às 16h", store=False, mid=fix)
    check("[sim + correction] the old sim never approves the 16h card", c.cal.n("create") == 0)
    st = await c.say("sim")
    check("[sim + correction] his new sim does",
          c.cal.n("create") == 1 and c.cal.calls[-1][1].get("start", "").startswith("2027-03-05T16"),
          str(c.cal.calls[-1:]))
    check("[sim + correction] and nothing is left held", st.get("pending_action") is None)


async def refusal_checks() -> None:
    print("\nHe calls the card off in his own words — and his next ok runs nothing")

    # "não cancela", "pode deixar", "não, deixa": no word list catches every way of saying no, so
    # each is "other", the fixing loop, and the held write stays. The model can only answer him
    # ("ok, não vou cancelar"); it has no way to withdraw the write. His "ok" to that answer then
    # read as a yes to the card, and ran the write he had just refused.
    c = Chat()
    c.cal.responses["find"] = {"ok": True, "summary": "1 [id=E7]", "data": {"items": [
        {"event_id": "E7", "title": "Dentista", "start": START}]}}
    c.cal.responses["delete"] = {"ok": True, "summary": "cancelled", "data": {
        "event_id": "E7", "title": "Dentista", "start": START, "had_attendees": False}}
    c.stub.script = [{"actions": [{"task": "calendar.find", "query": "dentista"}],
                      "workflow": {"task": "calendar.delete", "known_inputs": []}}]
    st = await c.say("@mary cancela meu dentista")
    check("[não cancela] the cancellation card went out",
          c.evo.cards() == 1 and (st.get("pending_action") or {}).get("task") == "calendar.delete")
    c.stub.script = [{"message": "Ok, não vou cancelar."}]
    await c.say("não cancela")
    c.stub.script = [{"message": None}]
    st = await c.say("ok")
    check("[não cancela] his ok to her answer cancels nothing", c.cal.n("delete") == 0,
          str(st.get("action_results")))
    check("[não cancela] and sends no cancellation", not any("Cancelled" in t for _, t in c.evo.sent))

    for refusal in ("pode deixar", "não, deixa"):
        c = Chat()
        await c.card()
        c.stub.script = [{"message": "Tudo bem, não vou agendar."}]
        await c.say(refusal)
        c.stub.script = [{"message": None}]
        await c.say("ok")
        check(f"[{refusal}] his ok to her answer books nothing", c.cal.n("create") == 0)
        await c.say("sim")
        check(f"[{refusal}] nor does a sim after it", c.cal.n("create") == 0)

    # The same rule, from the other side: a yes after she answered a question is an answer to
    # her answer. The model reads it and asks again, and that card must go out — the repeat
    # suppressor would otherwise swallow it, and every yes after it would be read the same way.
    c = Chat()
    await c.card()
    c.stub.script = [{"message": "Fica na Arlene Menna Barreto 253."}]
    await c.say("qual o endereço mesmo?")
    c.stub.script = [{"actions": [dict(CREATE)]}]  # it re-proposes the same booking
    calls = len(c.stub.calls)
    await c.say("pode agendar")
    check("[after her answer] his yes goes to the model, not to the old card",
          c.cal.n("create") == 0 and len(c.stub.calls) == calls + 1)
    check("[after her answer] the same card goes out again", c.evo.cards() == 2,
          f"{c.evo.cards()} cards")
    st = await c.say("sim")
    check("[after her answer] his sim under it books it", c.cal.n("create") == 1)
    check("[after her answer] and nothing is left held", st.get("pending_action") is None)

    # A real correction still re-proposes, and his yes to the corrected card books that.
    c = Chat()
    await c.card()
    c.stub.script = [{"actions": [{**CREATE, "start": "2027-03-05T16:00:00-03:00"}]}]
    await c.say("sim mas às 16h")
    check("[correction] 'sim mas às 16h' re-proposes", c.evo.cards() == 2 and c.cal.n("create") == 0)
    await c.say("sim")
    check("[correction] and his sim books the 16h",
          c.cal.n("create") == 1 and c.cal.calls[-1][1].get("start", "").startswith("2027-03-05T16"))


def unit_checks() -> None:
    print("\nThe carry rules and the yes words — units")
    s = Settings(evolution_apikey="x")
    policy = confirm_policies()["calendar"]
    echoes = InMemoryEchoes(ttl=60)
    now = time.time()
    card_text = frame("Confirming:\n\nCall\n\nShall I book it?", s.owner_name, "en")

    def state(**over):
        base = {"pending_action": dict(CREATE), "remote_jid": OWNER_JID, "msg_id": "t1",
                "text": "@mary agendar",
                "pending_card": {"id": "c1", "ts": int(now) - 60, "text": card_text,
                                 "sig": action_fingerprint(CREATE)}}
        base.update(over)
        return base

    def recs(*after):
        out = [{"id": "c1", "from_me": True, "text": card_text}]
        out += [{"id": f"r{i}", "from_me": me, "text": t} for i, (me, t) in enumerate(after)]
        return out + [{"id": "t1", "from_me": True, "text": "@mary agendar"}]

    def why(st, records, **kw):
        return carried_yes(st, records, policy=policy, echoes=echoes, settings=s, **kw)[1]

    check("a clean sim under the card carries", why(state(), recs((True, "sim"))) == "ok")
    check("half an hour on, it does not", why(state(), recs((True, "sim")), now=now + 1800) == "stale")
    check("a booking that has started does not",
          why(state(pending_action={**CREATE, "start": "2020-01-01T10:00:00-03:00"},
                    pending_card={"id": "c1", "ts": int(now) - 60, "text": card_text,
                                  "sig": action_fingerprint({**CREATE, "start": "2020-01-01T10:00:00-03:00"})}),
              recs((True, "sim"))) == "started")
    gone = {"task": "calendar.delete", "event_id": "E1", "start": "2020-01-01T10:00:00-03:00"}
    check("a cancellation of a past event still can",
          why(state(pending_action=gone, text="@mary cancela",
                    pending_card={"id": "c1", "ts": int(now) - 60, "text": card_text,
                                  "sig": action_fingerprint(gone)}),
              recs((True, "sim"))) == "ok")
    check("...and under it, '@lisa agendar' is not its command word",
          why(state(pending_action=gone, pending_card={"id": "c1", "ts": int(now) - 60,
                                                       "text": card_text,
                                                       "sig": action_fingerprint(gone)}),
              recs((True, "sim"))) == "tag_other")
    check("a card about another write does not vouch for this one",
          why(state(pending_action={**CREATE, "title": "Other"}), recs((True, "sim"))) == "card_mismatch")
    check("a card no longer in the fetched chat does not",
          why(state(), recs((True, "sim"))[1:]) == "card_not_found")
    check("without an id, the card is found by its text",
          why(state(pending_card={"id": None, "ts": int(now) - 60, "text": card_text,
                                  "sig": action_fingerprint(CREATE)}),
              recs((True, "sim"))) == "ok")
    check("anything Lisa said after the card stops it",
          why(state(), recs((True, "sim"), (True, frame("Something else", s.owner_name, "en"))))
          == "lisa_spoke_after")
    check("an image of his after the card stops it", why(state(), recs((True, ""))) == "owner_said_other")
    check("only the calendar carries",
          why(state(pending_action={"task": "setup.enroll", "chat_key": "x"}), recs((True, "sim")))
          == "not_carryable")
    check("no card, no carry", why(state(pending_card=None), recs((True, "sim"))) == "no_card")
    check("'@lisa' alone, after his sim, carries",
          why(state(text="@mary"), recs((True, "sim"))) == "ok")

    upd = {"task": "calendar.update", "event_id": "E1", "start": START}
    kept = kept_on_carry({"pending_action": upd, "seen_event_ids": ["E1", "E2"],
                          "seen_events": {"E1": {"title": "a"}, "E2": {"title": "b"}},
                          "session_lang": "pt"})
    check("a carried change keeps exactly its own event id",
          kept["seen_event_ids"] == ["E1"] and list(kept["seen_events"]) == ["E1"])
    check("and the card's language", kept["session_lang"] == "pt")
    check("an id that was never found is not invented into the gate",
          "seen_event_ids" not in kept_on_carry({"pending_action": upd, "seen_event_ids": []}))

    for text in ("pode seguir", "segue", "vai em frente", "pode ir", "bora", "sim, pode"):
        check(f"{text!r} is a yes", classify_confirmation(text) == "yes")
    check("'agendar' alone is a yes to a booking card", classify_confirmation("agendar", "create") == "yes")
    check("...but not on its own, card unknown", classify_confirmation("agendar") == "other")
    check("'cancela' is a yes to a cancellation card", classify_confirmation("cancela", "delete") == "yes")
    check("...and never a yes to a booking card", classify_confirmation("cancela", "create") == "other")
    check("'muda pra 16h' stays a correction on a change card",
          classify_confirmation("muda pra 16h", "update") == "other")
    check("'não agenda' is no yes", classify_confirmation("não agenda", "create") != "yes")


async def main() -> None:
    unit_checks()
    await close_override_checks()
    await carry_checks()
    await no_carry_checks()
    await burst_checks()
    await refusal_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
