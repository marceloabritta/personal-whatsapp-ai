"""Code's check that the time zone Lisa names for an event is the one its time is written in.

On 2026-11-26 a contact proposed "Wednesday the 9th at 8am PT" and the owner said to book it,
minding the zone. The event went in right, and the card read "09/Dec - Wednesday, 13:00": his own
clock, with nothing saying so, under a conversation that had only ever said 8am. The next day a
coffee in San Francisco "10am quarta" got a card reading "10:00 AM": the other clock this time,
again with nothing saying so. He had to ask "10am na hora local de San Francisco" before saying yes.

Which clock a card showed depended on which offset she happened to write in "start", and no card
named either. So the zone is no longer something only an offset carries. Every create and update
says which zone the time was said in (`timezone`, an IANA name, required in the schema), and code
checks the offset in "start" against that zone on that date, which also catches a daylight-saving
offset from the wrong half of the year. A mismatch is not shown to anyone: the reason node sends
her back once with the right offset (`correction`) and uses her second answer.

The field then stays on the action only when it names a clock other than the owner's
(`settle`). The card shows the time on both clocks (skills/calendar_format.py) and the event is
stored in that zone (tools/calendar.py). A booking on his own clock leaves here without the field,
exactly as it was before any of this existed.

Pure and synchronous: it runs inside the reply path."""
from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from ..zones import zone_key, zone_of

_WRITES = ("calendar.create", "calendar.update")


def _parse(iso) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _timed(action: dict) -> bool:
    """Does this write carry an hour? An all-day event has no zone at all."""
    start = action.get("start")
    return bool(start) and "T" in str(start) and action.get("all_day") is not True


def _offset_text(delta: timedelta | None) -> str:
    """'-08:00', the way it is written at the end of "start"."""
    minutes = int((delta or timedelta(0)).total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _wall_offset(zone: tzinfo, wall: datetime) -> timedelta:
    """The zone's offset when its own clock reads `wall`."""
    return wall.replace(tzinfo=zone).utcoffset() or timedelta(0)


def mismatches(result: dict) -> list[dict]:
    """Every create or update in this pass whose zone and time do not agree.

    [{kind, name, start, offset}]: `kind` is "unknown" for a name that is no zone, "offset" for a
    start whose offset is not that zone's on that date (`offset` then says what it is). An action
    with no `timezone`, no start, or no hour is not checked."""
    out: list[dict] = []
    for a in result.get("actions") or []:
        if not isinstance(a, dict) or a.get("task") not in _WRITES:
            continue
        if not a.get("timezone") or not _timed(a):
            continue
        zone = zone_of(a["timezone"])
        if zone is None:
            out.append({"kind": "unknown", "name": str(a["timezone"]), "start": a.get("start")})
            continue
        start = _parse(a["start"])
        if start is None or start.tzinfo is None:
            continue                # no offset written: `settle` puts the zone's own on it
        right = _wall_offset(zone, start.replace(tzinfo=None))
        if start.utcoffset() != right:
            out.append({"kind": "offset", "name": str(a["timezone"]), "start": a["start"],
                        "offset": _offset_text(right)})
    return out


def correction(found: list[dict]) -> str:
    """The note that sends her back: what does not agree, and the offset that would."""
    lines = []
    for f in found:
        if f["kind"] == "unknown":
            line = (f"- \"timezone\": \"{f['name']}\" is not an IANA time zone name. Write one "
                    f"like \"America/Sao_Paulo\" or \"America/Los_Angeles\".")
        else:
            line = (f"- \"start\": \"{f['start']}\" with \"timezone\": \"{f['name']}\". On that "
                    f"date {f['name']} is UTC{f['offset']}, so a time on that zone's clock ends "
                    f"in {f['offset']}.")
        if line not in lines:
            lines.append(line)
    return ("[System check, not from anyone in the chat] The time zone in your last answer does "
            "not match its time, so nothing was sent:\n" + "\n".join(lines) + "\n"
            "Decide which zone the people in the chat said the time in. \"timezone\" is that "
            "zone's IANA name, and \"start\" is the clock time they said, written with that "
            "zone's offset on that date. Do not convert the hour to another zone. Then answer "
            "again in full.")


def _settled(action: dict, home: tzinfo | None) -> dict:
    if not isinstance(action, dict) or action.get("task") not in _WRITES \
            or "timezone" not in action:
        return action
    rest = {k: v for k, v in action.items() if k != "timezone"}
    zone = zone_of(action.get("timezone"))
    if zone is None or not _timed(action):
        return rest
    start = _parse(action["start"])
    if start is None:
        return rest
    if start.tzinfo is None:
        # A clock time with no offset, and a zone beside it: the time is on that zone's clock.
        start = start.replace(tzinfo=zone)
        rest["start"] = start.isoformat()
        end = _parse(action.get("end")) if action.get("end") else None
        if end is not None and end.tzinfo is None and "T" in str(action["end"]):
            rest["end"] = end.replace(tzinfo=zone).isoformat()
    if home is not None and start.astimezone(zone).utcoffset() == start.astimezone(home).utcoffset():
        return rest                 # the same clock as his: there is nothing to tell apart
    return {**rest, "timezone": zone_key(action["timezone"])}


def settle(result: dict, home_tz: str) -> dict:
    """The result with `timezone` left only on the writes whose time is on another clock than the
    owner's, spelled as the IANA name. Everywhere else the field is taken off, so a booking on his
    own clock runs exactly as it did before the field existed."""
    actions = result.get("actions") or []
    if not any(isinstance(a, dict) and "timezone" in a for a in actions):
        return result
    home = zone_of(home_tz)
    return {**result, "actions": [_settled(a, home) for a in actions]}
