"""Google People API — the owner's own contacts, over People v1.

The sibling of tools/calendar.py, and it inherits that file's scars deliberately rather than by
copy-paste: a FRESH service per call (a memoised googleapiclient holds one keep-alive httplib2
socket that Google drops while idle — 10 of 26 calendar creates died on `[Errno 32] Broken pipe`),
and lazy imports so the module loads without the google libs present.

It does NOT inherit calendar's retry ladder, and that is the point. Calendar mints a client-side
event id, so a replayed insert 409s instead of double-booking. People has no request key and never
409s, so a blind retry of `createContact` duplicates a person in the owner's real address book.
Every mutation here runs ONCE; the outbox row retries on the next drain, where a read-back decides
whether the work is already done.

Auth: its OWN refresh token (`google_contacts_refresh_token`), never the calendar one. google-auth
only *warns* when a granted scope is missing, so a re-mint that quietly dropped calendar scope would
surface as a generic failure on "book lunch Friday", in production, with the working token already
overwritten. Two tokens means that can never happen.
"""
from __future__ import annotations

import logging
import socket
import threading

log = logging.getLogger("mary.tools.people")

SCOPES = ["https://www.googleapis.com/auth/contacts"]
SCOPES_READONLY = ["https://www.googleapis.com/auth/contacts.readonly"]

# Everything we ever read about a person. `metadata` is not optional decoration: updateContact
# REQUIRES person.metadata.sources in the body, and the precondition is checked against
# metadata.sources[].etag rather than the top-level person.etag.
# `nicknames` is what makes "add Zen" reach Zenaldo Tanaka: Google already stores the alias the
# owner actually uses, and reading it costs nothing over a field list we were fetching anyway.
#
# `organizations` is here for the card's company, which is what tells fifteen Felipes apart and what
# a search by company reads. Changing this list changes the sync request, and a sync token only
# works with the parameters that minted it — `list_all` recovers from that with a full sweep.
PERSON_FIELDS = "names,nicknames,emailAddresses,phoneNumbers,organizations,metadata"

# The whole card, read fresh when one is being shown or changed. Never part of the sync: nothing in
# the reply path needs a birthday, and the mirror has no reason to hold anyone's notes.
CARD_FIELDS = f"{PERSON_FIELDS},addresses,birthdays,biographies"

# Everything a person can hold, for the copy kept before a delete. A delete removes all of it, so a
# copy of just the fields Lisa edits would not be an undo.
ALL_FIELDS = ("addresses,ageRanges,biographies,birthdays,calendarUrls,clientData,coverPhotos,"
              "emailAddresses,events,externalIds,genders,imClients,interests,locales,locations,"
              "memberships,metadata,miscKeywords,names,nicknames,occupations,organizations,"
              "phoneNumbers,photos,relations,sipAddresses,skills,urls,userDefined")

# What a write can actually change. A person's default read merges in their Google profile, which
# updateContact ignores ("Any non-contact data in the person to update will be ignored"), so an
# entry read that way cannot be removed: the update succeeds and the value is still there.
SOURCES_CONTACT = ["READ_SOURCE_TYPE_CONTACT"]
SOURCES_MERGED = ["READ_SOURCE_TYPE_CONTACT", "READ_SOURCE_TYPE_PROFILE"]

# field -> (People field, the key holding its text).
_LIST_FIELDS = {
    "email": ("emailAddresses", "value"),
    "phone": ("phoneNumbers", "value"),
    "nickname": ("nicknames", "value"),
    "address": ("addresses", "formattedValue"),
}
# A contact source holds at most one of these, and sending two is a 400.
_SINGLE_FIELDS = {"name", "birthday", "notes"}
# Both live on the card's first organization, so changing one must not lose the other.
_ORG_FIELDS = {"company": "name", "title": "title"}

FIELDS = (*_LIST_FIELDS, *_SINGLE_FIELDS, *_ORG_FIELDS)

# Transport failures where the request never reached Google, so a replay is safe. Reads only —
# a mutation never replays here regardless (see the module docstring).
_TRANSIENT_EXC = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                  socket.timeout, TimeoutError)
_TRANSIENT_STATUS = {500, 502, 503, 504}


