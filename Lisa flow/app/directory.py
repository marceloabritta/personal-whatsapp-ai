"""The address book — who Marcelo knows, and how to reach them.

Three tiers, the shape `roster.py` established:
  1. an in-process snapshot — the ONLY tier the reply path touches;
  2. an optional Postgres mirror (`<log_schema>.contacts` + outbox + sync cursor + links);
  3. Google People, reached exclusively from background tasks.

THE READ IS PURE AND EXACT. `mentions()` does dictionary lookups and nothing else. The obvious
implementation — the repo's own normalize-then-difflib idiom from intent.py — was measured at
1.3 s / 5.0 s / 12.3 s against 500 / 2 000 / 5 000 contacts, as pure CPU inside an async node, which
blocks the event loop for every chat, not just this one. Exact lookups over the same book are
0.17 ms. So nickname tolerance is bought the way this file always said it should be: aliases are
precomputed during the sync (Google's own Nickname field) into the same exact-match index.

A BARE NAME BINDS, WHEN IT IS UNAMBIGUOUS. "add Zen" is how people actually ask, and refusing it
because a first name "could be anyone" made Lisa forget an address she demonstrably had. A lone
name resolves when the BOOK answers it with exactly one person, or when the ROOM does — several
Zens on file but only one of them in this chat. More than one candidate is not a guess, it is a
question, and it renders as one. A word matching more people than fit in the prompt is not a name.

IDENTITY IS A PHONE NUMBER, and a group has many. `state["phone"]` is the 1:1 partner (None in a
group — `state["number"]` there is the group id, and an opaque @lid elsewhere, whose tail looks
enough like a local number to bind a stranger). `state["participant_phones"]` is who has spoken in
the room. Membership is an identity SOURCE, never a hit on its own: in a twenty-person group every
member would otherwise outrank the people the turn is actually about.

FAIL CLOSED WHILE COLD, in both directions. Until a full sync has completed once, `mentions()`
matches nothing AND every write is refused. "Book not loaded yet" is indistinguishable from "no
address on file", and the guidance turns the latter into *write it* — so without this rail every
restart would re-create people who are already in the book.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from typing import Any, Optional, TypedDict

log = logging.getLogger("mary.directory")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Transcript speaker labels, which build_labeled_transcript prepends to every line. Scanning them
# would let Lisa match on her own header or on a push name.
_LABEL_RE = re.compile(r"^[^:\n]{0,40}:", re.MULTILINE)
# Name tokens shorter than this are never indexed; nor are these.
_MIN_TOKEN = 3
_STOP = {
    "de", "da", "do", "dos", "das", "e", "jr", "dr", "dra", "sr", "sra", "the", "and",
    "neto", "filho", "junior",
}
# A stored phone shorter than country+area is not comparable to a JID; indexing it invites a
# confident wrong bind (that is what an "ends with" tail match used to do).
_MIN_PHONE_DIGITS = 10

LEARNED = "learned"   # Lisa's own memory: serves reads, never pushed to Google
GOOGLE = "google"     # mirrored from / written to the real address book


class Contact(TypedDict, total=False):
    resource_name: str
    etag: str
    name: str
    nicknames: list
    emails: list
    phones: list
    source: str
    preferred: Optional[str]
    used_at: Optional[int]


# --- normalisation -------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Casefold + strip accents/punctuation → space-separated tokens (same as intent._normalize)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    return " ".join(s.split())


def strip_labels(text: str) -> str:
    return _LABEL_RE.sub(" ", text or "")


def to_e164(raw: str, region: str) -> Optional[str]:
    """Canonical E.164, or None — and None means NOT INDEXED. Identity fails closed.

    Hand-rolling this was the alternative and it is the wrong trade: a home-made parser's failure
    mode is a silently wrong match, which is the single thing this module most needs to avoid.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        import phonenumbers
    except ImportError:  # feature simply stays inert without the dep
        return None
    # A WhatsApp JID local part is an international number with the "+" stripped
    # ("5511987654321"), while a hand-saved contact is usually national ("11 98765-4321").
    # Try the region reading first, then the international one; a bare digit string that is too
    # long to be national is the JID case.
    candidates = [raw] if raw.startswith("+") else [raw, "+" + raw]
    for cand in candidates:
        try:
            n = phonenumbers.parse(cand, None if cand.startswith("+") else region)
            if phonenumbers.is_valid_number(n):
                return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            continue
    # Brazil's pre-2012 mobile spelling (no leading 9) is no longer a *valid* number, so
    # libphonenumber rejects it outright — and a contact saved that way would never match, which
    # is precisely the case the 9th digit keeps breaking. Repair it deterministically: insert the
    # 9 and re-validate. If that does not produce a valid number either, it stays unindexed.
    digits = "".join(ch for ch in raw if ch.isdigit())
    if region == "BR":
        # Either spelling of the legacy form: with the country code (5511 8765-4321) or the way
        # it is usually saved in a phone (11 8765-4321).
        m = (re.fullmatch(r"55(\d{2})(\d{8})", digits)
             or re.fullmatch(r"(\d{2})(\d{8})", digits))
        if m:
            try:
                n = phonenumbers.parse(f"+55{m.group(1)}9{m.group(2)}", None)
                if phonenumbers.is_valid_number(n):
                    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
            except Exception:
                pass
    return None


