"""Find one of the owner's GROUPS by a half-remembered name.

Contacts never come through here — they are added by forwarding a contact card, so their key is
read out of a vCard rather than guessed. Groups have no card, so this is the resolver, and it is
built for how the name will actually arrive: typed fast, partial, accented or not.

Ranking, in one sentence: score the name match, round it, then let RECENCY break the tie — a
group you spoke in this morning beats a marginally better string match you left in March.

    1.00  the subject is exactly the query
    0.92  every query token appears in the subject      "futebol terca" -> "Futebol de terça"
    0.85  the subject starts with the query
   >=0.62 difflib ratio                                  typo tolerance
    else  no match -> the caller shows the most recently active groups instead

Scores are rounded to one decimal BEFORE sorting, which is what makes recency the tie-break
rather than a decoration: two names that match about as well are ordered by when you last used
them. Reuses the normalise-then-fuzzy engine of `intent.py` and the calendar matcher — no new
dependency."""
from __future__ import annotations

import difflib

from .intent import _normalize

MATCH_THRESHOLD = 0.62


def _score(query: str, subject: str) -> float:
    """How well this group's subject answers the query, 0..1."""
    q, s = _normalize(query), _normalize(subject)
    if not q or not s:
        return 0.0
    if q == s:
        return 1.00
    q_tokens = q.split()
    s_tokens = set(s.split())
    if q_tokens and all(t in s_tokens for t in q_tokens):
        return 0.92
    if s.startswith(q):
        return 0.85
    return difflib.SequenceMatcher(None, q, s).ratio()


def merge_groups(chats: list[dict], groups: list[dict]) -> list[dict]:
    """Group rows with both a subject and a last-activity time.

    `chats` (from findChats) carries recency but often a thin label; `groups` (fetchAllGroups)
    carries the authoritative subject. Joined on the chat key, keeping every group we know a
    subject for even when it has no chat row yet."""
    by_key: dict[str, dict] = {}
    for c in chats or []:
        if c.get("kind") != "group" or not c.get("key"):
            continue
        by_key[c["key"]] = {"chat_key": c["key"], "chat_jid": c.get("jid") or "",
                            "label": c.get("label") or "", "last_ts": c.get("last_ts") or 0,
                            "size": None}
    for g in groups or []:
        if not g.get("key"):
            continue
        row = by_key.setdefault(g["key"], {"chat_key": g["key"], "chat_jid": g.get("jid") or "",
                                           "label": "", "last_ts": 0, "size": None})
        if g.get("label"):
            row["label"] = g["label"]       # the subject is authoritative
        if not row.get("chat_jid"):
            row["chat_jid"] = g.get("jid") or ""
        row["size"] = g.get("size")
    return [r for r in by_key.values() if r.get("label")]


def recent(groups: list[dict], limit: int = 5) -> list[dict]:
    """The most recently active groups — the answer when there is no query, or nothing matched."""
    return sorted(groups, key=lambda g: g.get("last_ts") or 0, reverse=True)[:limit]


def rank(query: str | None, groups: list[dict], *, limit: int = 5,
         threshold: float = MATCH_THRESHOLD) -> dict:
    """Resolve a group name. Returns {"matched": bool, "candidates": [...]}.

    `matched` is False when the caller should present the list as "here are your recent groups"
    rather than "these match" — an empty query, or nothing above the threshold. Either way the
    candidates are real groups drawn from the owner's own chat list, never invented."""
    if not (query or "").strip():
        return {"matched": False, "candidates": recent(groups, limit)}

    scored = [(round(_score(query, g.get("label") or ""), 1), g.get("last_ts") or 0, g)
              for g in groups]
    hits = [t for t in scored if t[0] >= round(threshold, 1)]
    if not hits:
        return {"matched": False, "candidates": recent(groups, limit)}

    hits.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return {"matched": True, "candidates": [g for _, _, g in hits[:limit]]}