def is_transient(exc: Exception) -> bool:
    """Did the request fail before Google could answer?"""
    if isinstance(exc, _TRANSIENT_EXC):
        return True
    return getattr(getattr(exc, "resp", None), "status", None) in _TRANSIENT_STATUS


def _status(exc: Exception):
    return getattr(getattr(exc, "resp", None), "status", None)


def _body_text(exc: Exception) -> str:
    return f"{getattr(exc, 'content', b'')!s} {exc!s}"


def is_expired_sync_token(exc: Exception) -> bool:
    """The People API's expired-syncToken signal.

    410 GONE is the CALENDAR contract. People returns **400** carrying an ErrorInfo whose reason is
    EXPIRED_SYNC_TOKEN, so a handler that only watches for 410 lets the sync loop die permanently
    about a week after deploy — silently, because the book simply stops updating.
    """
    if _status(exc) not in (400, 410):
        return False
    body = _body_text(exc)
    return "EXPIRED_SYNC_TOKEN" in body or "Sync token is expired" in body


def view(person: dict) -> dict:
    """A People `person` reduced to what the Directory stores."""
    names = person.get("names") or []
    meta = person.get("metadata") or {}
    return {
        "resource_name": person.get("resourceName") or "",
        "etag": person.get("etag") or "",
        "name": (names[0].get("displayName") if names else "") or "",
        "nicknames": [n["value"].strip() for n in (person.get("nicknames") or [])
                      if (n.get("value") or "").strip()],
        # Lowercased at the boundary so there is ONE canonical form everywhere: the index keys,
        # the stored list and `preferred` all compare directly. Storing Google's casing meant
        # `preferred in emails` silently failed and the preferred-address ordering never applied.
        #
        # dict.fromkeys because a default read returns one entry per SOURCE: an address that is on
        # both the contact and the person's Google profile comes back twice, and one address must
        # not read as the two that make Lisa ask "which?".
        "emails": list(dict.fromkeys(e["value"].strip().lower()
                                     for e in (person.get("emailAddresses") or []) if e.get("value"))),
        # canonicalForm is Google's own E.164 parse; the raw value is whatever was typed.
        "phones": list(dict.fromkeys(p.get("canonicalForm") or p.get("value") or ""
                                     for p in (person.get("phoneNumbers") or []))),
        "company": ((person.get("organizations") or [{}])[0].get("name") or "").strip(),
        "deleted": bool(meta.get("deleted")),
        "source": "google",
    }


def card_view(person: dict) -> dict:
    """`view` plus what only a fresh read carries — the whole card, for showing or changing it."""
    orgs = person.get("organizations") or [{}]
    birthdays = person.get("birthdays") or [{}]
    bios = person.get("biographies") or [{}]
    return {
        **view(person),
        "title": (orgs[0].get("title") or "").strip(),
        "addresses": [a.get("formattedValue") or "" for a in (person.get("addresses") or [])
                      if a.get("formattedValue")],
        "birthday": _date_text(birthdays[0].get("date")),
        "notes": (bios[0].get("value") or "").strip(),
    }


def _date_text(d: dict | None) -> str:
    """A People date as ISO, or MM-DD for a birthday saved without its year."""
    if not d or not d.get("month") or not d.get("day"):
        return ""
    md = f"{d['month']:02d}-{d['day']:02d}"
    return f"{d['year']:04d}-{md}" if d.get("year") else md


def parse_birthday(text: str) -> dict | None:
    """"1985-03-14" or "03-14" -> a People date. None for anything else: a bad date is the model's
    to correct, and guessing at one writes a wrong birthday onto a real card."""
    parts = (text or "").strip().split("-")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        month, day, year = nums[0], nums[1], None
    elif len(nums) == 3:
        year, month, day = nums
    else:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return {"year": year, "month": month, "day": day} if year else {"month": month, "day": day}


def _name_body(name: str, email: str = "") -> dict:
    parts = (name or "").strip().split()
    given = parts[0] if parts else (email.split("@")[0] if email else "")
    family = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {"givenName": given, "familyName": family}


def _digits(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())


