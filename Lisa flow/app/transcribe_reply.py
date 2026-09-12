"""How a transcript becomes a WhatsApp message — shared by both transcription paths.

The reactive path (`nodes/transcribe.py`, "@lisa transcribe" on a voice note) and the automatic
path (`nodes/auto_transcribe.py`, an enrolled chat) must produce byte-identical output for the
same transcript: same header, same prefix, same italic body, same long-audio .txt fallback. They
did drift-by-copy once; this module is the single definition so they cannot drift again.

The two paths differ in exactly two ways, both parameters here:
  - `quoted`  — the automatic path replies attached to the audio it transcribed;
  - `report_failures` — the reactive path was ASKED for a transcript, so a failure is answered
    honestly. The automatic path is ambient: a broken provider key must not post an apology into
    someone else's group every time they record. It stays silent and leaves the trace to say why.

A transcript is delivered verbatim. Failures are reported as failures — never a fabricated
transcript."""
from __future__ import annotations

import base64
from typing import Optional

from .identity import frame

# Per-language reply copy (matched to the transcript's detected language; en fallback).
MESSAGES = {
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

# Transcription errors that are transient or configuration — never a transcript.
FAILURE_KINDS = ("auth", "download", "provider", "timeout")


def copy_for(lang: str | None) -> dict:
    return MESSAGES["pt"] if (lang or "").lower().startswith("pt") else MESSAGES["en"]


def italic(text: str) -> str:
    """Render the transcript in WhatsApp italic. Italic (`_..._`) does not span line breaks,
    so wrap each non-empty line on its own; blank lines pass through."""
    return "\n".join(f"_{ln.strip()}_" if ln.strip() else "" for ln in text.split("\n"))


def inline_body(text: str, lang: str | None) -> str:
    """The message body for an inline transcript — prefix, blank line, italic transcript."""
    return f"{copy_for(lang)['prefix']}\n\n{italic(text)}"


async def deliver(
    *, evolution, result: dict, target: str, owner: str, settings,
    quoted: Optional[dict] = None, report_failures: bool = True,
) -> dict:
    """Send one transcript. Returns {sent_id, delivery, outcome, lang, text}.

    `target` is what Evolution addresses (a bare number for a 1:1, the full JID for a group —
    see clients.evolution.send_target). `delivery` is one of:
      inline | file | inline_fallback | failed_reported | empty_reported | silent
    """
    err = result.get("error")
    text = (result.get("text") or "").strip()
    lang = result.get("language") or "en"
    m = copy_for(lang)
    outcome = err or ("empty" if not text else "ok")

    sent_id: Optional[str] = None
    delivery = "silent"

    if err in FAILURE_KINDS:
        if report_failures:
            sent_id = await evolution.send_text(target, frame(m["failed"], owner, lang),
                                                quoted=quoted)
            delivery = "failed_reported"
    elif not text:
        if report_failures:
            sent_id = await evolution.send_text(target, frame(m["empty"], owner, lang),
                                                quoted=quoted)
            delivery = "empty_reported"
    elif result.get("duration_sec") and result["duration_sec"] > settings.long_audio_seconds:
        media_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ok = await evolution.send_media(
            target, mediatype="document", mimetype="text/plain",
            media_b64=media_b64, filename="audio-transcript.txt",
            caption=frame(m["long"], owner, lang), quoted=quoted,
        )
        if ok:
            delivery = "file"
        else:  # a text wall beats losing the transcript
            sent_id = await evolution.send_text(target, frame(inline_body(text, lang), owner, lang),
                                                quoted=quoted)
            delivery = "inline_fallback"
    else:
        sent_id = await evolution.send_text(target, frame(inline_body(text, lang), owner, lang),
                                            quoted=quoted)
        delivery = "inline"

    return {"sent_id": sent_id, "delivery": delivery, "outcome": outcome,
            "lang": lang, "text": text}
