"""Live check — does the real model set the presets in setup, and book with them?

run_presets.py proves the code around the model: the store, the cards, how a preset is written into
a booking. Only the model can show it fills the new setup fields from what he says, and turns
"na minha casa" into the address he saved. Each scenario runs the real graph with the real reasoner
against a fake WhatsApp, a fake Google Calendar and an in-memory presets store. Nothing is written
anywhere, and no message is sent to anyone.

It needs ANTHROPIC_API_KEY and the model settings, so it runs on the droplet, in a throwaway
container from the lisa image with this folder mounted and the live service's env passed in
(the env never touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_presets.py

LIVE_REPEAT (default 3) runs each scenario that many times, since one run of a model proves
little. Exits non-zero when any run fails.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from app.config import Settings  # noqa: E402
from app.presets import fold  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from run_presets import (  # noqa: E402
    ANA_JID, OWNER_JID, FakeGoogle, _Events, _Req, build, inserted, invoke, presets_with,
)

ADDRESS = "Rua Harmonia 123, Vila Madalena"
REPEAT = int(os.environ.get("LIVE_REPEAT", "3"))


class _ListingEvents(_Events):
    """The fake calendar, plus an empty agenda in case the model looks before it books."""

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Req({"items": []})


def settings() -> Settings:
    # Everything that reaches outside the process is off; the model settings and the key come
    # from the env, exactly as the live service has them. Setup is as it is live, plus presets.
    return Settings(evolution_apikey="x", redis_url=None, database_url=None,
                    MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, presets_enabled=True,
                    contacts_enabled=False, contacts_crud_enabled=False, review_enabled=False,
                    media_enabled=False, transcription_enabled=False,
                    auto_transcribe_enabled=True)


def fresh(presets=None):
    s = settings()
    deps, graph, ev = build(s, reasoner=build_reasoner(s), presets=presets)
    google = FakeGoogle()
    google._events = _ListingEvents()
    deps.tools["calendar"]._svc = google
    ev.history[OWNER_JID] = []
    ev.history[ANA_JID] = []
    return deps, graph


_n = 0


async def say(graph, text: str, jid: str = OWNER_JID) -> dict:
    global _n
    _n += 1
    body = {"data": {"key": {"id": f"LIVE{_n}", "remoteJid": jid, "fromMe": True},
                     "message": {"conversation": text}, "messageTimestamp": int(time.time()),
                     "pushName": "Marcelo"}}
    return await invoke(graph, body, jid=jid)


def held(st: dict) -> dict:
    return st.get("pending_action") or {}


def minutes_between(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    except Exception:
        return None


# ============================ setup, in his chat with himself ================================

async def set_length_and_video():
    deps, graph = fresh()
    st = await say(graph, "@lisa setup reuniões de 30 minutos e sem Google Meet")
    p = held(st)
    card = (p.get("task") == "setup.set_presets" and p.get("meeting_minutes") == 30
            and p.get("video_call") is False)
    await say(graph, "sim")
    pr = deps.presets
    saved = pr.meeting_minutes() == 30 and pr.video_call() is False and pr.notify_guests() is True
    return card and saved, f"held={p}"


async def guest_emails_off():
    deps, graph = fresh()
    st = await say(graph, "@lisa setup não quero que os convidados recebam e-mail por padrão")
    p = held(st)
    card = p.get("task") == "setup.set_presets" and p.get("notify_guests") is False
    await say(graph, "sim")
    pr = deps.presets
    saved = (pr.notify_guests() is False and pr.meeting_minutes() == 45
             and pr.video_call() is True)
    return card and saved, f"held={p}"


async def save_a_place():
    deps, graph = fresh()
    st = await say(graph, f"@lisa setup salva o endereço casa: {ADDRESS}")
    p = held(st)
    card = (p.get("task") == "setup.save_place" and fold(p.get("name") or "") == "casa"
            and (p.get("address") or "").strip() == ADDRESS)
    await say(graph, "salva")
    saved = (deps.presets.place("casa") or {}).get("address") == ADDRESS
    return card and saved, f"held={p} places={deps.presets.places()}"


async def remove_a_place():
    deps, graph = fresh(await presets_with(places={"casa": ADDRESS, "escritório": "Av. Paulista 1000"}))
    st = await say(graph, "@lisa setup apaga o endereço casa")
    p = held(st)
    card = p.get("task") == "setup.remove_place" and fold(p.get("name") or "") == "casa"
    await say(graph, "sim")
    names = [x["name"] for x in deps.presets.places()]
    return card and names == ["escritório"], f"held={p} places={names}"


async def menu_then_meetings():
    deps, graph = fresh()
    st = await say(graph, "@lisa setup")
    menu = st.get("reply") or ""
    listed = "Reuniões" in menu or "Meetings" in menu
    st = await say(graph, "2")
    shown = st.get("reply") or ""
    meetings = "45 min" in shown and ("Duração padrão" in shown or "Standard length" in shown)
    return listed and meetings, f"menu={menu!r} then={shown!r}"


async def untagged_answer_stays_in_setup():
    deps, graph = fresh()
    await say(graph, "@lisa setup")
    st = await say(graph, "reuniões de 40 min")
    p = held(st)
    ok = (st.get("domain") == "setup" and p.get("task") == "setup.set_presets"
          and p.get("meeting_minutes") == 40)
    return ok, f"domain={st.get('domain')} held={p}"


# ============================ booking, in a chat with someone else ===========================

async def _all_set():
    return await presets_with(meeting_minutes=30, video_call=False, notify_guests=False,
                              places={"casa": ADDRESS})


async def book_at_saved_place():
    deps, graph = fresh(await _all_set())
    st = await say(graph, "@lisa marca um almoço amanhã às 13h na minha casa", jid=ANA_JID)
    p = held(st)
    length = p.get("duration_min") or minutes_between(p.get("start"), p.get("end"))
    card = (p.get("task") == "calendar.create" and p.get("location") == ADDRESS
            and length == 30 and p.get("virtual") is not True and p.get("send_invites") is False
            and ADDRESS in (st.get("reply") or ""))
    await say(graph, "sim", jid=ANA_JID)
    ins = inserted(deps)
    body = ins.get("body") or {}
    start = (body.get("start") or {}).get("dateTime")
    end = (body.get("end") or {}).get("dateTime")
    event = (body.get("location") == ADDRESS and minutes_between(start, end) == 30
             and not (body.get("conferenceData") or {}).get("createRequest")
             and ins.get("sendUpdates") == "none")
    return card and event, f"held={p} location={body.get('location')} {start}->{end} " \
                           f"sendUpdates={ins.get('sendUpdates')}"


async def ask_for_meet_anyway():
    deps, graph = fresh(await _all_set())
    st = await say(graph, "@lisa marca uma call amanhã às 10h, com link do Meet", jid=ANA_JID)
    p = held(st)
    return p.get("task") == "calendar.create" and p.get("virtual") is True, f"held={p}"


async def nothing_set_books_as_before():
    deps, graph = fresh()
    st = await say(graph, "@lisa marca uma reunião amanhã às 15h", jid=ANA_JID)
    p = held(st)
    card = (p.get("task") == "calendar.create" and p.get("virtual") is not False
            and p.get("send_invites") is not False)
    await say(graph, "sim", jid=ANA_JID)
    ins = inserted(deps)
    body = ins.get("body") or {}
    start = (body.get("start") or {}).get("dateTime")
    end = (body.get("end") or {}).get("dateTime")
    event = (bool((body.get("conferenceData") or {}).get("createRequest"))
             and minutes_between(start, end) == 45)
    return card and event, f"held={p} {start}->{end} meet={bool(body.get('conferenceData'))}"


async def default_asked_of_the_calendar():
    """Found on the first live run: a default asked outside setup got "Combinado, a partir de
    agora não envio" from the calendar, with nothing saved. It must point to setup instead."""
    deps, graph = fresh()
    st = await say(graph, "@lisa a partir de agora não manda mais e-mail pros convidados",
                   jid=ANA_JID)
    reply = st.get("reply") or ""
    ok = (not held(st) and "setup" in reply.lower() and deps.presets.notify_guests() is True)
    return ok, f"domain={st.get('domain')} held={held(st)} reply={reply!r}"


SCENARIOS = [
    set_length_and_video, guest_emails_off, save_a_place, remove_a_place, menu_then_meetings,
    untagged_answer_stays_in_setup, book_at_saved_place, ask_for_meet_anyway,
    nothing_set_books_as_before, default_asked_of_the_calendar,
]


async def _one(fn):
    try:
        return await fn()
    except Exception as exc:  # a crash is a failed run, with its reason
        return False, f"raised {type(exc).__name__}: {exc}"


async def main() -> int:
    failed = 0
    for fn in SCENARIOS:
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
