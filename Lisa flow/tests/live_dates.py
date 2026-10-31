"""Live check — does the real model read each relative date from its own message's day?

The selftests prove the stamps and the date rule reach the prompt. Only the model can show it
follows them. Each scenario is a short chat built relative to NOW on the owner's clock, run through
the real graph up to the confirmation card: fake WhatsApp history, a stub calendar, the real
reasoner. Nothing is written anywhere, and no message is sent to anyone.

It needs ANTHROPIC_API_KEY and the model settings, so it runs on the droplet, in a throwaway
container from the lisa image with this folder mounted and the live service's env passed in
(the env never touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_dates.py

Exits non-zero when a scenario books the wrong day.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.prompt import _WEEKDAYS, now_in  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import confirm_policies, render_policies, resolve_gates  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_step3 import FakeEvolution, StubCalendar  # noqa: E402

CONTACT_JID = "5511900000001@s.whatsapp.net"


def settings() -> Settings:
    # Everything that reaches outside the process is switched off; the model settings and the
    # key come from the env, exactly as the live service has them.
    return Settings(evolution_apikey="x", redis_url=None, database_url=None,
                    contacts_enabled=False, review_enabled=False, media_enabled=False,
                    transcription_enabled=False, auto_transcribe_enabled=False)


async def run(history: list, tag_text: str, tag_ts: int) -> tuple[dict, str]:
    """One scenario → (the proposed action, or {}; what she sent into the chat)."""
    s = settings()
    evo = FakeEvolution(history)
    deps = Deps(settings=s, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=build_reasoner(s),
                redis=None, tools={"calendar": StubCalendar()},
                confirm_policies=confirm_policies(), render_policies=render_policies(),
                resolve_gates=resolve_gates())
    graph = build_graph(deps, MemorySaver())
    body = {"data": {
        "key": {"remoteJid": CONTACT_JID, "fromMe": True, "id": "TAG"},
        "message": {"conversation": tag_text},
        "messageTimestamp": tag_ts, "pushName": "Marcelo",
    }}
    config = {"configurable": {"thread_id": make_thread_id("secretaria", CONTACT_JID)}}
    st = await graph.ainvoke({"raw": body}, config=config)
    pending = st.get("pending_action") or {}
    said = evo.sent[-1][1] if evo.sent else ""
    return pending, said


def msg(mid: str, when: datetime, text: str, *, mine: bool) -> dict:
    return {"id": mid, "from_me": mine, "text": text,
            "push_name": None if mine else "Rodolfo", "ts": int(when.timestamp())}


async def main() -> int:
    tz, tag = Settings().calendar_timezone, settings().primary_tag
    now, _ = now_in(tz)
    today = now.date()
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    if now.hour >= 21:
        print("Too late in the day: the 'later today' slot would cross midnight. Run before 21:00.")
        return 2
    slot = now.hour + 2                                    # still ahead today

    def at(d, h, m=0) -> datetime:                         # day d, h:m on the owner's clock
        return now.replace(year=d.year, month=d.month, day=d.day, hour=h, minute=m,
                           second=0, microsecond=0)

    tag_ts = int((now - timedelta(minutes=1)).timestamp())
    passed_day = today - timedelta(days=2)
    passed_name = {0: "segunda", 1: "terça", 2: "quarta", 3: "quinta", 4: "sexta",
                   5: "sábado", 6: "domingo"}[passed_day.weekday()]

    scenarios = [
        ("yesterday's 'amanhã' is today", today, [
            msg("a1", at(yesterday, 18, 40), f"almoço amanhã então? {slot}h no New Dog", mine=False),
            msg("a2", at(yesterday, 18, 41), "fechado", mine=True),
        ], f"{tag} agendar"),
        ("today's 'amanhã' is tomorrow", tomorrow, [
            msg("b1", now - timedelta(minutes=20), f"almoço amanhã então? {slot}h no New Dog",
                mine=False),
            msg("b2", now - timedelta(minutes=19), "fechado", mine=True),
        ], f"{tag} agendar"),
        ("a request written late last night is for today", today, [
            msg("c1", at(yesterday, 23, 50), f"amanhã às {slot}h no New Dog?", mine=False),
            msg("c2", at(yesterday, 23, 52), "pode ser", mine=True),
        ], f"bom dia! {tag} marca"),
        (f"last week's '{passed_name}' has passed: ask, don't book it", None, [
            msg("d1", at(today - timedelta(days=9), 10, 0),
                f"vamos fazer {passed_name} às 15h? no New Dog", mine=False),
            msg("d2", at(today - timedelta(days=9), 10, 5), "fechado", mine=True),
        ], f"{tag} agendar"),
    ]

    failed = 0
    print(f"Owner's clock: {_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M} ({tz})\n")
    for name, want, history, tag_text in scenarios:
        pending, said = await run(history, tag_text, tag_ts)
        start = str(pending.get("start") or "")
        got = start[:10] or "(no booking proposed)"
        if want is None:
            # A date in the past must not be proposed; asking (or proposing a future day) is fine.
            ok = not start or start[:10] >= today.isoformat()
        else:
            ok = start[:10] == want.isoformat()
        failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       expected {want.isoformat() if want else 'a question, or a future day'}, "
              f"got {got}")
        if said:
            print("       sent: " + said.replace("\n", " / ")[:240])
        print()
    print(f"{len(scenarios) - failed}/{len(scenarios)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
