"""The deliberate lookup — contacts.find, when he is asking about somebody.

`Directory.mentions` gives up on a name that fits more people than the prompt holds, which is right
for a scan of everything said in a chat and wrong when the owner names the person he wants. This scans
the whole book and tolerates a misspelling, so it may be slow, and it lives apart from directory.py to
keep that file's hot path free of fuzzy matching by construction. It only ever runs in a worker thread
(Directory.search). It reads names, nicknames, company, email addresses and phone numbers.
"""
from __future__ import annotations

import difflib

from .directory import _MIN_TOKEN, _STOP, _normalize

_MIN_DIGITS = 6     # fewer than this in a search is not a phone number he is looking for
_CUTOFF = 0.8       # how close a misspelt word must be to a real one


def search_contacts(contacts: list, query: str) -> list:
    """Every contact this query fits, best first. Pure."""
    query = (query or "").strip()
    if not query:
        return []
    if "@" in query:
        wanted = query.lower()
        return [c for c in contacts
                if any(wanted in (e or "").lower() for e in c.get("emails") or [])]
    digits = "".join(ch for ch in query if ch.isdigit())
    if len(digits) >= _MIN_DIGITS and not any(ch.isalpha() for ch in query):
        return [c for c in contacts if _phone_contains(c, digits)]

    toks = _normalize(query).split()
    words = [t for t in toks if t not in _STOP] or toks
    if not words:
        return []
    rows = [(c, _search_tokens(c), " ".join(c.get("emails") or []).lower()) for c in contacts]
    hits = _rank(rows, words)
    if hits:
        return hits
    # Nothing at all: a spelling slip is the likely reason, and Felipe/Filipe are one person.
    vocabulary = {t for _c, hay, _e in rows for t in hay}
    close = [(difflib.get_close_matches(w, vocabulary, n=1, cutoff=_CUTOFF) or [w])[0] for w in words]
    return _rank(rows, close) if close != words else []


def _search_tokens(c: dict) -> set:
    parts = [c.get("name") or "", *(c.get("nicknames") or []), c.get("company") or ""]
    return set(_normalize(" ".join(parts)).split())


def _phone_contains(c: dict, digits: str) -> bool:
    return any(digits in "".join(ch for ch in (p or "") if ch.isdigit())
               for p in c.get("phones") or [])


def _rank(rows: list, words: list) -> list:
    scored = []
    for c, hay, emails in rows:
        score = 0
        for w in words:
            if w in hay:
                score += 3
            elif any(t.startswith(w) for t in hay):
                score += 2
            elif len(w) >= _MIN_TOKEN and w in emails:
                score += 1
            else:
                score = 0
                break
        if score:
            scored.append((score, c))
    scored.sort(key=lambda t: (-t[0], -(t[1].get("used_at") or 0), t[1].get("name") or ""))
    return [c for _s, c in scored]
