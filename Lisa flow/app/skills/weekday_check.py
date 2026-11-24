"""Code's check that every weekday Lisa writes belongs to the date beside it.

On 2026-11-24 (a Tuesday) the group agreed on "quinta 15hrs". Lisa's own reasoning read
"Thursday (2026-11-27)", her question said "quinta, 27/11", and the event went in on Friday the 27th.
The prompt already holds a table of the days around today with each weekday beside its date, and
tells her to look a date up there. She still worked it out, and got it a day wrong. The card did
show "27/nov - Sexta" (code writes that line), and he approved it without seeing it.

So the weekday is no longer something only her prose carries. Every create and update says which
weekday she means (`weekday`, required in the schema), and code checks it against the date in
"start". Any "quinta, 27/11" pair in her message is checked the same way. A mismatch is not shown to
anyone: the reason node sends her back once with the right dates (`correction`) and uses her second
answer. The field is then dropped, so nothing past the reason node ever sees it.

Pure and synchronous: it runs inside the reply path."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_PT = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")

# Weekday words as they appear in her messages, folded (lowercase, no accents) → 0..6 (Monday=0).
_WORDS: dict[str, int] = {}
for _i, _names in enumerate([
    ("monday", "segunda", "segunda-feira", "lunes"),
    ("tuesday", "terca", "terca-feira", "martes"),
    ("wednesday", "quarta", "quarta-feira", "miercoles"),
    ("thursday", "quinta", "quinta-feira", "jueves"),
    ("friday", "sexta", "sexta-feira", "viernes"),
    ("saturday", "sabado"),
    ("sunday", "domingo"),
]):
    for _n in _names:
        _WORDS[_n] = _i

_MONTHS: dict[str, int] = {}
for _i, _names in enumerate([
    ("jan", "january", "janeiro", "enero", "ene"),
    ("feb", "february", "fev", "fevereiro", "febrero"),
    ("mar", "march", "marco", "marzo"),
    ("apr", "april", "abr", "abril"),
    ("may", "mai", "maio", "mayo"),
    ("jun", "june", "junho", "junio"),
    ("jul", "july", "julho", "julio"),
    ("aug", "august", "ago", "agosto"),
    ("sep", "sept", "september", "set", "setembro", "septiembre"),
    ("oct", "october", "out", "outubro", "octubre"),
    ("nov", "november", "novembro", "noviembre"),
    ("dec", "december", "dez", "dezembro", "dic", "diciembre"),
], start=1):
    for _n in _names:
        _MONTHS[_n] = _i

_DAY_WORD = r"(?P<wd>" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")"
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))
# "quinta, 27/11", "quinta-feira (27/11/2026)", "Thursday 27/nov", "jueves 27 de noviembre",
# "Thursday, Nov 27". At most a few joining characters between the day word and the date, so a
# weekday and a date that merely share a sentence are never paired.
_JOIN = r"[\s,(\-–—]{1,4}(?:dia\s+|el\s+|the\s+)?"
_PAIR = re.compile(
    r"\b" + _DAY_WORD + r"\b" + _JOIN + r"(?:"
    r"(?P<d1>\d{1,2})/(?P<m1>\d{1,2})(?:/(?P<y1>\d{2,4}))?"
    r"|(?P<d2>\d{1,2})(?:\s*/\s*|\s+de\s+|\s+)(?P<m2>" + _MONTH_WORD + r")\b"
    r"|(?P<m3>" + _MONTH_WORD + r")\.?\s+(?P<d3>\d{1,2})\b"
    r")"
)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def _near(day: int, month: int, today: date, year: int | None = None) -> date | None:
    """The date with that day and month: in the year given, else the one nearest today."""
    years = [year] if year else [today.year - 1, today.year, today.year + 1]
    found = []
    for y in years:
        try:
            found.append(date(y, month, day))
        except ValueError:
            continue
    return min(found, key=lambda d: abs((d - today).days)) if found else None


def _day_of(iso) -> date | None:
    try:
        return datetime.fromisoformat(str(iso)).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(iso)[:10])
        except (TypeError, ValueError):
            return None


def _weekday_index(name) -> int | None:
    return _WORDS.get(_fold(str(name or "")).strip())


def mismatches(result: dict, today: date) -> list[dict]:
    """Every weekday in this pass that is not the weekday of the date beside it.

    [{where, said, date}] with `said` the weekday index she wrote and `date` the date it was
    written against. An action without a weekday, or without a start, is not checked."""
    out: list[dict] = []
    for a in result.get("actions") or []:
        if not isinstance(a, dict) or a.get("task") not in ("calendar.create", "calendar.update"):
            continue
        said = _weekday_index(a.get("weekday"))
        d = _day_of(a.get("start")) if a.get("start") else None
        if said is not None and d is not None and d.weekday() != said:
            out.append({"where": "action", "said": said, "date": d})
    for m in _PAIR.finditer(_fold(result.get("message") or "")):
        said = _WORDS[m.group("wd")]
        if m.group("d1"):
            y = m.group("y1")
            y = (int(y) + 2000 if len(y) == 2 else int(y)) if y else None
            a, b = int(m.group("d1")), int(m.group("m1"))
            # Day first is how she writes for him; an English reply may put the month first.
            # Only a pair that neither reading makes right is a mismatch.
            readings = [d for d in (_near(a, b, today, y) if 1 <= b <= 12 else None,
                                    _near(b, a, today, y) if 1 <= a <= 12 else None) if d]
            if any(r.weekday() == said for r in readings):
                continue
            d = readings[0] if readings else None
        elif m.group("d2"):
            d = _near(int(m.group("d2")), _MONTHS[m.group("m2")], today)
        else:
            d = _near(int(m.group("d3")), _MONTHS[m.group("m3")], today)
        if d is not None and d.weekday() != said:
            out.append({"where": "message", "said": said, "date": d})
    return out


def _named(d: date) -> str:
    return f"{d:%Y-%m-%d} is a {WEEKDAYS[d.weekday()]}"


def correction(found: list[dict]) -> str:
    """The note that sends her back: what does not match, and the dates that would."""
    lines = []
    for f in found:
        d, said = f["date"], f["said"]
        # The day with the weekday she wrote, in the same week as the date she wrote.
        alt = d + timedelta(days=(said - d.weekday() + 3) % 7 - 3)
        where = "an action you sent" if f["where"] == "action" else "your message"
        line = (f"- In {where}: {WEEKDAYS[said]} with {d:%Y-%m-%d}. But {_named(d)}; "
                f"the {WEEKDAYS[said]} of that week is {alt:%Y-%m-%d}.")
        if line not in lines:
            lines.append(line)
    return ("[System check, not from anyone in the chat] A weekday in your last answer does not "
            "match its date, so nothing was sent:\n" + "\n".join(lines) + "\n"
            "Find the day the people in the chat meant: when they named a weekday "
            "(\"quinta\", \"Thursday\"), that weekday is what they meant. Take its date from the "
            "table of the days around today, then answer again in full, with the weekday and the "
            "date agreeing everywhere.")


def strip_weekday(result: dict) -> dict:
    """The result with `weekday` taken off every action: it is a check, not an event field."""
    actions = result.get("actions") or []
    if not any(isinstance(a, dict) and "weekday" in a for a in actions):
        return result
    return {**result, "actions": [{k: v for k, v in a.items() if k != "weekday"}
                                  if isinstance(a, dict) else a for a in actions]}