def same_value(field: str, a: str, b: str) -> bool:
    """Do these two spellings name the same value on a card?

    Phones compare by digits, because one side is what Google stores (+5511987654321) and the other
    is how a person types it (11 98765-4321), and Brazil's missing ninth digit is the same number
    too. Only ever used WITHIN one card, to say which of its entries is meant: it is not the
    identity match that `directory.match_keys` is careful about, so a shorter tail is fine here."""
    a, b = (a or "").strip(), (b or "").strip()
    if field != "phone":
        return a.casefold() == b.casefold()
    ka, kb = _phone_keys(a), _phone_keys(b)
    if not ka or not kb:
        return False
    if ka & kb:
        return True
    short, long_ = sorted((_digits(a), _digits(b)), key=len)
    return len(short) >= 8 and long_.endswith(short) and len(long_) - len(short) <= 3


def _phone_keys(text: str) -> set:
    d = _digits(text)
    if not d:
        return set()
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    keys = {d}
    if len(d) == 11 and d[2] == "9":
        keys.add(d[:2] + d[3:])
    elif len(d) == 10:
        keys.add(d[:2] + "9" + d[2:])
    return keys


class ChangeError(Exception):
    """This change cannot apply to the card as it stands. Nothing was sent to Google."""


class GooglePeople:
    """Thin, synchronous People v1 client. Every method is called from a background worker."""

    def __init__(self, settings, *, readonly: bool = False) -> None:
        self.s = settings
        self._scopes = SCOPES_READONLY if readonly else SCOPES
        self._svc = None   # test seam ONLY: the selftests inject a fake
        self._creds = None
        # "Mutate requests for the same user should be sent sequentially to avoid increased latency
        # and failures." A confirmed write runs in a worker thread and the outbox drain in another,
        # so nothing else keeps them apart.
        self._write_lock = threading.Lock()

    # ---- auth / service ----------------------------------------------------------------

    def _credentials(self):
        if self._creds is None:
            from google.oauth2.credentials import Credentials

            self._creds = Credentials(
                token=None,
                refresh_token=self.s.google_contacts_refresh_token,
                client_id=self.s.google_client_id,
                client_secret=self.s.google_client_secret,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=self._scopes,
            )
        return self._creds

    def _service(self):
        """A fresh client per call — see the module docstring. `_svc` survives as a test seam."""
        if self._svc is not None:
            return self._svc
        from googleapiclient.discovery import build

        return build("people", "v1", credentials=self._credentials(),
                     cache_discovery=False, static_discovery=True)

    def check_scopes(self) -> tuple[bool, str]:
        """(ok, detail) — refresh once at boot and confirm contacts scope was actually granted.

        google-auth only warns on a missing scope, so without this the feature limps and fails
        mysteriously later. The caller turns the feature OFF rather than limping.
        """
        try:
            import google.auth.transport.requests as tr

            creds = self._credentials()
            creds.refresh(tr.Request())
        except Exception as exc:
            return False, f"contacts token refresh failed: {exc}"
        granted = set(getattr(creds, "granted_scopes", None) or [])
        if not granted:
            return True, "granted_scopes not reported; proceeding"
        # Check the scope THIS client needs, not any contacts scope. Google's consent screen offers
        # read and read-write as separate checkboxes, so accepting `.readonly` for the read-write
        # client let through a token whose every write 403s — the exact "limps and fails
        # mysteriously later" outcome this gate exists to prevent.
        if any(sc in granted for sc in self._scopes):
            return True, f"granted: {sorted(self._scopes)}"
        return False, (f"required scope NOT granted — need one of {sorted(self._scopes)}, "
                       f"have {sorted(granted)}")

    # ---- reads -------------------------------------------------------------------------

    def list_all(self, sync_token: str | None) -> tuple[list[dict], str | None, bool]:
        """(people, next_sync_token, was_full) over every page.

        With a sync token the response is a DELTA — only what changed, and empty when nothing did.
        The caller must MERGE it; assigning it over the snapshot (the way Roster.refresh replaces
        its rules, which is correct there because its read is an unconditional full SELECT) would
        empty the address book down to whatever moved in the last 15 minutes.
        """
        svc = self._service()
        out: list[dict] = []
        page = None
        token = sync_token
        was_full = sync_token is None
        while True:
            params = dict(resourceName="people/me", pageSize=1000,
                          personFields=PERSON_FIELDS, requestSyncToken=True)
            if page:
                params["pageToken"] = page
            if token:
                params["syncToken"] = token
            try:
                resp = svc.people().connections().list(**params).execute()
            except Exception as exc:
                # An expired token, or any other 400 on a token: "all other parameters ... must
                # match the first call that provided the sync token", so the first sync after
                # PERSON_FIELDS changes is refused, and retrying the delta would be refused forever
                # while the book silently stopped updating. A full sweep is the one recovery, and
                # it needs no token, so a 400 that is really about the request still raises below.
                if token and (is_expired_sync_token(exc) or _status(exc) == 400):
                    # Start over as a full sweep, in this same call.
                    log.warning("people sync token refused (%s); full resync", _status(exc))
                    token, page, out, was_full = None, None, [], True
                    continue
                raise
            out.extend(resp.get("connections") or [])
            page = resp.get("nextPageToken")
            if not page:
                return out, resp.get("nextSyncToken"), was_full

    def get(self, resource_name: str) -> dict:
        return self._service().people().get(
            resourceName=resource_name, personFields=PERSON_FIELDS).execute()

    def read_cards(self, resource_names: list) -> list:
        """The whole card for each, in one call. A card that is gone is left out, not an error."""
        if not resource_names:
            return []
        resp = self._service().people().getBatchGet(
            resourceNames=list(resource_names), personFields=CARD_FIELDS).execute()
        return [r["person"] for r in (resp.get("responses") or []) if r.get("person")]

    # ---- writes (each runs exactly once; the outbox owns retrying) ----------------------

    def apply_changes(self, resource_name: str, changes: list) -> tuple:
        """Apply `changes` to a card, against FRESH data, and report what really happened.

        Returns (person, report), where person is the merged result and report is
        {"noop": bool, "stuck": [{"field", "value"}]}. `noop` means the card already said this, which
        is what makes a replay safe with no request key. `stuck` lists removed values still on the
        person, because they come from his Google profile and no contact write can take them off.
        Lisa reads the merged person and writes the contact, so the two have to be told apart here,
        or a change to a profile value reports the wrong thing (see `_against_profile`).

        One re-read on a stale etag. Google refused that write outright, so nothing was applied,
        and the merge is rebuilt from the card as it now stands. Anything else is the caller's.
        """
        with self._write_lock:
            svc = self._service()
            for attempt in (1, 2):
                person = svc.people().get(resourceName=resource_name, personFields=CARD_FIELDS,
                                          sources=SOURCES_CONTACT).execute()
                showing = svc.people().get(resourceName=resource_name, personFields=CARD_FIELDS,
                                           sources=SOURCES_MERGED).execute()
                settled, gone = _against_profile(changes, person, showing)
                body, fields, noop = build_update(person, settled)
                if noop:
                    return showing, {"noop": True, "stuck": _stuck(showing, gone)}
                try:
                    merged = svc.people().updateContact(
                        resourceName=resource_name, updatePersonFields=",".join(fields),
                        personFields=CARD_FIELDS, sources=SOURCES_MERGED, body=body).execute()
                except Exception as exc:
                    if attempt == 1 and _is_stale_etag(exc):
                        log.warning("contact changed under the write; re-reading %s", resource_name)
                        continue
                    raise
                return merged, {"noop": False, "stuck": _stuck(merged, gone)}
        raise AssertionError("unreachable")  # the loop always returns or raises

    def create_card(self, name: str, changes: list) -> dict:
        """Create a person with everything given. Runs once: a replay would duplicate them."""
        seed = {"names": [_name_body(name)]}
        built, _fields, _noop = build_update(seed, changes)
        body = {**seed, **{k: v for k, v in built.items() if k not in ("etag", "metadata")}}
        with self._write_lock:
            return self._service().people().createContact(
                body=body, personFields=CARD_FIELDS).execute()

    def read_card(self, resource_name: str) -> dict:
        """The card as its contact source holds it, every field: what a delete removes."""
        return self._service().people().get(resourceName=resource_name, personFields=ALL_FIELDS,
                                            sources=SOURCES_CONTACT).execute()

    def delete_contact(self, resource_name: str) -> None:
        """Delete a card. Google's docs do not say this can be undone: read a copy first."""
        with self._write_lock:
            self._service().people().deleteContact(resourceName=resource_name).execute()

    def add_email(self, resource_name: str, email: str) -> dict:
        """Append one address, read-modify-write, in ONE call against FRESH data.

        `updatePersonFields` REPLACES the whole field — "append" is not an API operation, it is
        something you construct, and constructing it from a cached snapshot deletes every address
        added elsewhere since the last sync. This repo already paid for that class one layer down
        (skills/calendar_format.py: "the owner approved an unnamed change that removed every guest
        from the meeting"), so the snapshot never appears in this path.

        Returns the person; a no-op when the address is already there, which is what makes a
        replayed outbox row idempotent — People offers no request key to do it with.
        """
        with self._write_lock:
            svc = self._service()
            p = svc.people().get(resourceName=resource_name, personFields=PERSON_FIELDS).execute()
            existing = p.get("emailAddresses") or []
            if any((e.get("value") or "").lower() == email.lower() for e in existing):
                return p
            body = {
                "etag": p.get("etag"),
                # REQUIRED — omitting it is a 400 on every single write.
                "metadata": p.get("metadata") or {},
                # Existing entries kept WHOLE. Rebuilding them as {"value": e} would wipe every
                # type / formattedType / primary flag on the owner's real contact.
                "emailAddresses": [*existing, {"value": email}],
            }
            return svc.people().updateContact(
                resourceName=resource_name, updatePersonFields="emailAddresses",
                body=body).execute()

    def create(self, name: str, email: str) -> dict:
        """Create a person. Runs once — a replay would duplicate them (no idempotency key)."""
        body = {
            "names": [_name_body(name, email)],
            "emailAddresses": [{"value": email}],
        }
        with self._write_lock:
            return self._service().people().createContact(body=body).execute()


