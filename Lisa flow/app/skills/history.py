"""The history skill — "@lisa com quem eu falei sobre…", in the owner's chat with himself.

`kind="agent"`: the reason pass does not ask the reasoner for one enforced-JSON decision. It runs
the search agent (app/history/agent.py, `run_history_agent`), which has its own prompt and its own
read-only tools over every message Evolution has stored, and comes back with the same things a
reason pass does: a message, a state, a language, plus the files to send back and the notes it
keeps for its next turn (nodes/reason.py `_agent_pass`). So this skill has no verbs, no handler, no
confirm and no render. What it carries is where it may run:

  only_self_chat + owner_only — his chats are his. The router offers this skill nowhere but his
                 chat with himself, and only on his own message; the reason pass refuses again in
                 code; and the only place a file can go is that same chat (HistoryMedia.send_to_owner).
  holds_loop   — a search is a conversation. "e quando foi isso?", "manda o pdf", "não, a outra
                 Ana" are follow-ups, and they share words with every other skill. A fresh tag
                 re-decides.
  enabled_by   — HISTORY_ENABLED. Off, the skill cannot be routed to, the classifier is not told
                 about it, and no other skill's prompt or schema changes: the pre-feature build.

How a turn gets here (skills/router.py), in order:
  1. by name — the first word after the tag is "histórico"/"history"/"historial";
  2. by phrasing — `history_matcher`, a recall question or a search for something someone SENT;
  3. by the classifier, which is told about history only where it can be routed to.
"""
from __future__ import annotations

import re

from .base import Skill
from .calendar import _normalize

DESCRIBE = ("Search {owner_name}'s own WhatsApp history: who said what, when and in which chat, "
            "and the files people sent him.")

# Never used to answer: the search agent has its own instructions. It is what the registry renders
# for this skill like for every other one, so it says only what is true of the conversation.
GUIDANCE = ("This conversation is a search of {owner_name}'s own WhatsApp history. It is answered "
            "by the history search, with its own instructions and tools. You take no other actions "
            "here.")


# --- called by name -----------------------------------------------------------------------
#
# Only names that cannot mean anything else. "busca", "procura", "search" stay out on purpose: in
# his chat with himself they are a web search as often as a history one ("busca o preço do
# dólar"). With a history signal after them the matcher takes them ("busca a mensagem…", "procura
# o pdf que o João mandou"); without one the classifier decides, and it is told that a bare search
# for a name in his own chat is history.
_HISTORY_NAMES = {"historico", "history", "historial"}


def history_invoked(text: str) -> bool:
    """Does this message open with the skill's own name? Only the first word after any @tags."""
    words = [w for w in (text or "").split() if not w.startswith("@")]
    first = _normalize(words[0]).split() if words else []
    return bool(first) and first[0] in _HISTORY_NAMES


# --- the matcher --------------------------------------------------------------------------
#
# Phrases, not words. Every word here belongs to some other request too: "quem" is in "quem vai na
# reunião", "mandou" in "quem mandou matar…", "link" in "busca um link de tutorial". What marks a
# history request is the SHAPE — a question about who said or sent something, in the past tense,
# or a search for a thing somebody sent. Each language has its own verb list, so a Portuguese
# present ("pra quem eu mando o relatório?") is not read as a Spanish past.

def _alt(*words: str) -> str:
    """A regex alternation of the given space-separated words, longest first."""
    ws = {w for chunk in words for w in chunk.split()}
    return "(?:" + "|".join(sorted(ws, key=lambda w: (-len(w), w))) + ")"


_PT_PAST_W = (
    "falei conversei comentei combinei discuti mencionei mandei enviei encaminhei respondi "
    "escrevi falou conversou comentou combinou discutiu mencionou disse mandou enviou encaminhou "
    "postou compartilhou indicou recomendou passou respondeu escreveu falaram conversaram "
    "comentaram disseram mandaram enviaram postaram compartilharam indicaram recomendaram "
    "passaram falamos conversamos combinamos discutimos comentamos mandamos")
_EN_PAST_W = ("spoke talked chatted discussed mentioned said told sent shared forwarded posted "
              "texted messaged wrote recommended")
