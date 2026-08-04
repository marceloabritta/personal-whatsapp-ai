"""Offline verification for image/PDF media context — drives the real compiled graph with an
in-memory checkpointer, a stub reasoner, and a fake Evolution that serves media base64. No
network, no Postgres, no Anthropic key.

    cd "Lisa flow" && python tests/run_media.py
Exits non-zero on the first failed check.

Covers: media_info() classification, the image/PDF → inline base64 block assembly in context,
the caption / caption-less marker + provenance annotation, and the failure/oversize/disabled
degradations (marker, no block — the turn never breaks)."""
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
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402
from app.transcription import build_transcriber  # noqa: E402
from app.whatsapp import media_info  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"
_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _checks["pass" if cond else "fail"] += 1


class StubReasoner:
    """Captures the messages it is handed so we can inspect the assembled user turn."""

    def __init__(self) -> None:
        self.calls: list = []

    async def respond(self, *, system, messages, output_schema=None, server_tools=None, model=None, effort=None, think=False):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return {"state": "keep_listening", "message": None, "lang": "en", "usage": {},
                "provider_request_id": "req", "stop_reason": "end_turn",
                "tool_calls": [], "error_category": "none"}

    async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
        return {"domain": "web"}


class StubTranscriber:
    async def transcribe(self, audio, *, mimetype, language):
        return {"text": "", "duration_sec": 0.0, "language": "en", "error": "empty"}


class MediaEvolution:
    """Serves get_media_base64 from a per-id script: {id: (mimetype, decoded_bytes) | None}."""

    instance = "secretaria"

    def __init__(self, history=None, media=None) -> None:
        self.sent: list = []
        self.history = history or []
        self._media = media or {}

    async def send_text(self, number, text):
        self.sent.append((number, text))
        return f"echo{len(self.sent)}"

    async def get_media_base64(self, message_id, *, convert_to_mp4=False):
        spec = self._media.get(message_id)
        if not spec:
            return None
        mimetype, raw = spec
        return {"base64": base64.b64encode(raw).decode(), "mimetype": mimetype}

    async def fetch_history(self, jid):
        return list(self.history)


def media_rec(mid, *, media_type, mimetype=None, filename=None, text="",
              from_me=False, ts=0, push="João"):
    return {"id": mid, "from_me": from_me, "text": text, "push_name": push, "ts": ts,
            "is_audio": False, "media_type": media_type, "media_mimetype": mimetype,
            "media_filename": filename}


def tag_upsert(text="@mary look at these", *, mid="t1", jid=OWNER_JID):
    return {"data": {"key": {"remoteJid": jid, "fromMe": True, "id": mid},
                     "message": {"conversation": text},
                     "messageTimestamp": 1730000000, "pushName": "Tester"}}


def make_graph(history, media, *, media_enabled=True, item_bytes=15_000_000,
               budget=28_000_000, cap=8):
    settings = Settings(
        evolution_apikey="x", assemblyai_api_key="k", mary_trigger_tag="@mary",
        loop_ttl_seconds=60, context_window_messages=30, transcription_enabled=False,
        media_enabled=media_enabled, max_context_media=cap,
        media_max_item_bytes=item_bytes, media_request_budget_bytes=budget,
    )
    evo = MediaEvolution(history, media)
    reasoner = StubReasoner()
    svc = TranscriptionService(evo, StubTranscriber(), settings)
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=reasoner,
                transcription=svc, redis=None)
    return build_graph(deps, MemorySaver()), evo, reasoner


async def invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


def last_user_content(reasoner):
    """The content of the final user turn the reasoner was handed."""
    msgs = reasoner.calls[-1]["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    return users[-1]["content"]


def blocks_of(content, btype):
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == btype]


