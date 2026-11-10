"""The contacts skill, and one skill calling another — the offline selftest.

Run it with the repo venv:

    cd "Lisa flow" && .venv/bin/python tests/run_contacts_skill.py

No network, no Google, no Anthropic, no Postgres. The model is scripted, so this proves everything the
CODE decides: what reaches Google and in what shape, who can trigger what, what the room can read, and
how the conversation moves between skills. It cannot prove the model will choose to do any of it. That
takes a live run.

The Google side is the REAL GooglePeople client over a fake service that records every request and
behaves like the documented API, including refusing a stale etag and keeping profile data out of
reach, because every write bug in this area is about the shape of the request.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.carry import kept_on_carry  # noqa: E402
from app.config import Settings  # noqa: E402
from app.contactsearch import search_contacts  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.directory import Directory, LEARNED  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.intent import classify_confirmation  # noqa: E402
from app.nodes.confirm import _loop_text  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, callable_skills, confirm_policies, count_optionals, count_unions, output_schema_for,
    render_policies, resolve_gates, routable, side_effect_verbs, system_prompt_for,
)
from app.skills.contacts import (  # noqa: E402
    contacts_context, contacts_matcher, contacts_resolve_gate, problem_with,
)
from app.skills.contacts_format import (  # noqa: E402
    compose_create, compose_delete, compose_update, fmt_update,
)
from app.skills.calendar import calendar_revise  # noqa: E402
from app.skills.router import route_domain  # noqa: E402
from app.state import HANDOFF_MARK  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.contacts import (  # noqa: E402
    ContactsService, describe_person, mask_email, mask_phone, person_view, unmask_changes,
)
from app.tools.people import ChangeError, GooglePeople, build_update, same_value  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_step3 import StubCalendar, StubReasoner, _invoke  # noqa: E402

PASS = FAIL = 0

FELIPE_JID = "5511992890847@s.whatsapp.net"
OWNER_NUMBER = "5511976001033"
NEW_ADDRESS = "felipe@rssf.com.br"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  — {detail}" if detail else ""))


def on_settings(**kw) -> Settings:
    base = dict(evolution_apikey="x", loop_ttl_seconds=60, context_window_messages=30,
                max_tool_actions=4, contacts_enabled=True, contacts_crud_enabled=True,
                google_contacts_refresh_token="t", owner_name="Marcelo",
                mary_trigger_tag="@lisa", contacts_default_region="BR")
    base.update(kw)
    return Settings(**base)


# --- a fake of the People API ----------------------------------------------------------------

class _Err(Exception):
    def __init__(self, status: int, content: bytes = b"") -> None:
        super().__init__(content.decode() if content else str(status))
        self.resp = type("R", (), {"status": status})()
        self.content = content


class _Call:
    def __init__(self, fn) -> None:
        self.fn = fn

    def execute(self):
        return self.fn()


class FakePeopleService:
    """Behaves like the documented API where it matters: a stale etag is refused with
    failedPrecondition, a contact-only read leaves out profile data, and an update merges profile
    data back into its answer, so a profile-sourced value cannot be removed."""

    def __init__(self, people: list | None = None) -> None:
        self.db = {p["resourceName"]: copy.deepcopy(p) for p in (people or [])}
        self.profile: dict = {}          # resourceName -> {api_field: [entries]} from a Google profile
        self.updates: list = []          # (resourceName, updatePersonFields, body)
        self.creates: list = []
        self.deletes: list = []
        self.reads: list = []
        self.order: list = []
        self.stale_next = 0              # refuse this many updates with a stale etag
        self.fail_with: Exception | None = None
        self.seq = itertools.count(100)

    def people(self):
        return self

    def connections(self):
        return self

    def get(self, resourceName=None, personFields=None, sources=None):
        def run():
            self.reads.append((resourceName, personFields, sources))
            if resourceName not in self.db:
                raise _Err(404)
            found = copy.deepcopy(self.db[resourceName])
            if sources != ["READ_SOURCE_TYPE_CONTACT"]:
                for f, extra in (self.profile.get(resourceName) or {}).items():
                    found[f] = [*(found.get(f) or []), *extra]
            return found
        return _Call(run)

    def getBatchGet(self, resourceNames=None, personFields=None):
        def run():
            found = []
            for r in resourceNames:
                if r in self.db:
                    person_ = copy.deepcopy(self.db[r])
                    for f, extra in (self.profile.get(r) or {}).items():  # a default read merges it
                        person_[f] = [*(person_.get(f) or []), *extra]
                    found.append({"person": person_})
            return {"responses": found}
        return _Call(run)

    def updateContact(self, resourceName=None, updatePersonFields=None, personFields=None,
                      sources=None, body=None):
        def run():
            if self.fail_with:
                raise self.fail_with
            if self.stale_next:
                self.stale_next -= 1
                self.db[resourceName]["etag"] = "changed-elsewhere"
                raise _Err(400, b'{"error":{"status":"FAILED_PRECONDITION","reason":"failedPrecondition"}}')
            if body.get("etag") != self.db[resourceName]["etag"]:
                raise _Err(400, b'{"error":{"status":"FAILED_PRECONDITION"}}')
            if not body.get("metadata", {}).get("sources"):
                raise _Err(400, b"metadata.sources is required")
            self.updates.append((resourceName, updatePersonFields, copy.deepcopy(body)))
            self.order.append("update")
            for f in updatePersonFields.split(","):
                self.db[resourceName][f] = copy.deepcopy(body.get(f) or [])
            self.db[resourceName]["etag"] = f"e{next(self.seq)}"
            merged = copy.deepcopy(self.db[resourceName])
            for f, extra in (self.profile.get(resourceName) or {}).items():
                merged[f] = [*(merged.get(f) or []), *extra]
            return merged
        return _Call(run)

    def createContact(self, body=None, personFields=None):
        def run():
            if self.fail_with:
                raise self.fail_with
            rn = f"people/new{next(self.seq)}"
            self.creates.append(copy.deepcopy(body))
            names = body.get("names") or [{}]
            display = " ".join(x for x in (names[0].get("givenName"), names[0].get("familyName")) if x)
            person = {**copy.deepcopy(body), "resourceName": rn, "etag": "e0",
                      "names": [{**names[0], "displayName": display}],
                      "metadata": {"sources": [{"type": "CONTACT", "etag": "s0"}]}}
            self.db[rn] = person
            return copy.deepcopy(person)
        return _Call(run)

    def deleteContact(self, resourceName=None):
        def run():
            if self.fail_with:
                raise self.fail_with
            self.deletes.append(resourceName)
            self.order.append("delete")
            self.db.pop(resourceName, None)
            return {}
        return _Call(run)

    def list(self, **params):
        def run():
            self.reads.append(("list", params.get("syncToken"), params.get("personFields")))
            if params.get("syncToken") == "minted-with-other-fields":
                raise _Err(400, b'{"error":{"status":"INVALID_ARGUMENT"}}')
            return {"connections": [copy.deepcopy(p) for p in self.db.values()],
                    "nextSyncToken": "fresh"}
        return _Call(run)


def person(rn, name, emails=(), phones=(), company="", **extra):
    out = {"resourceName": rn, "etag": f"etag-{rn}", "names": [{"displayName": name}],
           "emailAddresses": [dict(e) if isinstance(e, dict) else {"value": e} for e in emails],
           "phoneNumbers": [{"value": p, "canonicalForm": p} for p in phones],
           "metadata": {"sources": [{"type": "CONTACT", "etag": f"src-{rn}"}]}}
    if company:
        out["organizations"] = [{"name": company}]
    out.update(extra)
    return out


class FakeStore:
    def __init__(self, fail_tombstone: bool = False, order: list | None = None) -> None:
        self.upserts: list = []
        self.deleted: list = []
        self.tombstones: list = []
        self.replaced: list = []
        self.fail_tombstone = fail_tombstone
        self.order = order if order is not None else []

    async def upsert(self, c):
        self.upserts.append(dict(c))

    async def delete(self, rn):
        self.deleted.append(rn)

    async def tombstone(self, rn, p):
        if self.fail_tombstone:
            raise RuntimeError("disk full")
        self.tombstones.append((rn, copy.deepcopy(p)))
        self.order.append("tombstone")

    async def replace(self, rows, token, *, full):
        self.replaced.append((len(rows), token, full))

    async def enqueue(self, job):
        pass

    async def pending(self, *, limit=20):
        return []


def book(people: list, *, store=True) -> tuple:
    """(directory, service, store) over a loaded book, with the real GooglePeople in between."""
    svc = FakePeopleService(people)
    gp = GooglePeople(on_settings())
    gp._svc = svc
    st = FakeStore(order=svc.order) if store else None
    d = Directory(on_settings(), store=st, people=gp)
    from app.tools.people import view
    d.load([{**view(p), "preferred": None, "used_at": None} for p in people], ready=True)
    return d, svc, st


# --- 1. the merge: what a write sends ---------------------------------------------------------

def merge_checks() -> None:
    print("\nthe merge")
    p = person("people/1", "Felipe R", emails=[{"value": "old@x.com", "type": "work"}, "b@x.com"],
               phones=["+5511987654321"], company="Acme",
               birthdays=[{"date": {"month": 3, "day": 14}}],
               biographies=[{"value": "note", "contentType": "TEXT_PLAIN"}])
    body, fields, noop = build_update(p, [{"field": "email", "op": "replace", "old": "old@x.com",
                                           "new": "new@x.com"}])
    check("a replace names only the field it touches", fields == ["emailAddresses"], str(fields))
    check("a replace keeps the entry's type", body["emailAddresses"][0] == {"type": "work", "value": "new@x.com"})
    check("a replace leaves the other entry whole", body["emailAddresses"][1] == {"value": "b@x.com"})
    check("every write carries etag and metadata", body["etag"] and body["metadata"]["sources"])

    _b, fields, noop = build_update(p, [{"field": "email", "op": "add", "old": "", "new": "OLD@x.com"}])
    check("adding an address already there is a no-op", noop and not fields)
    _b, _f, noop = build_update(p, [{"field": "email", "op": "replace", "old": "gone@x.com", "new": "b@x.com"}])
    check("replaying a replace that already landed is a no-op", noop)
    try:
        build_update(p, [{"field": "email", "op": "replace", "old": "gone@x.com", "new": "n@x.com"}])
        check("a replace of a value no longer there is refused", False)
    except ChangeError:
        check("a replace of a value no longer there is refused", True)
    try:
        build_update(p, [{"field": "email", "op": "replace", "old": "", "new": "n@x.com"}])
        check("a blind replace on two addresses is refused, not guessed", False)
    except ChangeError:
        check("a blind replace on two addresses is refused, not guessed", True)

    body, fields, _n = build_update(p, [{"field": "email", "op": "replace", "old": "old@x.com", "new": "b@x.com"}])
    check("replacing onto an address already there collapses them",
          [e["value"] for e in body["emailAddresses"]] == ["b@x.com"])

    body, fields, _n = build_update(p, [{"field": "phone", "op": "replace", "old": "11 98765-4321",
                                         "new": "+5511999998888"}])
    check("a phone is matched however it was typed", body["phoneNumbers"][0]["value"] == "+5511999998888")

    body, fields, _n = build_update(p, [{"field": "title", "op": "add", "old": "", "new": "CEO"}])
    check("the title keeps the company on the same organization",
          body["organizations"] == [{"name": "Acme", "title": "CEO"}])
    _b, fields, noop = build_update(p, [{"field": "birthday", "op": "replace", "old": "", "new": "03-14"}])
    check("the same birthday is a no-op", noop)
    body, fields, _n = build_update(p, [{"field": "birthday", "op": "replace", "old": "", "new": "1985-03-14"}])
    check("a birthday becomes a People date", body["birthdays"] == [{"date": {"year": 1985, "month": 3, "day": 14}}])
    try:
        build_update(p, [{"field": "birthday", "op": "replace", "old": "", "new": "next tuesday"}])
        check("a birthday that is not a date is refused", False)
    except ChangeError:
        check("a birthday that is not a date is refused", True)
    body, fields, _n = build_update(p, [{"field": "notes", "op": "remove", "old": "", "new": ""}])
    check("removing the notes clears the field", body["biographies"] == [] and fields == ["biographies"])
    try:
        build_update(p, [{"field": "name", "op": "remove", "old": "", "new": ""}])
        check("a card cannot lose its name", False)
    except ChangeError:
        check("a card cannot lose its name", True)

    check("a phone with and without the ninth digit is one number",
          same_value("phone", "+551187654321", "11 98765-4321"))
    check("the same digits in another area code are not", not same_value("phone", "+5521987654321", "11 98765-4321"))


# --- 2. the Google client ---------------------------------------------------------------------

def client_checks() -> None:
    print("\nthe Google client")
    svc = FakePeopleService([person("people/1", "Felipe", emails=["old@x.com"])])
    gp = GooglePeople(on_settings())
    gp._svc = svc
    change = [{"field": "email", "op": "replace", "old": "old@x.com", "new": "new@x.com"}]

    merged, report = gp.apply_changes("people/1", change)
    check("the update reaches Google", svc.updates and svc.updates[-1][1] == "emailAddresses")
    check("the card a write is built from is read as contact data only",
          any(r[2] == ["READ_SOURCE_TYPE_CONTACT"] for r in svc.reads), str(svc.reads))
    check("a clean update reports nothing stuck", report == {"noop": False, "stuck": []})
    n = len(svc.updates)
    _m, report = gp.apply_changes("people/1", change)
    check("a replay sends nothing and reports a no-op", report["noop"] and len(svc.updates) == n)

    svc = FakePeopleService([person("people/1", "Felipe", emails=["old@x.com"])])
    gp._svc = svc
    svc.stale_next = 1
    _m, report = gp.apply_changes("people/1", change)
    check("a stale etag re-reads and applies once",
          len(svc.updates) == 1 and svc.db["people/1"]["emailAddresses"][0]["value"] == "new@x.com")
    svc = FakePeopleService([person("people/1", "Felipe", emails=["old@x.com"])])
    gp._svc = svc
    svc.stale_next = 2
    try:
        gp.apply_changes("people/1", change)
        check("a second stale etag gives up rather than looping", False)
    except _Err:
        check("a second stale etag gives up rather than looping", True)

    svc = FakePeopleService([person("people/1", "Felipe", emails=["old@x.com", "work@x.com"])])
    svc.profile["people/1"] = {"emailAddresses": [{"value": "old@x.com"}]}
    gp._svc = svc
    _m, report = gp.apply_changes("people/1", [{"field": "email", "op": "remove", "old": "old@x.com", "new": ""}])
    check("a removed value still on his Google profile is reported, not hidden",
          [g["value"] for g in report["stuck"]] == ["old@x.com"], str(report))

    svc = FakePeopleService([])
    gp._svc = svc
    created = gp.create_card("Ana Maria Silva", [{"field": "email", "op": "add", "old": "", "new": "ana@x.com"},
                                                 {"field": "phone", "op": "add", "old": "", "new": "+5511999990000"}])
    body = svc.creates[-1]
    check("a create carries the name", body["names"] == [{"givenName": "Ana", "familyName": "Maria Silva"}])
    check("a create carries the rest", body["emailAddresses"] == [{"value": "ana@x.com"}]
          and body["phoneNumbers"] == [{"value": "+5511999990000"}])
    check("a create sends no etag or metadata", "etag" not in body and "metadata" not in body)
    check("a create returns the person", created.get("resourceName"))

    svc = FakePeopleService([person("people/1", "F")])
    gp._svc = svc
    held: list = []
    orig = gp._write_lock
    class Spy:
        def __enter__(self):
            held.append(True)
            return orig.__enter__()
        def __exit__(self, *a):
            return orig.__exit__(*a)
    gp._write_lock = Spy()
    gp.apply_changes("people/1", [{"field": "email", "op": "add", "old": "", "new": "n@x.com"}])
    gp.create_card("X", [])
    gp.delete_contact("people/1")
    gp.add_email("people/new100", "q@x.com") if "people/new100" in svc.db else None
    check("every write goes through the one lock", len(held) >= 3)

    # A sync token only works with the parameters that minted it, and PERSON_FIELDS just changed.
    svc = FakePeopleService([person("people/1", "Felipe")])
    gp = GooglePeople(on_settings())
    gp._svc = svc
    people, token, was_full = gp.list_all("minted-with-other-fields")
    check("the first sync after a field change recovers with a full sweep",
          was_full and token == "fresh" and len(people) == 1)
    svc2 = FakePeopleService([])
    svc2.list = lambda **kw: _Call(lambda: (_ for _ in ()).throw(_Err(400, b"really bad request")))
    gp._svc = svc2
    try:
        gp.list_all(None)
        check("a 400 with no token still raises", False)
    except _Err:
        check("a 400 with no token still raises", True)


# --- 3. search --------------------------------------------------------------------------------

def search_checks() -> None:
    print("\nsearch")
    felipes = [{"resource_name": f"people/f{i}", "name": f"Felipe {s}", "emails": [], "phones": [],
                "nicknames": [], "company": ""}
               for i, s in enumerate(["Souza", "Lima", "Costa", "Dias", "Melo", "Pires", "Reis", "Lopes"])]
    others = [
        {"resource_name": "people/a", "name": "Ana Silva", "emails": ["ana@acme.com"],
         "phones": ["+5511987654321"], "nicknames": ["Aninha"], "company": "Acme"},
        {"resource_name": "people/j", "name": "José Antônio", "emails": ["ze@rssf.com.br"], "phones": [],
         "nicknames": [], "company": "RSSF Advogados"},
    ]
    all_ = felipes + others
    check("every Felipe comes back, however many there are", len(search_contacts(all_, "felipe")) == 8)
    check("a surname narrows it", [c["name"] for c in search_contacts(all_, "felipe lima")] == ["Felipe Lima"])
    check("no accents needed", search_contacts(all_, "jose antonio")[0]["resource_name"] == "people/j")
    check("a nickname finds the card", search_contacts(all_, "aninha")[0]["resource_name"] == "people/a")
    check("a company finds the card", search_contacts(all_, "rssf")[0]["resource_name"] == "people/j")
    check("an email finds the card", search_contacts(all_, "ana@acme")[0]["resource_name"] == "people/a")
    check("a phone finds the card, however it is typed",
          search_contacts(all_, "98765-4321")[0]["resource_name"] == "people/a")
    check("a misspelling still finds him", len(search_contacts(all_, "filipe")) == 8)
    check("a name nobody has finds nobody", search_contacts(all_, "zebedeu") == [])
    check("an empty query finds nobody", search_contacts(all_, "  ") == [])

    # Seen live: every "reunião COM o X" surfaced these two as a which-one question.
    d, _svc, _st = book([person("people/c1", "Alain - Crypto.com"), person("people/c2", "Erika Hoteis. com"),
                         TESTOLINO])
    found = [c["name"] for c, _why in d.mentions("marca uma reunião com o Testolini dia 20/11")]
    check("'com' is a word, not the end of a web address", found == [], str(found))
    found = [c["name"] for c, _why in d.mentions("reunião com o Zeferino")]
    check("and a real name in the same sentence still resolves", found == ["Zeferino Testolino"], str(found))


# --- 4. the router ----------------------------------------------------------------------------

async def router_checks() -> None:
    print("\nthe router")
    for text in ("@lisa atualize o email do felipe nos contatos", "@lisa apaga o contato do joao",
                 "@lisa salva na agenda o telefone da ana", "@lisa salva na agenda o e-mail da ana"):
        check(f"matcher yes: {text!r}", contacts_matcher(text) == "yes")
    for text in ("@lisa manda o contrato pro felipe", "@lisa agenda de amanha",
                 "@lisa marca reuniao com o contato da ana", "@lisa qual a capital do peru"):
        check(f"matcher no: {text!r}", contacts_matcher(text) == "no")

    on, off = on_settings(), Settings(evolution_apikey="x")
    felipe = "@lisa atualize o email do felipe nos contatos"
    domain, how = await route_domain({"text": felipe, "from_me": True}, on)
    check("Felipe's request now routes to contacts, with no model call",
          (domain, how) == ("contacts", "matcher"), f"{domain}/{how}")
    domain, _how = await route_domain({"text": "@lisa salva na agenda o telefone da ana", "from_me": True}, on)
    check("the Brazilian 'agenda' reaches the book before the calendar", domain == "contacts")
    domain, _how = await route_domain({"text": "@lisa marca reuniao com o contato da ana", "from_me": True}, on)
    check("a booking that mentions a contact stays calendar", domain == "calendar")
    domain, how = await route_domain({"text": felipe, "from_me": True}, off)
    check("with the skill off, the request never reaches it", domain != "contacts", domain)

    class Cls:
        def __init__(self):
            self.seen = None
            self.system = None
        async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
            self.seen = schema["properties"]["domain"]["enum"]
            self.system = system
            return {"domain": self.seen[0]}

    r = Cls()
    await route_domain({"text": "atualiza o email do felipe", "from_me": True}, on, reasoner=r)
    check("the classifier can pick contacts for the owner", "contacts" in r.seen, str(r.seen))
    check("the classifier is told what contacts means", '"contacts"' in r.system)
    r = Cls()
    await route_domain({"text": "me passa o telefone da ana", "from_me": False}, on, reasoner=r)
    check("the classifier cannot send anyone else's message there", "contacts" not in r.seen, str(r.seen))
    domain, how = await route_domain({"text": "me passa seu contato", "from_me": False}, on)
    check("nor can the matcher", domain != "contacts", domain)
    domain, how = await route_domain({"text": "ok", "from_me": False, "loop_domain": "contacts",
                                      "loop_opened": False}, on)
    check("someone else's message does not knock a contacts loop out of contacts",
          (domain, how) == ("contacts", "loop"), f"{domain}/{how}")
    r = Cls()
    await route_domain({"text": "qual a capital do peru", "from_me": True}, off, reasoner=r)
    check("with the skill off, the classifier is told nothing about it", '"contacts"' not in r.system)
    check("a registry with no settings fails closed", "contacts" not in routable({}, SKILLS))


# --- 5. the contract ---------------------------------------------------------------------------

def contract_checks() -> None:
    print("\nthe contract")
    on, off = on_settings(), Settings(evolution_apikey="x")
    cs = output_schema_for("contacts", settings=on)
    check("contacts schema is inside the union cap", count_unions(cs) <= 16, str(count_unions(cs)))
    check("contacts schema is inside the optional cap", count_optionals(cs) <= 24, str(count_optionals(cs)))
    tasks = {b["properties"]["task"]["const"] for b in cs["properties"]["actions"]["items"]["anyOf"]}
    check("contacts exposes its four verbs",
          tasks == {f"contacts.{v}" for v in ("find", "create", "update", "delete")}, str(tasks))
    cal_on = output_schema_for("calendar", settings=on)
    check("calendar with the call still fits the union cap", count_unions(cal_on) <= 16, str(count_unions(cal_on)))
    check("the call costs calendar no optional", count_optionals(cal_on) == count_optionals(
        output_schema_for("calendar", settings=off)), str(count_optionals(cal_on)))
    check("calendar can call contacts when it is on",
          cal_on["properties"]["call"]["anyOf"][1]["properties"]["skill"]["enum"] == ["contacts"])
    check("contacts can answer calendar", callable_skills("contacts", on) == ["calendar"])
    cal_off = output_schema_for("calendar", settings=off)
    check("with the skill off, calendar's contract has no call", "call" not in cal_off["properties"])
    check("with the skill off, calendar's prompt never mentions it",
          "contacts skill" not in system_prompt_for("calendar", off).lower()
          and '"call"' not in system_prompt_for("calendar", off))
    check("with the skill on, calendar's prompt offers it",
          "THE CONTACTS SKILL" in system_prompt_for("calendar", on))
    check("web and setup can call nothing",
          callable_skills("web", on) == [] and callable_skills("setup", on) == [])
    prompt = system_prompt_for("contacts", on, called_by={"domain": "calendar", "goal": "Find Felipe's email"})
    check("a called skill is told who is waiting, and for what",
          "The calendar skill brought you in" in prompt and "Find Felipe's email" in prompt)
    check("every prompt renders", all(system_prompt_for(n, on) for n in SKILLS))


# --- 6. who may see what -----------------------------------------------------------------------

async def privacy_checks() -> None:
    print("\nwho may see what")
    check("an email is masked", mask_email("felipe@rssf.com.br") == "f***@r***.com.br", mask_email("felipe@rssf.com.br"))
    check("a phone is masked", mask_phone("+5511987654321") == "••••4321")
    v = person_view({"resource_name": "people/1", "name": "Ana", "emails": ["ana@x.com"],
                     "phones": ["+5511987654321"], "company": "Acme"}, visible=False,
                    card={"name": "Ana", "emails": ["ana@x.com"], "phones": ["+5511987654321"],
                          "company": "Acme", "notes": "secret", "birthday": "1985-03-14",
                          "addresses": ["Rua X"], "title": "CEO"})
    line = describe_person(v)
    check("a hidden card leaks no address", "ana@x.com" not in line and "a***@x***.com" in line, line)
    check("a hidden card leaks no phone", "987654321" not in line, line)
    check("a hidden card leaks no notes, birthday, street or title",
          not any(s in line for s in ("secret", "1985", "Rua X", "CEO")), line)
    check("a hidden card still names the person and company", "Ana" in line and "Acme" in line)
    v2 = {**v, "visible": True, "self_chat": True}
    check("in his own chat a card shows everything", all(s in describe_person(v2) for s in ("ana@x.com", "secret", "Rua X")))
    v3 = {**v, "visible": True, "self_chat": False}
    check("to someone in the chat it shows their details, not his notes about them",
          "ana@x.com" in describe_person(v3) and "secret" not in describe_person(v3), describe_person(v3))
    check("fetched for an invite: the email only, nothing else",
          "ana@x.com" in describe_person(v, reveal_emails=True)
          and "987654321" not in describe_person(v, reveal_emails=True)
          and "secret" not in describe_person(v, reveal_emails=True))

    d, _svc, _st = book([person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511987654321"]),
                         person("people/f", "Felipe Dias", emails=["felipe@old.com"], phones=["+5511992890847"])])
    tool = ContactsService(on_settings())
    tool.directory = d
    res = await tool.run("find", {"query": "ana", "_phone": "5511992890847", "_self_chat": False})
    check("find in Felipe's chat masks Ana", "ana@x.com" not in res["summary"] and "a***@x***.com" in res["summary"], res["summary"])
    res = await tool.run("find", {"query": "felipe", "_phone": "5511992890847", "_self_chat": False})
    check("find in Felipe's chat shows Felipe his own card", "felipe@old.com" in res["summary"], res["summary"])
    res = await tool.run("find", {"query": "ana", "_phone": None, "_self_chat": True})
    check("find in his own chat shows everything", "ana@x.com" in res["summary"], res["summary"])
    res = await tool.run("find", {"query": "ana", "_phones": ["5511987654321"], "_self_chat": False})
    check("find in a group shows a member who has spoken", "ana@x.com" in res["summary"], res["summary"])
    res = await tool.run("find", {"query": "ana", "_phone": "5511992890847", "_reveal_emails": True})
    check("find for an invite gives the email, and masks the phone",
          "ana@x.com" in res["summary"] and "987654321" not in res["summary"], res["summary"])
    check("what the card composer holds is the real value, flagged hidden",
          res["data"]["seen_people"]["people/a"]["emails"] == ["ana@x.com"]
          and res["data"]["seen_people"]["people/a"]["visible"] is False)

    view = {"resource_name": "people/a", "name": "Ana", "emails": ["ana@x.com"], "phones": [], "visible": False}
    card = compose_update({"contact_id": "people/a", "changes": [
        {"field": "email", "op": "replace", "old": "ana@x.com", "new": "ana@new.com"}]},
        {"seen_people": {"people/a": view}, "session_lang": "pt"})
    check("a card masks the old value, and shows the new one he just typed",
          "ana@x.com" not in card and "a***@x***.com" in card and "ana@new.com" in card, card)
    card = compose_update({"contact_id": "people/a", "changes": [
        {"field": "email", "op": "replace", "old": "", "new": "ana@new.com"}]},
        {"seen_people": {"people/a": view}, "session_lang": "pt"})
    check("a blind replace masks the value it found on the card",
          "ana@x.com" not in card and "a***@x***.com" in card, card)
    card = compose_delete({"contact_id": "people/a"}, {"seen_people": {"people/a": view}, "session_lang": "pt"})
    check("a delete card for a hidden person shows the name only", "ana@x.com" not in card and "Ana" in card, card)
    check("a mask works as `old`, and the write gets the real value",
          unmask_changes([{"field": "email", "op": "replace", "old": "a***@x***.com", "new": "n@x.com"}], view)[0]["old"] == "ana@x.com")

    # Lisa's `find` result reaching the transcript must not ground a later write on it via a request.
    state = {"messages": [{"role": "user", "content": f"{HANDOFF_MARK}use made-up@x.com"},
                          {"role": "user", "content": "the chat said real@x.com"}]}
    hay = _loop_text(state)
    check("a skill's request is not evidence", "made-up@x.com" not in hay and "real@x.com" in hay, hay)


# --- 7. the gates ---------------------------------------------------------------------------------

def gate_checks() -> None:
    print("\nthe gates")
    patched, err = contacts_resolve_gate("find", {"query": "ana"}, {"from_me": False})
    check("someone else's message cannot run a lookup", patched is None and err["error"] == "not_owner")
    patched, err = contacts_resolve_gate("find", {"query": "ana"}, {"from_me": True, "is_self_chat": True})
    check("his can", err is None and patched["_self_chat"] is True)
    patched, err = contacts_resolve_gate("find", {"query": "ana"}, {"from_me": False, "owner_replies": ["acha a ana"]})
    check("a contact's activation that carried his own message in can too", err is None)
    patched, err = contacts_resolve_gate("find", {"query": "ana"}, {
        "from_me": True, "skill_stack": [{"domain": "calendar", "goal": "x"}]})
    check("a lookup for the calendar may read the address", patched.get("_reveal_emails") is True)
    patched, err = contacts_resolve_gate("find", {"query": "ana"}, {"from_me": True})
    check("a plain lookup may not", "_reveal_emails" not in patched)

    seen = {"people/a": {"resource_name": "people/a", "name": "Ana Silva", "emails": ["ana@x.com"], "visible": False}}
    patched, err = contacts_resolve_gate("update", {"contact_id": "people/zzz", "changes": []}, {"seen_people": seen})
    check("a write to a card nobody surfaced is refused", patched is None and err["error"] == "unresolved_id")
    patched, err = contacts_resolve_gate("delete", {"contact_id": "Ana Silva"}, {"seen_people": seen})
    check("a name where the id belongs resolves when it is unambiguous", patched["contact_id"] == "people/a")
    patched, err = contacts_resolve_gate("update", {"contact_id": "people/a", "changes": [
        {"field": "email", "op": "replace", "old": "a***@x***.com", "new": "n@x.com"}]}, {"seen_people": seen})
    check("the gate swaps a mask for the real value", patched["changes"][0]["old"] == "ana@x.com")

    d, _s, _st = book([person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511987654321"])])
    base = {"messages": [{"role": "user", "content": "o novo email é ana@new.com, tel 11 91234-5678"}]}
    upd = {"task": "contacts.update", "contact_id": "people/a"}
    state = {**base, "seen_people": seen}
    check("a value from the chat can be proposed",
          problem_with({**upd, "changes": [{"field": "email", "op": "replace", "old": "", "new": "ana@new.com"}]}, state) is None)
    check("a phone from the chat can be proposed, however it was typed",
          problem_with({**upd, "changes": [{"field": "phone", "op": "add", "old": "", "new": "+5511912345678"}]}, state) is None)
    bad = problem_with({**upd, "changes": [{"field": "email", "op": "replace", "old": "", "new": "invented@x.com"}]}, state)
    check("an invented address is refused before any card", bad and "invented@x.com" in bad, str(bad))
    bad = problem_with({**upd, "changes": [{"field": "phone", "op": "add", "old": "", "new": "+5511900000000"}]}, state)
    check("an invented phone is refused before any card", bool(bad), str(bad))
    two = {"people/a": {"name": "Ana", "emails": ["a@x.com", "b@x.com"]}}
    bad = problem_with({**upd, "changes": [{"field": "email", "op": "replace", "old": "", "new": "ana@new.com"}]},
                       {**base, "seen_people": two})
    check("two addresses and no `old` makes her ask", bad and "Ask him which" in bad, str(bad))
    bad = problem_with({"task": "contacts.delete", "contact_id": "people/zzz"}, state)
    check("deleting a card nobody surfaced is refused before any card", bool(bad))
    create = {"task": "contacts.create", "name": "Ana Dois", "changes": [
        {"field": "email", "op": "add", "old": "", "new": "ana@x.com"}]}
    bad = problem_with(create, {"messages": [{"role": "user", "content": "ana@x.com"}]}, d)
    check("creating someone already on file is refused, and names them", bad and "Ana Silva" in bad, str(bad))
    create = {"task": "contacts.create", "name": "Ana Dois", "changes": [
        {"field": "phone", "op": "add", "old": "", "new": "11 98765-4321"}]}
    bad = problem_with(create, {"messages": [{"role": "user", "content": "11 98765-4321"}]}, d)
    check("the same goes for a number already on file", bad and "Ana Silva" in bad, str(bad))
    check("a new card with no name is refused",
          bool(problem_with({"task": "contacts.create", "name": " ", "changes": []}, {})))


# --- 8. the yes ---------------------------------------------------------------------------------

def yes_checks() -> None:
    print("\nreading his answer")
    policy = SKILLS["contacts"].confirm
    delete = {"task": "contacts.delete", "contact_id": "people/a"}
    update = {"task": "contacts.update", "contact_id": "people/a"}
    create = {"task": "contacts.create", "name": "Ana"}
    for word in ("sim", "pode", "apaga", "apagar", "exclui"):
        check(f"{word!r} approves a contact delete", policy.detect(word, delete) == "yes")
    for word in ("cancela", "cancelar", "cancel"):
        check(f"{word!r} calls a contact delete OFF", policy.detect(word, delete) == "no")
    check("'cancela' still approves a CALENDAR delete",
          classify_confirmation("cancela", "delete") == "yes")
    check("'salva' approves a new card", policy.detect("salva", create) == "yes")
    check("'atualiza' approves a change", policy.detect("atualiza", update) == "yes")
    check("'nao' declines", policy.detect("nao", update) == "no")
    check("a correction stays a correction", policy.detect("nao, o outro email", update) == "other")
    check("no action reads as the bare classifier", policy.detect("sim") == "yes")


# --- 9. the handler -----------------------------------------------------------------------------

async def handler_checks() -> None:
    print("\nthe handler")
    tool = ContactsService(on_settings())
    res = await tool.run("find", {"query": "x"})
    check("no address book: refused, and it says nothing changed", not res["ok"] and res["error"] == "unavailable")
    cold = Directory(on_settings())
    tool.directory = cold
    res = await tool.run("find", {"query": "x"})
    check("a cold book fails closed, it does not say 'nobody'", not res["ok"] and res["error"] == "unavailable")

    d, svc, st = book([person("people/f", "Felipe Dias", emails=["felipe@old.com"], phones=["+5511992890847"])])
    tool.directory = d
    res = await tool.run("update", {"contact_id": "people/f", "_visible": True, "changes": [
        {"field": "email", "op": "replace", "old": "felipe@old.com", "new": NEW_ADDRESS}]})
    check("update succeeds", res["ok"], str(res))
    check("Google has the new address", svc.db["people/f"]["emailAddresses"] == [{"value": NEW_ADDRESS}])
    check("the next turn already reads it", d.by_email(NEW_ADDRESS)["name"] == "Felipe Dias")
    check("the old address no longer resolves", d.by_email("felipe@old.com") is None)
    check("the mirror has it", st.upserts[-1]["emails"] == [NEW_ADDRESS])
    res = await tool.run("update", {"contact_id": "people/f", "_visible": True, "changes": [
        {"field": "email", "op": "replace", "old": "felipe@old.com", "new": NEW_ADDRESS}]})
    check("a replay says it was already so", res["ok"] and res["data"]["noop"] is True)
    n_upserts = len(st.upserts)
    check("and leaves the snapshot alone, since its read is contact-only", len(st.upserts) == n_upserts
          and d.by_email(NEW_ADDRESS) is not None)
    text = fmt_update([res], {"session_lang": "pt"})
    check("and the card says so too", "já está assim" in text, text)
    res = await tool.run("update", {"contact_id": "people/gone", "changes": [
        {"field": "email", "op": "add", "old": "", "new": "x@y.com"}]})
    check("a card that is gone is reported, not guessed", res["error"] == "not_found")
    res = await tool.run("update", {"contact_id": "people/f", "changes": [
        {"field": "email", "op": "replace", "old": "nope@x.com", "new": "n@x.com"}]})
    check("a change that cannot apply is a validation error, with no write", res["error"] == "validation")

    # delete
    d, svc, st = book([person("people/a", "Ana", emails=["ana@x.com"])])
    tool.directory = d
    res = await tool.run("delete", {"contact_id": "people/a"})
    check("delete succeeds", res["ok"] and "people/a" in svc.deletes)
    check("the copy is saved BEFORE the delete", svc.order == ["tombstone", "delete"], str(svc.order))
    check("the copy holds the whole card", st.tombstones[0][1]["emailAddresses"] == [{"value": "ana@x.com"}])
    check("she stops offering the card", d.get("people/a") is None and d.by_email("ana@x.com") is None)
    check("the mirror drops it", st.deleted == ["people/a"])

    d, svc, st = book([person("people/a", "Ana")])
    st.fail_tombstone = True
    tool.directory = d
    res = await tool.run("delete", {"contact_id": "people/a"})
    check("no copy, no delete", not res["ok"] and svc.deletes == [] and "people/a" in svc.db, str(res))
    d, svc, _st = book([person("people/a", "Ana")], store=False)
    tool.directory = d
    res = await tool.run("delete", {"contact_id": "people/a"})
    check("nowhere to keep a copy, no delete", res["error"] == "no_store" and svc.deletes == [])

    # learned
    d, svc, st = book([])
    d.load([{"resource_name": "learned/z@x.com", "name": "Zeca", "emails": ["z@x.com"], "phones": [],
             "nicknames": [], "source": LEARNED, "preferred": None, "used_at": None}], ready=True)
    tool.directory = d
    res = await tool.run("update", {"contact_id": "learned/z@x.com", "_visible": True, "changes": [
        {"field": "phone", "op": "add", "old": "", "new": "+5511912345678"}]})
    check("editing a card she only knows herself makes it real",
          res["ok"] and svc.creates and svc.creates[0]["emailAddresses"] == [{"value": "z@x.com"}]
          and svc.creates[0]["phoneNumbers"] == [{"value": "+5511912345678"}], str(res))
    check("and the learned row is gone", d.get("learned/z@x.com") is None and st.deleted == ["learned/z@x.com"])
    d.load([{"resource_name": "learned/q@x.com", "name": "Quim", "emails": ["q@x.com"], "phones": [],
             "nicknames": [], "source": LEARNED, "preferred": None, "used_at": None}], ready=True)
    res = await tool.run("delete", {"contact_id": "learned/q@x.com"})
    n = len(svc.deletes)
    check("deleting one touches only her memory", res["ok"] and n == 0)

    # failures
    d, svc, st = book([person("people/a", "Ana", emails=["ana@x.com"])])
    tool.directory = d
    svc.fail_with = _Err(503)
    res = await tool.run("update", {"contact_id": "people/a", "changes": [
        {"field": "email", "op": "add", "old": "", "new": "n@x.com"}]})
    check("Google down is named transient", res["error"] == "transient")
    res = await tool.run("create", {"name": "Bia", "changes": []})
    check("a create that lost its answer says which verb, for the honest message",
          res["error"] == "transient" and res["data"]["verb"] == "create")
    from app.skills.contacts_format import fmt_failure
    check("and it warns about duplicating rather than offering a retry",
          "duplicar" in fmt_failure([res], {"session_lang": "pt"}))
    await asyncio.sleep(0)
    svc.fail_with = _Err(403)
    res = await tool.run("delete", {"contact_id": "people/a"})
    check("a refused token is named auth", res["error"] == "auth", str(res))
    svc.fail_with = _Err(404)
    res = await tool.run("update", {"contact_id": "people/a", "changes": [
        {"field": "email", "op": "add", "old": "", "new": "n@x.com"}]})
    check("a card Google says is gone stops being offered", res["error"] == "not_found" and d.get("people/a") is None)

    # cards
    view = {"name": "Ana", "emails": ["a@x.com"], "visible": True, "source": LEARNED}
    card = compose_update({"contact_id": "learned/a", "changes": [
        {"field": "phone", "op": "add", "old": "", "new": "+5511"}]},
        {"seen_people": {"learned/a": view}, "session_lang": "pt"})
    check("a card for someone not yet in Google says it will create them", "vou criar" in card, card)
    card = compose_create({"name": "Ana Silva", "changes": [{"field": "email", "op": "add", "old": "", "new": "a@x.com"}]},
                          {"session_lang": "en"})
    check("a create card shows what will be saved", "Ana Silva" in card and "a@x.com" in card and "save" in card, card)


# --- 10. the mirror -----------------------------------------------------------------------------

async def mirror_checks() -> None:
    print("\nthe mirror")
    d, svc, st = book([person("people/a", "Ana")])
    gone = {"resourceName": "people/a", "metadata": {"deleted": True}}
    d.people.list_all = lambda token: ([gone], "t2", False)
    await d.refresh()
    check("a deletion a delta reports leaves the mirror too", st.deleted == ["people/a"], str(st.deleted))
    check("and the snapshot", d.get("people/a") is None)


# --- 11. the whole loop, through the graph --------------------------------------------------------

class ChatEvolution:
    instance = "secretaria"

    def __init__(self, start_ts: int = 1793630000) -> None:
        self.sent: list = []
        self.history: list = []
        # Fixed by default, so the offline suite reads the same any day. A live run passes now:
        # stamped in the past, "sexta" is a day already gone and the real model asks about it.
        self._ts = itertools.count(start_ts)

    def store(self, mid, text, *, from_me, name="Marcelo"):
        self.history.append({"id": mid, "from_me": from_me, "text": text, "push_name": name,
                             "ts": next(self._ts)})

    async def send_text(self, number, text, *, quoted=None):
        mid = f"lisa{len(self.sent)}"
        self.sent.append((number, text))
        self.store(mid, text, from_me=True)
        return mid

    async def fetch_history(self, jid):
        return list(self.history)


class Chat:
    def __init__(self, people: list, *, self_chat: bool = False, **settings_kw) -> None:
        self.settings = on_settings(**settings_kw)
        self.jid = f"{OWNER_NUMBER}@s.whatsapp.net" if self_chat else FELIPE_JID
        if self_chat:
            self.settings.owner_jid = self.jid
        self.evo = ChatEvolution()
        self.stub = StubReasoner()
        self.cal = StubCalendar()
        self.directory, self.google, self.store = book(people)
        self.contacts = ContactsService(self.settings)
        self.contacts.directory = self.directory
        self.deps = Deps(settings=self.settings, evolution=self.evo,
                         sessions=InMemorySessions(ttl=60), echoes=InMemoryEchoes(ttl=3600),
                         trace=build_trace(), reasoner=self.stub, redis=None,
                         tools={"calendar": self.cal, "contacts": self.contacts},
                         confirm_policies=confirm_policies(), render_policies=render_policies(),
                         resolve_gates=resolve_gates(), side_effects=side_effect_verbs(),
                         directory=self.directory)
        self.graph = build_graph(self.deps, MemorySaver())
        self._n = itertools.count(1)

    def felipe(self, text):
        self.evo.store(f"f{next(self._n)}", text, from_me=False, name="Felipe Rodrigues")

    async def say(self, text, *, from_me=True, name="Marcelo", script=None, stored=False):
        """`stored` means the message is in the chat already, somewhere earlier than the end."""
        if script is not None:
            self.stub.script = list(script)
        mid = f"m{next(self._n)}"
        if not stored:
            self.evo.store(mid, text, from_me=from_me, name=name)
        else:
            mid = next(r["id"] for r in reversed(self.evo.history) if r["text"] == text)
        body = {"data": {"key": {"remoteJid": self.jid, "fromMe": from_me, "id": mid},
                         "message": {"conversation": text},
                         "messageTimestamp": int(time.time()), "pushName": name}}
        return await _invoke(self.graph, body, jid=self.jid)

    def last(self) -> str:
        return self.evo.sent[-1][1] if self.evo.sent else ""


FELIPE_CARD = person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])


def fifteen_felipes() -> list:
    surnames = ["Souza", "Lima", "Costa", "Melo", "Pires", "Reis", "Lopes", "Alves",
                "Nunes", "Rocha", "Faria", "Gomes", "Braga", "Prado"]
    return [FELIPE_CARD, *(person(f"people/x{i}", f"Felipe {s}", emails=[f"f{i}@x.com"])
                           for i, s in enumerate(surnames))]


async def felipe_checks() -> None:
    print("\nthe conversation that started this")
    c = Chat(fifteen_felipes())
    c.felipe("Só uma obs: o meu e-mail foi atualizado, ok?")
    c.felipe(f"Agora é {NEW_ADDRESS}")
    c.felipe("Eu sigo recebendo e-mails do antigo escritório, mas achei melhor te atualizar")

    # Turn 1: routed to contacts; she searches, and asks which, because fifteen cards fit.
    await c.say("@lisa atualize o email do felipe nos contatos", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe"}]},
        {"lang": "pt", "state": "awaiting_answer", "message": "Tenho 15 Felipes. Qual o sobrenome dele?"},
    ])
    check("it is no longer a refusal: it routes to contacts and searches", len(c.stub.calls) == 2)
    readback = c.stub.calls[1]["messages"][-1]["content"]
    check("she is told how many fit", "15 card(s)" in readback, readback[:80])
    check("the other Felipes' addresses never reach her in his chat", "f0@x.com" not in readback, readback[:200])
    check("she asks, she does not pick", c.last().endswith("sobrenome dele?"), c.last())
    check("nothing has been written", c.google.updates == [])
    st = await c.graph.aget_state({"configurable": {"thread_id": make_thread_id("secretaria", c.jid)}})
    logged = str(st.values.get("action_results"))
    check("the activation record keeps no card details",
          "felipe@oldfirm.com" not in logged and "seen_people" not in logged, logged[:200])
    check("yet the loop still remembers them for the cards",
          "people/f" in st.values.get("seen_people", {}))

    # Turn 2: he narrows it, she proposes, and the card names the change.
    await c.say("Felipe Dias", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/f", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}]},
    ])
    card = c.last()
    check("the card shows the new address", NEW_ADDRESS in card, card)
    check("it hides the old one: he is not a member of this chat as far as the book knows",
          "felipe@oldfirm.com" not in card and "f***@o***.com" in card, card)
    check("the card asks", "Posso alterar?" in card, card)
    check("still nothing written, before the yes", c.google.updates == [])

    # Felipe agreeing means nothing.
    await c.say("sim", from_me=False, name="Felipe Rodrigues", script=[])
    check("Felipe's own 'sim' does not approve it", c.google.updates == [])

    # Turn 3: his yes.
    n_model = len(c.stub.calls)
    await c.say("sim", script=[])
    check("his 'sim' runs it", len(c.google.updates) == 1)
    check("Google has the new address, and only that",
          c.google.db["people/f"]["emailAddresses"] == [{"value": NEW_ADDRESS}])
    check("with no model call", len(c.stub.calls) == n_model)
    check("the result is composed in code", "Contato atualizado" in c.last() and NEW_ADDRESS in c.last(), c.last())
    check("the next booking reads the new address", c.directory.by_email(NEW_ADDRESS)["name"] == "Felipe Dias")


async def not_on_file_checks() -> None:
    print("\nsomeone who is not on file")
    c = Chat(fifteen_felipes())
    c.felipe(f"Agora é {NEW_ADDRESS}")
    st = await c.say("@lisa salva o contato do felipe rodrigues", script=[
        {"lang": "pt", "actions": [{"task": "contacts.create", "name": "Felipe Rodrigues", "changes": [
            {"field": "email", "op": "add", "old": "", "new": NEW_ADDRESS}]}]},
    ])
    check("a new person gets a card", "Confirmando novo contato" in c.last() and NEW_ADDRESS in c.last(), c.last())
    await c.say("salva", script=[])
    check("his own word from the card approves it", len(c.google.creates) == 1)
    check("and the result is reported", "Contato salvo" in c.last(), c.last())

    # An address the conversation never contained never gets a card at all.
    c = Chat([])
    await c.say("@lisa salva o contato da ana", script=[
        {"lang": "pt", "actions": [{"task": "contacts.create", "name": "Ana", "changes": [
            {"field": "email", "op": "add", "old": "", "new": "made.up@example.com"}]}]},
        {"lang": "pt", "state": "awaiting_answer", "message": "Qual o email dela?"},
    ])
    check("an invented address gets no card, and she is told why",
          not any("Confirmando" in t for _n, t in c.evo.sent)
          and "made.up@example.com" in c.stub.calls[1]["messages"][-1]["content"])
    check("so she asks instead", c.last() == "*[Assistente IA do Marcelo]:*\n\nQual o email dela?" or "Qual o email" in c.last(), c.last())
    check("nothing reached Google", c.google.creates == [])


async def contact_cannot_ask_checks() -> None:
    print("\nanyone else in the chat")
    c = Chat([person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511987654321"])])
    await c.say("@lisa salva o contato do joao", script=[{"lang": "pt", "message": "Qual o número?"}])
    await c.say("me passa o telefone da ana", from_me=False, name="Felipe Rodrigues", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "ana"}]},
        {"lang": "pt", "message": None},
    ])
    results = c.stub.calls[-1]["messages"][-1]["content"] if len(c.stub.calls) > 2 else ""
    check("a contact's message cannot run a lookup", "Only the owner" in results, results)
    check("the number never appears anywhere she reads", "987654321" not in str(c.stub.calls[-1]["messages"]))
    check("nor in the chat", not any("987654321" in t for _n, t in c.evo.sent))


async def decline_and_delete_checks() -> None:
    print("\ndeclining, and deleting")
    c = Chat([person("people/a", "Ana Silva", emails=["ana@x.com"])], self_chat=True)
    await c.say("@lisa apaga o contato da ana silva", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "ana silva"}]},
        {"lang": "pt", "actions": [{"task": "contacts.delete", "contact_id": "people/a"}]},
    ])
    check("a delete asks first, on its own card", "Apagar este contato?" in c.last(), c.last())
    check("in his own chat the card shows which one", "ana@x.com" in c.last(), c.last())
    await c.say("cancela", script=[{"lang": "pt", "message": "Ok."}])
    check("'cancela' keeps the contact", c.google.deletes == [] and "people/a" in c.google.db)

    await c.say("@lisa apaga o contato da ana silva", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "ana silva"}]},
        {"lang": "pt", "actions": [{"task": "contacts.delete", "contact_id": "people/a"}]},
    ])
    await c.say("apaga", script=[])
    check("'apaga' deletes it", c.google.deletes == ["people/a"])
    check("a copy was kept first", c.store.tombstones[0][0] == "people/a")
    check("the result is reported", "Contato apagado" in c.last(), c.last())


# --- 12. one skill calling another -----------------------------------------------------------------

BOOKING = {"task": "calendar.create", "title": "Marcelo & Felipe", "start": "2027-03-05T15:00:00-03:00",
           "attendees": [NEW_ADDRESS]}
CREATED = {"ok": True, "summary": "created", "data": {
    "title": "Marcelo & Felipe", "start": "2027-03-05T15:00:00-03:00", "attendees": [NEW_ADDRESS],
    "notified": True, "html_link": "http://cal/1"}}


def domains_called(c: Chat) -> list:
    """Which skill each model call ran as, read off the prompt it was given."""
    out = []
    for call in c.stub.calls:
        s = call["system"]
        if "you keep " in s and "real Google Contacts" in s:
            out.append("contacts")
        elif "you manage" in s and "Google Calendar" in s:
            out.append("calendar")
        else:
            out.append("other")
    return out


async def call_read_checks() -> None:
    print("\ncalendar calls contacts for an address, then carries on")
    c = Chat(fifteen_felipes()[1:] + [person("people/f", "Felipe Dias", emails=[NEW_ADDRESS])])
    c.cal.responses["create"] = CREATED
    st = await c.say("@lisa marca reuniao com o Felipe Dias sexta 15h", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Find Felipe Dias's email, for an invite"}},
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "call": {"skill": "calendar", "request": f"Felipe Dias: {NEW_ADDRESS}"}},
        {"lang": "pt", "actions": [dict(BOOKING)]},
    ])
    check("the loop ran calendar, contacts, contacts, calendar",
          domains_called(c) == ["calendar", "contacts", "contacts", "calendar"], str(domains_called(c)))
    check("the called skill was told who was waiting",
          "The calendar skill brought you in" in c.stub.calls[1]["system"])
    readback = c.stub.calls[2]["messages"][-1]["content"]
    check("for an invite, it could read the address it was sent for", NEW_ADDRESS in readback, readback)
    check("calendar got the answer back",
          c.stub.calls[3]["messages"][-1]["content"].startswith(HANDOFF_MARK)
          and NEW_ADDRESS in c.stub.calls[3]["messages"][-1]["content"])
    check("calendar is no longer being told it was called",
          "brought you in" not in c.stub.calls[3]["system"])
    check("nobody is left waiting", st.get("skill_stack") == [])
    check("nothing was said until there was something to say", len(c.evo.sent) == 1)
    card = c.last()
    check("the booking card goes out, naming the guest", "Felipe Dias" in card and NEW_ADDRESS in card, card)
    check("nothing booked before his yes", c.cal.n("create") == 0)
    await c.say("sim", script=[])
    check("his yes books it", c.cal.n("create") == 1)
    check("with the guest the contacts skill found",
          c.cal.calls[-1][1]["attendees"] == [NEW_ADDRESS])


TESTOLINO = person("people/z", "Zeferino Testolino", emails=["zeferino@example.com"])
GUESTLESS = {"task": "calendar.create", "title": "Marcelo & Testolini", "start": "2027-03-05T15:00:00-03:00"}


async def revise_checks() -> None:
    print("\na booking for someone with no address asks contacts first, even when the model skips it")
    c = Chat([TESTOLINO, *fifteen_felipes()])
    c.cal.responses["create"] = CREATED
    st = await c.say("@lisa marca reuniao com o Testolini sexta 15h", script=[
        {"lang": "pt", "actions": [dict(GUESTLESS)]},               # the skip seen live
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "testolini"}]},
        {"lang": "pt", "call": {"skill": "calendar", "request": "Zeferino Testolino: zeferino@example.com"}},
        {"lang": "pt", "actions": [{**GUESTLESS, "title": "Marcelo & Zeferino Testolino",
                                    "attendees": ["zeferino@example.com"]}]},
    ])
    check("the guestless booking went to contacts, and came back",
          domains_called(c) == ["calendar", "contacts", "contacts", "calendar"], str(domains_called(c)))
    asked = c.stub.calls[1]["messages"][-1]["content"]
    check("contacts was asked for that person's address", "Find Testolini's email, for an invite" in asked, asked)
    check("the search found him", "zeferino@example.com" in c.stub.calls[2]["messages"][-1]["content"])
    back = c.stub.calls[3]["messages"][-1]["content"]
    check("calendar heard the answer", back.startswith(HANDOFF_MARK) and "zeferino@example.com" in back, back)
    check("with his request still in front of it",
          any("Testolini sexta 15h" in m["content"] for m in c.stub.calls[3]["messages"]))
    check("only one card went out, the one with the guest",
          len(c.evo.sent) == 1 and "zeferino@example.com" in c.last(), str(c.evo.sent))
    check("nothing booked before his yes", c.cal.n("create") == 0)
    await c.say("sim", script=[])
    check("his yes books it with the guest", c.cal.calls[-1][1].get("attendees") == ["zeferino@example.com"])

    print("\n...but only once, and only when nothing explains the empty guest list")
    c = Chat([TESTOLINO])
    await c.say("@lisa marca reuniao com o Testolini sexta 15h", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Find Testolini's email, for an invite"}},
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "testolini zzz"}]},
        {"lang": "pt", "call": {"skill": "calendar", "request": "Nobody called Testolini is on file."}},
        {"lang": "pt", "actions": [dict(GUESTLESS)]},
    ])
    check("contacts found nobody: the guestless card goes out, no second search",
          domains_called(c) == ["calendar", "contacts", "contacts", "calendar"]
          and "Testolini" in c.last(), f"{domains_called(c)} {c.last()!r}")

    s = on_settings()
    owner = {"from_me": True, "messages": [], "skill_calls": 0}
    def revised(action, state=owner, settings=s):
        result = {"actions": [action], "message": None, "call": None}
        return calendar_revise(result, state, settings) is not result
    check("the guestless 'Marcelo & Testolini' is revised", revised(dict(GUESTLESS)))
    check("not when he asked for a quiet event", not revised({**GUESTLESS, "send_invites": False}))
    check("not when it has a guest", not revised({**GUESTLESS, "attendees": ["a@b.com"]}))
    check("not a topic title", not revised({**GUESTLESS, "title": "Dentista"}))
    check("not someone else's 'Ana & Pedro'", not revised({**GUESTLESS, "title": "Ana & Pedro"}))
    check("not with the flag off", not revised(dict(GUESTLESS), settings=on_settings(contacts_crud_enabled=False)))
    check("not for a message that is not his", not revised(dict(GUESTLESS), state={**owner, "from_me": False}))
    check("not after a call this activation", not revised(dict(GUESTLESS), state={**owner, "skill_calls": 1}))
    check("not while calendar is itself the one called",
          not revised(dict(GUESTLESS), state={**owner, "skill_stack": [{"domain": "contacts"}]}))
    listed = {**owner, "seen_contacts": {"z@x.com": {"name": "Zeferino Testolini"}}}
    check("not when the block listed him", not revised(dict(GUESTLESS), state=listed))
    heard = {**owner, "messages": [{"role": "user",
                                    "content": f"{HANDOFF_MARK}The contacts skill answered: none"}]}
    check("not once contacts has answered in this loop", not revised(dict(GUESTLESS), state=heard))


async def call_write_checks() -> None:
    print("\ncalendar calls contacts for a change, he approves, and calendar carries on")
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.cal.responses["create"] = CREATED
    c.felipe(f"Agora é {NEW_ADDRESS}")
    st = await c.say("@lisa atualiza o email dele e marca reuniao sexta 15h", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": f"Change Felipe Dias's email to {NEW_ADDRESS}"}},
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/f", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}]},
    ])
    check("the contact card goes out", "Confirmando alteração" in c.last(), c.last())
    check("calendar is waiting", [f["domain"] for f in st.get("skill_stack") or []] == ["calendar"])

    n_sent = len(c.evo.sent)
    st = await c.say("sim", script=[{"lang": "pt", "actions": [dict(BOOKING)]}])
    check("his yes runs the contact change", c.google.db["people/f"]["emailAddresses"] == [{"value": NEW_ADDRESS}])
    results = [t for _n, t in c.evo.sent[n_sent:]]
    check("two messages: the result, then the next card", len(results) == 2, str(results))
    check("first the contact result", "Contato atualizado" in results[0], str(results))
    check("then the booking card, with no further prompting", "Posso agendar?" in results[1], str(results))
    check("calendar was told the change happened",
          any(NEW_ADDRESS in str(m.get("content")) and HANDOFF_MARK in str(m.get("content"))
              for m in c.stub.calls[-1]["messages"]))
    check("nobody is left waiting", st.get("skill_stack") == [])
    check("the booking still waits for its own yes", c.cal.n("create") == 0)
    await c.say("sim", script=[])
    check("and runs on it", c.cal.n("create") == 1)
    check("no write ever ran on someone else's approval", len(c.google.updates) == 1)


async def next_step_checks() -> None:
    print("\ncontacts, then calendar, on one yes")
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.felipe(f"Agora é {NEW_ADDRESS}")
    step = {"skill": "calendar", "request": "Book a meeting with Felipe Dias on Friday at 15h"}
    update = {"task": "contacts.update", "contact_id": "people/f", "changes": [
        {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}
    find = {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]}

    st = await c.say("@lisa atualize o email do felipe nos contatos e marca com ele sexta 15h",
                     script=[find, {"lang": "pt", "actions": [dict(update)], "call": step}])
    check("the next step rides with the card", st.get("pending_call") == step)
    check("it does not happen yet", c.cal.calls == [] and len(c.stub.calls) == 2)
    st = await c.say("sim", script=[{"lang": "pt", "actions": [dict(BOOKING)]}])
    check("the yes runs the write, then moves to calendar",
          len(c.google.updates) == 1 and domains_called(c)[-1] == "calendar", str(domains_called(c)))
    check("it does not leave contacts waiting to be answered", st.get("skill_stack") == [])
    check("the booking gets its own card", "Posso agendar?" in c.last(), c.last())

    # He declines: the step behind it goes too.
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.felipe(f"Agora é {NEW_ADDRESS}")
    await c.say("@lisa atualize o email do felipe nos contatos e marca com ele sexta 15h",
                script=[find, {"lang": "pt", "actions": [dict(update)], "call": step}])
    st = await c.say("nao", script=[{"lang": "pt", "message": "Ok."}])
    check("declining drops the next step with it", not st.get("pending_call") and not st.get("next_call"))
    check("and calendar never runs", "calendar" not in domains_called(c))

    # The write fails: what was approved on the strength of it does not run.
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.felipe(f"Agora é {NEW_ADDRESS}")
    await c.say("@lisa atualize o email do felipe nos contatos e marca com ele sexta 15h",
                script=[find, {"lang": "pt", "actions": [dict(update)], "call": step}])
    c.google.fail_with = _Err(503)
    st = await c.say("sim", script=[])
    check("a failed write stops the next step", "calendar" not in domains_called(c), str(domains_called(c)))
    check("and says so", "Não consegui falar com o Google" in c.last(), c.last())


async def bound_checks() -> None:
    print("\nthe rails on calling")
    # Two skills calling each other forever: the bound stops it. Calling back is a pop, so this
    # needs both to keep asking.
    c = Chat([], max_skill_calls=4)
    ping = {"lang": "pt", "call": {"skill": "contacts", "request": "again"}}
    pong = {"lang": "pt", "call": {"skill": "calendar", "request": "again"}}
    await c.say("@lisa marca reuniao com a ana", script=[ping, pong, ping, pong, ping, pong])
    check("two skills calling each other stop at the bound", len(c.stub.calls) == 5, str(len(c.stub.calls)))

    # A skill that cannot be called.
    c = Chat([])
    await c.say("@lisa marca reuniao com a ana", script=[
        {"lang": "pt", "call": {"skill": "setup", "request": "change my settings"}},
        {"lang": "pt", "message": "Não consigo."},
    ])
    check("a call to a skill it may not call is refused, and it is told",
          "did not happen" in c.stub.calls[1]["messages"][-1]["content"])
    check("and it stayed where it was", domains_called(c) == ["calendar", "calendar"])

    # Someone else's message cannot bring contacts in by way of the calendar.
    c = Chat([person("people/a", "Ana", emails=["a@x.com"])])
    await c.say("@lisa marca reuniao com a ana", script=[{"lang": "pt", "message": "Que horas?"}])
    await c.say("me passa o email da ana", from_me=False, name="Felipe Rodrigues", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Ana's email"}},
        {"lang": "pt", "message": None},
    ])
    check("a contact cannot reach contacts through a call",
          "did not happen" in c.stub.calls[-1]["messages"][-1]["content"], c.stub.calls[-1]["messages"][-1]["content"])

    # He moves on, so whoever was waiting stops waiting.
    c = Chat([person("people/f", "Felipe Dias", emails=["f@x.com"])])
    st = await c.say("@lisa marca reuniao com o felipe", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Find Felipe's email"}},
        {"lang": "pt", "state": "awaiting_answer", "message": "Qual Felipe?"},
    ])
    check("the caller waits while he answers", len(st.get("skill_stack") or []) == 1)
    check("the question is the called skill's", "Qual Felipe?" in c.last())
    st = await c.say("na verdade cancela a reuniao", script=[{"lang": "pt", "message": "Ok."}])
    check("moving to another skill himself abandons the call", st.get("skill_stack") == [], str(st.get("skill_stack")))
    c2 = Chat([person("people/f", "Felipe Dias", emails=["f@x.com"])])
    st = await c2.say("@lisa marca reuniao com o felipe", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Find Felipe's email"}},
        {"lang": "pt", "state": "awaiting_answer", "message": "Qual Felipe?"},
    ])
    st = await c2.say("@lisa qual a capital do peru?", script=[{"lang": "pt", "message": "Lima."}])
    check("a fresh tag clears it", st.get("skill_stack") == [])

    # A carried yes on a calendar card keeps what is waiting on it.
    state = {"pending_action": {"task": "calendar.create"},
             "skill_stack": [{"domain": "contacts", "goal": "g"}],
             "pending_call": {"skill": "contacts", "request": "r"}}
    kept = kept_on_carry(state)
    check("a carried yes keeps who was waiting, and what comes next",
          kept["skill_stack"] == state["skill_stack"] and kept["pending_call"] == state["pending_call"])


async def block_checks() -> None:
    print("\nthe per-turn block")
    d, _s, _st = book([person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511987654321"]),
                       person("people/f", "Felipe Dias", emails=["felipe@x.com"], phones=["+5511992890847"])])
    settings = on_settings()
    out = contacts_context({"turn_text": "fala com a Ana Silva", "phone": "5511992890847", "from_me": True},
                           {"directory": d, "settings": settings})
    check("it lists who the turn points at, with an id", "[people/a]" in out["block"], out["block"])
    check("it hides anyone not in this chat", "ana@x.com" not in out["block"], out["block"])
    check("it shows the person this chat is with", "felipe@x.com" in out["block"], out["block"])
    check("what it surfaced can be written to", set(out["state"]["seen_people"]) >= {"people/a", "people/f"})
    out = contacts_context({"turn_text": "atualiza o email", "phone": "5511000000000", "from_me": True},
                           {"directory": d, "settings": settings})
    check("it says when nobody on file has this chat's number",
          out and "no card has that number" in out["block"], str(out))
    cold = Directory(settings)
    check("a cold book gives no block", contacts_context({"turn_text": "x"}, {"directory": cold, "settings": settings}) is None)


# --- 13. what an independent review found ---------------------------------------------------------
#
# Each of these was reproduced against the real graph before it was fixed, and each one passed every
# check above while being wrong.

FIND_ANA = {"lang": "pt", "actions": [{"task": "contacts.find", "query": "ana silva"}]}
DELETE_ANA = {"lang": "pt", "actions": [{"task": "contacts.delete", "contact_id": "people/a"}]}


def ana(**kw):
    return person("people/a", "Ana Silva", emails=["ana@x.com"], **kw)


async def refusal_checks() -> None:
    print("\na refusal in his own words")
    for refusal in ("não apaga", "cancela por favor", "pode cancelar", "não, deixa", "espera"):
        c = Chat([ana()], self_chat=True)
        await c.say("@lisa apaga o contato da ana silva", script=[FIND_ANA, DELETE_ANA])
        st = await c.say(refusal, script=[{"lang": "pt", "message": "Ok, não vou apagar."}])
        check(f"{refusal!r} withdraws the delete card", not st.get("pending_action"))
        await c.say("ok", script=[{"lang": "pt", "message": "Certo."}])
        check(f"so {refusal!r}, then 'ok', deletes nothing", c.google.deletes == [], str(c.google.deletes))
    for label, action in (
        ("update", {"task": "contacts.update", "contact_id": "people/a", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": "ana@new.com"}]}),
        ("create", {"task": "contacts.create", "name": "Bia Lima", "changes": [
            {"field": "email", "op": "add", "old": "", "new": "ana@new.com"}]}),
    ):
        c = Chat([ana()], self_chat=True)
        await c.say("@lisa o contato da ana silva agora é ana@new.com", script=[
            FIND_ANA, {"lang": "pt", "actions": [dict(action)]}])
        await c.say("cancela", script=[{"lang": "pt", "message": "Ok, cancelado."}])
        await c.say("ok", script=[{"lang": "pt", "message": "Certo."}])
        check(f"'cancela' on an {label} card, then 'ok', writes nothing",
              c.google.updates == [] and c.google.creates == [])

    # A correction still works: she is told, and proposes the corrected card.
    c = Chat([ana()], self_chat=True)
    await c.say("@lisa o email no contato da ana silva agora é ana@new.com, ou ana@other.com", script=[
        FIND_ANA, {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": "ana@new.com"}]}]}])
    first = c.last()
    await c.say("nao, o outro", script=[{"lang": "pt", "actions": [
        {"task": "contacts.update", "contact_id": "people/a", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": "ana@other.com"}]}]}])
    check("she is told the card is off", "withdrawn" in str(c.stub.calls[-1]["messages"][-1]["content"]))
    check("the corrected card goes out", "ana@other.com" in c.last() and c.last() != first, c.last())
    await c.say("sim", script=[])
    check("and his yes runs the correction, not the original",
          c.google.db["people/a"]["emailAddresses"] == [{"value": "ana@other.com"}])

    # An identical card can go out again after being withdrawn, or he would get silence.
    c = Chat([ana()], self_chat=True)
    await c.say("@lisa apaga o contato da ana silva", script=[FIND_ANA, DELETE_ANA])
    n = len(c.evo.sent)
    await c.say("espera, deixa eu ver", script=[DELETE_ANA])
    check("a withdrawn card can be asked again", len(c.evo.sent) == n + 1 and "Apagar" in c.last(), c.last())

    # She answered someone else in between, so his "sim" is not about the card any more.
    c = Chat([ana()], self_chat=False)
    await c.say("@lisa apaga o contato da ana silva", script=[FIND_ANA, DELETE_ANA])
    await c.say("que horas você chega?", from_me=False, name="Felipe Rodrigues",
                script=[{"lang": "pt", "message": "Ele já te responde."}])
    await c.say("sim", script=[{"lang": "pt", "message": "Sim para o quê?"}])
    check("a 'sim' after she spoke again does not reach the older card", c.google.deletes == [], str(c.google.deletes))


async def other_skill_checks() -> None:
    print("\nanother skill's question")
    c = Chat([ana()], self_chat=True)
    c.cal.responses["create"] = CREATED
    await c.say("@lisa apaga o contato da ana silva", script=[FIND_ANA, DELETE_ANA])
    st = await c.say("antes, tenho reuniao com ela sexta?", script=[
        {"lang": "pt", "call": {"skill": "calendar", "request": "Is there a meeting with Ana on Friday?"}},
        {"lang": "pt", "state": "awaiting_answer", "message": "Sim, sexta 15h. Quer que eu mova?"},
    ])
    check("moving skills withdraws the card", not st.get("pending_action"), str(st.get("pending_action")))
    await c.say("sim", script=[{"lang": "pt", "message": "Para quando?"}])
    check("his 'sim' to calendar's question does not delete Ana", c.google.deletes == [], str(c.google.deletes))

    # The mirror: a calendar card standing, contacts asks, and his answer has to reach contacts.
    c = Chat([person("people/f", "Felipe Dias", emails=["f@x.com"]),
              person("people/g", "Felipe Lima", emails=["g@x.com"])])
    c.cal.responses["create"] = CREATED
    await c.say("@lisa marca reuniao com o felipe dias sexta 15h com felipe@rssf.com.br",
                script=[{"lang": "pt", "actions": [dict(BOOKING)]}])
    st = await c.say("confere nos contatos o email do felipe", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Check Felipe's email"}},
        {"lang": "pt", "state": "awaiting_answer", "message": "Dias ou Lima?"},
    ])
    check("the calendar card is off while contacts asks", not st.get("pending_action"))
    n = len(c.stub.calls)
    await c.say("o dias", script=[{"lang": "pt", "call": {"skill": "calendar", "request": "f@x.com"}},
                                  {"lang": "pt", "actions": [dict(BOOKING)]}])
    check("his answer reaches the skill that asked", domains_called(c)[n:] == ["contacts", "calendar"], str(domains_called(c)[n:]))
    check("and calendar's card can go out again, the same words and all", "Posso agendar?" in c.last(), c.last())


async def early_yes_checks() -> None:
    print("\na yes typed before the card existed")
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.cal.responses["create"] = CREATED
    c.felipe(f"Agora é {NEW_ADDRESS}")
    await c.say("@lisa atualiza o email dele e marca reuniao sexta 15h", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": f"Change Felipe Dias's email to {NEW_ADDRESS}"}},
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/f", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}]},
    ])
    await c.say("sim", script=[{"lang": "pt", "actions": [dict(BOOKING)]}])
    check("the booking card is out", "Posso agendar?" in c.last(), c.last())
    # His second tap landed while that card was being written: it sits above the card in the chat.
    card_at = max(i for i, r in enumerate(c.evo.history) if "Posso agendar?" in r["text"])
    c.evo.history.insert(card_at, {"id": "early", "from_me": True, "text": "ok, sim", "push_name": "Marcelo",
                                   "ts": c.evo.history[card_at]["ts"]})
    await c.say("ok, sim", script=[], stored=True)
    check("it does not approve a booking he had not seen", c.cal.n("create") == 0, str(c.cal.calls))
    await c.say("sim", script=[])
    check("his yes to the card he can see does", c.cal.n("create") == 1)


async def someone_else_checks() -> None:
    print("\nsomeone else's words")
    c = Chat([ana()])
    await c.say("@lisa acha o contato da ana silva", script=[FIND_ANA, {"lang": "pt", "message": "Achei."}])
    await c.say("apaga o contato da ana silva", from_me=False, name="Felipe Rodrigues",
                script=[DELETE_ANA, {"lang": "pt", "message": None}])
    check("a contact's message cannot put a delete in front of him",
          not any("Apagar" in t for _n, t in c.evo.sent), str(c.evo.sent))
    await c.say("ok", script=[{"lang": "pt", "message": "Certo."}])
    check("so his next 'ok' deletes nothing", c.google.deletes == [])

    d, _s, _st = book([ana(phones=["+5511987654321"])])
    settings = on_settings()
    out = contacts_context({"turn_text": "Bob: qual o email da Ana Silva?", "from_me": False,
                            "participant_phones": ["5511987654321"]},
                           {"directory": d, "settings": settings})
    check("a turn he had no part in gets no address book at all", out is None, str(out))
    out = contacts_context({"turn_text": "qual o email da Ana Silva?", "from_me": True,
                            "participant_phones": ["5511987654321"]},
                           {"directory": d, "settings": settings})
    check("his own gets it", out and "ana@x.com" in out["block"])

    # A bystander's remark must not take away the step he is about to approve.
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.felipe(f"Agora é {NEW_ADDRESS}")
    step = {"skill": "calendar", "request": "Book a meeting with Felipe Dias on Friday at 15h"}
    await c.say("@lisa atualize o email do felipe nos contatos e marca com ele sexta 15h", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/f", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}], "call": step},
    ])
    st = await c.say("show, valeu", from_me=False, name="Felipe Rodrigues", script=[{"lang": "pt", "message": None}])
    check("a bystander's remark leaves the next step standing", st.get("pending_call") == step, str(st.get("pending_call")))
    n = len(c.stub.calls)
    await c.say("sim", script=[{"lang": "pt", "actions": [dict(BOOKING)]}])
    check("so his yes runs both", len(c.google.updates) == 1 and domains_called(c)[n:] == ["calendar"], str(domains_called(c)[n:]))


async def membership_checks() -> None:
    print("\nwho counts as being in the chat")
    felipe = "5511992890847"
    c = Chat([person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511987654321"],
                     biographies=[{"value": "secret note"}]),
              person("people/f", "Felipe Dias", emails=["felipe@x.com"], phones=["+" + felipe])])
    d = c.directory
    await d.note_used("felipe@x.com", phone=felipe)
    await d.note_used("ana@x.com", phone=felipe)   # one booking from his chat, with both on it
    check("a booking with Ana does not put Ana in Felipe's chat", d.member_ids(felipe, []) == {"people/f"}, str(d.member_ids(felipe, [])))
    await c.say("@lisa qual o telefone da ana silva?", script=[FIND_ANA, {"lang": "pt", "message": "x"}])
    seen = c.stub.calls[-1]["messages"][-1]["content"]
    check("so she still sees Ana masked", "ana@x.com" not in seen and "987654321" not in seen and "secret" not in seen, seen)
    check("a number is on the card that holds it, whatever the links say", [x["name"] for x in d.by_phone(felipe)] == ["Felipe Dias"])
    d2, _s, _st = book([person("people/1", "Um", phones=["+5511912345678"]), person("people/2", "Dois", phones=["+5511912345678"])])
    check("a number on two cards puts neither in the chat", d2.member_ids("5511912345678", []) == set())


async def invite_checks() -> None:
    print("\nreading an address for an invite")
    c = Chat(fifteen_felipes())
    await c.say("@lisa marca reuniao com o felipe sexta 15h", script=[
        {"lang": "pt", "call": {"skill": "contacts", "request": "Find Felipe's email, for an invite"}},
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe"}]},
        {"lang": "pt", "state": "awaiting_answer", "message": "Qual Felipe?"},
    ])
    seen = c.stub.calls[-1]["messages"][-1]["content"]
    check("fifteen candidates are a question, not fifteen addresses", "@x.com" not in seen and "@oldfirm.com" not in seen, seen[:300])
    await c.say("o dias", script=[
        {"lang": "pt", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "pt", "message": "x"},
    ])
    seen = c.stub.calls[-1]["messages"][-1]["content"]
    check("the one he means gives its address", "felipe@oldfirm.com" in seen, seen)


async def single_value_checks() -> None:
    print("\nfields that hold one value")
    notes = [{"value": "ten years of notes", "contentType": "TEXT_PLAIN"}]
    add = {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [
        {"field": "notes", "op": "add", "old": "", "new": "vegana"}]}]}
    c = Chat([ana(biographies=notes)], self_chat=True)
    await c.say("@lisa anota no contato da ana silva que ela é vegana", script=[FIND_ANA, add])
    await c.say("sim", script=[])
    check("adding to the notes keeps what was there",
          c.google.db["people/a"]["biographies"][0]["value"] == "ten years of notes\nvegana",
          str(c.google.db["people/a"]["biographies"]))

    rewrite = {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [
        {"field": "notes", "op": "replace", "old": "", "new": "vegana"}]}]}
    c = Chat([ana(biographies=notes)], self_chat=True)
    await c.say("@lisa troca as notas do contato da ana silva", script=[FIND_ANA, rewrite])
    check("a rewrite shows what it is about to lose", "ten years of notes" in c.last() and "→" in c.last(), c.last())

    # Never found: only the block surfaced her, so the card cannot name what is there, and says so.
    c = Chat([ana(biographies=notes)], self_chat=True)
    await c.say("@lisa troca as notas do contato da Ana Silva", script=[rewrite])
    check("a card that has not read the notes says it overwrites them", "substitui" in c.last(), c.last())

    # In her own chat, the owner's notes on her stay masked on the card too.
    member = person("people/a", "Ana Silva", emails=["ana@x.com"], phones=["+5511992890847"], biographies=notes)
    c = Chat([member])
    await c.say("@lisa troca as notas do contato da ana silva", script=[FIND_ANA, rewrite])
    check("a card in her chat hides his notes on her", "ten years" not in c.last() and "•••" in c.last(), c.last())
    seen = c.stub.calls[1]["messages"][-1]["content"]
    check("and so does what she reads", "ten years" not in seen and "ana@x.com" in seen, seen)

    title = {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [
        {"field": "title", "op": "add", "old": "", "new": "CTO"}]}]}
    c = Chat([ana(organizations=[{"name": "Acme", "title": "CEO"}])], self_chat=True)
    await c.say("@lisa coloca o cargo no contato da ana silva", script=[FIND_ANA, title])
    check("'adding' a title that exists reads as the replacement it is",
          "CEO → CTO" in c.last() and "Adicionar" not in c.last(), c.last())

    # The block runs on every pass, and must not knock the whole card out of the loop's memory.
    c = Chat([ana(biographies=notes)], self_chat=True)
    st = await c.say("@lisa acha a ana silva", script=[FIND_ANA, {"lang": "pt", "message": "Achei."}])
    check("the whole card survives the next pass", st["seen_people"]["people/a"].get("whole") is True, str(st["seen_people"]))


async def profile_checks() -> None:
    print("\na value that only lives on the Google profile")
    async def run(ch):
        c = Chat([person("people/a", "Ana Silva")])
        c.google.profile["people/a"] = {"emailAddresses": [{"value": "ana@profile.com"}]}
        c.directory._contacts["people/a"]["emails"] = ["ana@profile.com"]
        c.directory._reindex()
        await c.say("@lisa troca o email no contato da ana silva para ana@new.com", script=[
            FIND_ANA, {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [dict(ch)]}]},
        ])
        await c.say("sim", script=[{"lang": "pt", "message": "(read back)"}])
        return c

    c = await run({"field": "email", "op": "remove", "old": "a***@p***.com", "new": ""})
    check("removing it does not claim the card 'already says that'", "já está assim" not in c.last(), c.last())
    check("it says it is still showing, and why", "perfil Google" in c.last(), c.last())
    check("in the masked form this chat was given", "ana@profile.com" not in c.last() and "a***@p***.com" in c.last(), c.last())
    check("and sends Google nothing", c.google.updates == [])

    c = await run({"field": "email", "op": "replace", "old": "", "new": "ana@new.com"})
    check("a blind replace puts the new one on the card", c.google.db["people/a"]["emailAddresses"] == [{"value": "ana@new.com"}])
    check("and says the old one is still showing", "perfil Google" in c.last() and "ana@profile.com" not in c.last(), c.last())

    c = await run({"field": "email", "op": "replace", "old": "a***@p***.com", "new": "ana@new.com"})
    check("a named replace does the same", c.google.db["people/a"]["emailAddresses"] == [{"value": "ana@new.com"}], str(c.google.db["people/a"].get("emailAddresses")))
    check("rather than telling her the card lost it", "no longer has" not in str(c.stub.calls[-1]["messages"][-1]["content"]))
    leaked = [m for m in c.stub.calls[-1]["messages"] if "ana@profile.com" in str(m.get("content"))]
    check("the real value never reaches her in this chat", not leaked, str(leaked)[:200])

    # A stuck value that also sits on the card, in a masked chat.
    c = Chat([person("people/a", "Ana Silva", emails=["ana@gmail.com"])])
    c.google.profile["people/a"] = {"emailAddresses": [{"value": "ana@gmail.com"}]}
    await c.say("@lisa troca o email no contato da ana silva para ana@new.com", script=[
        FIND_ANA, {"lang": "pt", "actions": [{"task": "contacts.update", "contact_id": "people/a", "changes": [
            {"field": "email", "op": "replace", "old": "a***@g***.com", "new": "ana@new.com"}]}]},
    ])
    await c.say("sim", script=[])
    check("an address on both the contact and the profile still resolves from its mask",
          c.google.db["people/a"]["emailAddresses"] == [{"value": "ana@new.com"}],
          str(c.google.db["people/a"].get("emailAddresses")))
    check("a stuck value is posted, and posted masked",
          "perfil Google" in c.last() and "ana@gmail.com" not in c.last() and "a***@g***.com" in c.last(), c.last())
    from app.tools.people import view
    check("one address on two sources is one address",
          view(person("people/z", "Z", emails=["a@x.com", "A@x.com"], phones=["+55119", "+55119"]))
          ["emails"] == ["a@x.com"])


async def value_checks() -> None:
    print("\nvalues that are not values")
    state = {"seen_people": {"people/a": {"name": "A", "emails": ["ana@x.com"]}},
             "messages": [{"role": "user", "content": "[contacts.find result] email: a***@x***.com"},
                          {"role": "user", "content": "o email é joana@acme.com"}]}
    upd = {"task": "contacts.update", "contact_id": "people/a"}
    bad = problem_with({**upd, "changes": [{"field": "email", "op": "add", "old": "", "new": "a***@x***.com"}]}, state)
    check("a mask is refused as a new address, though it is in the conversation", bool(bad), str(bad))
    bad = problem_with({**upd, "changes": [{"field": "email", "op": "add", "old": "", "new": "ana@acme.com"}]}, state)
    check("ana@acme.com is not grounded by joana@acme.com", bool(bad), str(bad))
    ok = problem_with({**upd, "changes": [{"field": "email", "op": "add", "old": "", "new": "joana@acme.com"}]}, state)
    check("the address actually said is", ok is None, str(ok))
    bad = problem_with({**upd, "changes": [{"field": "phone", "op": "add", "old": "", "new": "••••4321"}]}, state)
    check("a masked phone is refused too", bool(bad), str(bad))


async def rest_checks() -> None:
    print("\nthe rest")
    d, svc, st = book([ana(urls=[{"value": "https://ana.example"}], relations=[{"person": "Bob"}])])
    tool = ContactsService(on_settings())
    tool.directory = d
    await tool.run("delete", {"contact_id": "people/a"})
    copied = st.tombstones[0][1]
    check("the copy keeps the fields Lisa never edits", copied.get("urls") and copied.get("relations"), str(copied))
    from app.tools.people import ALL_FIELDS
    asked = [r for r in svc.reads if r[1] == ALL_FIELDS]
    check("because it asks Google for every field", bool(asked), str(svc.reads))

    from app.review.prompt import build_judge_prompt
    off = build_judge_prompt("Marcelo", "@lisa")
    on = build_judge_prompt("Marcelo", "@lisa", contacts_skill=True)
    check("with the skill off, the judge is not told she can edit contacts", "Google Contacts" not in off)
    check("with it on, it is", "Google Contacts" in on and "f***@a***.com" in on)

    from app.review.store import _context_for
    lines = [{"who": "Marcelo", "trace_id": "t", "text": "sim"},
             {"who": "AI Assistant", "trace_id": "t", "text": "Contato atualizado"},
             {"who": "AI Assistant", "trace_id": "t", "text": "Posso agendar?"}]
    texts = [ln["text"] for ln in _context_for(lines, "t")]
    check("the judge reading her second reply still sees her first", texts == ["sim", "Contato atualizado"], str(texts))
    texts = [ln["text"] for ln in _context_for(lines[:2], "t")]
    check("and never the reply it is judging", texts == ["sim"], str(texts))
    texts = [ln["text"] for ln in _context_for(lines[:2], "t", wrote=False)]
    check("a silent pass after a reply keeps that reply", texts == ["sim", "Contato atualizado"], str(texts))

    # A language with no programmatic card: the model writes the result, and still hands back.
    c = Chat([person("people/f", "Felipe Dias", emails=["felipe@oldfirm.com"])])
    c.cal.responses["create"] = CREATED
    c.felipe(f"Agora é {NEW_ADDRESS}")
    await c.say("@lisa atualiza o email dele e marca reuniao sexta 15h", script=[
        {"lang": "fr", "call": {"skill": "contacts", "request": f"Change Felipe Dias's email to {NEW_ADDRESS}"}},
        {"lang": "fr", "actions": [{"task": "contacts.find", "query": "felipe dias"}]},
        {"lang": "fr", "actions": [{"task": "contacts.update", "contact_id": "people/f", "changes": [
            {"field": "email", "op": "replace", "old": "", "new": NEW_ADDRESS}]}]},
    ])
    st = await c.say("sim", script=[{"lang": "fr", "message": "C'est fait."},
                                    {"lang": "fr", "actions": [dict(BOOKING)]}])
    check("a called skill hands back in any language", st.get("skill_stack") == [] and domains_called(c)[-1] == "calendar", str(domains_called(c)))


async def main() -> int:
    merge_checks()
    client_checks()
    search_checks()
    await router_checks()
    contract_checks()
    await privacy_checks()
    gate_checks()
    yes_checks()
    await handler_checks()
    await mirror_checks()
    await block_checks()
    await felipe_checks()
    await not_on_file_checks()
    await contact_cannot_ask_checks()
    await decline_and_delete_checks()
    await call_read_checks()
    await revise_checks()
    await call_write_checks()
    await next_step_checks()
    await bound_checks()
    await refusal_checks()
    await other_skill_checks()
    await early_yes_checks()
    await someone_else_checks()
    await membership_checks()
    await invite_checks()
    await single_value_checks()
    await profile_checks()
    await value_checks()
    await rest_checks()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