_EN_BASE_W = "talk speak chat discuss mention say tell send share forward text message write"
_ES_PAST_W = ("hable hablo hablamos hablaron converse conversamos comente comento dijo dije "
              "dijeron mando mande mandaron envio envie enviaron compartio menciono escribio "
              "recomendo")
# Things that only exist in a chat. A search for one of these is a search of his chats.
_CHAT_W = ("mensagem mensagens msg msgs conversa conversas audio audios chat chats message "
           "messages conversation conversations mensaje mensajes conversacion conversaciones")
# Things the web has too. A search for one is his chats only when somebody sent it, or he points
# at one he remembers ("aquele pdf").
_FILE_W = (
    "arquivo arquivos pdf pdfs documento documentos doc docs foto fotos imagem imagens print "
    "prints link links video videos anexo anexos planilha planilhas contato endereco localizacao "
    "numero file files document documents photo photos picture pictures image images screenshot "
    "screenshots attachment attachments spreadsheet contact address location number archivo "
    "archivos imagen imagenes enlace enlaces captura contacto direccion")
_SEARCH_W = ("busca buscar busque procura procurar procure acha achar ache encontra encontrar "
             "encontre localiza localizar recupera find search locate encuentra encuentre")
# Asking for a file to be sent back here: "me manda aquele pdf que o Pedro enviou".
_SEND_W = "manda mande envia envie reenvia reenvie send resend mandame enviame"

_PT_PAST = _alt(_PT_PAST_W)
_EN_PAST = _alt(_EN_PAST_W)
_EN_BASE = _alt(_EN_BASE_W)
_ES_PAST = _alt(_ES_PAST_W)
_ANY_PAST = _alt(_PT_PAST_W, _EN_PAST_W, _ES_PAST_W)
_CHAT_NOUNS = _alt(_CHAT_W)
_FILE_NOUNS = _alt(_FILE_W)
_ALL_NOUNS = _alt(_CHAT_W, _FILE_W)
_SEARCH = _alt(_SEARCH_W)
_FETCH = _alt(_SEARCH_W, _SEND_W)
_DET = _alt(
    "o a os as esse essa esses essas este esta aquele aquela aqueles aquelas minha minhas meu meus "
    "the that those this my el la los las ese esa aquel aquella mis mi")
_THAT = _alt("aquele aquela aqueles aquelas that those aquel aquella")
_QWORD = r"\b(?:quem|onde|quando|qual|quais|who|where|when|which|what|quien|donde|cuando|cual)\b"
_W = r"(?:\s+\w+)"  # one more word