def match_keys(e164: str) -> list:
    """E.164 plus the BR pre-2012 mobile spelling, in whichever direction is missing.

    Nothing else — no tails, no prefixes, no "ends with". A bucket on the last 8 digits collides
    across area codes (11 98765-4321 vs 21 98765-4321) and binds a stranger; this is the one real
    case that motivated it, made explicit and bounded. Country and area codes always compare in full.
    """
    keys = [e164]
    m = re.fullmatch(r"\+55(\d{2})9(\d{8})", e164)
    if m:
        keys.append(f"+55{m.group(1)}{m.group(2)}")
    m = re.fullmatch(r"\+55(\d{2})(\d{8})", e164)
    if m:
        keys.append(f"+55{m.group(1)}9{m.group(2)}")
    return keys


def _name_tokens(name: str) -> list:
    return [t for t in _normalize(name).split() if len(t) >= _MIN_TOKEN and t not in _STOP]


def _alias_tokens(contact: dict) -> list:
    """Every token this person can be called by — full name plus Google's Nickname field.

    One flat list, in one exact-match index: "Zen" has to reach Zenaldo Tanaka, and the measured
    cost of the fuzzy alternative (5 s over 2 000 contacts, blocking the event loop) is why the
    tolerance is precomputed here instead of being matched for at read time.
    """
    toks = _name_tokens(contact.get("name") or "")
    for nick in contact.get("nicknames") or []:
        for t in _name_tokens(nick):
            if t not in toks:
                toks.append(t)
    return toks


def _given(contact: dict) -> str:
    toks = _name_tokens(contact.get("name") or "")
    return toks[0] if toks else ""