# --- the merge -----------------------------------------------------------------------------

def build_update(person: dict, changes: list) -> tuple:
    """(body, updatePersonFields, noop) for these changes against this card. Pure, and it raises
    ChangeError instead of guessing, before anything has been sent.

    Same discipline as `add_email`: updatePersonFields REPLACES each field it names, so every field
    touched is rebuilt from the fresh card, its untouched entries kept WHOLE (their type, primary
    flag and metadata), and every field not touched is left out so Google leaves it alone."""
    edited: dict = {}

    def current(api_field: str) -> list:
        if api_field not in edited:
            edited[api_field] = [dict(e) for e in (person.get(api_field) or [])]
        return edited[api_field]

    for ch in changes or []:
        field, op = ch.get("field"), ch.get("op")
        old, new = (ch.get("old") or "").strip(), (ch.get("new") or "").strip()
        if field in _LIST_FIELDS:
            api_field, key = _LIST_FIELDS[field]
            _change_list(current(api_field), key, field, op, old, new)
        elif field in _ORG_FIELDS:
            _change_org(current("organizations"), _ORG_FIELDS[field], op, new)
        elif field == "name":
            if op == "remove" or not new:
                raise ChangeError("A contact cannot lose its name; give the new one instead.")
            names = person.get("names") or []
            if not names or (names[0].get("displayName") or "").casefold() != new.casefold():
                edited["names"] = [_name_body(new)]
        elif field == "birthday":
            edited["birthdays"] = _birthday_entries(person.get("birthdays") or [], op, new)
        elif field == "notes":
            edited["biographies"] = _notes_entries(person.get("biographies") or [], op, new)
        else:
            raise ChangeError(f"No such contact field: {field!r}.")

    # A change that landed where the card already was is no change at all, and naming that field in
    # updatePersonFields would only rewrite it for nothing.
    edited = {k: v for k, v in edited.items() if v != (person.get(k) or [])}
    body = {"etag": person.get("etag"), "metadata": person.get("metadata") or {}, **edited}
    return body, sorted(edited), not edited