async def main() -> None:
    JPEG = b"\xff\xd8\xff\xe0jpegbytes"
    PDF = b"%PDF-1.4 pdfbytes"

    # ---- 0. media_info (pure) ----
    print("0. media_info classification")
    img = {"imageMessage": {"mimetype": "image/jpeg", "caption": "hi"}}
    pdf = {"documentMessage": {"mimetype": "application/pdf", "fileName": "report.pdf"}}
    pdf_cap = {"documentWithCaptionMessage": {"message": {"documentMessage": {
        "mimetype": "application/pdf", "fileName": "wrapped.pdf"}}}}
    docx = {"documentMessage": {"mimetype":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "fileName": "notes.docx"}}
    check("image → type image + mimetype", media_info(img) == {
        "type": "image", "mimetype": "image/jpeg", "filename": None})
    check("pdf document → filename + mimetype", media_info(pdf) == {
        "type": "document", "mimetype": "application/pdf", "filename": "report.pdf"})
    check("documentWithCaption → unwrapped", media_info(pdf_cap) == {
        "type": "document", "mimetype": "application/pdf", "filename": "wrapped.pdf"})
    check("docx → document but non-pdf mimetype", media_info(docx)["mimetype"] !=
          "application/pdf")

    # ---- 1. image + PDF in the window → inline base64 blocks ----
    print("1. image + PDF → blocks")
    history = [
        media_rec("img1", media_type="image", mimetype="image/jpeg", ts=1),
        media_rec("doc1", media_type="document", mimetype="application/pdf",
                  filename="report.pdf", ts=2),
        media_rec("txt1", media_type="text", text="see attached", ts=3),
    ]
    media = {"img1": ("image/jpeg", JPEG), "doc1": ("application/pdf", PDF)}
    graph, evo, reasoner = make_graph(history, media)
    await invoke(graph, tag_upsert())
    check("reasoner was called", len(reasoner.calls) == 1)
    content = last_user_content(reasoner)
    check("turn is a content list", isinstance(content, list))
    imgs, docs = blocks_of(content, "image"), blocks_of(content, "document")
    check("one image block", len(imgs) == 1)
    check("image is base64 with the served mimetype",
          imgs and imgs[0]["source"]["type"] == "base64" and
          imgs[0]["source"]["media_type"] == "image/jpeg" and
          base64.b64decode(imgs[0]["source"]["data"]) == JPEG)
    check("one document block", len(docs) == 1)
    check("document is application/pdf with the filename title",
          docs and docs[0]["source"]["media_type"] == "application/pdf" and
          docs[0].get("title") == "report.pdf" and
          base64.b64decode(docs[0]["source"]["data"]) == PDF)
    text0 = content[0]["text"] if content and content[0].get("type") == "text" else ""
    check("transcript is the first block", bool(text0))
    check("image line annotated + marked", "(image)" in text0 and "[image]" in text0)
    check("pdf line annotated + marked",
          "(PDF: report.pdf)" in text0 and "[PDF: report.pdf]" in text0)
    check("text row still present", "see attached" in text0)

    # ---- 2. caption is kept, no redundant marker ----
    print("2. caption preserved")
    history = [media_rec("img2", media_type="image", mimetype="image/png",
                         text="nice sunset", ts=1)]
    media = {"img2": ("image/png", JPEG)}
    graph, evo, reasoner = make_graph(history, media)
    await invoke(graph, tag_upsert())
    content = last_user_content(reasoner)
    text0 = content[0]["text"]
    check("caption survives as the line text", "nice sunset" in text0)
    check("no [image] marker when a caption exists", "[image]" not in text0)
    check("still one image block", len(blocks_of(content, "image")) == 1)

    # ---- 3. disabled → plain string turn, no blocks ----
    print("3. media_enabled=false")
    history = [media_rec("img3", media_type="image", mimetype="image/jpeg", ts=1)]
    graph, evo, reasoner = make_graph(history, {"img3": ("image/jpeg", JPEG)},
                                      media_enabled=False)
    await invoke(graph, tag_upsert())
    content = last_user_content(reasoner)
    check("turn is a plain string", isinstance(content, str))

    # ---- 4. download failure → marker, no block, turn intact ----
    print("4. download failure")
    history = [media_rec("gone", media_type="image", mimetype="image/jpeg", ts=1)]
    graph, evo, reasoner = make_graph(history, {})  # no media served → get returns None
    await invoke(graph, tag_upsert())
    content = last_user_content(reasoner)
    text0 = content if isinstance(content, str) else content[0]["text"]
    check("no image block on failure", len(blocks_of(content, "image")) == 0)
    check("marked unavailable in the transcript", "[image — unavailable]" in text0)

    # ---- 5. oversize → marker "too large", no block ----
    print("5. oversize file")
    history = [media_rec("big", media_type="document", mimetype="application/pdf",
                         filename="huge.pdf", ts=1)]
    graph, evo, reasoner = make_graph(history, {"big": ("application/pdf", PDF)},
                                      item_bytes=4)  # PDF bytes exceed 4
    await invoke(graph, tag_upsert())
    content = last_user_content(reasoner)
    text0 = content if isinstance(content, str) else content[0]["text"]
    check("no block for the oversized file", len(blocks_of(content, "document")) == 0)
    check("marked too large", "[PDF: huge.pdf — too large]" in text0)

    # ---- 6. a fresh tag keeps her own past reply as context (not filtered) ----
    # Regression: she transcribed an image, then a second @mary asked for a calculation. The
    # reset used to filter her reply out of the re-seed, so the old "transcribe" order looked
    # unanswered and she re-transcribed. Her framed reply must survive the reset, labeled
    # "AI Assistant", so she can see the transcribe is done.
    print("6. own reply kept on reset, labeled AI Assistant")
    ai_header = "[Marcelo's AI Assistant]:"
    history = [
        media_rec("img6", media_type="image", mimetype="image/jpeg",
                  text="@mary transcribe this", from_me=True, ts=1),
        # her prior reply, echoed back by Evolution as fromMe with the header stamp
        media_rec("airep6", media_type="text", from_me=True, ts=2,
                  text=f"*{ai_header}*\n\nHere is the transcript: 12 + 30"),
        media_rec("ask6", media_type="text", from_me=True, ts=3,
                  text="@mary now add those two numbers"),
    ]
    graph, evo, reasoner = make_graph(history, {"img6": ("image/jpeg", JPEG)})
    await invoke(graph, tag_upsert("@mary now add those two numbers", mid="ask6"))
    content = last_user_content(reasoner)
    text0 = content if isinstance(content, str) else content[0]["text"]
    check("her past reply survives the reset (not filtered)",
          "Here is the transcript: 12 + 30" in text0)
    check("her past reply is labeled AI Assistant",
          "AI Assistant: " in text0 and "AI Assistant: @mary" not in text0)
    check("owner's own lines are not labeled AI Assistant",
          "AI Assistant: @mary now add" not in text0)

    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
