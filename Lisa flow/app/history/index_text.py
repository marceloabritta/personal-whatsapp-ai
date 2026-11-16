"""What one stored WhatsApp message says, and how the index spells it. Pure functions, no I/O.

Evolution keeps every message as Baileys' raw protobuf-as-JSON, and the words are not in one place.
A plain text is `conversation` (Evolution folds `extendedTextMessage` into it on the way in, but not
inside wrappers); media carry a `caption`; documents a `fileName`; disappearing chats nest the whole
message under `ephemeralMessage.message`, view-once under `viewOnceMessage(V2).message`, album
children under `associatedChildMessage.message`; polls, contact cards, locations, buttons and
templates each keep their words under their own keys. `extract()` knows each shape, and falls back
to a walk over the usual text keys for anything new. It never reads inside `contextInfo`: that is
where a reply carries a copy of the message it quotes, and indexing it would make every reply match
what it answered.

Searching is case- and accent-insensitive, so the index stores a folded copy of the words (`fold`)
and the store folds every term the same way. Folding happens HERE, in Python, not with Postgres'
unaccent(): unaccent is not IMMUTABLE (it reads a dictionary), so it cannot sit in an index, and
doing both sides with one function means a term and the column can never disagree about what "é"
is. `fold_map` keeps the way back to the original text, for snippets.

Lisa's own messages go out from the owner's account, so they come back as fromMe; the header she
stamps on each one ("[<owner>'s AI Assistant]:", identity.py) is the only thing that tells them
apart. They are kept, flagged `by_lisa`, with the header cut off. The owner's own "@lisa …"
requests are flagged `to_lisa`: searching for what he asked about should find the conversation he
had with someone, not the question he just typed.

Evolution's row ids are cuid v1 ("c" + 8 base-36 digits of milliseconds + counter + noise), so they
sort by the time Evolution stored the row — not the time it was sent. That is what makes the
indexer's cursor cheap (see indexer.py); `cuid_ms` / `cuid_floor` read and build that prefix."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from ..identity import all_headers, matched_tag

# --- folding ------------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def fold(text: Optional[str]) -> str:
    """Lowercase, accents off, every run of whitespace one space. "São  Paulo\\n" -> "sao paulo "."""
    if not text:
        return ""
    if not text.isascii():
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _WS.sub(" ", text.lower())


def fold_map(text: str) -> tuple[str, list[int]]:
    """fold(), plus where each folded character came from in `text` — so a match found in the
    folded copy can be cut out of the original, accents and capitals intact."""
    out: list[str] = []
    where: list[int] = []
    prev_space = False
    for i, ch in enumerate(text or ""):
        f = ch.lower() if ch.isascii() else "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).lower()
        for c in f:
            if c.isspace():
                if prev_space:
                    continue
                c, prev_space = " ", True
            else:
                prev_space = False
            out.append(c)
            where.append(i)
    return "".join(out), where


def fold_regex(pattern: str) -> str:
    """A POSIX regex made accent-blind without touching its syntax. Only accents come off: case is
    left to the `~*` operator, because lowercasing a pattern turns \\S, \\W and \\D into something
    else."""
    if not pattern or pattern.isascii():
        return pattern or ""
    return "".join(c for c in unicodedata.normalize("NFKD", pattern) if not unicodedata.combining(c))


def like_pattern(term: str) -> str:
    """'%term%' for LIKE, folded, with LIKE's own wildcards escaped (backslash is the default)."""
    t = fold(term).strip()
    t = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


