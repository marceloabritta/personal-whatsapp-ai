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


# --- confirmation intent (yes / other) — the programmatic happy-path gate -------------------
#
# When a skill has proposed a write and is waiting for the owner's go-ahead, the next message is
# classified here with NO model call. "yes" fires ONLY when the reply is purely affirmative
# (+ filler), so every tricky case — "yes but at 4pm", "no", a question — falls to "other" and is
# handled by the model, exactly as today. Multi-word affirmatives are matched as normalized bigrams.

_YES_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "kk", "go", "confirm",
    "confirmed", "perfect", "correct", "right", "please", "yea", "yup",
    "sim", "s", "isso", "pode", "manda", "mande", "confirma", "confirmado", "vai", "beleza",
    "blz", "ta", "certo", "perfeito", "claro", "fechado", "combinado", "positivo",
    "si", "dale", "hazlo", "correcto", "perfecto", "vale", "adelante",
}
# Multi-word affirmatives (normalized, space-joined). Matched as contiguous bigrams.
_YES_PHRASES = {
    "go ahead", "do it", "send it", "sounds good", "that works", "yes please", "go for it",
    "pode ser", "pode sim", "pode mandar", "manda ver", "vai la", "isso mesmo", "pode agendar",
    "esta certo", "ta bom", "por favor",
    "hazlo ya", "esta bien", "de acuerdo",
}
# Declining a proposal. Deliberately NARROW: a word only belongs here if it cannot also mean
# "yes" to the question being asked. "cancela" is the sharpest example and is excluded — on a
# `calendar.delete` confirmation ("Cancelar?") it means GO AHEAD, so treating it as a refusal
# would silently drop exactly the cancellation the owner was approving. "para" (stop / for) and a
# bare "deixa" ("deixa às 15h" = leave it at 3pm) are ambiguous the same way and are excluded too.
_NO_WORDS = {
    "no", "nope", "nah", "naw", "dont", "nevermind",
    "nao", "n", "negativo",
}
_NO_PHRASES = {
    "never mind", "forget it", "dont bother", "do not", "not now", "no need", "skip it",
    "leave it", "drop it",
    "nao precisa", "deixa pra", "pra la", "deixa quieto", "esquece isso", "melhor nao",
    "nao quero", "nao vamos", "sem necessidade", "nao precisamos",
    "mejor no", "olvidalo", "no hace", "no importa",
}
# Single words that decline on their own but are too generic to list above.
_NO_SOLO = {"esquece", "olvidalo", "nevermind"}

# Filler that carries no instruction — ignored when deciding if a reply is a CLEAN yes.
_CONFIRM_FILLER = {
    "please", "pls", "plz", "por", "favor", "obrigado", "obrigada", "thanks", "thank", "you",
    "the", "it", "this", "that", "entao", "then", "ai", "e", "mas",
}


def classify_confirmation(text: str) -> str:
    """"yes" | "no" | "other" — what does this reply do to a pending proposal? (no model call).

    "yes" only when every substantive token is affirmative (or filler). "no" on the same rule for
    declines — a clean refusal and nothing else. Everything in between ("sim, mas 17h", "não, 16h",
    a question) is "other", which keeps the proposal alive for the model to correct.

    The distinction matters because "other" is the FIXING loop: the pending write is deliberately
    kept so a correction can replace it. Without a "no", answering "não precisa" left the proposal
    standing — the owner had declined and Lisa was still holding the write, waiting."""
    tokens = _normalize(text).split()
    if not tokens:
        return "other"

    # Declines first: "no" is a substantive token to the affirmative pass below, so a reply like
    # "nao precisa" would otherwise just fall through to "other".
    n_matched = [False] * len(tokens)
    for i in range(len(tokens) - 1):
        if f"{tokens[i]} {tokens[i + 1]}" in _NO_PHRASES:
            n_matched[i] = n_matched[i + 1] = True
    n_hit = any(n_matched)
    n_other = False
    for i, tok in enumerate(tokens):
        if n_matched[i]:
            continue
        if tok in _NO_WORDS or tok in _NO_SOLO:
            n_hit = True
        elif tok not in _CONFIRM_FILLER:
            n_other = True
    if n_hit and not n_other:
        return "no"
    # Consume affirmative bigrams first, so their words aren't counted as "other".
    matched = [False] * len(tokens)
    for i in range(len(tokens) - 1):
        if f"{tokens[i]} {tokens[i + 1]}" in _YES_PHRASES:
            matched[i] = matched[i + 1] = True
    hit = any(matched)
    other = False
    for i, tok in enumerate(tokens):
        if matched[i]:
            continue
        if tok in _YES_WORDS:
            hit = True
        elif tok not in _CONFIRM_FILLER:
            other = True
    return "yes" if (hit and not other) else "other"


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


# --- language detection (programmatic, no model call) ---------------------------------------
#
# The reason node locks the session language from the MODEL's own `lang` output, which it infers
# from the surrounding history. In the owner's self-chat that history is mostly Portuguese, so a
# conversation opened in English ("@lisa setup") came back in Portuguese. For a configuration
# conversation that is simply wrong: it should follow the words the owner is actually typing,
# starting in English and moving to Portuguese when he does.

_PT_MARKERS = {
    "que", "nao", "sim", "voce", "vc", "por", "favor", "obrigado", "obrigada", "para", "pra",
    "com", "uma", "isso", "muito", "tem", "mas", "quero", "pode", "manda", "mande", "numero",
    "numeros", "cartao", "grupo", "grupos", "audio", "audios", "meu", "minha", "dele", "dela",
    "contato", "contatos", "telefone", "errado", "certo", "agora", "depois", "tambem", "entao",
    "esse", "essa", "este", "esta", "aqui", "ali", "quais", "qual", "ambos", "todos", "coloca",
    "adiciona", "remove", "muda", "troca", "usa", "usar", "pegou", "pegue", "reenviar",
}
_EN_MARKERS = {
    "the", "what", "which", "add", "remove", "delete", "edit", "change", "please", "yes", "no",
    "group", "groups", "contact", "contacts", "number", "numbers", "both", "send", "sent",
    "list", "show", "setup", "settings", "use", "this", "that", "and", "with", "for", "chat",
    "chats", "active", "inbound", "outbound",
}


def detect_language(text: str) -> str | None:
    """"pt" | "en" | None — which language the owner is writing in, or None when there is no
    signal either way ("1", "sim ok", a bare number).

    Accented characters are a strong Portuguese signal on their own; otherwise it is a simple
    marker-word count. Deliberately conservative: None means "keep whatever is already locked",
    which is safer than flip-flopping on a two-word message."""
    raw = text or ""
    if any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", raw)):
        return "pt"
    tokens = set(_normalize(raw).split())
    if not tokens:
        return None
    pt = len(tokens & _PT_MARKERS)
    en = len(tokens & _EN_MARKERS)
    if pt > en:
        return "pt"
    if en > pt:
        return "en"
    return None
