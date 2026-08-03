"""transcribe — the deterministic fast path (NO model call).

Runs only when `parse` recognised a pure transcribe request: the owner replied to a voice
note with the tag and nothing but a transcribe verb. Download the audio, transcribe it, and
send exactly one message — inline for a short clip, a .txt document for a long one. One-shot:
no listening window is opened, mirroring the legacy skill's "exactly one message".

The transcript is what the owner asked for, so it is delivered verbatim under the framed
header. Failures are reported honestly — never a fabricated transcript."""
from __future__ import annotations

import base64

from ..identity import frame
from ..state import MessageState
from ..trace import Trace

# Per-language reply copy (matched to the transcript's detected language; en fallback).
_MSG = {
    "en": {
        "prefix": "Here is the transcribed audio:",
        "long": "The audio is long, so I put the transcript in a file. Here it is.",
        "empty": "I transcribed it, but no speech came through (silent or very short audio).",
        "failed": "I couldn't transcribe that audio — the download or transcription failed. "
                  "Want me to try again?",
    },
    "pt": {
        "prefix": "Aqui está o áudio transcrito:",
        "long": "O áudio é longo, então coloquei a transcrição em um arquivo. Aqui está.",
        "empty": "Transcrevi, mas não saiu nenhuma fala (áudio silencioso ou muito curto).",
        "failed": "Não consegui transcrever esse áudio — o download ou a transcrição falhou. "
                  "Quer que eu tente de novo?",
    },
}


def _msg(lang: str | None) -> dict:
    return _MSG["pt"] if (lang or "").lower().startswith("pt") else _MSG["en"]


def _italic(text: str) -> str:
    """Render the transcript in WhatsApp italic. Italic (`_..._`) does not span line breaks,
    so wrap each non-empty line on its own; blank lines pass through."""
    return "\n".join(f"_{ln.strip()}_" if ln.strip() else "" for ln in text.split("\n"))


async def transcribe_node(
    state: MessageState, *, evolution, transcription, echoes, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    number = state["number"]
    owner = settings.owner_name
    wa_id = state.get("quoted_audio_id")
    loop_id = trace.new_loop_id(number)  # one-shot loop so the transcript is durable

    result = await transcription.get(wa_id)
    err = result.get("error")
    text = (result.get("text") or "").strip()
    lang = result.get("language") or "en"
    m = _msg(lang)

    sent_id: str | None = None
    delivery = "failed"
    outcome = err or ("empty" if not text else "ok")

    if err in ("auth", "download", "provider", "timeout"):
        sent_id = await evolution.send_text(number, frame(m["failed"], owner, lang))
        delivery = "failed_reported"
    elif not text:
        sent_id = await evolution.send_text(number, frame(m["empty"], owner, lang))
        delivery = "empty_reported"
    elif result.get("duration_sec") and result["duration_sec"] > settings.long_audio_seconds:
        media_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ok = await evolution.send_media(
            number, mediatype="document", mimetype="text/plain",
            media_b64=media_b64, filename="audio-transcript.txt",
            caption=frame(m["long"], owner, lang),
        )
        if ok:
            delivery = "file"
        else:  # a text wall beats losing the transcript the owner asked for
            sent_id = await evolution.send_text(number, frame(f"{m['prefix']}\n\n{_italic(text)}", owner, lang))
            delivery = "inline_fallback"
    else:
        sent_id = await evolution.send_text(number, frame(f"{m['prefix']}\n\n{_italic(text)}", owner, lang))
        delivery = "inline"

    if sent_id:
        echoes.record(jid, sent_id)  # never re-ingest our own reply
    if text:
        # Durable: log both the transcript we delivered, marked as audio-sourced.
        trace.user(tid, "AI Assistant", text, loop_id=loop_id, wa_id=sent_id or None,
                   source="audio", status="delivered")

    trace.code(
        tid, node="transcribe", loop_id=loop_id, chat_id=jid,
        wa_audio_id=wa_id, outcome=outcome, delivery=delivery,
        duration_sec=result.get("duration_sec"), lang=lang,
        prompt_version=settings.prompt_version,
    )
    return {"sent": bool(sent_id) or delivery == "file", "reply": text or None}