def _change_list(entries: list, key: str, field: str, op: str, old: str, new: str) -> None:
    """Apply one change to a repeated field, in place."""
    def at(value: str) -> int | None:
        for i, e in enumerate(entries):
            if same_value(field, e.get(key) or "", value):
                return i
        return None

    if op == "add":
        if not new:
            raise ChangeError(f"Adding a {field} needs the value to add.")
        if at(new) is None:
            entries.append({key: new})
    elif op == "remove":
        target = old or new
        if not target:
            raise ChangeError(f"Removing a {field} needs to say which one.")
        i = at(target)
        if i is not None:
            del entries[i]
    elif op == "replace":
        if not new:
            raise ChangeError(f"Replacing a {field} needs the new value.")
        if old:
            i = at(old)
            if i is None:
                if at(new) is not None:
                    return  # already applied: this is the replay
                # Not the value itself: this goes back to the model, which may only have the mask.
                raise ChangeError(f"This card no longer has that {field}.")
        elif len(entries) > 1:
            raise ChangeError(f"This card has {len(entries)} of field {field}; say which to replace.")
        elif entries:
            i = 0
        else:
            entries.append({key: new})
            return
        if same_value(field, entries[i].get(key) or "", new):
            return
        dupe = at(new)
        # Only the text changes: the entry keeps its type, so a work address stays a work address.
        kept = {k: v for k, v in entries[i].items() if k == "type"}
        entries[i] = {**kept, key: new}
        if dupe is not None:
            del entries[dupe]
    else:
        raise ChangeError(f"No such operation: {op!r}.")


