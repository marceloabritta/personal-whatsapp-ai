"""End-to-end verification for voice-note transcription — drives the real compiled graph
with an in-memory checkpointer, a stub transcriber, and a fake Evolution. No network, no
Postgres, no AssemblyAI key.

    cd "Lisa flow" && python tests/run_transcription.py
Exits non-zero on the first failed check.

Covers: the intent matcher, the deterministic fast path (short/long/failure, NO model call),
passive window ingestion + the transcript cache, the compositional slow-lane injection, and
provenance (the "(voice message — transcribed)" annotation + source="audio" in the log)."""
from __future__ import annotations

import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.cache import TranscriptionService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.intent import classify_transcribe  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402
from app.transcription import build_transcriber  # noqa: E402
from app.transcription.assemblyai import AssemblyAITranscriber  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"
_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _checks["pass" if cond else "fail"] += 1


class StubReasoner:
    def __init__(self) -> None:
        self.calls: list = []

    async def respond(self, *, system, messages):
        self.calls.append({"system": system, "messages": list(messages)})
        return {"state": "keep_listening", "message": None, "lang": "en",
                "usage": {}, "provider_request_id": "req", "stop_reason": "end_turn",
                "tool_calls": [], "error_category": "none"}


class StubTranscriber:
    """Maps audio bytes back to the wa id the FakeEvolution encoded, so results are scriptable
    per id and the call log reveals cache hits (a cached id is never transcribed twice)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: dict[str, dict] = {}
        self.default = {"text": "hello world", "duration_sec": 5.0,
                        "language": "en", "error": None}

    async def transcribe(self, audio: bytes, *, mimetype, language):
        wa_id = audio.decode("utf-8", "ignore")
        self.calls.append(wa_id)
        return dict(self.results.get(wa_id, self.default))


class FakeEvolution:
    instance = "secretaria"

    def __init__(self, history=None, *, missing_media=False) -> None:
        self.sent: list = []
        self.media: list = []
        self.history = history or []
        self.missing_media = missing_media

    async def send_text(self, number, text):
        mid = f"echo{len(self.sent)}"
        self.sent.append((number, text))
        return mid

    async def send_media(self, number, *, mediatype, mimetype, media_b64, filename, caption):
        self.media.append({"number": number, "mediatype": mediatype, "mimetype": mimetype,
                           "media_b64": media_b64, "filename": filename, "caption": caption})
        return True

    async def get_media_base64(self, message_id, *, convert_to_mp4=False):
        if self.missing_media:
            return None
        # Encode the id itself as the "audio", so the stub transcriber can key on it.
        return {"base64": base64.b64encode(message_id.encode()).decode(), "mimetype": "audio/ogg"}

    async def fetch_history(self, jid):
        return list(self.history)


def rec(mid, text, *, from_me=True, ts=0, push="Tester", is_audio=False):
    return {"id": mid, "from_me": from_me, "text": text, "push_name": push, "ts": ts,
            "is_audio": is_audio}


def upsert(text, *, from_me=True, mid="m1", jid=OWNER_JID):
    return {"data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
                     "message": {"conversation": text},
                     "messageTimestamp": 1730000000, "pushName": "Tester"}}


def upsert_reply_audio(text, *, quoted_id, mid="r1", from_me=True, jid=OWNER_JID):
    """An extendedTextMessage reply whose contextInfo quotes an audioMessage."""
    return {"data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
        "message": {"extendedTextMessage": {"text": text, "contextInfo": {
            "stanzaId": quoted_id, "quotedMessage": {"audioMessage": {"seconds": 5}}}}},
        "messageTimestamp": 1730000000, "pushName": "Tester"}}


def make_env(history=None, *, settings=None, missing_media=False):
    settings = settings or Settings(evolution_apikey="x", assemblyai_api_key="k",
                                    mary_trigger_tag="@mary", loop_ttl_seconds=60,
                                    context_window_messages=30)
    evo = FakeEvolution(history, missing_media=missing_media)
    tr = StubTranscriber()
    reasoner = StubReasoner()
    svc = TranscriptionService(evo, tr, settings)
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=reasoner,
                transcription=svc, redis=None)
    graph = build_graph(deps, MemorySaver())
    return deps, evo, tr, reasoner, graph


async def invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


async def main() -> None:
    # ---- 0. the intent matcher (pure) ----
    print("0. intent matcher")
    tags = ["@mary"]
    check("'@mary transcribe' → transcribe",
          classify_transcribe("@mary transcribe", tags, "") == "transcribe")
    check("'@mary transcreve' (pt) → transcribe",
          classify_transcribe("@mary transcreve", tags, "") == "transcribe")
    check("'@mary transcrbe' (typo) → transcribe (fuzzy)",
          classify_transcribe("@mary transcrbe", tags, "") == "transcribe")
    check("'@mary schedule what he said' → compositional",
          classify_transcribe("@mary schedule what he said", tags, "") == "compositional")
    check("bare '@mary' + on_empty=True → transcribe",
          classify_transcribe("@mary", tags, "", on_empty=True) == "transcribe")
    check("bare '@mary' + on_empty=False → compositional",
          classify_transcribe("@mary", tags, "", on_empty=False) == "compositional")

    # ---- 1. fast path — short clip, NO model call ----
    print("1. fast path — short clip")
    deps, evo, tr, reasoner, graph = make_env(history=[])
    tr.results["aud1"] = {"text": "confirma a reunião de quinta", "duration_sec": 6.0,
                          "language": "pt", "error": None}
    await invoke(graph, upsert_reply_audio("@mary transcreve", quoted_id="aud1", mid="c1"))
    check("exactly one message sent", len(evo.sent) == 1)
    sent = evo.sent[-1][1]
    check("framed with PT header (detected language)",
          sent.startswith("*[Assistente IA do Marcelo]:*"))
    check("carries the transcript", "confirma a reunião de quinta" in sent)
    check("carries the transcribe prefix", "Aqui está o áudio transcrito:" in sent)
    check("reasoner was NEVER called", len(reasoner.calls) == 0)
    check("no media (short → inline)", len(evo.media) == 0)
    check("reply recorded as an echo", deps.echoes.is_ours(OWNER_JID, "echo0"))
    tx = [e for e in deps.trace.transcript if e.get("source") == "audio"]
    check("transcript logged with source=audio", len(tx) == 1)

    # ---- 2. fast path — long clip → .txt document ----
    print("2. fast path — long clip")
    deps, evo, tr, reasoner, graph = make_env(history=[])
    tr.results["aud2"] = {"text": "x" * 500, "duration_sec": 200.0, "language": "en", "error": None}
    await invoke(graph, upsert_reply_audio("@mary transcribe", quoted_id="aud2", mid="c2"))
    check("delivered as a media document", len(evo.media) == 1)
    check("filename is the transcript .txt", evo.media[-1]["filename"] == "audio-transcript.txt")
    decoded = base64.b64decode(evo.media[-1]["media_b64"]).decode()
    check("document body is the transcript", decoded == "x" * 500)
    check("no inline text send", len(evo.sent) == 0)
    check("reasoner was NEVER called", len(reasoner.calls) == 0)

    # ---- 3. fast path — transcription failure is honest ----
    print("3. fast path — provider failure")
    deps, evo, tr, reasoner, graph = make_env(history=[])
    tr.results["aud3"] = {"text": "", "error": "provider"}
    await invoke(graph, upsert_reply_audio("@mary transcribe", quoted_id="aud3", mid="c3"))
    sent = evo.sent[-1][1]
    check("one honest failure message sent", len(evo.sent) == 1)
    check("says it couldn't transcribe", "couldn't transcribe" in sent)
    check("no fabricated transcript", "x" * 10 not in sent)
    check("reasoner was NEVER called", len(reasoner.calls) == 0)

    # ---- 4. passive window audio → heard + annotated ----
    print("4. passive — window audio")
    deps, evo, tr, reasoner, graph = make_env(history=[
        rec("w1", "", from_me=False, push="João", is_audio=True),
        rec("t1", "@mary resolve isso"),
    ])
    tr.results["w1"] = {"text": "marca almoço terça 13h", "duration_sec": 8.0,
                        "language": "pt", "error": None}
    await invoke(graph, upsert("@mary resolve isso", mid="t1"))
    check("reasoner was called (slow lane)", len(reasoner.calls) == 1)
    seed = " ".join(m["content"] for m in reasoner.calls[-1]["messages"])
    check("transcript folded into context", "marca almoço terça 13h" in seed)
    check("line annotated as transcribed voice", "(voice message — transcribed)" in seed)
    audio_logs = [e for e in deps.trace.transcript if e.get("source") == "audio"]
    check("audio line logged with source=audio", any(a.get("wa_id") == "w1" for a in audio_logs))

    # ---- 5. the transcript cache — an id is transcribed at most once ----
    print("5. transcript cache")
    _, _, tr5, _, _ = make_env(history=[])
    svc = TranscriptionService(FakeEvolution([]), tr5, Settings(assemblyai_api_key="k"))
    r1 = await svc.get("dup")
    r2 = await svc.get("dup")
    check("second get is a cache hit (one transcribe call)", tr5.calls.count("dup") == 1)
    check("both gets return the same text", r1["text"] == r2["text"])

    # ---- 6. compositional reply-to-audio → slow lane, injected line ----
    print("6. compositional reply-to-audio")
    deps, evo, tr, reasoner, graph = make_env(history=[])
    tr.results["aud6"] = {"text": "pode marcar o dentista", "duration_sec": 6.0,
                          "language": "pt", "error": None}
    await invoke(graph, upsert_reply_audio(
        "@mary agenda o que ele pediu", quoted_id="aud6", mid="c6"))
    check("reasoner was called (compositional → slow lane)", len(reasoner.calls) == 1)
    seed = " ".join(m["content"] for m in reasoner.calls[-1]["messages"])
    check("quoted audio transcribed into context", "pode marcar o dentista" in seed)
    check("injected as an explicit replied-to line", "replied to a voice message" in seed)
    check("no fast-path send happened", len(evo.sent) == 0 and len(evo.media) == 0)

    # ---- 7. graceful degrade — transcription disabled ----
    print("7. transcription disabled → placeholder, not a silent drop")
    off = Settings(evolution_apikey="x", transcription_enabled=False, mary_trigger_tag="@mary")
    deps, evo, tr, reasoner, graph = make_env(history=[
        rec("w7", "", from_me=False, push="João", is_audio=True),
        rec("t7", "@mary o que ele disse?"),
    ], settings=off)
    await invoke(graph, upsert("@mary o que ele disse?", mid="t7"))
    seed = " ".join(m["content"] for m in reasoner.calls[-1]["messages"])
    check("voice note shown as a placeholder", "[voice message]" in seed)
    check("transcriber never called when disabled", len(tr.calls) == 0)

    # ---- 8. provider-neutral seam ----
    print("8. provider-neutral factory")
    check("assemblyai resolves to its backend",
          isinstance(build_transcriber(Settings(transcription_provider="assemblyai")),
                     AssemblyAITranscriber))
    try:
        build_transcriber(Settings(transcription_provider="nope"))
        check("unknown provider raises", False)
    except ValueError:
        check("unknown provider raises", True)

    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