_PATTERNS = [re.compile(p) for p in (
    # --- Portuguese ---
    rf"\b(?:com|pra|para) quem\b{_W}{{0,4}}?\s+{_PT_PAST}\b",           # com quem eu falei
    rf"\bquem (?:foi que |que )?(?:me|nos) {_PT_PAST}\b",                # quem me mandou
    rf"\bquem foi que {_PT_PAST}\b",                                     # quem foi que comentou
    rf"\bquem {_PT_PAST} {_DET}?\s*(?:\w+ )?{_ALL_NOUNS}\b",             # quem mandou o pdf
    r"\bquem (?:falou|comentou|mencionou|conversou) (?:de|do|da|dos|das|sobre|comigo)\b",
    r"\bquem (?:era|foi) (?:aquele|aquela|o cara|a pessoa|a mina|o contato)\b",
    rf"\b(?:qual|que) (?:grupo|conversa|chat)\b{_W}{{0,6}}?\s+{_PT_PAST}\b",  # em qual grupo falaram
    rf"\blembra (?:quando|que|daquela|daquele|da vez)\b{_W}{{0,8}}?\s+{_PT_PAST}\b",
    rf"\blembra (?:daquela|daquele|daquelas|daqueles|da|do) {_ALL_NOUNS}\b",
    r"\b(?:falei|conversei|comentei) com (?:alguem|um cara|uma pessoa|uma mina|um amigo"
    r"|uma amiga|um pessoal|uns caras)\b",
    r"\balguem (?:me (?:mandou|falou|comentou|disse|enviou|indicou|recomendou|passou)"
    r"|mandou|enviou|postou|compartilhou)\b",
    r"\bo que (?:o|a|os|as|aquele|aquela) \w+(?: \w+)? (?:me|nos) (?:disse|falou|mandou|comentou"
    r"|respondeu|escreveu|enviou|passou|disseram|falaram|mandaram)\b",   # o que o João me disse
    r"\bo que eu (?:falei|disse|mandei|respondi|escrevi|comentei|enviei) (?:pro|pra|para|ao|a|com"
    r"|no|na)\b",
    # --- English ---
    rf"\bwho (?:did|have) (?:i|we) (?:ever )?{_EN_BASE}\b",              # who did I talk to
    r"\bwho (?:sent|told|texted|messaged|forwarded|shared|mentioned|recommended|showed) (?:me|us)\b",
    rf"\bwho (?:was it )?(?:that )?(?:sent|shared|posted|forwarded) {_DET}?\s*(?:\w+ )?{_ALL_NOUNS}\b",
    r"\b(?:i|we) (?:spoke|talked|chatted) (?:to|with) (?:someone|somebody|a guy|a girl|a friend"
    r"|some guy|a person)\b",
    rf"\bremember when\b{_W}{{0,8}}?\s+{_EN_PAST}\b",
    rf"\bwhat did \w+(?: \w+)? {_EN_BASE}(?: to)? (?:me|us)\b",          # what did Pedro send me
    r"\bwhat did i (?:say|tell|send|write|promise)\b",
    rf"\bwhich (?:group|chat|conversation)\b{_W}{{0,6}}?\s+{_alt(_EN_PAST_W, 'did')}\b",
    # --- Spanish ---
    rf"\bcon quien\b{_W}{{0,4}}?\s+{_ES_PAST}\b",
    rf"\bquien (?:me|nos) {_ES_PAST}\b",
    r"\bquien fue (?:el |la )?que\b",
    rf"\ben (?:que|cual) (?:grupo|chat|conversacion)\b{_W}{{0,6}}?\s+{_ES_PAST}\b",
    rf"\b(?:recuerdas|te acuerdas de) (?:cuando|que|esa|ese|aquella|aquel)\b{_W}{{0,8}}?\s+{_ES_PAST}\b",
    # --- a search, in any of the three ---
    rf"\b{_SEARCH} {_DET} (?:\w+ ){{0,2}}?{_CHAT_NOUNS}\b",              # acha a conversa
    rf"\b{_FETCH} {_THAT} (?:\w+ )?{_FILE_NOUNS}\b",                 # acha aquele pdf
    # ...a thing somebody sent: "procura o arquivo que o contador mandou", "find the pdf João sent"
    rf"\b{_FETCH}\b{_W}{{0,4}}?\s+{_ALL_NOUNS}\b{_W}{{0,6}}?\s+{_ANY_PAST}\b",
    # ...anything "que X mandou", with an object between: "acha o contrato que o Pedro mandou"
    # ("acha que…" with nothing between is an opinion, not a search).
    rf"\b{_SEARCH}{_W}{{1,5}}?\s+(?:que|that|which)\s+(?:\w+\s+){{0,3}}?(?:me\s+|nos\s+)?{_ANY_PAST}\b",
)]

# An explicit scope: "no histórico", "nas minhas conversas", "in my chats". Counts with a search
# verb or a question word anywhere in the message ("manda no whatsapp do João…" is not a search).
_SCOPE = re.compile(
    r"\b(?:no (?:meu )?historico|nas (?:minhas )?(?:conversas|mensagens)|no (?:meu )?whatsapp"
    r"|no zap|nos (?:meus )?grupos|in (?:my|our) (?:chats|messages|conversations|whatsapp|history"
    r"|groups)|(?:on|in) whatsapp|chat history|en mis (?:chats|mensajes|conversaciones)"
    r"|en (?:el )?whatsapp|en el historial)\b")
_SEARCH_RE = re.compile(rf"\b{_SEARCH}\b")
_QWORD_RE = re.compile(_QWORD)

