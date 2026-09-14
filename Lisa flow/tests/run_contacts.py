"""Contact memory — the offline selftest.

Every check here exists because a reviewer found the defect it guards, or because a measurement
contradicted the design. Run it with the repo venv:

    cd "Lisa flow" && .venv/bin/python tests/run_contacts.py

No network, no Google, no Postgres: a fake People service records every request body so the tests
can assert what WOULD have been sent — which is the only way to catch the write bugs, since they
are all about the shape of the request rather than its result.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings                                    # noqa: E402
from app.directory import Directory, GOOGLE, to_e164, match_keys   # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  — {detail}" if detail else ""))


def contact(rn, name, emails=(), phones=(), source=GOOGLE):
    return {"resource_name": rn, "etag": f"etag-{rn}", "name": name,
            "emails": list(emails), "phones": list(phones), "source": source,
            "preferred": None, "used_at": None}


# --- the fake People service ---------------------------------------------------------------

class FakePeople:
    """Records every call. `pages` lets a test script a paged / delta / failing sync."""

    def __init__(self, people=None, *, fail_add=None):
        self.people = {p["resourceName"]: p for p in (people or [])}
        self.calls: list = []          # (method, payload)
        self.update_bodies: list = []
        self.created: list = []
        self.fail_add = fail_add       # an exception to raise from add_email

    # reads
    def list_all(self, sync_token):
        self.calls.append(("list_all", sync_token))
        return list(self.people.values()), "token-2", sync_token is None

    def get(self, rn):
        self.calls.append(("get", rn))
        return self.people[rn]

    # writes
    def add_email(self, rn, email):
        self.calls.append(("add_email", (rn, email)))
        if self.fail_add:
            raise self.fail_add
        p = self.people[rn]                       # read-modify-write against FRESH data
        existing = p.get("emailAddresses") or []
        if any((e.get("value") or "").lower() == email.lower() for e in existing):
            return p
        body = {"etag": p.get("etag"), "metadata": p.get("metadata") or {},
                "emailAddresses": [*existing, {"value": email}]}
        self.update_bodies.append(body)
        p["emailAddresses"] = body["emailAddresses"]
        return p

    def create(self, name, email):
        self.calls.append(("create", (name, email)))
        rn = f"people/new{len(self.created)}"
        p = {"resourceName": rn, "etag": "e", "names": [{"displayName": name}],
             "emailAddresses": [{"value": email}], "metadata": {"sources": [{"etag": "s"}]}}
        self.created.append(p)
        self.people[rn] = p
        return p


class FakeStore:
    def __init__(self):
        self.jobs: list = []
        self.next_id = 1
        self.upserts: list = []
        self.links: dict = {}
        self.failed: list = []

    async def enqueue(self, job):
        self.jobs.append({**job, "id": self.next_id, "attempts": 0})
        self.next_id += 1

    async def pending(self, *, limit=20):
        return list(self.jobs[:limit])

    async def done(self, job_id):
        self.jobs = [j for j in self.jobs if j["id"] != job_id]

    async def fail(self, job_id, error, max_attempts):
        for j in self.jobs:
            if j["id"] == job_id:
                j["attempts"] += 1
                self.failed.append(error)
                if j["attempts"] >= max_attempts:
                    await self.done(job_id)
                    return True
        return False

    async def upsert(self, c):
        self.upserts.append(dict(c))

    async def replace(self, contacts, token, *, full):
        pass

    async def link(self, phone, rn, source):
        self.links[phone] = rn


def settings(**kw):
    base = dict(contacts_enabled=True, contacts_default_region="BR",
                google_contacts_refresh_token="x", contacts_write_attempts=3,
                contacts_create_people=True)
    base.update(kw)
    return Settings(**base)


def book():
    d = Directory(settings())
    d.load([
        contact("people/1", "Ana Silva", ["ana.silva@acme.com"], ["+55 11 98765-4321"]),
        contact("people/2", "Bruno Tavares", ["bruno@t.co", "b.t@gmail.com"], ["+55 21 91234-5678"]),
        contact("people/3", "Carla Mendes", [], ["+55 11 3456-7890"]),
        contact("people/4", "Ana Souza", ["ana.souza@x.com"]),
    ])
    return d


# --- 1. phone identity ---------------------------------------------------------------------

def phone_checks():
    print("\nphone identity")
    check("JID (international, no +) parses", to_e164("5511987654321", "BR") == "+5511987654321")
    check("national parses", to_e164("11 98765-4321", "BR") == "+5511987654321")
    check("landline parses", to_e164("+55 11 3456-7890", "BR") == "+551134567890")
    check("legacy 8-digit repaired (with CC)", to_e164("551187654321", "BR") == "+5511987654321")
    check("legacy 8-digit repaired (national)", to_e164("11 8765-4321", "BR") == "+5511987654321")
    check("junk is not a number", to_e164("ramal 22", "BR") is None)
    check("empty is not a number", to_e164("", "BR") is None)
    check("match keys carry both BR spellings",
          set(match_keys("+5511987654321")) == {"+5511987654321", "+551187654321"})

    d = book()
    check("chat phone resolves its contact",
          [c["name"] for c, _ in d.mentions("oi", phone="5511987654321")] == ["Ana Silva"])
    # Different area code, same 8-digit tail: the rule rev 1 used would have bound this.
    d2 = Directory(settings())
    d2.load([contact("people/9", "Outra Pessoa", ["x@x.com"], ["+55 21 98765-4321"])])
    check("same tail, different area code does NOT match",
          d2.mentions("oi", phone="5511987654321") == [])


# --- 2. retrieval --------------------------------------------------------------------------

def retrieval_checks():
    print("\nretrieval")
    d = book()
    check("full name matches",
          [c["name"] for c, _ in d.mentions("marca com a Ana Silva amanha")] == ["Ana Silva"])
    check("BARE FIRST NAME BINDS NOBODY", d.mentions("marca com a Ana amanha") == [])
    check("phone + first name is the strongest signal",
          d.mentions("marca com a Ana", phone="5511987654321")[0][1] == "phone+name")
    check("literal email matches",
          [c["name"] for c, _ in d.mentions("o email e bruno@t.co")] == ["Bruno Tavares"])
    check("speaker labels are not scanned", d.mentions("Ana Silva: bom dia") == [])
    check("a group does not serve the global book",
          d.mentions("marca com a Ana Silva", group=True) == [])
    check("a group still resolves the participant's own phone",
          len(d.mentions("oi", phone="5511987654321", group=True)) == 1)
    check("cold directory matches nothing",
          Directory(settings()).mentions("Ana Silva", phone="5511987654321") == [])

    block = d.block(d.mentions("Ana Silva e Bruno Tavares"), "Marcelo")
    check("block flags a contact with two addresses", "MORE THAN ONE" in block)
    check("block never auto-picks for the model", "ask which" in block)
    c3 = d.mentions("Carla Mendes")
    check("block states 'no email on file'", "no email on file" in d.block(c3, "Marcelo"))

    # latency — the claim rev 1 got wrong by 2500x
    big = [contact(f"people/{i}", f"Nome{i} Sobrenome{i}", [f"n{i}@x.com"],
                   [f"+5511 9{10000000 + i}"]) for i in range(2000)]
    d.load(big)
    text = " ".join(["bom dia marcelo vamos marcar aquela reuniao amanha sobre o projeto"] * 20)
    t0 = time.perf_counter()
    for _ in range(20):
        d.mentions(text)
    ms = (time.perf_counter() - t0) / 20 * 1000
    check(f"mentions() over 2000 contacts is fast ({ms:.2f} ms)", ms < 5.0, f"{ms:.2f} ms")

    # Guard the IMPORT, not the word — the module names difflib in a docstring explaining
    # precisely why it is not used, and that explanation should not trip its own guard.
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "directory.py")).read()
    check("difflib is never imported on the hot path",
          "import difflib" not in src and "difflib." not in src)


# --- 3. write rules (Q1/Q3 resolution) -----------------------------------------------------

def write_rule_checks():
    print("\nwrite rules")
    d = book()
    check("contact with NO address -> add",
          (d.plan_write("Carla Mendes", "carla@x.com") or {}).get("kind") == "add_email")
    check("CORRECTION on a contact that has one -> NO WRITE",
          d.plan_write("Ana Silva", "ana.new@acme.com") is None)
    check("address already known -> no write",
          d.plan_write("Ana Silva", "ana.silva@acme.com") is None)
    check("unknown person, identity unproven -> learned only",
          (d.plan_write("Novo Alguem", "novo@x.com") or {}).get("kind") == "learn")
    check("wrong phone + different name does not target the chat's contact",
          (d.plan_write("Novo Alguem", "novo@x.com", phone="5511987654321") or {}).get("kind")
          == "create")
    check("garbage email is refused", d.plan_write("X", "not-an-email") is None)
    check("cold directory plans nothing",
          Directory(settings()).plan_write("Ana Silva", "a@b.com") is None)


# --- 4. the Google write path --------------------------------------------------------------

async def people_checks():
    print("\nthe write path")
    from app.tools.people import GooglePeople, is_expired_sync_token

    person = {"resourceName": "people/3", "etag": "etag-3",
              "names": [{"displayName": "Carla Mendes"}],
              "emailAddresses": [{"value": "old@x.com", "type": "work"}],
              "metadata": {"sources": [{"etag": "src-etag"}]}}
    fake = FakePeople([person])
    gp = GooglePeople(settings())
    gp._svc = None
    # exercise the real add_email against a stub service object
    class Svc:
        def people(self):
            return self
        def get(self, resourceName=None, personFields=None):
            class R:
                def execute(_):
                    return person
            return R()
        def updateContact(self, resourceName=None, updatePersonFields=None, body=None):
            fake.update_bodies.append(body)
            class R:
                def execute(_):
                    return {**person, **body}
            return R()
    gp._svc = Svc()

    gp.add_email("people/3", "new@x.com")
    body = fake.update_bodies[-1]
    check("write carries metadata (400 on every write without it)", "metadata" in body)
    check("write carries etag", body.get("etag") == "etag-3")
    check("existing entry preserved WHOLE (type not stripped)",
          body["emailAddresses"][0].get("type") == "work")
    check("new address appended, not replaced",
          [e["value"] for e in body["emailAddresses"]] == ["old@x.com", "new@x.com"])

    n = len(fake.update_bodies)
    gp.add_email("people/3", "OLD@x.com")   # case-insensitive duplicate
    check("replayed job is a no-op (idempotency is a read-back)", len(fake.update_bodies) == n)

    class Resp:
        def __init__(self, status):
            self.status = status

    class Err(Exception):
        def __init__(self, status, content):
            self.resp = Resp(status)
            self.content = content

    check("expired sync token is detected at 400",
          is_expired_sync_token(Err(400, b'{"reason":"EXPIRED_SYNC_TOKEN"}')))
    check("expired sync token also accepted at 410",
          is_expired_sync_token(Err(410, b"Sync token is expired")))
    check("an ordinary 400 is not a sync-token expiry",
          not is_expired_sync_token(Err(400, b'{"reason":"INVALID_ARGUMENT"}')))


# --- 5. sync -------------------------------------------------------------------------------

async def sync_checks():
    print("\nsync")
    s = settings()
    people = [{"resourceName": f"people/{i}", "etag": "e",
               "names": [{"displayName": f"Nome{i} Sobrenome{i}"}],
               "emailAddresses": [{"value": f"n{i}@x.com"}],
               "phoneNumbers": [], "metadata": {}} for i in range(50)]
    fake = FakePeople(people)
    d = Directory(s, people=fake)
    await d.refresh(full=True)
    check("full sync loads the book", len(d._contacts) == 50)
    check("directory becomes ready", d.ready)

    # A DELTA returning one person must not replace the snapshot.
    class DeltaPeople(FakePeople):
        def list_all(self, sync_token):
            changed = {"resourceName": "people/0", "etag": "e2",
                       "names": [{"displayName": "Nome0 Mudou"}],
                       "emailAddresses": [{"value": "novo@x.com"}],
                       "phoneNumbers": [], "metadata": {}}
            return [changed], "token-3", False
    d.people = DeltaPeople()
    await d.refresh()
    check("DELTA MERGES — the other 49 survive", len(d._contacts) == 50,
          f"got {len(d._contacts)}")
    check("the changed person is updated", d._contacts["people/0"]["name"] == "Nome0 Mudou")

    # A tombstone removes the contact.
    class TombPeople(FakePeople):
        def list_all(self, sync_token):
            return [{"resourceName": "people/1", "metadata": {"deleted": True}}], "t4", False
    d.people = TombPeople()
    await d.refresh()
    check("a deleted contact is forgotten", "people/1" not in d._contacts)

    # A learned entry survives a full resync (it is not in Google).
    d.people = FakePeople(people)
    await d._apply_learned({"resource_name": None, "name": "Aprendido", "email": "ap@x.com"})
    await d.refresh(full=True)
    check("learned entries survive a full resync", d.by_email("ap@x.com") is not None)


# --- 6. outbox -----------------------------------------------------------------------------

async def outbox_checks():
    print("\noutbox")
    s = settings()
    person = {"resourceName": "people/3", "etag": "e",
              "names": [{"displayName": "Carla Mendes"}],
              "emailAddresses": [], "phoneNumbers": [], "metadata": {"sources": [{"etag": "s"}]}}
    fake = FakePeople([person])
    store = FakeStore()
    d = Directory(s, store=store, people=fake)
    d.load([contact("people/3", "Carla Mendes", [], ["+55 11 3456-7890"])])

    await d.remember("Carla Mendes", "carla@x.com")
    check("a write is recorded durably BEFORE it is attempted", len(store.jobs) == 1)
    check("nothing has reached Google yet",
          not [c for c in fake.calls if c[0] in ("add_email", "create")])

    await d.drain()
    check("the drain performs the write", ("add_email", ("people/3", "carla@x.com")) in fake.calls)
    check("a completed job is deleted, not kept", store.jobs == [])

    # terminal failure rolls the optimistic patch back
    fake2 = FakePeople([person], fail_add=RuntimeError("boom"))
    store2 = FakeStore()
    d2 = Directory(settings(contacts_write_attempts=1), store=store2, people=fake2)
    d2.load([contact("people/3", "Carla Mendes", [], [])])
    d2._contacts["people/3"]["emails"] = ["carla@x.com"]   # optimistic patch
    d2._reindex()
    await store2.enqueue({"kind": "add_email", "resource_name": "people/3",
                          "name": "Carla", "email": "carla@x.com"})
    await d2.drain()
    check("a terminal failure rolls the optimistic patch back",
          "carla@x.com" not in d2._contacts["people/3"]["emails"])

    # note_used writes the identity link
    d3 = book()
    d3.store = FakeStore()
    await d3.note_used("ana.silva@acme.com", phone="5511987654321")
    check("a successful booking records the preferred address",
          d3._contacts["people/1"]["preferred"] == "ana.silva@acme.com")
    check("a successful booking binds the identity link",
          d3.store.links.get("+5511987654321") == "people/1")


# --- 7. the confirm node --------------------------------------------------------------------

async def confirm_checks():
    print("\nconfirm node")
    from app.nodes.confirm import _grounded, _verb_of

    check("a remember whose address is in the turn is allowed",
          _grounded({"email": "ana@acme.com"}, "o email dela e ana@acme.com"))
    check("A HALLUCINATED ADDRESS IS REFUSED",
          not _grounded({"email": "invented@x.com"}, "marca com a ana amanha"))
    check("the check is case-insensitive",
          _grounded({"email": "Ana@Acme.com"}, "escreve pra ana@acme.com"))
    check("a malformed address is refused", not _grounded({"email": "nope"}, "nope"))
    check("verb extraction", _verb_of({"task": "calendar.remember"}) == "remember")


# --- 8. message composition -----------------------------------------------------------------

def card_checks():
    print("\ncards")
    from app.skills.calendar_format import compose_create, _guest_names

    st = {"session_lang": "pt",
          "seen_contacts": {"ana.silva@acme.com": {"name": "Ana Silva",
                                                   "resource_name": "people/1", "n_emails": 1}},
          "side_effects": [{"task": "calendar.remember", "name": "Bruno Tavares",
                            "email": "bruno@t.co"}]}
    names = _guest_names(st)
    check("names come from the injected block", names.get("ana.silva@acme.com") == "Ana Silva")
    check("a person learned THIS TURN is already named", names.get("bruno@t.co") == "Bruno Tavares")

    card = compose_create({"task": "calendar.create", "title": "Budget review",
                           "start": "2026-09-14T15:00:00-03:00",
                           "attendees": ["ana.silva@acme.com", "bruno@t.co", "x@y.com"]}, st)
    check("card names a known guest", "Ana Silva — ana.silva@acme.com" in card)
    check("card names a just-learned guest", "Bruno Tavares — bruno@t.co" in card)
    check("card falls back to the bare address", "x@y.com" in card.lower())


# --- 9. the contract ------------------------------------------------------------------------

def contract_checks():
    print("\ncontract")
    from app.skills import (SKILLS, count_optionals, count_unions, output_schema_for,
                            side_effect_verbs)

    s = output_schema_for("calendar")
    u, o = count_unions(s), count_optionals(s)
    check(f"unions within cap ({u}/16)", u <= 16)
    check(f"optionals within cap ({o}/24)", o <= 24)
    branches = [b["properties"]["task"]["const"] for b in s["properties"]["actions"]["items"]["anyOf"]]
    check("calendar.remember is in the schema", "calendar.remember" in branches)
    check("remember is registered as a side effect",
          side_effect_verbs()["calendar"] == {"remember"})
    check("remember has no optional fields (so it costs nothing)",
          SKILLS["calendar"].schemas["remember"]["required"] == ["name", "email"])
    check("the guidance tells the model the rules", "ADDRESS BOOK" in SKILLS["calendar"].guidance)
    check("the guidance forbids inventing an address",
          "NEVER invent" in SKILLS["calendar"].guidance)


# --- 10. disabled build ----------------------------------------------------------------------

def disabled_checks():
    print("\ncontacts disabled")
    from app.deps import build_deps

    deps = build_deps(Settings())
    check("no directory is built", deps.directory is None)
    cal = (deps.tools or {}).get("calendar")
    check("the calendar handler has no directory", getattr(cal, "directory", None) is None)

    from app.skills import context_block_for

    block, patch = context_block_for("calendar", {"turn_text": "Ana Silva"},
                                     {"directory": None, "settings": Settings()})
    check("no block is produced", block is None and patch == {})

    from app.graph import build_graph

    build_graph(deps)
    check("the graph still compiles", True)


async def main() -> int:
    phone_checks()
    retrieval_checks()
    write_rule_checks()
    await people_checks()
    await sync_checks()
    await outbox_checks()
    await confirm_checks()
    card_checks()
    contract_checks()
    disabled_checks()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
