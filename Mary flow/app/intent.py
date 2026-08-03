"""The transcribe-intent matcher — the fast/slow fork, with NO model call.

The reactive command (reply to a voice note + "@mary transcribe") is unambiguous, so the
model earns nothing on it — it only adds latency, token cost, and a degeneration risk. This
classifies the message's remainder (after the tag, minus any echoed quote text) into:

  "transcribe"    → the deterministic fast path: download → transcribe → send, no reason turn;
  "compositional" → an extra instruction is present ("transcribe AND schedule it") → the
                    normal reason path, where the quoted audio is transcribed into context.

Matching is robust and multilingual: normalise (casefold + strip accents/punctuation), then
a curated lexicon plus a difflib fuzzy fallback — the SAME engine the calendar's `find`
resolver uses, so no new dependency."""
from __future__ import annotations

import difflib
import unicodedata

from .identity import matched_tag

# Curated, multilingual — extend freely. Matched exactly OR by fuzzy ratio (typo tolerance).
_TRANSCRIBE_WORDS = {
    "transcribe", "transcript", "transcription", "transcribed",
    "transcreve", "transcrever", "transcricao", "transcricoes", "transcreva", "transcrita",
    "transcribir", "transcripcion", "transcribeme",
}

# Filler that carries no instruction — ignored when deciding if a request is transcribe-ONLY,
# so "please transcribe this for me" still counts as transcribe, not compositional.
_FILLER = {
    "please", "pls", "plz", "this", "that", "it", "the", "audio", "voice", "message", "msg",
    "note", "for", "me", "por", "favor", "isso", "esse", "essa", "esta", "este", "o", "a",
    "audios", "mensagem", "aqui", "ai", "me", "pra", "para", "de", "do", "da", "esto", "esta",
    "porfa", "porfavor", "el", "la",
}


def _normalize(s: str) -> str:
    """Casefold and strip accents + punctuation, leaving space-separated word tokens."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    return " ".join(s.split())


def _is_transcribe_word(tok: str, threshold: float) -> bool:
    if tok in _TRANSCRIBE_WORDS:
        return True
    # A short token can't fuzzily be "transcribe" without being an exact hit above.
    if len(tok) < 6:
        return False
    return any(
        difflib.SequenceMatcher(None, tok, w).ratio() >= threshold
        for w in _TRANSCRIBE_WORDS
    )


def classify_transcribe(
    text: str, tags: list[str], quoted_text: str | None, *, threshold: float = 0.82,
    on_empty: bool = True,
) -> str:
    """Return "transcribe" or "compositional" for a message that replies to a voice note.

    `text` is the trigger message; `tags` the trigger tags; `quoted_text` the replied-to
    message's text (dropped so an echoed quote never reads as an instruction). `on_empty`
    decides a bare tag with no words (settings.transcribe_on_empty_reply)."""
    tag = matched_tag(text, tags)
    body = text[len(tag):] if tag else text
    body = _normalize(body)
    if quoted_text:
        for q in _normalize(quoted_text).split():
            # remove the echoed quote tokens once each so real instruction words survive
            body = body.replace(q, "", 1)
    tokens = _normalize(body).split()

    if not tokens:
        return "transcribe" if on_empty else "compositional"

    hit = any(_is_transcribe_word(t, threshold) for t in tokens)
    other = any(
        not _is_transcribe_word(t, threshold) and t not in _FILLER for t in tokens
    )
    return "transcribe" if (hit and not other) else "compositional"
