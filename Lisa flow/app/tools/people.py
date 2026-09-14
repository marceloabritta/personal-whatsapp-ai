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

log = logging.getLogger("mary.tools.people")

SCOPES = ["https://www.googleapis.com/auth/contacts"]
SCOPES_READONLY = ["https://www.googleapis.com/auth/contacts.readonly"]

# Everything we ever read about a person. `metadata` is not optional decoration: updateContact
# REQUIRES person.metadata.sources in the body, and the precondition is checked against
# metadata.sources[].etag rather than the top-level person.etag.
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,metadata"

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
        "emails": [e["value"] for e in (person.get("emailAddresses") or []) if e.get("value")],
        # canonicalForm is Google's own E.164 parse; the raw value is whatever was typed.
        "phones": [p.get("canonicalForm") or p.get("value") or ""
                   for p in (person.get("phoneNumbers") or [])],
        "deleted": bool(meta.get("deleted")),
        "source": "google",
    }


class GooglePeople:
    """Thin, synchronous People v1 client. Every method is called from a background worker."""

    def __init__(self, settings, *, readonly: bool = False) -> None:
        self.s = settings
        self._scopes = SCOPES_READONLY if readonly else SCOPES
        self._svc = None   # test seam ONLY: the selftests inject a fake
        self._creds = None

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
                if token and is_expired_sync_token(exc):
                    # Start over as a full sweep, in this same call.
                    log.warning("people sync token expired; full resync")
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

    # ---- writes (each runs exactly once; the outbox owns retrying) ----------------------

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
            resourceName=resource_name, updatePersonFields="emailAddresses", body=body).execute()

    def create(self, name: str, email: str) -> dict:
        """Create a person. Runs once — a replay would duplicate them (no idempotency key)."""
        parts = (name or "").strip().split()
        given = parts[0] if parts else (email.split("@")[0] if email else "")
        family = " ".join(parts[1:]) if len(parts) > 1 else ""
        body = {
            "names": [{"givenName": given, "familyName": family}],
            "emailAddresses": [{"value": email}],
        }
        return self._service().people().createContact(body=body).execute()
