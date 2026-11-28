"""Time zones by name: finding one, and what to call it on a card.

Shared by the calendar tool (which zone an event is stored in), the zone check
(skills/timezone_check.py) and the cards (skills/calendar_format.py). Nothing here raises: a name
that is not a zone is an answer, and a card with a plainer label beats no card."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

# Names people and models still write that Debian's tzdata no longer ships (they moved to
# tzdata-legacy), each with the zone it has always meant.
_ALIASES = {
    "US/Pacific": "America/Los_Angeles", "US/Mountain": "America/Denver",
    "US/Arizona": "America/Phoenix", "US/Central": "America/Chicago",
    "US/Eastern": "America/New_York", "US/Hawaii": "Pacific/Honolulu",
    "US/Alaska": "America/Anchorage", "Brazil/East": "America/Sao_Paulo",
    "PST8PDT": "America/Los_Angeles", "MST7MDT": "America/Denver",
    "CST6CDT": "America/Chicago", "EST5EDT": "America/New_York",
    "GB": "Europe/London", "Portugal": "Europe/Lisbon", "Japan": "Asia/Tokyo",
    "Singapore": "Asia/Singapore", "Hongkong": "Asia/Hong_Kong",
    "Asia/Calcutta": "Asia/Kolkata", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "Z": "UTC", "Etc/UTC": "UTC", "Etc/GMT": "UTC", "GMT": "UTC", "Etc/Zulu": "UTC",
    "Zulu": "UTC", "UCT": "UTC", "Universal": "UTC", "Etc/Universal": "UTC",
}


def _canonical(name) -> str:
    key = str(name or "").strip()
    return _ALIASES.get(key, key)


def zone_of(name) -> tzinfo | None:
    """The zone with that name, or None."""
    key = _canonical(name)
    if not key:
        return None
    if key == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(key)
    except Exception:
        return None


def zone_key(name) -> str | None:
    """The IANA name we use for a zone as someone wrote it, or None when it is not a zone."""
    key = _canonical(name)
    return key if key and zone_of(key) is not None else None


def is_nobodys_clock(name) -> bool:
    """UTC and its other spellings: what a booking tool stamps on an event, not the clock of
    anyone in the meeting. An event stored that way is shown on the owner's clock alone."""
    return _canonical(name) == "UTC"


# --- what a zone is called on a card --------------------------------------------------------
#
# "10:00 AM PT (Pacífico)": the short name people say, then the place. The zones he actually
# deals with get the names he uses for them; any other gets the abbreviation its own clock
# carries ("CET", "GMT") or its distance from GMT, and its city.

_NAMED = {
    "America/Los_Angeles": ("PT", {"pt": "Pacífico", "en": "Pacific", "es": "Pacífico"}),
    "America/Denver": ("MT", {"pt": "Montanhas dos EUA", "en": "Mountain",
                              "es": "Montaña de EE. UU."}),
    "America/Chicago": ("CT", {"pt": "Central dos EUA", "en": "Central",
                               "es": "Centro de EE. UU."}),
    "America/New_York": ("ET", {"pt": "Leste dos EUA", "en": "Eastern",
                                "es": "Este de EE. UU."}),
    "America/Sao_Paulo": ("BRT", {"pt": "Brasília", "en": "Brasília", "es": "Brasilia"}),
}

# City names as they are written in each language, where that is not the IANA spelling.
_CITIES = {
    "Sao Paulo": {"pt": "São Paulo", "en": "São Paulo", "es": "São Paulo"},
    "London": {"pt": "Londres", "es": "Londres"},
    "Lisbon": {"pt": "Lisboa", "es": "Lisboa"},
    "New York": {"pt": "Nova York", "es": "Nueva York"},
    "Mexico City": {"pt": "Cidade do México", "es": "Ciudad de México"},
    "Berlin": {"pt": "Berlim", "es": "Berlín"},
    "Rome": {"pt": "Roma", "es": "Roma"},
    "Zurich": {"pt": "Zurique", "es": "Zúrich"},
    "Amsterdam": {"pt": "Amsterdã", "es": "Ámsterdam"},
    "Tokyo": {"pt": "Tóquio", "es": "Tokio"},
    "Shanghai": {"pt": "Xangai", "es": "Shanghái"},
    "Singapore": {"pt": "Singapura", "es": "Singapur"},
    "Kolkata": {"pt": "Índia", "en": "India", "es": "India"},
}


def gmt_label(offset: timedelta | None) -> str:
    """'GMT-8', 'GMT+5:30', 'GMT'."""
    minutes = int((offset or timedelta(0)).total_seconds() // 60)
    if not minutes:
        return "GMT"
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours}" + (f":{rest:02d}" if rest else "")


def zone_label(name, when: datetime, lang: str) -> str:
    """What a card calls the zone `name` at the moment `when`: 'PT (Pacífico)', 'CET (Berlim)',
    'GMT-3 (Buenos Aires)'. `when` is an aware datetime; daylight saving picks the abbreviation.
    A name that is not a zone gets the distance from GMT of `when` itself."""
    key = zone_key(name)
    zone = zone_of(key) if key else None
    if zone is None:
        return gmt_label(when.utcoffset())
    if key == "UTC":
        return "UTC"
    if key in _NAMED:
        short, places = _NAMED[key]
        return f"{short} ({places.get(lang) or places['en']})"
    local = when.astimezone(zone)
    abbr = local.tzname() or ""
    short = abbr if abbr.isalpha() else gmt_label(local.utcoffset())
    city = key.rsplit("/", 1)[-1].replace("_", " ")
    city = (_CITIES.get(city) or {}).get(lang, city)
    return f"{short} ({city})" if "/" in key else short