def snippet(text: str, needles: list[str], *, width: int = 300, regex: bool = False) -> str:
    """About `width` characters of `text` around the first place any needle matches, with "…" where
    it was cut. Needles are raw search terms (folded here) or, with regex=True, patterns."""
    text = text or ""
    if len(text) <= width:
        return text
    folded, where = fold_map(text)
    best: Optional[tuple[int, int]] = None
    for n in needles or []:
        if not n:
            continue
        if regex:
            try:
                m = re.search(fold_regex(n), folded, re.IGNORECASE)
            except re.error:
                m = None
            span = (m.start(), m.end()) if m else None
        else:
            f = fold(n).strip()
            i = folded.find(f) if f else -1
            span = (i, i + len(f)) if i >= 0 else None
        if span and (best is None or span[0] < best[0]):
            best = span
    if best is None or not where:
        start = 0
    else:
        s = where[min(best[0], len(where) - 1)]
        e = where[min(max(best[1] - 1, best[0]), len(where) - 1)] + 1
        start = max(0, min(s - (width - (e - s)) // 2, len(text) - width))
    end = min(len(text), start + width)
    # Cut at a word boundary when one is near, so the snippet does not start mid-word.
    if start > 0:
        sp = text.find(" ", start, start + 20)
        if sp != -1:
            start = sp + 1
    if end < len(text):
        sp = text.rfind(" ", end - 20, end)
        if sp > start:
            end = sp
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def cap(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- cuid v1 row ids ----------------------------------------------------------------------------

_CUID = re.compile(r"c[0-9a-z]{24}")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def cuid_ms(cid: Optional[str]) -> Optional[int]:
    """The millisecond Evolution stored this row, read off its cuid v1 id. None for another shape."""
    if not cid or not _CUID.fullmatch(cid):
        return None
    return int(cid[1:9], 36)


def cuid_floor(ms: int) -> str:
    """The smallest cuid v1 a row stored at `ms` or later can have: compare ids against it."""
    ms = max(0, int(ms))
    digits = ""
    while ms:
        ms, r = divmod(ms, 36)
        digits = _B36[r] + digits
    return "c" + digits.rjust(8, "0")


# --- jids ---------------------------------------------------------------------------------------

def jid_domain(jid: Optional[str]) -> str:
    return (jid or "").rpartition("@")[2]


def bare_jid(jid: Optional[str]) -> str:
    """A jid without its device suffix: "5511999990000:12@s.whatsapp.net" -> "5511999990000@s…"."""
    jid = (jid or "").strip().lower()
    if not jid or "@" not in jid:
        return jid
    local, _, dom = jid.partition("@")
    return f"{local.split(':')[0]}@{dom}"


def phone_of(jid: Optional[str]) -> Optional[str]:
    """The digits of a phone jid; None for a @lid, a group or anything else."""
    if jid_domain(jid) != "s.whatsapp.net":
        return None
    local = (jid or "").split("@")[0].split(":")[0]
    return local if local.isdigit() else None


def chat_kind_of(jid: str, owner_jid: Optional[str]) -> str:
    if owner_jid and jid == owner_jid:
        return "self"
    return "group" if jid_domain(jid) in ("g.us", "newsletter") else "contact"


_DIGITS_ONLY = re.compile(r"[\d\s+().\-]*")
# What WhatsApp files as the sender's name on the owner's own early rows; never a person's name.
_NOT_NAMES = {"você", "voce", "you", "eu", "me"}


def clean_name(name: Optional[str], jid: Optional[str] = None) -> Optional[str]:
    """A name worth showing, or None: blank, a bare phone number or the jid's own digits are not
    names (Evolution files the number in pushName when it has nothing better)."""
    n = (name or "").strip()
    if not n or _DIGITS_ONLY.fullmatch(n) or n.lower() in _NOT_NAMES:
        return None
    if jid and n.lower() == (jid.split("@")[0] or "").lower():
        return None
    return n[:200]


# --- message shapes -----------------------------------------------------------------------------

# Containers whose real message sits one level down, under "message".
_WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
             "documentWithCaptionMessage", "associatedChildMessage", "lottieStickerMessage",
             "groupMentionedMessage", "botInvokeMessage", "editedMessage")
# Keys that ride along with any message and say nothing about its content.
_NOISE = ("messageContextInfo", "senderKeyDistributionMessage", "deviceSentMessage")

# Baileys' content key -> our kind. None = carries no words worth indexing (skipped).
_KIND = {
    "conversation": "text", "extendedTextMessage": "text",
    "imageMessage": "image",
    "videoMessage": "video", "ptvMessage": "video",
    "audioMessage": "voice", "pttMessage": "voice",
    "documentMessage": "document",
    "stickerMessage": "sticker", "stickerPackMessage": "sticker",
    "contactMessage": "contact", "contactsArrayMessage": "contact",
    "locationMessage": "location", "liveLocationMessage": "location",
    "pollCreationMessage": "poll", "pollCreationMessageV2": "poll", "pollCreationMessageV3": "poll",
    "pollCreationMessageV4": "poll", "pollCreationMessageV5": "poll",
    "reactionMessage": "reaction",
    "buttonsResponseMessage": "text", "listResponseMessage": "text",
    "templateButtonReplyMessage": "text", "interactiveResponseMessage": "text",
    "templateMessage": "other", "buttonsMessage": "other", "listMessage": "other",
    "interactiveMessage": "other", "groupInviteMessage": "other", "eventMessage": "other",
    "productMessage": "other", "orderMessage": "other", "requestPaymentMessage": "other",
    "sendPaymentMessage": "other", "newsletterAdminInviteMessage": "other",
}
# Stored rows that are not messages anyone wrote: encrypted edit/vote envelopes, pins, receipts,
# album headers (each picture of an album is its own row), history notices, call logs.
_SKIP = {"protocolMessage", "secretEncryptedMessage", "pollUpdateMessage", "keepInChatMessage",
         "pinInChatMessage", "placeholderMessage", "albumMessage", "messageHistoryNotice", "call",
         "encReactionMessage", "encEventResponseMessage", "senderKeyDistributionMessage",
         "messageContextInfo", "peerDataOperationRequestMessage", "callLogMesssage",
         "callLogMessage", "scheduledCallCreationMessage", "bcallMessage"}
# Where the fallback walk looks for words, and the subtrees it never enters.
_TEXT_KEYS = ("conversation", "text", "caption", "fileName", "title", "description", "contentText",
              "hydratedContentText", "hydratedTitleText", "selectedDisplayText", "displayName",
              "name", "address", "footerText", "hydratedFooterText", "groupName")
_NO_ENTER = ("contextInfo", "quotedMessage", "jpegThumbnail", "pngThumbnail", "thumbnail",
             "messageContextInfo", "senderKeyDistributionMessage", "vcard", "waveform",
             "streamingSidecar", "scansSidecar", "annotations", "interactiveAnnotations")


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _join(*parts: Any) -> str:
    out: list[str] = []
    for p in parts:
        p = _s(p)
        if p and p not in out:
            out.append(p)
    return "\n".join(out)


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def unwrap(msg: Any, depth: int = 0) -> tuple[Optional[str], Any]:
    """(content key, content) of a Baileys message, through any wrappers. (None, None) for an
    empty or unrecognisable one."""
    if not isinstance(msg, dict) or depth > 6:
        return None, None
    keys = [k for k in msg if k not in _NOISE and msg.get(k) is not None]
    for k in keys:
        if k in _WRAPPERS and isinstance(msg[k], dict) and isinstance(msg[k].get("message"), dict):
            return unwrap(msg[k]["message"], depth + 1)
    # Prefer a key we know; then anything that looks like content.
    for k in keys:
        if k in _KIND:
            return k, msg[k]
    for k in keys:
        if k.endswith("Message") or k == "conversation":
            return k, msg[k]
    return (keys[0], msg[keys[0]]) if keys else (None, None)


def _walk_text(node: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8 or len(out) > 40:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _NO_ENTER:
                continue
            if k in _TEXT_KEYS and isinstance(v, str):
                if v.strip() and v.strip() not in out:
                    out.append(v.strip())
            elif isinstance(v, (dict, list)):
                _walk_text(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:50]:
            _walk_text(v, out, depth + 1)


def _words(ctype: str, c: Any) -> tuple[str, Optional[str], Optional[str], Optional[int]]:
    """(text, file_name, mimetype, seconds) for one content node."""
    if ctype == "conversation":
        return _s(c), None, None, None
    if not isinstance(c, dict):
        return "", None, None, None
    mime = _s(c.get("mimetype")) or None
    if ctype == "extendedTextMessage":
        return _s(c.get("text")), None, None, None
    if ctype in ("imageMessage", "stickerMessage"):
        return _s(c.get("caption")), None, mime, None
    if ctype in ("videoMessage", "ptvMessage", "audioMessage", "pttMessage"):
        return _s(c.get("caption")), None, mime, _int(c.get("seconds"))
    if ctype == "documentMessage":
        fname = _s(c.get("fileName")) or None
        title, caption = _s(c.get("title")), _s(c.get("caption"))
        # The title is usually the file name minus its extension, and a captioned document often
        # repeats its file name as the caption; keep either only when it says more.
        if title and fname and fold(title) in fold(fname):
            title = ""
        if caption and fname and fold(caption) in fold(fname):
            caption = ""
        return _join(caption, title), fname, mime, None
    if ctype == "contactMessage":
        return _s(c.get("displayName")), None, None, None
    if ctype == "contactsArrayMessage":
        names = [x.get("displayName") for x in (c.get("contacts") or []) if isinstance(x, dict)]
        return _join(c.get("displayName"), *names), None, None, None
    if ctype in ("locationMessage", "liveLocationMessage"):
        return _join(c.get("name"), c.get("address"), c.get("caption"), c.get("comment")), None, None, None
    if ctype.startswith("pollCreationMessage"):
        opts = [o.get("optionName") for o in (c.get("options") or []) if isinstance(o, dict)]
        return _join(c.get("name"), *opts), None, None, None
    if ctype == "reactionMessage":
        return _s(c.get("text")), None, None, None
    if ctype == "templateMessage":
        for k in ("hydratedTemplate", "hydratedFourRowTemplate", "fourRowTemplate"):
            t = c.get(k)
            if isinstance(t, dict):
                return _join(t.get("hydratedTitleText"), t.get("hydratedContentText"),
                             t.get("hydratedFooterText"), _s((t.get("imageMessage") or {}).get("caption"))
                             ), None, None, None
        imt = c.get("interactiveMessageTemplate")
        if isinstance(imt, dict):
            return _join((imt.get("header") or {}).get("title"), (imt.get("body") or {}).get("text"),
                         (imt.get("footer") or {}).get("text")), None, None, None
    if ctype == "interactiveMessage":
        return _join((c.get("header") or {}).get("title"), (c.get("body") or {}).get("text"),
                     (c.get("footer") or {}).get("text")), None, None, None
    if ctype == "interactiveResponseMessage":
        return _join((c.get("body") or {}).get("text")), None, None, None
    if ctype == "buttonsMessage":
        return _join(c.get("headerText"), c.get("contentText"), c.get("footerText")), None, None, None
    if ctype == "listMessage":
        return _join(c.get("title"), c.get("description"), c.get("footerText")), None, None, None
    if ctype == "listResponseMessage":
        return _join(c.get("title"), c.get("description")), None, None, None
    if ctype in ("buttonsResponseMessage", "templateButtonReplyMessage"):
        return _join(c.get("selectedDisplayText")), None, None, None
    if ctype == "groupInviteMessage":
        return _join(c.get("groupName"), c.get("caption")), None, None, None
    out: list[str] = []
    _walk_text(c, out)
    return "\n".join(out), None, mime, _int(c.get("seconds"))


def _quoted(c: Any, ctx: Any) -> Optional[str]:
    for node in (ctx, c.get("contextInfo") if isinstance(c, dict) else None):
        if isinstance(node, dict) and _s(node.get("stanzaId")):
            return _s(node.get("stanzaId"))
    return None


# Tags the owner has used for his assistant over time. A message opening with any of them was
# addressed to the assistant, whatever today's tag is (settings.tags is added to these).
LEGACY_TAGS = ("@lisa", "@mary", "@brain", "@assistant")


def all_tags(tags: Optional[list]) -> list[str]:
    return list(dict.fromkeys([*(t.lower() for t in (tags or []) if t), *LEGACY_TAGS]))


def strip_header(text: str, owner_name: str) -> Optional[str]:
    """The body of one of Lisa's messages with her header cut off; None when it is not hers."""
    t = (text or "").lstrip("*_~ \t\r\n")
    for h in sorted(all_headers(owner_name), key=len, reverse=True):
        if t.startswith(h):
            return t[len(h):].lstrip("*_~ \t\r\n")
    return None


def extract(message_type: Optional[str], message: Any, *, context_info: Any = None,
            from_me: bool = False, owner_name: str = "", tags: Optional[list] = None) -> Optional[dict]:
    """What one "Message" row says, or None when it says nothing worth indexing.

    Returns {kind, text, file_name, mimetype, seconds, quoted_msg_id, by_lisa, to_lisa, edit_of}.
    `edit_of` is set on an edit (protocolMessage.editedMessage): `text` is then the new wording of
    the message with that id."""
    if not isinstance(message, dict) or not message:
        return None
    # An edit: Baileys' protocolMessage type 14 (MESSAGE_EDIT) carrying the new message.
    pm = message.get("protocolMessage")
    if isinstance(pm, dict):
        edited = pm.get("editedMessage")
        target = _s((pm.get("key") or {}).get("id"))
        if not isinstance(edited, dict) or not target:
            return None
        ctype, c = unwrap(edited)
        if not ctype:
            return None
        text, fname, mime, secs = _words(ctype, c)
        if not text:
            return None
        out = _finish(_KIND.get(ctype, "text"), text, fname, mime, secs, None,
                      from_me=from_me, owner_name=owner_name, tags=tags)
        out["edit_of"] = target
        return out

    ctype, c = unwrap(message)
    if ctype is None or ctype in _SKIP or (message_type in _SKIP and ctype not in _KIND):
        return None
    kind = _KIND.get(ctype)
    if kind is None:
        if not ctype.endswith("Message") and ctype != "conversation":
            return None
        kind = "other"
    text, fname, mime, secs = _words(ctype, c)
    quoted = _quoted(c, context_info)
    if kind == "reaction" and isinstance(c, dict):
        quoted = _s((c.get("key") or {}).get("id")) or quoted
    # Media keep their row even without words: a picture, a voice note or a file is something
    # that was sent, and read_chat should show it. A text-ish message with no words is noise.
    if not text and not fname and kind in ("text", "other", "contact", "poll", "reaction"):
        return None
    return _finish(kind, text, fname, mime, secs, quoted, from_me=from_me,
                   owner_name=owner_name, tags=tags)


def _finish(kind, text, fname, mime, secs, quoted, *, from_me, owner_name, tags) -> dict:
    by_lisa = to_lisa = False
    if from_me and text and owner_name:
        body = strip_header(text, owner_name)
        if body is not None:
            by_lisa, text = True, body
    if from_me and not by_lisa and text:
        to_lisa = matched_tag(text.lstrip(), all_tags(tags)) is not None
    # Postgres text cannot hold a NUL; jsonb never stores one, but a wrapper might decode one.
    text = (text or "").replace("\x00", "")
    fname = fname.replace("\x00", "") if fname else fname
    return {"kind": kind, "text": text, "file_name": fname, "mimetype": mime,
            "seconds": secs, "quoted_msg_id": quoted, "by_lisa": by_lisa, "to_lisa": to_lisa,
            "edit_of": None}


# An @mention is stored as the raw number of the person's jid ("@111111111111111"): their lid,
# or their phone on older rows.
MENTION = re.compile(r"@(\d{6,20})\b")


def mention_jids(text: str) -> list[str]:
    """Every jid an @mention in `text` could stand for."""
    out: list[str] = []
    for d in MENTION.findall(text or ""):
        out += [f"{d}@lid", f"{d}@s.whatsapp.net"]
    return out


def expand_mentions(text: str, names: dict) -> str:
    """"@111111111111111" -> "@Ana Paula" where the jid's name is known; left as it is otherwise."""
    if not text or not names or "@" not in text:
        return text

    def sub(m: re.Match) -> str:
        d = m.group(1)
        n = names.get(f"{d}@lid") or names.get(f"{d}@s.whatsapp.net")
        return f"@{n}" if n else m.group(0)

    return MENTION.sub(sub, text)


def search_norm(text: str, file_name: Optional[str]) -> str:
    """The column searches run against: the words and the file name, folded."""
    return fold(_join(text, file_name))
