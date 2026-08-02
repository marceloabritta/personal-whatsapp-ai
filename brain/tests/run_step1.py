"""End-to-end Step-1 verification — drives the real compiled graph with simulated
Evolution payloads and asserts the gate behaviour + the two-level trace.

No live Evolution: a recording stub stands in for the send client. Run with:
    cd brain && python tests/run_step1.py
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.identity import frame  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER = "5531999999999@s.whatsapp.net"
CONTACT_FROMME = False

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool) -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}")
    _checks["pass" if cond else "fail"] += 1


class StubEvolution:
    """Records send_text calls instead of hitting the network."""

    instance = "secretaria"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, number: str, text: str) -> bool:
        self.sent.append((number, text))
        return True


def upsert(text: str, *, from_me: bool, jid: str = OWNER, msg_id: str = "m1") -> dict:
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id},
            "message": {"conversation": text},
            "messageTimestamp": 1730000000,
            "pushName": "Tester",
        },
    }


def fresh_deps() -> tuple[Deps, StubEvolution]:
    settings = Settings(evolution_apikey="test", mary_trigger_tag="@mary")
    evo = StubEvolution()
    deps = Deps(
        settings=settings,
        evolution=evo,
        sessions=InMemorySessions(ttl=1800),
        trace=build_trace(),
    )
    return deps, evo


async def main() -> None:
    ack_body = "🌿 Mary here — listening."
    expected_reply = frame(ack_body)

    # 1. Owner @mary -> replies.
    print("1. owner sends '@mary hi'")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    st = await graph.ainvoke({"raw": upsert("@mary hi", from_me=True)})
    check("decision == ack", st["decision"] == "ack")
    check("trigger == tag", st["trigger"] == "tag")
    check("sent == True", st.get("sent") is True)
    check("one message sent", len(evo.sent) == 1)
    check("reply framed with ack body", evo.sent and evo.sent[0][1] == expected_reply)
    check("reply target is the number", evo.sent and evo.sent[0][0] == OWNER.split("@")[0])
    check("transcript has 'you' line", any(r["who"] == "you" for r in deps.trace.transcript))
    check("transcript has 'mary' line", any(r["who"] == "mary" for r in deps.trace.transcript))
    check("code trace recorded gate+send", any(e["node"] == "send" for e in deps.trace.events))

    # 2. Owner plain 'hi' -> silent.
    print("2. owner sends plain 'hi' (no tag, no session)")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    st = await graph.ainvoke({"raw": upsert("hi", from_me=True)})
    check("decision == stop", st["decision"] == "stop")
    check("nothing sent", len(evo.sent) == 0)
    check("no user transcript for chatter", len(deps.trace.transcript) == 0)

    # 3. Non-owner '@mary' -> ignored.
    print("3. a contact sends '@mary hi' (fromMe=False)")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    st = await graph.ainvoke(
        {"raw": upsert("@mary hi", from_me=False, jid="5531888888888@s.whatsapp.net")}
    )
    check("decision == stop", st["decision"] == "stop")
    check("nothing sent", len(evo.sent) == 0)

    # 4. Mary's own echoed reply -> ignored (no loop).
    print("4. Mary's own reply echoes back (fromMe=True, own header)")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    st = await graph.ainvoke({"raw": upsert(expected_reply, from_me=True)})
    check("is_own detected", st["is_own"] is True)
    check("decision == stop", st["decision"] == "stop")
    check("nothing sent (no loop)", len(evo.sent) == 0)

    # 5. Continuation: session open, owner untagged follow-up -> replies.
    print("5. after @mary opened a session, owner sends untagged 'and again'")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    await graph.ainvoke({"raw": upsert("@mary hi", from_me=True, msg_id="a")})
    st = await graph.ainvoke({"raw": upsert("and again", from_me=True, msg_id="b")})
    check("decision == ack", st["decision"] == "ack")
    check("trigger == session", st["trigger"] == "session")
    check("two replies total", len(evo.sent) == 2)

    # 6. Prefix trap: '@maryland' is a different word -> not a trigger.
    print("6. owner sends '@maryland is nice' (tag is a prefix but a different word)")
    deps, evo = fresh_deps()
    graph = build_graph(deps)
    st = await graph.ainvoke({"raw": upsert("@maryland is nice", from_me=True)})
    check("tag not matched", st.get("tag") is None)
    check("decision == stop", st["decision"] == "stop")
    check("nothing sent", len(evo.sent) == 0)

    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
