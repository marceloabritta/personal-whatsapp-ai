"""She stays on what she was called for — and reads each message once.

On 2026-11-10, in a chat with a friend, he tagged her to work out a return ("@lisa calcule o
retorno da cota jr no cenário descrito acima"). While she computed, the friend asked HIM about his
product: "Ta usando o que de LLM? / Modelo? / Opus 5.5?". She read those in order and let them
pass. One turn later "Modelo?" was put in front of her again, alone, as the newest line, right
under her own answer, and she told the chat which model she runs on.

Two things were wrong, and this file holds both:

  1. The replay. An activation reads everything since the cursor, so messages that pile up behind
     a slow turn are all read by the first activation in the queue. When the others ran, the race
     guard took their message for one Evolution had not stored yet and appended it again, and the
     cursor moved back onto it, so the next turn re-read everything after it. A loop now reads
     each message once, and a turn with nothing new ends with no model call.
  2. The rule. Nothing told her that what other people may bring her is only the thing he called
     her in for. The prompt now says so, and quotes the tagged message on every turn.

Drives the real compiled graph against a fake WhatsApp that stores messages the way Evolution
does. No network, no Postgres, no Anthropic key. Whether the real model follows the rule is
tests/live_topic.py.

    python tests/run_called_topic.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.nodes.context import CALLED_FOR_MAX, called_for_text, route_after_context  # noqa: E402
from app.prompt import build_system_prompt  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, confirm_policies, render_policies, resolve_gates, system_prompt_for,
)
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_step3 import StubCalendar, StubReasoner  # noqa: E402

_checks = {"pass": 0, "fail": 0}

DANIEL_JID = "5512990000001@s.whatsapp.net"
T0 = 1794357700          # 2026-11-10 21:41:40 in São Paulo
TAG = "@lisa calcule o retorno da cota jr no cenário descrito acima"
RULE = "What you are here for."


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class StoredEvolution:
    """A WhatsApp that keeps what was sent, like Evolution's database: `fetch_history` returns it
    oldest first, and her own messages land in it too."""
    instance = "secretaria"

    def __init__(self) -> None:
        self.sent: list = []
        self.history: list = []

    def store(self, mid, text, *, from_me, name="Daniel Frageri", ts=None, at=None) -> None:
        row = {"id": mid, "from_me": from_me, "text": text, "push_name": name,
               "ts": ts if ts is not None else T0 + len(self.history)}
        self.history.insert(len(self.history) if at is None else at, row)

    async def send_text(self, number, text, *, quoted=None):
        mid = f"lisa{len(self.sent)}"
        self.sent.append((number, text))
        self.store(mid, text, from_me=True, name="Marcelo")
        return mid

    async def fetch_history(self, jid):
        return list(self.history)


class Chat:
    """His chat with Daniel. `arrive` is a message landing in WhatsApp while a turn holds the
    chat's lock; `wake` is that message's own activation, when its turn in the queue comes."""

    def __init__(self) -> None:
        self.settings = Settings(evolution_apikey="x", MARY_TRIGGER_TAG="@lisa",
                                 loop_ttl_seconds=60, context_window_messages=30)
        self.evo = StoredEvolution()
        self.stub = StubReasoner(classify_domain="web")
        self.cal = StubCalendar()
        self.deps = Deps(settings=self.settings, evolution=self.evo,
                         sessions=InMemorySessions(ttl=60), echoes=InMemoryEchoes(ttl=3600),
                         trace=build_trace(), reasoner=self.stub, redis=None,
                         tools={"calendar": self.cal}, confirm_policies=confirm_policies(),
                         render_policies=render_policies(), resolve_gates=resolve_gates())
        self.graph = build_graph(self.deps, MemorySaver())
        self.rows: dict = {}

    def arrive(self, mid: str, text: str, *, me: bool = False, stored: bool = True,
               at: int | None = None, ts: int | None = None, message: dict | None = None,
               quoted: tuple | None = None) -> None:
        name = "Marcelo" if me else "Daniel Frageri"
        if stored:
            self.evo.store(mid, text, from_me=me, name=name, at=at, ts=ts)
        data = {"key": {"remoteJid": DANIEL_JID, "fromMe": me, "id": mid},
                "message": message or {"conversation": text},
                "messageTimestamp": ts if ts is not None else T0 + len(self.evo.history),
                "pushName": name}
        if quoted:
            data["contextInfo"] = {"stanzaId": quoted[0],
                                   "quotedMessage": {"conversation": quoted[1]}}
        self.rows[mid] = {"data": data}

    async def wake(self, mid: str) -> dict:
        config = {"configurable": {"thread_id": make_thread_id("secretaria", DANIEL_JID)}}
        return await self.graph.ainvoke({"raw": self.rows[mid]}, config=config)

    async def say(self, mid: str, text: str, **kw) -> dict:
        self.arrive(mid, text, **kw)
        return await self.wake(mid)

    def turn(self, n: int = -1) -> str:
        """The newest thing the model was shown on its n-th call."""
        last = self.stub.calls[n]["messages"][-1]
        content = last["content"] if isinstance(last, dict) else getattr(last, "content", "")
        return content if isinstance(content, str) else str(content)

    def shown(self, text: str) -> int:
        """How many times a line has been put in front of the model, over the whole thread."""
        thread = self.stub.calls[-1]["messages"]
        return sum(str(m["content"] if isinstance(m, dict) else getattr(m, "content", "")).count(text)
                   for m in thread)

    def records(self) -> list:
        return [e for e in self.deps.trace.events if e.get("node") == "record"]


async def the_night_of_the_tenth() -> None:
    """The chat as it went, message for message, with the queue it built up behind her."""
    print("\nThe 2026-11-10 chat, replayed")
    c = Chat()
    c.evo.store("h1", "o produto será um AI chatbot, tipo o chatGPT", from_me=True, name="Marcelo")
    c.evo.store("h2", "Sinistro", from_me=False)
    c.evo.store("h3", "só que os 800 rendem tipo CDI + 10%", from_me=True, name="Marcelo")

    # He tags her. She needs one thing before she can compute, and asks.
    c.stub.script = [{"state": "awaiting_answer", "lang": "pt",
                      "message": "Os 7-8% são ao mês ou ao ano?"}]
    st = await c.say("tag", TAG, me=True)
    check("the tag opens the loop and she asks her question", len(c.stub.calls) == 1
          and "ao mês ou ao ano" in c.evo.sent[-1][1])
    check("what he called her for is kept", st.get("called_for") == TAG, str(st.get("called_for")))

    # While that turn ran (48 s that night), seven messages landed. Their activations queue up.
    c.arrive("d1", "Ta usando o que de LLM?")
    c.arrive("d2", "Modelo?")
    c.arrive("d3", "Opus 5.5?")
    c.arrive("m1", "supondo que vc esteja lá girando 10 milhões por mes", me=True)
    c.arrive("m2", "vc coloca 2 e 8 vc pega emprestado", me=True)
    c.arrive("m3", "os 10 vao render 7% a.m.", me=True)
    c.arrive("m4", "mas os 8, vc vai pagar CDI + 8% ao ano, tipo. 22% a.a.", me=True)

    # The first of them reads all seven, in order, and she answers the sum.
    c.stub.script = [{"lang": "pt", "message": "Retorno da cota jr: 27,7% a.m."}]
    st = await c.wake("d1")
    seen = c.turn()
    check("the first queued turn reads everything that piled up, in order",
          seen.index("Ta usando o que de LLM?") < seen.index("Modelo?") < seen.index("Opus 5.5?")
          < seen.index("os 10 vao render"), detail=repr(seen))
    check("she answers the sum", "27,7%" in c.evo.sent[-1][1] and len(c.stub.calls) == 2)

    # Two more of his lines land while she computes.
    c.arrive("m5", "todo o excedente fica pro rendimento da cota junior", me=True)
    c.arrive("m6", "lisa 7-8% ao mes", me=True)

    # "Modelo?" wakes next. She has read it. That night it came back as the newest line here.
    st = await c.wake("d2")
    seen = c.turn()
    check("[Modelo?] its own turn reads only what is new", "todo o excedente" in seen
          and "lisa 7-8% ao mes" in seen, detail=repr(seen))
    check("[Modelo?] the question is not put in front of her a second time",
          "Modelo?" not in seen, detail=repr(seen))
    check("[Modelo?] the newest line is his, not the friend's old question",
          seen.rstrip().endswith("Marcelo: lisa 7-8% ao mes"), detail=repr(seen[-80:]))
    check("[Modelo?] the cursor is on the newest message, not back on the old one",
          st.get("last_whatsapp_message_id") == "m6", str(st.get("last_whatsapp_message_id")))

    c.arrive("d4", "Sim, o spread fica top")
    st = await c.wake("d3")
    check("[Opus 5.5?] the next turn reads only the one new line",
          st.get("context_message_ids") == ["d4"], str(st.get("context_message_ids")))
    calls = len(c.stub.calls)
    sent = len(c.evo.sent)

    # The rest of the queue. Every one of these was a model call that night, over the same lines.
    for mid in ("m1", "m2", "m3", "m4", "m5", "m6", "d4"):
        st = await c.wake(mid)
        if not st.get("nothing_new") or st.get("context_message_ids"):
            break
    check("a message an earlier turn already read wakes with nothing new",
          st.get("nothing_new") is True and st.get("context_message_ids") == [],
          f"{st.get('nothing_new')} {st.get('context_message_ids')}")
    check("...so there is no model call for it", len(c.stub.calls) == calls,
          f"{len(c.stub.calls)} calls, was {calls}")
    check("...and nothing is said", len(c.evo.sent) == sent)
    check("...the cursor stays on the newest message", st.get("last_whatsapp_message_id") == "d4",
          str(st.get("last_whatsapp_message_id")))
    check("...and the loop stays open", c.deps.sessions.is_open(DANIEL_JID))
    check("four model calls in all, where that night took ten", len(c.stub.calls) == 4,
          str(len(c.stub.calls)))
    held = c.records()[-1]
    check("the silent turn is on the record as silent, with no tokens of its own",
          held.get("delivery_result") == "silent" and held.get("response") is None
          and held.get("input_tokens") is None and held.get("provider_request_id") is None,
          detail=str({k: held.get(k) for k in ("delivery_result", "response", "input_tokens")}))
    from app.review.store import _nothing_decided
    graded = [r for r in c.records() if not _nothing_decided(r)]
    check("the review grades the four turns she took, not the seven she was never asked",
          len(c.records()) == 11 and len(graded) == 4, f"{len(graded)} of {len(c.records())}")

    # A new message after all that is read once, alone.
    c.stub.script = [{"lang": "pt", "message": None}]
    st = await c.say("m7", "e se a inadimplência for 5%?", me=True)
    check("the next new message is read alone", st.get("context_message_ids") == ["m7"],
          str(st.get("context_message_ids")))
    for line in ("Modelo?", "Ta usando o que de LLM?", "Opus 5.5?", "lisa 7-8% ao mes",
                 "Sim, o spread fica top"):
        check(f"over the whole loop she was shown {line!r} once", c.shown(line) == 1,
              str(c.shown(line)))
    system = str(c.stub.calls[-1]["system"])
    check("every turn of the loop is told what he called her for",
          all(f'with this message: "{TAG}"' in str(call["system"]) for call in c.stub.calls))
    check("...after the reusable part of the prompt, with the rule itself inside it",
          system.index(RULE) < c.stub.calls[-1]["system"].stable_ends[0]
          < c.stub.calls[-1]["system"].stable_ends[1] < system.index("with this message:"))


async def the_race_guard_still_guards() -> None:
    print("\nThe race guard — a message Evolution has not stored yet")
    c = Chat()
    c.stub.script = [{"message": "ok"}]
    await c.say("tag", TAG, me=True)

    # The webhook beat the database: the message is not in the history when its turn runs.
    c.stub.script = [{"message": None}]
    st = await c.say("r1", "e com 5%?", me=True, stored=False)
    check("a message not stored yet is still read", "e com 5%?" in c.turn()
          and st.get("context_message_ids") == ["r1"], detail=repr(c.turn()))
    check("the cursor moves onto it", st.get("last_whatsapp_message_id") == "r1")

    # By the next turn it is stored, with a newer one after it.
    c.evo.store("r1", "e com 5%?", from_me=True, name="Marcelo")
    c.stub.script = [{"message": None}]
    st = await c.say("r2", "e com 6%?", me=True)
    check("once stored it is not read again", st.get("context_message_ids") == ["r2"]
          and c.shown("e com 5%?") == 1, str(st.get("context_message_ids")))

    # Two messages in one second can sort either way round. One stored BEFORE the cursor that
    # no turn has read is still read, and the cursor does not move back to it.
    c.arrive("x1", "espera, e o custo da senior?", me=True, at=len(c.evo.history) - 1)
    c.stub.script = [{"message": None}]
    st = await c.wake("x1")
    check("a stored message no turn has read is read, wherever it sorts",
          "e o custo da senior?" in c.turn() and st.get("context_message_ids") == ["x1"],
          detail=repr(c.turn()))
    check("...and the cursor does not move back to it", st.get("last_whatsapp_message_id") == "r2",
          str(st.get("last_whatsapp_message_id")))
    c.stub.script = [{"message": None}]
    st = await c.say("r3", "pronto", me=True)
    check("...so the turn after it re-reads nothing", st.get("context_message_ids") == ["r3"]
          and c.shown("e com 6%?") == 1, str(st.get("context_message_ids")))


async def nothing_she_can_read() -> None:
    print("\nA message with nothing in it for her")
    c = Chat()
    c.stub.script = [{"state": "awaiting_answer", "message": "Ao mês ou ao ano?"}]
    await c.say("tag", TAG, me=True)
    left = c.deps.sessions.ttl_left(DANIEL_JID)
    calls = len(c.stub.calls)
    c.deps.sessions._exp[DANIEL_JID] -= 100

    # A reaction carries no text and no file. It used to cost a model call over an unchanged thread.
    st = await c.say("rx", "", message={"reactionMessage": {"text": "👍", "key": {"id": "tag"}}})
    check("a reaction is nothing new", st.get("nothing_new") is True)
    check("...no model call, nothing said", len(c.stub.calls) == calls and len(c.evo.sent) == 1)
    check("...and it does not cut short the wait for his answer",
          c.deps.sessions.ttl_left(DANIEL_JID) >= left - 105,
          f"{c.deps.sessions.ttl_left(DANIEL_JID)}s of {left}s")
    check("a turn with something to read is not held",
          route_after_context({"nothing_new": False}) == "resolve"
          and route_after_context({}) == "resolve"
          and route_after_context({"nothing_new": True}) == "hold")

    # A tag is never "nothing new", even when an earlier turn has read the message.
    c.arrive("m1", "outra coisa", me=True)
    c.arrive("tag2", "@lisa quanto é 2+2?", me=True)
    c.stub.script = [{"message": None}]
    await c.wake("m1")                       # reads m1 AND the tagged message behind it
    c.stub.script = [{"message": "4"}]
    st = await c.wake("tag2")
    check("a tag an earlier turn read still opens its own loop and is answered",
          st.get("nothing_new") is False and c.evo.sent[-1][1].endswith("4")
          and st.get("called_for") == "@lisa quanto é 2+2?", str(st.get("called_for")))
    check("...and the new loop is told its own request, not the last one's",
          'with this message: "@lisa quanto é 2+2?"' in str(c.stub.calls[-1]["system"])
          and TAG not in str(c.stub.calls[-1]["system"]))


async def a_reply_brings_its_pointer() -> None:
    print("\nA WhatsApp reply an earlier turn already read")
    c = Chat()
    c.evo.store("old", "o contrato novo é de 2 milhões", from_me=False)
    c.stub.script = [{"message": "ok"}]
    await c.say("tag", TAG, me=True)

    # Two messages queue. The first one's turn reads both, but only a message's own activation
    # knows what it is a reply to.
    c.arrive("q1", "mais uma coisa", me=True)
    c.arrive("q2", "usa esse valor", me=True, quoted=("old", "o contrato novo é de 2 milhões"))
    c.stub.script = [{"message": None}]
    await c.wake("q1")
    check("the first turn read the reply as a plain line", "usa esse valor" in c.turn()
          and "is replying to" not in c.turn(), detail=repr(c.turn()))
    c.stub.script = [{"message": None}]
    st = await c.wake("q2")
    seen = c.turn()
    check("its own turn shows it again, with what it points at",
          "Marcelo: usa esse valor" in seen and '[Marcelo is replying to this message from '
          'Daniel Frageri' in seen and '"o contrato novo é de 2 milhões"]' in seen,
          detail=repr(seen))
    check("...without moving the cursor back", st.get("last_whatsapp_message_id") == "q2",
          str(st.get("last_whatsapp_message_id")))


async def a_yes_is_run_once() -> None:
    """The case the owner-only guard (`seen_owner_ids`) was written for, now through the same door."""
    print("\nA yes read ahead of its own turn")
    c = Chat()
    c.stub = c.deps.reasoner = StubReasoner(classify_domain="calendar")
    c.graph = build_graph(c.deps, MemorySaver())
    c.cal.responses["create"] = {"ok": True, "summary": "created", "data": {
        "title": "Call", "start": "2026-11-12T15:00:00-03:00", "html_link": "http://cal/1"}}
    c.stub.script = [{"state": "awaiting_answer", "message": None, "actions": [
        {"task": "calendar.create", "title": "Call", "start": "2026-11-12T15:00:00-03:00"}]}]
    st = await c.say("tag", "@lisa schedule a call thursday at 3pm", me=True)
    check("the card is out and the write is held", bool(st.get("pending_action")))
    c.arrive("b1", "Boa !")
    c.arrive("y1", "sim", me=True)
    st = await c.wake("b1")
    check("the friend's turn carries his yes in, and the write runs", c.cal.n("create") == 1)
    sent = len(c.evo.sent)
    st = await c.wake("y1")
    check("the yes's own turn finds nothing new", st.get("nothing_new") is True)
    check("...and books nothing twice, says nothing twice",
          c.cal.n("create") == 1 and len(c.evo.sent) == sent, f"{c.cal.n('create')} creates")


def prompt_checks() -> None:
    print("\nThe prompt — what she is here for")
    s = Settings(evolution_apikey="x", contacts_enabled=True, contacts_crud_enabled=True)
    for name in SKILLS:
        plain = system_prompt_for(name, s)
        told = system_prompt_for(name, s, called_for=TAG, session_lang="pt")
        text = str(told)
        check(f"[{name}] the rule is in the part of the prompt every chat shares",
              0 < text.find(RULE) < told.stable_ends[0])
        check(f"[{name}] the tagged message is quoted, outside both reusable prefixes",
              text.find(f'with this message: "{TAG}"') > told.stable_ends[1])
        check(f"[{name}] quoting it changes neither prefix",
              (text[:told.stable_ends[0]], text[:told.stable_ends[1]])
              == (str(plain)[:plain.stable_ends[0]], str(plain)[:plain.stable_ends[1]]))
        check(f"[{name}] it comes after the language lock and before the date lines",
              text.find('This conversation is in "pt"') < text.find("with this message:")
              < text.find("Current date and time:"))
        check(f"[{name}] with no tagged message there is no such line",
              "called you into this conversation with this message" not in str(plain))

    p = build_system_prompt("Marcelo", "@lisa")
    check("other people may talk to her about that subject, variations included",
          "The other people in the chat can talk to you about that subject" in p
          and "ask for a variation on it" in " ".join(p.split()))
    check("what he asks her himself later is hers too",
          "once he has, it is part of what you are here for." in p)
    check("anything else from them is not hers, by name or not",
          "Anything else from them is not yours to answer" in p
          and "even when they call you by name" in p)
    check("a question about herself is named as one of those", "which model runs you" in p)
    check("she stays silent rather than declining out loud",
          "Say nothing and keep listening; do not explain why." in p)
    check("only he gives her a new subject",
          "A new subject is yours only when he puts it to you himself" in p)
    check("a question belongs to whoever it was put to",
          "A question belongs to whoever it was put to." in p
          and "because it comes right after something you said" in p)
    check("speaking up 'when the conversation needs you' is gone",
          "when the conversation clearly needs you" not in p
          and "when the request you are working on clearly needs you to" in p)

    check("the tagged message is kept as one line",
          called_for_text("@lisa  agendar\n\namanhã 13h ") == "@lisa agendar amanhã 13h")
    long = called_for_text("@lisa " + "muito " * 200)
    check("a very long one is cut, and says so",
          len(long) == CALLED_FOR_MAX + 1 and long.endswith("…"), str(len(long)))
    check("an empty one is no line at all", called_for_text("") is None
          and called_for_text(None) is None)


def _finish() -> None:
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    prompt_checks()
    asyncio.run(the_night_of_the_tenth())
    asyncio.run(the_race_guard_still_guards())
    asyncio.run(nothing_she_can_read())
    asyncio.run(a_reply_brings_its_pointer())
    asyncio.run(a_yes_is_run_once())
    _finish()