def _change_org(orgs: list, key: str, op: str, new: str) -> None:
    value = "" if op == "remove" else new
    if not orgs:
        if value:
            orgs.append({key: value})
        return
    if value:
        orgs[0][key] = value
    else:
        orgs[0].pop(key, None)
        if not orgs[0].get("name") and not orgs[0].get("title"):
            del orgs[0]


def _birthday_entries(existing: list, op: str, new: str) -> list:
    if op == "remove":
        return []
    date = parse_birthday(new)
    if date is None:
        raise ChangeError(f"{new!r} is not a date; send YYYY-MM-DD, or MM-DD with no year.")
    # An unchanged date returns the card's own entry, so the caller sees no change to send.
    if existing and existing[0].get("date") == date:
        return existing
    return [{"date": date}]


def _notes_entries(existing: list, op: str, new: str) -> list:
    """`add` appends. Notes are one field on the card, so an add that replaced it would turn "she is
    vegan" into the loss of everything he had written about her."""
    before = (existing[0].get("value") or "").strip() if existing else ""
    if op == "remove":
        after = ""
    elif op == "add" and before:
        after = before if new in before else f"{before}\n{new}"
    else:
        after = new
    if after == before:
        return existing
    return [{"value": after, "contentType": "TEXT_PLAIN"}] if after else []


def _against_profile(changes: list, card: dict, showing: dict) -> tuple:
    """(changes as they can apply to the contact, the values meant to go away).

    `showing` is what Lisa and the owner see, and it can include his Google profile, which a contact
    write cannot touch. So a value that only lives there is not an error, and not "already done":
    replacing it adds the new value beside it, removing it changes nothing, and both report that it
    is still showing. Also pins down a blind replace, so the card's one value is named from here on.
    """
    settled, gone = [], []
    for ch in changes or []:
        field, op = ch.get("field"), ch.get("op")
        if field not in _LIST_FIELDS or op not in ("replace", "remove"):
            settled.append(ch)
            continue
        api_field, key = _LIST_FIELDS[field]
        on_card = [e.get(key) or "" for e in card.get(api_field) or []]
        visible = [e.get(key) or "" for e in showing.get(api_field) or []]
        old = (ch.get("old") or "").strip() or ((ch.get("new") or "").strip() if op == "remove" else "")
        if not old and len(visible) == 1:
            old = visible[0]
        if not old:
            settled.append(ch)
            continue
        gone.append({"field": field, "value": old})
        profile_only = (not any(same_value(field, v, old) for v in on_card)
                        and any(same_value(field, v, old) for v in visible))
        if not profile_only:
            settled.append({**ch, "old": old})
        elif op == "replace":
            settled.append({**ch, "op": "add", "old": ""})
    return settled, gone


def _stuck(person: dict, gone: list) -> list:
    """The values meant to go away that the person still carries."""
    out = []
    for g in gone:
        api_field, key = _LIST_FIELDS[g["field"]]
        if any(same_value(g["field"], e.get(key) or "", g["value"]) for e in person.get(api_field) or []):
            out.append(g)
    return out


def _is_stale_etag(exc: Exception) -> bool:
    return _status(exc) == 400 and "failedprecondition" in _body_text(exc).replace("_", "").lower()