# Another skill's verb as the first word: that skill's request, whatever recall phrase follows.
# "marca reunião com quem me mandou o pdf" is a booking; "salva o número que o João mandou" is the
# address book; "me lembra de…" is a reminder.
_OTHER_VERBS = {
    "marca", "marcar", "marque", "agenda", "agendar", "agende", "remarca", "remarcar", "remarque",
    "desmarca", "desmarcar", "cancela", "cancelar", "cancele", "adiciona", "adicionar",
    "adicione", "convida", "convidar", "convide", "bloqueia", "bloquear", "schedule",
    "reschedule", "book", "cancel", "invite", "add", "block", "salva", "salvar", "salve",
    "atualiza", "atualizar", "apaga", "apagar", "deleta", "save", "update", "delete",
    "transcreve", "transcrever", "transcribe", "agendame", "programa",
}
# "me lembra de…", "lembra de me avisar…", "remind me…" ask for a reminder; "lembra quando a gente
# falou…" asks her to remember.
_REMINDER = re.compile(
    r"^(?:me (?:lembra|lembre|lembrar)|(?:lembra|lembre|lembrar) de|recuerdame|remind)\b")


def _after_tags(text: str) -> str:
    return " ".join(w for w in (text or "").split() if not w.startswith("@"))


def history_matcher(text: str) -> str:
    """"yes" | "no" — is this turn plainly a search of his own chats? (no model call).

    Never "maybe": either the phrasing is unmistakable or the classifier decides. The router also
    refuses this skill outside his chat with himself and on anyone's message but his, so a "yes"
    here is not enough on its own to route."""
    norm = _normalize(_after_tags(text))
    if not norm:
        return "no"
    first = norm.split()[0]
    if first in _OTHER_VERBS or _REMINDER.match(norm):
        return "no"
    if any(p.search(norm) for p in _PATTERNS):
        return "yes"
    if _SCOPE.search(norm) and (_SEARCH_RE.search(norm) or _QWORD_RE.search(norm)):
        return "yes"
    return "no"


# Which language a recall question is in, for the few words she says herself before the agent has
# answered ("🔎 Procurando…"). intent.detect_language is right when it speaks, but "quem me mandou o
# pdf do aluguel?" has none of its markers; these are the words a search request is made of.
_LANG_HINTS = {
    "pt": set(_PT_PAST_W.split()) | {
        "quem", "acha", "procura", "busca", "conversa", "mensagem", "arquivo", "lembra", "sobre",
        "ontem", "semana", "passada", "do", "da", "dos", "das", "aquele", "aquela", "onde", "foi",
        "eu", "ele", "ela", "nao", "voce", "gente", "um", "uma"},
    "en": set(_EN_PAST_W.split()) | {
        "who", "did", "find", "search", "about", "yesterday", "last", "week", "where", "the", "my",
        "was", "it", "remember", "that", "someone", "what", "which"},
    "es": set(_ES_PAST_W.split()) | {
        "quien", "con", "el", "del", "donde", "ayer", "mensaje", "encuentra", "recuerdas", "fue",
        "yo", "sobre", "semana", "pasada"},
}


def guess_lang(text: str) -> str | None:
    """"pt" | "en" | "es" | None — by the words a search request is made of; None on a tie."""
    tokens = _normalize(_after_tags(text)).split()
    score = {lang: sum(t in hints for t in tokens) for lang, hints in _LANG_HINTS.items()}
    best = max(score.values())
    winners = [lang for lang, n in score.items() if n == best]
    return winners[0] if best and len(winners) == 1 else None


async def run_agent(**kwargs):
    """The search agent, looked up when called: nothing under app/history is imported by the reply
    path while the skill is off, and a test can swap `app.history.agent.run_history_agent`."""
    from ..history import agent

    return await agent.run_history_agent(**kwargs)


HISTORY = Skill(
    name="history",
    kind="agent",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    matcher=history_matcher,
    invoked_by=history_invoked,
    only_self_chat=True,
    owner_only=True,
    holds_loop=True,
    enabled_by="history_enabled",
    # The agent picks its own model (settings.history_model); nothing here is used by a reason call.
)
