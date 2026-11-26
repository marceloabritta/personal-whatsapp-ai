"""The card after a booking says it is booked, whether the guests were invited, and a short link.

On 2026-11-26 Marcelo found the card excessive: it repeated the title, the date and the guest
line from the confirmation he had just approved. He asked for "Booked. I've sent invites out to
the guests." plus the event link, worded a little differently from booking to booking, with the
link shortened (Google's is about 130 characters).

This file holds:
  1. the card itself, in pt / en / es, with and without guests and invites;
  2. the wording varying across events and staying put for one event;
  3. the shortener: a good answer, every way it can fail, and the cache;
  4. the calendar handler shortening the link after a create or update, never before or instead
     of the write, and the long link when the shortener is absent or fails.

No network: the shortener runs over an httpx MockTransport.

    python tests/run_booked_card.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.skills.calendar_format import _BOOKED, fmt_create  # noqa: E402
from app.tools.calendar import GoogleCalendarService  # noqa: E402
from app.tools.shortlink import ShortLinks  # noqa: E402

_checks = {"pass": 0, "fail": 0}

LONG = "https://www.google.com/calendar/event?eid=" + "Q" * 90
SHORT = "https://tinyurl.com/2x8k4pqz"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def result(**data) -> list:
    base = {"event_id": "abc123", "title": "Marcelo & Acme", "start": "2026-11-27T14:45:00-03:00",
            "end": "2026-11-27T15:30:00-03:00", "html_link": SHORT}
    return [{"ok": True, "summary": "created", "data": {**base, **data}}]


def test_card() -> None:
    print("the card")
    guests = ["ana@example.com", "bia@example.com"]
    for lang, label in (("pt", "Link do evento"), ("en", "Event link"), ("es", "Enlace del evento")):
        card = fmt_create(result(attendees=guests, notified=True), {"session_lang": lang})
        lines = card.split("\n")
        check(f"[{lang}] two lines and a blank: what happened, then the link",
              len(lines) == 3 and lines[1] == "" and lines[2] == f"{label}: {SHORT}", repr(card))
        check(f"[{lang}] no title, no date, no guest repeated",
              "Acme" not in card and "27" not in card.replace(SHORT, "")
              and "ana@" not in card, repr(card))
        check(f"[{lang}] says the invites went out", lines[0] in [v[1] for v in _BOOKED[lang]],
              repr(lines[0]))

        bare = fmt_create(result(), {"session_lang": lang})
        check(f"[{lang}] no guests: no word about invites",
              bare.split("\n")[0] in [v[0] for v in _BOOKED[lang]], repr(bare))

        silent = fmt_create(result(attendees=guests, notified=False), {"session_lang": lang})
        check(f"[{lang}] guests added without an invite: says so, never that one was sent",
              silent.split("\n")[0] in [v[2] for v in _BOOKED[lang]], repr(silent))

        nolink = fmt_create(result(html_link=None, attendees=guests, notified=True),
                            {"session_lang": lang})
        check(f"[{lang}] no link from Google: the one line alone", "\n" not in nolink, repr(nolink))

    en = fmt_create(result(event_id="e0", attendees=["a@example.com"], notified=True),
                    {"session_lang": "en"})
    check("his own words are one of the ways she says it",
          ("Booked.", "Booked. I've sent invites out to the guests.",
           "Booked. The guests didn't get an invite.") in _BOOKED["en"])
    check("an unknown language falls back to English",
          fmt_create(result(), {"session_lang": "de"}).split("\n")[0] in [v[0] for v in _BOOKED["en"]])
    check("each language has the same number of ways",
          len({len(v) for v in _BOOKED.values()}) == 1 and len(_BOOKED["en"]) >= 3, str(en))


def test_variety() -> None:
    print("\nthe wording varies")
    openings = {fmt_create(result(event_id=f"ev{i}", attendees=["a@example.com"], notified=True),
                           {"session_lang": "pt"}).split("\n")[0] for i in range(40)}
    check("40 bookings use every way of saying it", len(openings) == len(_BOOKED["pt"]),
          str(openings))
    once = [fmt_create(result(event_id="same"), {"session_lang": "en"}) for _ in range(5)]
    check("one booking always reads the same", len(set(once)) == 1)


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_shortener() -> None:
    print("\nthe shortener")
    asked = []

    def ok(req: httpx.Request) -> httpx.Response:
        asked.append(req)
        return httpx.Response(200, text=SHORT + "\n")

    s = ShortLinks(client=transport(ok))
    got = await s.shorten(LONG)
    check("a good answer becomes the link", got == SHORT, repr(got))
    check("the long link goes as the url parameter of the keyless endpoint",
          asked and asked[0].url.host == "tinyurl.com"
          and asked[0].url.path == "/api-create.php" and asked[0].url.params.get("url") == LONG)
    await s.shorten(LONG)
    check("the same link is asked for once", len(asked) == 1, str(len(asked)))
    check("nothing to shorten: None stays None", await s.shorten(None) is None)
    check("a non-https link is left alone and never sent", await s.shorten("http://cal/1")
          == "http://cal/1" and len(asked) == 1)

    for name, handler in (
        ("a 5xx", lambda r: httpx.Response(503, text="busy")),
        ("an error page with 200", lambda r: httpx.Response(200, text="<html>Error</html>")),
        ("another site's link", lambda r: httpx.Response(200, text="https://evil.example/x")),
        ("an empty answer", lambda r: httpx.Response(200, text="")),
    ):
        got = await ShortLinks(client=transport(handler)).shorten(LONG)
        check(f"{name}: the long link", got == LONG, repr(got))

    def boom(req):
        raise httpx.ConnectTimeout("slow", request=req)

    got = await ShortLinks(client=transport(boom)).shorten(LONG)
    check("a timeout: the long link", got == LONG, repr(got))


class FakeEvents:
    def __init__(self) -> None:
        self.calls = []

    def insert(self, **kw):
        self.calls.append(("insert", kw))
        return self

    def get(self, **kw):
        self.calls.append(("get", kw))
        return self

    def patch(self, **kw):
        self.calls.append(("patch", kw))
        return self

    def execute(self):
        return {"id": "E1", "summary": "Acme", "htmlLink": LONG,
                "start": {"dateTime": "2026-11-27T14:45:00-03:00"},
                "end": {"dateTime": "2026-11-27T15:30:00-03:00"},
                "attendees": [{"email": "ana@example.com"}]}


class FakeService:
    def __init__(self) -> None:
        self.ev = FakeEvents()

    def events(self):
        return self.ev


def cal(shortlinks) -> GoogleCalendarService:
    h = GoogleCalendarService(Settings(evolution_apikey="x"))
    h._svc = FakeService()
    h.shortlinks = shortlinks
    return h


async def test_handler() -> None:
    print("\nthe calendar handler")
    create = {"title": "Acme", "start": "2026-11-27T14:45:00-03:00",
              "attendees": ["ana@example.com"]}
    h = cal(ShortLinks(client=transport(lambda r: httpx.Response(200, text=SHORT))))
    res = await h.run("create", create)
    check("a create comes back with the short link", res.get("ok")
          and res["data"]["html_link"] == SHORT, str(res.get("data", {}).get("html_link")))
    check("the summary carries the short link, not the long one",
          SHORT in res["summary"] and LONG not in res["summary"], res.get("summary", ""))
    check("the write ran once", sum(1 for v, _ in h._svc.ev.calls if v == "insert") == 1)

    res = await h.run("update", {"event_id": "E1", "title": "Acme"})
    check("an update comes back with the short link",
          res.get("ok") and res["data"]["html_link"] == SHORT, str(res))

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    h = cal(ShortLinks(client=transport(boom)))
    res = await h.run("create", create)
    check("shortener down: the booking still lands, with the long link",
          res.get("ok") and res["data"]["html_link"] == LONG
          and sum(1 for v, _ in h._svc.ev.calls if v == "insert") == 1, str(res)[:200])

    h = cal(None)
    res = await h.run("create", create)
    check("no shortener attached: Google's link, as before",
          res.get("ok") and res["data"]["html_link"] == LONG)


def test_deps() -> None:
    print("\nthe switch")
    from app.deps import build_deps

    on = build_deps(Settings(evolution_apikey="x"))
    check("on by default: the calendar handler gets a shortener",
          isinstance(on.tools["calendar"].shortlinks, ShortLinks))
    off = build_deps(Settings(evolution_apikey="x", short_links_enabled=False))
    check("SHORT_LINKS_ENABLED=false: none", off.tools["calendar"].shortlinks is None)


async def main() -> None:
    test_card()
    test_variety()
    await test_shortener()
    await test_handler()
    test_deps()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