class Directory:
    """The address book in memory, with an optional durable tier and a People client behind it."""

    def __init__(self, settings, *, store: Any = None, people: Any = None) -> None:
        self.s = settings
        self.store = store
        self.people = people
        self._contacts: dict = {}       # resource_name -> Contact
        self._by_pair: dict = {}        # (tok_a, tok_b) -> [Contact]
        self._by_token: dict = {}       # one name/nickname token -> [Contact]
        self._by_email: dict = {}       # address -> Contact
        self._by_phone: dict = {}       # E.164 match key -> [Contact]
        self._links: dict = {}          # E.164 -> resource_name
        # phone string -> [Contact]. `to_e164` is a libphonenumber parse, not a dict lookup, and
        # a group hands us one number PER MEMBER PER TURN: at 100 members that measured 6.3 ms of
        # blocking CPU inside an async node, over this module's own 5 ms budget. Membership barely
        # changes between turns, so the parse is done once. Dropped whenever the book or the
        # links move, which is the only way an answer here can go stale.
        self._phone_cache: dict = {}
        self._sync_token: Optional[str] = None
        self._ready = False
        # Did we successfully read the durable mirror at boot? Only then may a full sweep reap
        # rows from it — otherwise an empty snapshot would delete learned addresses and links.
        self._mirror_loaded = store is None
        self._last_full = 0.0
        self._tasks: set = set()        # STRONG refs; a bare create_task can be GC'd mid-flight
        self._sync_task: Optional[asyncio.Task] = None
        self._drain_task: Optional[asyncio.Task] = None

    # --- the hot path (pure, no I/O, never raises) ---------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready

    def mentions(self, turn_text: str, *, phone: Optional[str] = None,
                 phones: Optional[list] = None, limit: int = 5) -> list:
        """Contacts this turn is provably about. The ONLY method the reply path calls.

        Exact lookups only. Returns [(contact, why)] where `why` is "phone+name" | "email" |
        "phone" | "name" | "name?", strongest first — and "name?" means AMBIGUOUS: the caller
        must render it as a question and must not read an address off it.

        `phone` is the 1:1 chat partner. `phones` is everyone who has spoken in this chat, which
        in a group is the membership; it never produces a hit by itself, it only says WHICH of
        several same-named people is the one being talked about, and lifts a name match to the
        "phone matches and name matches" bar without costing a confirmation turn.
        """
        if not self._ready:
            return []
        try:
            hits: dict = {}

            # Emails come out of the RAW text first: _normalize strips "@" and ".".
            for m in _EMAIL_RE.finditer(turn_text or ""):
                c = self._by_email.get(m.group(0).lower())
                if c:
                    hits[c["resource_name"]] = (c, "email")

            if phone:
                for c in self._resolve_phone(phone):
                    hits.setdefault(c["resource_name"], (c, "phone"))

            # The room. Resolved up front, used only to disambiguate and to promote — see above.
            members: dict = {}
            for p in phones or []:
                for c in self._resolve_phone(p):
                    members[c["resource_name"]] = c

            toks = _normalize(strip_labels(turn_text)).split()
            tokset = set(toks)

            for a, b in zip(toks, toks[1:]):
                for c in self._by_pair.get((a, b), ()):
                    hits.setdefault(c["resource_name"], (c, "name"))

            # A lone name — "add Zen". Tokens already accounted for by a contact we resolved are
            # skipped, so "Ana Silva" does not also fire the eight other Silvas in the book.
            covered = {t for c, _w in hits.values() for t in _alias_tokens(c)}
            for t in toks:
                if t in covered:
                    continue
                cands = self._by_token.get(t) or []
                if not cands:
                    continue
                here = [c for c in cands if c["resource_name"] in members]
                if len(cands) == 1:
                    pick = cands[0]
                elif len(here) == 1:
                    pick = here[0]      # several on file, one in the room: that is the one
                elif len(cands) <= limit:
                    # Not a guess worth making against a real address book. Surfaced as a
                    # question instead, by name only — `block` renders no address for these.
                    for c in cands:
                        hits.setdefault(c["resource_name"], (c, "name?"))
                    covered |= {tok for c in cands for tok in _alias_tokens(c)}
                    continue
                else:
                    continue            # a common word that happens to be somebody's surname
                hits[pick["resource_name"]] = (pick, "name")
                covered |= set(_alias_tokens(pick))

            # Phone + name agreement is the strongest signal available. In a group "the phone" is
            # the member list, so a member the turn names by name clears the same bar.
            for rn, (c, why) in list(hits.items()):
                if why in ("phone", "name") and rn in members:
                    hits[rn] = (c, "phone+name")
                elif why == "phone" and _given(c) and _given(c) in tokset:
                    hits[rn] = (c, "phone+name")

            order = {"phone+name": 0, "email": 1, "phone": 2, "name": 3, "name?": 4}
            ranked = sorted(hits.values(), key=lambda t: (order.get(t[1], 9),
                                                          -(t[0].get("used_at") or 0)))
            return ranked[:limit]
        except Exception:  # the reply path must never see an exception from here
            log.exception("directory.mentions failed")
            return []

    def _resolve_phone(self, phone: str) -> list:
        cached = self._phone_cache.get(phone)
        if cached is not None:
            return cached
        found = self._resolve_phone_uncached(phone)
        if len(self._phone_cache) >= 4096:   # unbounded growth is the only other failure here
            self._phone_cache.clear()
        self._phone_cache[phone] = found
        return found

    def _resolve_phone_uncached(self, phone: str) -> list:
        e164 = to_e164(phone, self.s.contacts_default_region)
        if not e164:
            return []
        rn = self._links.get(e164)
        if rn and rn in self._contacts:
            return [self._contacts[rn]]
        seen: dict = {}
        for k in match_keys(e164):
            for c in self._by_phone.get(k, ()):
                seen[c["resource_name"]] = c
        # More than one contact on a number is not a match — it is a question.
        return list(seen.values()) if len(seen) == 1 else []

    def by_email(self, email: str) -> Optional[dict]:
        return self._by_email.get((email or "").lower())

    def block(self, found: list, owner: str) -> str:
        """The prompt snippet. Pure string building; empty list -> "".

        The same record is served in a group as in a 1:1. It used to be filtered down to the
        addresses the room had already typed, on the theory that a private address must not be
        read out to people who never had it — but the person being invited receives the invitation,
        which carries the guest list, so the room learns the address anyway the moment the meeting
        is booked. The filter bought no privacy and cost Lisa the address she was asked for.
        """
        if not found:
            return ""
        lines = [
            f"\n\nAddress book — the people this conversation mentions, from {owner}'s Google "
            f"Contacts. These are facts: use them, and never invent an address.",
        ]
        ask: list = []
        for c, why in found:
            name = c.get("name") or "(unnamed)"
            if why == "name?":
                # Ambiguous by construction — showing any of their addresses would be picking one.
                ask.append(name)
                continue
            emails = list(c.get("emails") or [])
            pref = (c.get("preferred") or "").lower()
            if pref and pref in [e.lower() for e in emails]:  # order only — never auto-selects
                emails = ([e for e in emails if e.lower() == pref]
                          + [e for e in emails if e.lower() != pref])
            if not emails:
                lines.append(f"- {name} — no email on file")
            elif len(emails) == 1:
                lines.append(f"- {name} — {emails[0]}")
            else:
                lines.append(f"- {name} — {' | '.join(emails)}  (MORE THAN ONE: ask which)")
            if why == "name":
                lines[-1] += "  [named in the chat, identity not confirmed — confirm before using]"
        if ask:
            lines.append(f"- A name used here fits more than one person: {', '.join(ask)}. "
                         f"ASK WHICH — and use no address until he answers.")
        return "\n".join(lines)

    # --- learning ------------------------------------------------------------------------

    def plan_write(self, name: str, email: str, *, phone: Optional[str] = None) -> Optional[dict]:
        """What SHOULD happen for this learned pair — or None for "nothing".

        The write rules, and the reason each exists:
          * address already known            -> nothing
          * contact has NO address           -> add it (the case the feature exists for)
          * contact HAS one, this is another -> NOTHING. Appending here is the trap: the owner's
            rule is "two addresses means always ask", so a single correction would permanently cost
            a question on every future booking with that person.
          * nobody matches, identity proven  -> create (Q1), only when a phone confirms who this is
          * nobody matches, identity unproven-> remember it as `learned`, in Lisa's mirror only
        """
        if not self._ready:
            return None
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return None
        if self._by_email.get(email):
            return None

        want = _name_tokens(name or "")
        if not want:
            # A name that yields no index tokens ("Zé", "Dr", "") tells us nothing about WHO this
            # address belongs to. It used to fall through to "the person in this chat", which
            # wrote a third party's address onto the chat partner's real contact card.
            return {"kind": "learn", "resource_name": None, "name": name, "email": email}

        target = None
        if phone:
            found = self._resolve_phone(phone)
            # The phone identifies the CHAT, not necessarily the person being discussed. It is a
            # valid target only when the first name agrees — Q2's "phone matches and first name
            # matches" bar. Otherwise the owner is talking about somebody else entirely.
            if len(found) == 1 and _given(found[0]) and _given(found[0]) == want[0]:
                target = found[0]

        if target is None:
            # Match the way the READ side matches, or the two disagree and Lisa surfaces a contact
            # from the book and then creates a duplicate of him: the read index is pair-based and
            # tolerant of extra tokens ("Ana Silva" resolves "Ana Maria Silva Costa"), so an exact
            # token-list equality here missed every stored middle name and second surname.
            # Ambiguity is not a match — the same rule _resolve_phone already applies.
            cands = self._candidates_by_name(want)
            if len(cands) == 1:
                target = cands[0]
            elif len(cands) > 1:
                return None  # which one? not a guess worth making against a real address book

        if target is not None:
            if target.get("emails"):
                return None  # has one already — use it for the invite, do not append
            kind = "learn" if target.get("source") != GOOGLE else "add_email"
            return {"kind": kind, "resource_name": target["resource_name"],
                    "name": name, "email": email}

        # Nobody in the book matches. Creating them is what the owner asked for, but it cannot be
        # justified by "the chat partner is a known contact" — that test made `create` fire
        # precisely when the identity was WRONG, so every third party mentioned in a chat with a
        # known contact accreted a real Google contact. For a person who is genuinely not in the
        # book there is no identity proof to be had; the honest trigger is simply a real name
        # (given + family) and an address the conversation actually contained.
        if self.s.contacts_create_people and len(want) >= 2:
            return {"kind": "create", "resource_name": None, "name": name, "email": email}
        return {"kind": "learn", "resource_name": None, "name": name, "email": email}

    def _candidates_by_name(self, want: list) -> list:
        """Contacts whose stored name contains every token of `want`, via the read index.

        It has to bind on the same evidence the READ side binds on. When they disagree, Lisa
        surfaces someone from the book and then writes the address onto a brand-new duplicate of
        him — which is exactly what a bare first name used to do here, now that one resolves.
        Ambiguity is still refused by the caller rather than guessed.
        """
        if not want:
            return []
        if len(want) == 1:
            return list(self._by_token.get(want[0]) or [])
        seen: dict = {}
        for c in self._by_pair.get((want[0], want[1]), ()):
            toks = set(_alias_tokens(c))
            if all(w in toks for w in want):
                seen[c["resource_name"]] = c
        return list(seen.values())

    async def remember(self, name: str, email: str, *, phone: Optional[str] = None) -> None:
        """Durable row FIRST, then the worker performs the Google write. Never awaited by a node."""
        job = self.plan_write(name, email, phone=phone)
        if job is None:
            return
        if job["kind"] == "learn":
            await self._apply_learned(job)
            return
        if self.store is not None:
            await self.store.enqueue(job)
        else:  # no durable tier -> no write (a write with no record is not worth making)
            await self._apply_learned(job)

    async def _apply_learned(self, job: dict) -> None:
        rn = job.get("resource_name") or f"learned/{job['email']}"
        c = self._contacts.get(rn) or {
            "resource_name": rn, "etag": "", "name": job.get("name") or "",
            "emails": [], "phones": [], "source": LEARNED,
            "preferred": None, "used_at": None,
        }
        addr = (job["email"] or "").strip().lower()
        if addr not in (c.get("emails") or []):
            c["emails"] = [*(c.get("emails") or []), addr]
        self._contacts[rn] = c
        self._reindex()
        if self.store is not None:
            try:
                await self.store.upsert(c)
            except Exception as exc:
                log.warning("directory learned-upsert failed: %s", exc)

    async def note_used(self, email: str, *, phone: Optional[str] = None) -> None:
        """Record the address that actually went on an invite, and bind the identity.

        A booking the owner approved is the strongest identity evidence available and costs no
        extra turn — so it is what writes a `contact_links` row.
        """
        email = (email or "").strip().lower()
        c = self.by_email(email)
        if c is None:
            return
        c["preferred"] = email
        c["used_at"] = int(time.time())
        if self.store is not None:
            try:
                await self.store.upsert(c)
            except Exception as exc:
                log.warning("directory note_used upsert failed: %s", exc)
        if phone:
            e164 = to_e164(phone, self.s.contacts_default_region)
            if e164 and self._links.get(e164) != c["resource_name"]:
                self._links[e164] = c["resource_name"]
                # `_links` is read by _resolve_phone, and this is the one path that changes it
                # without rebuilding the index.
                self._phone_cache = {}
                if self.store is not None:
                    try:
                        await self.store.link(e164, c["resource_name"], "booked")
                    except Exception as exc:
                        log.warning("directory link failed: %s", exc)

    def spawn(self, coro) -> None:
        """Detach work while KEEPING a strong reference.

        asyncio holds only a weak reference to a running task, so a bare create_task can be
        collected mid-flight — and it also throws away the coroutine's result, which is the only
        failure signal there is.
        """
        try:
            task = asyncio.create_task(self._guard(coro))
        except RuntimeError:  # no running loop (sync tests)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('{"side_effect":"failed","error":"%s"}', exc)

    # --- index ---------------------------------------------------------------------------

    def _reindex(self) -> None:
        by_pair: dict = {}
        by_token: dict = {}
        by_email: dict = {}
        by_phone: dict = {}
        for c in self._contacts.values():
            toks = _name_tokens(c.get("name") or "")
            nicks = [t for t in _alias_tokens(c) if t not in toks]
            if toks:
                # every (first token, later token) pair, capped — so "Ana Silva" still resolves
                # someone stored as "Ana Maria Silva Costa". A nickname pairs with every real
                # token in both directions, so "Zen Tanaka" reaches Zenaldo Tanaka too.
                pairs = [(toks[0], t) for t in toks[1:]] + list(zip(toks, toks[1:]))
                for n in nicks:
                    pairs += [(n, t) for t in toks] + [(t, n) for t in toks]
                seen_pairs: set = set()
                for pair in pairs:
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    by_pair.setdefault(pair, []).append(c)
                    if len(seen_pairs) >= 12:
                        break
            # The single-token index — what lets a bare "Zen" resolve at all.
            for t in _alias_tokens(c):
                by_token.setdefault(t, []).append(c)
            for e in c.get("emails") or []:
                by_email.setdefault(e.lower(), c)
            for p in c.get("phones") or []:
                digits = "".join(ch for ch in (p or "") if ch.isdigit())
                if len(digits) < _MIN_PHONE_DIGITS:
                    continue
                e164 = to_e164(p, self.s.contacts_default_region)
                if not e164:
                    continue
                for k in match_keys(e164):
                    by_phone.setdefault(k, []).append(c)
        self._by_pair, self._by_token = by_pair, by_token
        self._by_email, self._by_phone = by_email, by_phone
        self._phone_cache = {}

    def _forget(self, resource_name: str) -> None:
        self._contacts.pop(resource_name, None)
        for k, rn in list(self._links.items()):
            if rn == resource_name:
                self._links.pop(k, None)

    def load(self, contacts: list, links: Optional[dict] = None,
             sync_token: Optional[str] = None, *, ready: bool = True,
             mirror_loaded: bool = True) -> None:
        """Seed the snapshot directly (the durable tier at boot, and the selftests)."""
        self._contacts = {c["resource_name"]: dict(c) for c in contacts}
        self._links = dict(links or {})
        self._sync_token = sync_token
        self._mirror_loaded = mirror_loaded
        self._reindex()
        self._ready = ready

    # --- sync ----------------------------------------------------------------------------

    async def refresh(self, *, full: bool = False) -> None:
        """Pull from People. A delta MERGES; only a completed full sweep may replace."""
        if self.people is None:
            return
        token = None if full else self._sync_token
        people, next_token, was_full = await asyncio.to_thread(self.people.list_all, token)

        if was_full:
            # Build into a scratch dict across ALL pages and swap atomically, so a crash
            # mid-sweep leaves the old book standing rather than a fragment.
            scratch: dict = {}
            for p in people:
                v = self._view(p)
                if not v["resource_name"] or v.get("deleted"):
                    continue
                scratch[v["resource_name"]] = self._merge_local(v)
            # Lisa's own learned entries are not in Google and must survive a full sweep.
            for rn, c in self._contacts.items():
                if c.get("source") == LEARNED:
                    scratch.setdefault(rn, c)
            self._contacts = scratch
            self._last_full = time.monotonic()
        else:
            for p in people:
                v = self._view(p)
                if not v["resource_name"]:
                    continue
                if v.get("deleted"):
                    self._forget(v["resource_name"])
                else:
                    self._contacts[v["resource_name"]] = self._merge_local(v)

        # A delta that returned nobody changed nothing, and that is the ordinary case: the sync
        # runs four times an hour and the address book does not. Rebuilding the index anyway cost
        # a measured 144 ms of blocking CPU (115 ms before the alias index) inside the event loop
        # every tick, re-wrote all 2 000 mirror rows, and dropped the phone cache that keeps a
        # big group's membership off the reply path. Only the sync cursor actually moved.
        changed = was_full or bool(people)
        self._sync_token = next_token
        if changed or not self._ready:
            self._reindex()
        self._ready = True
        if self.store is not None:
            try:
                # `full` here means "this sweep may REAP rows the sweep did not return". That is
                # only true if we know what was in the mirror to begin with: if the boot load
                # failed (`_mirror_loaded` False) the snapshot started empty, and reaping against
                # it would delete every learned address and every identity link permanently —
                # rows that exist nowhere else. Mirror the contacts, reap nothing.
                #
                # With nothing changed the row list is empty and `replace` persists the cursor
                # alone — which is the one thing that DID move, and it has to be durable or a
                # restart resumes from a token that eventually expires.
                rows = list(self._contacts.values()) if changed else []
                await self.store.replace(rows, next_token,
                                         full=was_full and self._mirror_loaded)
            except Exception as exc:
                log.warning("directory mirror write failed: %s", exc)

    @staticmethod
    def _view(person: dict) -> dict:
        from .tools.people import view

        return view(person)

    def _merge_local(self, v: dict) -> dict:
        """Keep the local-only columns Google has nowhere to store."""
        old = self._contacts.get(v["resource_name"]) or {}
        v["preferred"] = old.get("preferred")
        v["used_at"] = old.get("used_at")
        return v

    async def drain(self) -> None:
        """One pass over the outbox. Each job runs its API call ONCE (no idempotency key)."""
        if self.store is None or self.people is None or not self._ready:
            return
        jobs = await self.store.pending(limit=20)
        for job in jobs or []:
            try:
                if job["kind"] == "add_email":
                    person = await asyncio.to_thread(
                        self.people.add_email, job["resource_name"], job["email"])
                elif job["kind"] == "create":
                    person = await asyncio.to_thread(
                        self.people.create, job.get("name") or "", job["email"])
                else:
                    await self.store.done(job["id"])
                    continue
                v = self._view(person)
                if v["resource_name"]:
                    self._contacts[v["resource_name"]] = self._merge_local(v)
                    self._reindex()
                await self.store.done(job["id"])
            except Exception as exc:
                # A terminal failure must not leave an optimistic patch asserting an address
                # Google has never seen — the incremental sync will never correct it, because
                # that contact never changed.
                #
                # `create` gets ONE attempt, ever. It carries no idempotency key and, unlike
                # add_email, has no read-back that makes a replay a no-op — so a lost response
                # (Google committed, the socket died) would create the same person twice on the
                # next drain. A lost create costs one un-learned address; a duplicated create
                # costs the owner a hand-merge in his real address book, and poisons matching for
                # that person until he does it.
                attempts = 1 if job["kind"] == "create" else self.s.contacts_write_attempts
                terminal = await self.store.fail(job["id"], str(exc), attempts)
                if terminal:
                    self._rollback(job)
                log.warning("contact write failed (%s): %s", job["kind"], exc)

    def _rollback(self, job: dict) -> None:
        rn = job.get("resource_name")
        c = self._contacts.get(rn) if rn else None
        if c and job.get("email") in (c.get("emails") or []):
            c["emails"] = [e for e in c["emails"] if e != job["email"]]
            self._reindex()

    def start(self) -> None:
        """Background sync + outbox drain. Nothing in the reply path ever awaits these."""
        if self.people is None or self._sync_task is not None:
            return

        async def _sync() -> None:
            while True:
                try:
                    stale = (time.monotonic() - self._last_full) > (
                        self.s.contacts_full_resync_hours * 3600)
                    await self.refresh(full=stale or not self._ready)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("contacts sync failed: %s", exc)
                await asyncio.sleep(self.s.contacts_sync_seconds)

        async def _drain() -> None:
            while True:
                try:
                    await self.drain()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("contacts drain failed: %s", exc)
                await asyncio.sleep(30)

        self._sync_task = asyncio.create_task(_sync())
        self._drain_task = asyncio.create_task(_drain())

    async def aclose(self) -> None:
        for t in (self._sync_task, self._drain_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._sync_task = self._drain_task = None
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
