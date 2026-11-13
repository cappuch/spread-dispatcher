"""Read dealer notes (Finnish, Swedish, English) into dispatch constraints.

The world acts on a note whether or not a system reads it: a pickup can be later
than the request says, a handover can wait for payment, a delivery deadline can
be tighter, or the driver can need a manual licence or plates.  This module turns
the common phrasings into absolute world minutes.

`parse_note(text, lang, request)` returns a dict with any of:

    pickup_after       earliest pickup (world minute)
    pickup_before      latest pickup
    delivery_before    a tighter delivery deadline
    ready_at           the car is ready later (also raises pickup_after)
    release_at         handover after payment (also raises pickup_after)
    manual             the driver needs a manual licence
    plates             the pickup needs transfer plates (withPlates)
    summer_tyres       the car cannot be driven in snow
    deadline_override  an explicit, tighter order deadline
    private_slot       the note books a private seller's slot

`parse_order_note` is the same reader for an `order_changed` message, with the
event minute as the day on which a time without a day falls.

Only the standard library is used.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

DAY = 1440
HOUR = 60

# --------------------------------------------------------------------------- #
# Normalisation


_DASHES = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPACES = re.compile(r"[\u00a0\u2007\u202f]")


def _normalise(text: str) -> str:
    """Lower case, one dash, ordinary spaces.  Accents stay: they carry words."""
    text = (text or "").lower()
    text = _DASHES.sub("-", text)
    text = _SPACES.sub(" ", text)
    text = text.replace("\u2019", "'")
    return text


# --------------------------------------------------------------------------- #
# Days

_WEEKDAY_NAMES = {
    # Finnish, with the common inflected forms
    "maanantai": 0, "maanantaina": 0,
    "tiistai": 1, "tiistaina": 1,
    "keskiviikko": 2, "keskiviikkona": 2,
    "torstai": 3, "torstaina": 3,
    "perjantai": 4, "perjantaina": 4,
    "lauantai": 5, "lauantaina": 5,
    "sunnuntai": 6, "sunnuntaina": 6,
    # Swedish
    "måndag": 0, "tisdag": 1, "onsdag": 2, "torsdag": 3,
    "fredag": 4, "lördag": 5, "söndag": 6,
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_WEEKDAY_SHORT = {
    # English and Swedish short forms are safe in any text.
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2,
    "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
    "mån": 0, "tis": 1, "ons": 2, "tors": 3, "fre": 4, "lör": 5, "sön": 6,
}

# Two-letter Finnish forms are read only in Finnish text, and only with a dot or
# followed by a clock word: otherwise "to" (English) and "ma" are common words.
_FI_SHORT = {"ma": 0, "ti": 1, "ke": 2, "to": 3, "pe": 4, "la": 5, "su": 6}

_DAY_WORDS = [
    (re.compile(r"\bylihuomenna\b|\bi\s+övermorgon\b|\bövermorgon\b|"
                r"\bday\s+after\s+tomorrow\b"), 2),
    (re.compile(r"\bhuomenna\b|\bhuomena\b|\bi\s+morgon\b|\bimorgon\b|"
                r"\bi\s+morron\b|\bimorron\b|\btomorrow\b|\btomorow\b|"
                r"\btommorow\b|\btmrw\b|\btmr\b"), 1),
    (re.compile(r"\btänään\b|\btanaan\b|\bidag\b|\bi\s+dag\b|\btoday\b"), 0),
]

_SENTENCE = re.compile(r"[.!?](?=\s|$)")


def _weekday_pattern(lang: str) -> re.Pattern:
    names = sorted(_WEEKDAY_NAMES, key=len, reverse=True)
    shorts = sorted(_WEEKDAY_SHORT, key=len, reverse=True)
    alts = [re.escape(w) for w in names] + [re.escape(w) for w in shorts]
    if (lang or "").lower().startswith("fi"):
        alts += [re.escape(w) for w in _FI_SHORT]
    body = "|".join(alts)
    return re.compile(r"(?<![a-zåäö])(" + body + r")(?:\.)?(?![a-zåäö])")


def _weekday_delta(arrival_day: int, weekday: int) -> int:
    """The weekday 2 to 6 days after arrival.  Never arrival day or next day."""
    delta = (weekday - (arrival_day % 7)) % 7
    if delta < 2:
        delta += 7
    return delta


def _day_tokens(text: str, lang: str) -> list[dict]:
    """Day words and weekday names, as spans with their delta in days."""
    out: list[dict] = []
    for rx, delta in _DAY_WORDS:
        for m in rx.finditer(text):
            out.append({"start": m.start(), "end": m.end(), "delta": delta})
    # "ma-pe" / "mån-fre" are opening-hour ranges, not one named day.
    ranges = [(m.start(), m.end()) for m in re.finditer(
        r"\b(?:ma|ti|ke|to|pe|la|su|mån|tis|ons|tors|fre|lör|sön)"
        r"\s*-\s*(?:ma|ti|ke|to|pe|la|su|mån|tis|ons|tors|fre|lör|sön)\b",
        text)]
    for m in _weekday_pattern(lang).finditer(text):
        if any(a <= m.start() < b for a, b in ranges):
            continue
        word = m.group(1)
        if word in _FI_SHORT:
            # A Finnish short form needs a dot or a clock word after it.
            tail = text[m.end():m.end() + 12]
            if not (m.group(0).endswith(".") or re.match(
                    r"\s*(?:klo|kl\.?|kello|klockan|at|by|senast|före|\d)", tail)):
                continue
        weekday = _WEEKDAY_NAMES.get(word, _WEEKDAY_SHORT.get(word, _FI_SHORT.get(word)))
        if weekday is None:
            continue
        out.append({"start": m.start(), "end": m.end(), "weekday": weekday})
    # "the day after tomorrow" contains "tomorrow": keep the longer reading.
    out = [d for d in out
           if not any(o is not d and o["start"] <= d["start"] and d["end"] <= o["end"]
                      and (o["end"] - o["start"]) > (d["end"] - d["start"])
                      for o in out)]
    out.sort(key=lambda d: d["start"])
    return out


def _resolve_day(dt: dict, arrival_day: int) -> int:
    if "weekday" in dt:
        return arrival_day + _weekday_delta(arrival_day, dt["weekday"])
    return arrival_day + dt.get("delta", 0)


def _day_for(token: dict, days: list[dict], arrival_day: int,
             lookback: int = 40) -> tuple[int, bool]:
    """The day of a time, and whether a day word named it explicitly."""
    best = None
    for dt in days:
        if dt["end"] <= token["start"] and token["start"] - dt["end"] <= lookback:
            best = dt
        elif dt["start"] >= token["end"] and dt["start"] - token["end"] <= 14:
            best = dt
    if best is None:
        return arrival_day, False
    return _resolve_day(best, arrival_day), True


# --------------------------------------------------------------------------- #
# Clock times

_NUM_WORDS = {
    # Finnish, base and the forms used before "mennessä" / "asti"
    "yksi": 1, "yhteen": 1, "kaksi": 2, "kahteen": 2, "kolme": 3, "kolmeen": 3,
    "neljä": 4, "neljään": 4, "viisi": 5, "viiteen": 5, "kuusi": 6, "kuuteen": 6,
    "seitsemän": 7, "seitsemään": 7, "kahdeksan": 8, "kahdeksaan": 8,
    "yhdeksän": 9, "yhdeksään": 9, "kymmenen": 10, "kymmeneen": 10,
    "yhdeltä": 1, "kahdelta": 2, "kolmelta": 3, "neljältä": 4,
    "viideltä": 5, "kuudelta": 6, "seitsemältä": 7, "kahdeksalta": 8,
    "yhdeksältä": 9, "kymmeneltä": 10,
    "yksitoista": 11, "kaksitoista": 12,
    # Swedish
    "ett": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7,
    "åtta": 8, "nio": 9, "tio": 10, "elva": 11, "tolv": 12,
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_WORD_ALT = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))

_RANGE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?\s*-\s*"
    r"(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?(?![\d])", re.I)
_KLO4_RE = re.compile(r"(?:\bklo|\bkl\.?|\bkello|\bklockan)\s*(\d{2})(\d{2})(?!\d)", re.I)
_KLO_RE = re.compile(r"(?:\bklo|\bkl\.?|\bkello|\bklockan)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?", re.I)
_HHMM_RE = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?!\d)")
_HHDOT_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2})(?!\d)")
_AMPM_RE = re.compile(r"(?<![\d.])(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b", re.I)
# A bare hour counts when a time word carries it: "20 mennessä", "10 jälkeen".
_BARE_HOUR_RE = re.compile(
    r"(?<![\d.:])(\d{1,2})(?!\d)(?![.:]\d)\s*"
    r"(?=(?:jälkeen|jalkeen|alkaen|mennessä|mennessa|asti|saakka|viim\.?|"
    r"viimeistään|latest|senast|före|before|by)\b)")
# A bare HHMM after a time word: "2000 mennessä".
_BARE_HHMM_RE = re.compile(
    r"(?<![\d.:])(\d{2})(\d{2})(?!\d)\s*(?=(?:mennessä|mennessa|jälkeen|"
    r"jalkeen|asti|saakka|alkaen|latest|senast|at the latest)\b)")

# A bare hour after a time word: "ennen 18", "senast i morgon 20".
_BARE_CONTEXT_RE = re.compile(
    r"(?:ennen|before|före|senast|viim\.?|viimeistään|viimeistaan|mennessä|"
    r"mennessa|asti|saakka|jälkeen|jalkeen|alkaen|aikaisintaan|"
    r"aikaisintaaan|\bfrom\b|\bafter\b|\bby\b|latest|tidigast|"
    r"not before|ei ennen|inte före)"
    r"[^.;\d]{0,25}(\d{1,2})(?!\d)(?![.:]\d)")
_HALF_RE = re.compile(
    r"\b(?:puoli|halv|half)\s+(\d{1,2}|" + _WORD_ALT + r")\b", re.I)
_WORD_CLOCK_RE = re.compile(
    r"(?:\bklo|\bkl\.?|\bkello|\bklockan|\bat\b|\bby\b|\bsenast\b|\bföre\b|"
    r"\bennen\b|\bmennessä\b|\bviimeistään\b|\bviim\.?\b)\s+(" + _WORD_ALT + r")\b",
    re.I)


def _to_minute(hour: int, minute: int = 0) -> Optional[int]:
    if minute < 0 or minute > 59 or hour < 0 or hour > 24:
        return None
    if hour == 24 and minute:
        return None
    return hour * HOUR + minute


def _ampm(hour: int, minute: int, suffix: Optional[str]) -> Optional[int]:
    if suffix:
        suffix = suffix.lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    return _to_minute(hour, minute)


def _working_hour(n: int) -> Optional[int]:
    """An hour in words is a working hour, 9 to 17: "two" is 14:00."""
    if 9 <= n <= 12:
        return n
    if 1 <= n <= 5:
        return n + 12
    return None


def _add(tokens: list[dict], start: int, end: int, first: int, last: int,
         is_range: bool, raw: str) -> None:
    tokens.append({"start": start, "end": end, "first": first,
                   "last": last, "range": is_range, "raw": raw})


def _overlaps(spans: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    return any(a < end and start < b for a, b in spans)


def _time_tokens(text: str) -> list[dict]:
    """Clock times and hour ranges, with their minute of the day."""
    tokens: list[dict] = []
    used: list[tuple[int, int]] = []

    for m in _RANGE_RE.finditer(text):
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        first = _ampm(int(h1), int(m1 or 0), ap1)
        last = _ampm(int(h2), int(m2 or 0), ap2)
        if first is None or last is None:
            continue
        # "10-5 pm" is 10:00 to 17:00, not 22:00 to 17:00.
        if ap2 and not ap1:
            alternative = _ampm(int(h1), int(m1 or 0), ap2)
            if alternative is not None and alternative < last:
                first = alternative
        if last <= first:
            continue
        _add(tokens, m.start(), m.end(), first, last, True, m.group(0))
        used.append((m.start(), m.end()))

    def free(start: int, end: int) -> bool:
        return not _overlaps(used, start, end)

    for m in _KLO4_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        value = _to_minute(int(m.group(1)), int(m.group(2)))
        if value is None:
            continue
        _add(tokens, m.start(), m.end(), value, value, False, m.group(0))
        used.append((m.start(), m.end()))

    for rx in (_KLO_RE, _HHMM_RE, _HHDOT_RE, _AMPM_RE):
        for m in rx.finditer(text):
            if not free(m.start(), m.end()):
                continue
            if rx is _AMPM_RE:
                value = _ampm(int(m.group(1)), int(m.group(2) or 0), m.group(3))
            elif rx is _KLO_RE:
                value = _ampm(int(m.group(1)), int(m.group(2) or 0), m.group(3))
            else:
                value = _to_minute(int(m.group(1)), int(m.group(2) or 0))
            if value is None:
                continue
            _add(tokens, m.start(), m.end(), value, value, False, m.group(0))
            used.append((m.start(), m.end()))

    for m in _BARE_HHMM_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        value = _to_minute(int(m.group(1)), int(m.group(2)))
        if value is None:
            continue
        _add(tokens, m.start(), m.end(), value, value, False, m.group(0))
        used.append((m.start(), m.end()))

    for rx in (_BARE_HOUR_RE, _BARE_CONTEXT_RE):
        for m in rx.finditer(text):
            if not free(m.start(1) if rx is _BARE_CONTEXT_RE else m.start(),
                        m.end()):
                continue
            value = _to_minute(int(m.group(1)), 0)
            if value is None:
                continue
            start = m.start(1) if rx is _BARE_CONTEXT_RE else m.start()
            _add(tokens, start, m.end(), value, value, False, m.group(0))
            used.append((start, m.end()))

    for m in _HALF_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
        if n is None:
            continue
        hour = _working_hour(n)
        if hour is None and n == 6:
            hour = 18
        if hour is None:
            continue
        value = _to_minute(hour, 0) - 30
        if value is None:
            continue
        _add(tokens, m.start(), m.end(), value, value, False, m.group(0))
        used.append((m.start(), m.end()))

    for m in _WORD_CLOCK_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        n = _NUM_WORDS.get(m.group(1).lower())
        hour = _working_hour(n) if n is not None else None
        if hour is None:
            continue
        value = _to_minute(hour, 0)
        if value is None:
            continue
        _add(tokens, m.start(), m.end(), value, value, False, m.group(0))
        used.append((m.start(), m.end()))

    tokens.sort(key=lambda t: t["start"])
    return tokens


# --------------------------------------------------------------------------- #
# What a time is for

_ROLE_PATTERNS = [
    ("release", re.compile(
        r"\bmaksu\w*|\bmaksaa\b|\bmaksanut\b|\bpayment\b|\bpaid\b|\bpay\b|"
        r"\bbetalning\w*|\bbetala\w*")),
    ("delivery", re.compile(
        r"\btoimitus\w*|\btoimitetaan\b|\btoimita\b|\bperille\b|\bperillä\b|"
        r"\bperilla\b|\bsaapuu\b|\bsaapumis\w*|\bvastaanot\w*|\bvastaaont\w*|"
        r"\b(?:vast\w*|vs\w*)\s+liike\b|\bmot\w*tag\w*|\bkohde\b|\bkohtee\w*|"
        r"\bdestination\b|\bdeliver\w*|\barriv\w*|\breceiv\w*|\bframme\b|"
        r"\bleverans\w*|\bleverera\w*|\bmottag\w*|\bdeadline\b|"
        r"\bmääräaika\b|\bmääraika\b|\baikaraja\b|\bfrist\b|\bpå plats\b|"
        r"\bmust be there\b")),
    ("pickup", re.compile(
        r"\bnouto\b|\bnoutaa\b|\bnoutam\w*|\bnoudettavissa\b|\bnoudettava\b|"
        r"\bhaettav\w*|\bhaetaan\b|\bhakea\b|\bhaku\b|\bhämtning\w*|"
        r"\bhämtas\b|\bpickup\b|\bpick up\b|\bcollect\w*|\bcollection\b|"
        r"\bluovut\w*|\bvalmis\b|\bvalmiina\b|\bvalm\w*|\bready\b|\bklar\b|"
        r"\bsaa noutaa\b|\bsaa\b|\brelease\b|"
        r"\bvi stänger\b|\bwe close\b|\btäällä\b")),
    ("hours", re.compile(
        r"\bauki\b|\bavoinna\b|\baukiolo\w*|\bopen\w*|\böppet\b|\bstänger\b|"
        r"\bstängd\b|\bsulkee\b|\bsulkeutuu\b|\bkiinni\b|\bclosed\b|"
        r"\bcloses\b|\bclose\b|\basti\b|\bsaakka\b|\bvardagar\b|\barkisin\b|"
        r"\barkipäivisin\b|\bweekdays\b|\bonly\b|\bhours\b|\btäällä\b|"
        r"\bshop\b|\bstore\b|\bliike\b")),
]

_PRIORITY = {"release": 0, "delivery": 1, "pickup": 2, "hours": 3}

_BEFORE_RE = re.compile(
    r"\bennen\b|\bmennessä\b|\bmennessa\b|\bviim\.?\b|\bviimeistään\b|"
    r"\bviimeistaan\b|\bbefore\b|\bby\b|\blatest\b|\bsenast\b|\bföre\b|"
    r"\basti\b|\bsaakka\b|\buntil\b|\btill\b|\bdeadline\b|\bsulkee\b|"
    r"\bmoved to\b|\buusi määräaika\b|\bsiirretty\b|\bflyttad\b|"
    r"\bsulkeutuu\b|\bkiinni\b|\bcloses\b|\bclose\b|\bstänger\b|\bstängd\b")
_AFTER_RE = re.compile(
    r"\bjälkeen\b|\bjalkeen\b|\balkaen\b|\blähtien\b|\baikaisintaan\b|"
    r"\baikaisintaaan\b|\bafter\b|\bfrom\b|\bearliest\b|\bnot before\b|"
    r"\bei ennen\b|\befter\b|\btidigast\b|\binte före\b|\bsen jälkeen\b|"
    r"\bfrån\b")
_CLOSING_RE = re.compile(
    r"\bsulkee\b|\bsulkeutuu\b|\bkiinni\b|\bcloses\b|\bclose\b|\bstänger\b|"
    r"\bstängd\b")
_READY_RE = re.compile(
    r"\bvalm\w*|\bready\b|\bklar\b|\bnot ready\b|\bej klar\b|\binte klar\b")


_ABBREVIATIONS = {"viim", "kl", "klo", "kello", "n", "t", "esim", "ref",
                  "no", "nr", "puh", "jne", "mm",
                  "ma", "ti", "ke", "to", "pe", "la", "su",
                  "mon", "tue", "wed", "thu", "fri", "sat", "sun",
                  "mån", "tis", "ons", "tors", "fre", "lör", "sön"}


def _clauses(text: str) -> list[tuple[int, int]]:
    """Sentence spans, keeping "viim." and "kl." inside their sentence."""
    bounds = [0]
    for m in re.finditer(r"[.!?]\s+", text):
        if m.group(0)[0] == ".":
            word = re.search(r"([a-zåäö]+)\.$", text[max(0, m.start() - 12):m.start() + 1])
            if word and word.group(1) in _ABBREVIATIONS:
                continue
        bounds.append(m.start() + 1)
    if bounds[-1] != len(text):
        bounds.append(len(text))
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _clause_at(text: str, pos: int) -> tuple[int, int]:
    for lo, hi in _clauses(text):
        if lo <= pos <= hi:
            return lo, hi
    return 0, len(text)


def _nearest_role(text: str, start: int, end: int,
                  before: int = 60, after: int = 40) -> tuple[Optional[str], int]:
    """The role keyword nearest a time: release, delivery, pickup or hours."""
    lo, hi = _clause_at(text, start)
    before, after = max(0, min(before, start - lo)), max(0, min(after, hi - end))
    best: Optional[tuple] = None
    for role, rx in _ROLE_PATTERNS:
        for m in rx.finditer(text):
            if m.end() < start - before or m.start() > end + after:
                continue
            if m.start() >= start and m.end() <= end:
                distance = 0
            else:
                distance = min(abs(start - m.end()), abs(m.start() - end))
            key = (distance, _PRIORITY[role], m.start())
            if best is None or key < best[0]:
                best = (key, role, m.start())
    if best is None:
        return None, -1
    return best[1], best[2]


_DELIVERY_SUBJECT = re.compile(
    r"\bkohde\b|\bkohtee\w*|\bperill\w*\b|\bvastaanot\w*|\bvastaaont\w*|"
    r"\b(?:vast\w*|vs\w*)\s+liike\b|\bmot\w*tag\w*|\breceiv\w*|\bdestination\b")
_PICKUP_SUBJECT = re.compile(
    r"\bliike\b|\bshop\b|\bstore\b|\btäällä\b|\bwe close\b|"
    r"\bvi stänger\b|\bnouto\b|\bhämtning\b|\bpickup\b|"
    r"\bpick up\b|\bcollect\b")


def _nearest_subject(text: str, start: int, end: int) -> str:
    lo, hi = _clause_at(text, start)
    # "perillä" and "kohde" are the destination wherever they stand.
    if re.search(r"\bperill\w*|\bkohde\b|\bkohtee\w*|\bvastaanot\w*|\bvastaaont\w*|"
                 r"\bmot\w*tag\w*", text[lo:hi]):
        return "delivery"
    best: Optional[tuple[tuple[int, int], str]] = None
    for role, rx in (("delivery", _DELIVERY_SUBJECT), ("pickup", _PICKUP_SUBJECT)):
        for m in rx.finditer(text, lo, hi):
            distance = 0 if m.start() >= start and m.end() <= end else min(
                abs(start - m.end()), abs(m.start() - end))
            key = (distance, 0 if role == "delivery" else 1)
            if best is None or key < best[0]:
                best = (key, role)
    return best[1] if best else "pickup"


def _has(rx: re.Pattern, text: str, start: int, end: int,
         before: int = 35, after: int = 30) -> bool:
    lo, hi = _clause_at(text, start)
    window = text[max(lo, start - before): min(hi, end + after)]
    return bool(rx.search(window))


def _clause_before(text: str, pos: int, maxlen: int = 45) -> str:
    chunk = text[max(0, pos - maxlen):pos]
    cut = 0
    for m in _SENTENCE.finditer(chunk):
        cut = m.end()
    return chunk[cut:]


def _denied(text: str, start: int) -> bool:
    """A denied time does not count: "not 3pm", "ei klo 21", "(not 13.00)"."""
    before = text[max(0, start - 18):start]
    tail = re.search(r"(?:^|[\s(])(?:ei|inte|not)\s+(?:klo\s*|kl\.?\s*|at\s*)?$",
                     before)
    if not tail:
        return False
    if re.search(r"(?:ei|inte|not)\s+(?:ennen|aikaisemmin|före|förrän|before)\b",
                 before):
        return False
    return True


def _correction_positions(text: str) -> list[int]:
    return [m.start() for m in re.finditer(
        r"\beikun\b|\bei\s+kun\b|\beiku\b|\bcorrection\b|\bcorrig\w*|"
        r"\bi\s+mean\b|\bsorry[^.;]{0,15}i\s+mean\b|\brather\b|\binstead\b|"
        r"\btarkennus\b|\bkorjaus\b|\brättelse\b|\bsnarare\b|\boikeastaan\b",
        text)]


def _classify(text: str, tok: dict) -> tuple[Optional[str], bool]:
    """The key a time belongs to, and whether it names an explicit deadline."""
    s, e = tok["start"], tok["end"]
    role, _ = _nearest_role(text, s, e)
    explicit_deadline = False

    # A payment with a handover ("auton saa", "luovutus") releases the car.
    payment_context = text[max(0, s - 75):min(len(text), e + 25)]
    if re.search(r"\bmaksu\w*|\bmaksaa\b|\bpayment\b|\bpaid\b|"
                 r"\bbetalning\w*", payment_context) and re.search(
            r"\bsaa\b|\bluovut\w*|\bhandover\b|\breleased?\b|\bhanded\b",
            payment_context):
        role = "release"

    # "must be there by ..." outranks a nearby "picks it up".
    if re.search(r"must\s+be\s+(?:there|at)\b[^.;]{0,25}\bby\b|"
                 r"pitää olla perillä|oltava perillä",
                 text[max(0, s - 50):min(len(text), e + 40)]):
        role = "delivery"

    after = _has(_AFTER_RE, text, s, e)
    before = _has(_BEFORE_RE, text, s, e)

    # "nobody after 18" and "closed after 17" mean by then, not from then.
    if after and _has(_CLOSING_RE, text, s, e, before=30, after=30) and \
            _has(re.compile(r"\bei ketään\b|\bei mitään\b|\bno one\b|\bnobody\b"),
                 text, s, e, before=25, after=25):
        after, before = False, True
    if after and re.search(r"\b(?:kiinni|closed|sulkee|stänger)\b[^.;]{0,12}$",
                           text[max(0, s - 28):s]):
        after, before = False, True

    if role == "release":
        return "release", False

    if tok["range"]:
        if role == "delivery":
            return "delivery_before", False
        if role == "pickup":
            return "pickup_before", False

    if role == "delivery":
        if before:
            explicit_deadline = bool(re.search(
                r"\bennen\b|\bmennessä\b|\bmennessa\b|\bviim\.?\b|\bviimeistään\b|\bbefore\b|"
                r"\bby\b|\blatest\b|\bsenast\b|\bföre\b|\bdeadline\b|"
                r"\bviimeistaan\b|\bmoved to\b|\buusi määräaika\b|\bsiirretty\b|\bflyttad\b|"
                r"\bmääräaika\b|\baikaraja\b",
                text[max(0, s - 40):min(len(text), e + 20)]))
            return "delivery_before", explicit_deadline
        if after:
            return None, False
        return "delivery_before", False

    if role == "pickup":
        ready_clause = _clause_before(text, s)
        if _READY_RE.search(ready_clause):
            return "ready", False
        if after:
            return "pickup_after", False
        if before:
            return "pickup_before", False
        return None, False

    # Only opening-hour words, or no keyword at all.
    subject = _nearest_subject(text, s, e)

    if subject == "delivery":
        # Delivery hours and a bare closing time cap the delivery.
        return "delivery_before", False
    if after:
        return "pickup_after", False
    return "pickup_before", False


# --------------------------------------------------------------------------- #
# Feature words

_MANUAL_RE = re.compile(
    r"\bmanuaali\w*|\bmanuaalivaihte\w*|\bmanuaalikort\w*|\bmanuell\w*|"
    r"\bmanual\b|\bkäsivaihte\w*|\bei automaatti\b")
_MANUAL_NO_RE = re.compile(r"\bei manuaali\w*|\binte manuell\w*|\bnot manual\b")

_PLATES_RE = re.compile(
    r"\bkilv\w*|\bkilpi\w*|\bilman kilpi\w*|\bkilvet\s+(?:poistettu|puuttuu)\b|"
    r"\bei\s+rekkareita\b|\brekkareita\b|\brekisterikilvet\s+irrotettu\b|"
    r"\bsiirtokilvet\b|\bkoekilvet\b|\bprovskyltar\b|\btransfer plates\b|"
    r"\bno plates\b|\bwithout plates\b|\bplates?\s+(?:removed|missing)\b|"
    r"\bmissing plates\b|\bsaknar skylt\w*|\binga skylt\w*|\bskyltar saknas\b|"
    r"\brekisteristä\b[^.;]{0,20}\bsiirtokilvet\b")

_SUMMER_RE = re.compile(r"\bkesärenka\w*|\bkesärengas\w*|\bsommardäck\w*|"
                        r"\bsummer\s+tyre\w*|\bsummer\s+tire\w*")
_WINTER_NO_RE = re.compile(r"\bei talvirenka\w*|\bno winter\s+tyre\w*|"
                           r"\butan vinterdäck\w*|\binte vinterdäck\w*")
_CARGO_RE = re.compile(
    r"takakont\w*|tavaratila\w*|takapenk\w*|kyydiss\w*|kyydis\w*|mukaan\b|"
    r"kontissa\b|bagage\w*|bakluck\w*|\bboot\b|\btrunk\b|\bback\b|\bspare\b|"
    r"\bextra\b|följer med|kulkee mukana|vanteineen|vanteilla|\bset of\b|"
    r"\bsarja\b|go with")
_ON_CAR_RE = re.compile(
    r"\balla\b|\bajossa\b|\bfitted\b|\bon the car\b|\bstill\b|\bvielä\b|"
    r"\bfortfarande\b|\bpå\b|\bajetaan\b|\bdriven\b")

_PRIVATE_RE = re.compile(
    r"myyjä\s+(?:kotona|paikalla)|myyja\s+(?:kotona|paikalla)|"
    r"seller\s+(?:is\s+)?(?:home|available|there)|"
    r"säljaren\s+(?:hemma|på plats)|hos säljaren|"
    r"private\s+(?:address|slot)|yksityis(?:koti|osoite)")


def _yes_manual(text: str) -> bool:
    if _MANUAL_NO_RE.search(text):
        return False
    return bool(_MANUAL_RE.search(text))


def _yes_plates(text: str) -> bool:
    # Every plate word in these notes is about a car whose plates are gone or
    # about fetching transfer plates; a car with its own plates is never noted.
    return bool(_PLATES_RE.search(text))


def _yes_summer(text: str) -> bool:
    if _WINTER_NO_RE.search(text):
        return True
    for m in _SUMMER_RE.finditer(text):
        around_after = text[m.end():m.end() + 32]
        around_before = _clause_before(text, m.start(), 25)
        if _CARGO_RE.search(around_after) or _CARGO_RE.search(around_before):
            continue
        if _ON_CAR_RE.search(around_after) or _ON_CAR_RE.search(around_before):
            return True
        return True
    return False


# --------------------------------------------------------------------------- #
# The reader


def _arrival_day(request: Optional[dict]) -> int:
    if isinstance(request, dict):
        for key in ("arrivedAt", "at"):
            value = request.get(key)
            if isinstance(value, int):
                return value // DAY
    return 0


def _legs(request: Optional[dict]) -> list:
    if not isinstance(request, dict):
        return []
    if isinstance(request.get("legs"), list):
        return request["legs"]
    order = request.get("order")
    if isinstance(order, dict) and isinstance(order.get("legs"), list):
        return order["legs"]
    return []


def _pickup_day(request: Optional[dict], arrival_day: int) -> int:
    try:
        leg = _legs(request)[0]
        return int(leg["pickup"]["window"]["from"]) // DAY
    except (KeyError, IndexError, TypeError, ValueError):
        return arrival_day


def _delivery_day(request: Optional[dict], arrival_day: int) -> int:
    options = (request or {}).get("options") or []
    deadlines = [o["deadline"] for o in options
                 if isinstance(o, dict) and isinstance(o.get("deadline"), int)]
    if deadlines:
        return min(deadlines) // DAY
    try:
        leg = _legs(request)[0]
        return int(leg["delivery"]["window"]["to"]) // DAY
    except (KeyError, IndexError, TypeError, ValueError):
        return arrival_day


def _weekday_range(text: str) -> bool:
    return bool(re.search(r"\barkisin\b|\barkipäivisin\b|\bweekdays\b|"
                          r"\bvardagar\b|\bma\s*-\s*pe\b|\bmån\s*-\s*fre\b|"
                          r"\bmon\s*-\s*fri\b", text))


def _shift_to_business(day: int, text: str) -> int:
    if not _weekday_range(text):
        return day
    while day % 7 >= 5:
        day += 1
    return day


def _parse(text: str, lang: str, request: Optional[dict],
           arrival_day: int, now: Optional[int] = None) -> Dict[str, Any]:
    text = _normalise(text)
    if not text.strip():
        return {}
    tokens = _time_tokens(text)
    days = _day_tokens(text, lang)

    pickup_day = _pickup_day(request, arrival_day)
    delivery_day = _delivery_day(request, arrival_day)

    for tok in tokens:
        explicit_day, named = _day_for(
            tok, days, arrival_day, lookback=25 if tok["range"] else 38)
        tok["named_day"] = named
        tok["day"] = explicit_day
        tok["denied"] = _denied(text, tok["start"])
        key, explicit_deadline = _classify(text, tok)
        tok["key"] = key
        tok["deadline"] = explicit_deadline

    # A correction inherits the fact it corrects: "aikaisintaan klo 8, ei kun 7".
    corrections = _correction_positions(text)
    for i, tok in enumerate(tokens):
        if tok["key"] is not None or tok["denied"]:
            continue
        earlier = [c for c in corrections if c < tok["start"]]
        if earlier and tok["start"] - earlier[-1] <= 12:
            previous = [t for t in tokens[:i] if t["key"] and not t["denied"]]
            if previous:
                tok["key"] = previous[-1]["key"]

    # A correction that names only a weekday moves the time it corrects.
    _shift_corrected_weekday(text, tokens, days, arrival_day)

    explicit = [tok["day"] * DAY + tok["first"] for tok in tokens
                if tok["key"] == "delivery_before" and tok["deadline"]
                and not tok["denied"]]
    explicit_deadline = min(explicit) if explicit else None
    option_deadlines = [o["deadline"] for o in (request or {}).get("options") or []
                        if isinstance(o, dict) and isinstance(o.get("deadline"), int)]
    earliest_option = min(option_deadlines) if option_deadlines else None

    found: Dict[str, Any] = {}
    candidates: Dict[str, list[tuple[int, int, bool]]] = {}

    def add(key: str, minute: int, pos: int = 0, daily: bool = False) -> None:
        candidates.setdefault(key, []).append((pos, minute, daily))

    for tok in tokens:
        if tok["denied"] or not tok["key"]:
            continue
        key = tok["key"]
        pos = tok["start"]
        if tok["range"]:
            first, last, named = tok["first"], tok["last"], tok["named_day"]
            if key in ("pickup_before", "pickup_after") or key == "ready":
                base = tok["day"] if named else _shift_to_business(pickup_day, text)
                start_abs = base * DAY + first
                end_abs = base * DAY + last
                add("pickup_after", start_abs, pos)
                add("pickup_before", end_abs, pos, daily=not named)
                if key == "ready":
                    add("ready", start_abs, pos)
            else:
                if named:
                    base = tok["day"]
                    cap = None
                elif explicit_deadline is not None:
                    base = explicit_deadline // DAY
                    cap = explicit_deadline
                else:
                    base = _shift_to_business(delivery_day, text)
                    cap = earliest_option
                bound = base * DAY + last
                if cap is not None and bound >= cap:
                    continue
                add("delivery_before", bound, pos, daily=not named)
            continue
        day = tok["day"]
        if not tok["named_day"] and _weekday_range(text) and key in (
                "pickup_before", "pickup_after", "delivery_before"):
            day = _shift_to_business(day, text)
        value = day * DAY + tok["first"]
        if key == "ready":
            add("ready", value, pos)
            add("pickup_after", value, pos)
        elif key == "release":
            add("release", value, pos)
            add("pickup_after", value, pos)
        elif key == "pickup_after":
            add("pickup_after", value, pos)
        elif key == "pickup_before":
            add("pickup_before", value, pos, daily=not tok["named_day"])
        elif key == "delivery_before":
            add("delivery_before", value, pos,
                daily=not tok["named_day"] and not tok["deadline"])
            if tok["deadline"]:
                add("deadline_override", value, pos)

    # A closing hour is a daily hour: if the car is ready later, the closing
    # hour is on that later day, not on the day the note was written.
    after_values = [m for _, m, _ in candidates.get("pickup_after", [])]
    target = max(after_values) if after_values else now
    if target is not None and "pickup_before" in candidates:
        shifted = []
        for pos, minute, daily in candidates["pickup_before"]:
            if daily and minute < target:
                day = target // DAY
                minute = day * DAY + minute % DAY
                while minute < target:
                    minute += DAY
            shifted.append((pos, minute, daily))
        candidates["pickup_before"] = shifted

    if now is not None and "delivery_before" in candidates:
        shifted = []
        for pos, minute, daily in candidates["delivery_before"]:
            while daily and minute <= now:
                minute += DAY
            shifted.append((pos, minute, daily))
        candidates["delivery_before"] = shifted

    corrections = _correction_positions(text)
    last_correction = corrections[-1] if corrections else -1
    for key, values in candidates.items():
        values.sort(key=lambda v: v[0])
        late = [minute for pos, minute, daily in values if pos > last_correction]
        if late:
            # A correction wins, even when it loosens an earlier time.
            found[key] = late[-1]
        elif key in ("pickup_before", "delivery_before", "deadline_override"):
            found[key] = min(minute for _, minute, daily in values)
        else:
            found[key] = max(minute for _, minute, daily in values)

    # Feature words.
    if _yes_manual(text):
        found["manual"] = True
    if _yes_plates(text):
        found["plates"] = True
    if _yes_summer(text):
        found["summer_tyres"] = True
    if _PRIVATE_RE.search(text):
        found["private_slot"] = True
        # A seller-at-home slot is a pickup slot even if no pickup word is near.
        slot = _slot_from_text(text, tokens, arrival_day)
        if slot:
            found["pickup_after"] = max(found.get("pickup_after", slot[0]), slot[0])
            found["pickup_before"] = min(found.get("pickup_before", slot[1]), slot[1])

    # A ready time or a release time is also the earliest pickup.
    try:
        window_from = int(_legs(request)[0]["pickup"]["window"]["from"])
        if "pickup_after" in found:
            found["pickup_after"] = max(found["pickup_after"], window_from)
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    if "ready" in found:
        found["ready_at"] = found.pop("ready")
    if "release" in found:
        found["release_at"] = found.pop("release")

    # A ready time or a release time is also the earliest pickup.
    for key in ("ready_at", "release_at"):
        if key in found:
            found["pickup_after"] = max(found.get("pickup_after", found[key]),
                                        found[key])

    # Keep only the documented keys, and only integers or True.
    return {k: v for k, v in found.items()
            if k in _DOCUMENTED and isinstance(v, (int, bool))}


_DOCUMENTED = {
    "pickup_after", "pickup_before", "delivery_before", "ready_at",
    "release_at", "manual", "plates", "summer_tyres", "deadline_override",
    "private_slot",
}


def _shift_corrected_weekday(text: str, tokens: list[dict], days: list[dict],
                             arrival_day: int) -> None:
    """Handle "ready thu 9:00. correction: fri.": the weekday alone moves 9:00."""
    if not tokens:
        return
    names = "|".join(sorted(_WEEKDAY_NAMES, key=len, reverse=True))
    rx = re.compile(
        r"(?:\beikun\b|\bei\s+kun\b|\bcorrection\b|\bcorrig\w*|"
        r"\bi\s+mean\b|\bsorry[^.;]{0,15}i\s+mean\b|\btarkennus\b|\bkorjaus\b|"
        r"\brättelse\b)\s*[:.,]?\s*(" + names + r")\b")
    for m in rx.finditer(text):
        name_at = m.start(1)
        if any(tok["start"] >= m.start() and tok["start"] < name_at for tok in tokens):
            continue
        before = [t for t in tokens if t["end"] <= m.start()]
        if not before:
            continue
        tok = max(before, key=lambda t: t["end"])
        weekday = _WEEKDAY_NAMES[m.group(1)]
        day = arrival_day + _weekday_delta(arrival_day, weekday)
        tok["day"] = day
        tok["named_day"] = True


def _slot_from_text(text: str, tokens: list[dict],
                    arrival_day: int) -> Optional[tuple[int, int]]:
    """A private seller's "home 7-10" slot, even without a pickup keyword."""
    for tok in tokens:
        if not tok["range"]:
            continue
        lo, hi = _clause_at(text, tok["start"])
        if _PRIVATE_RE.search(text[lo:hi]):
            base = tok["day"] if tok["named_day"] else arrival_day
            return base * DAY + tok["first"], base * DAY + tok["last"]
    return None


# --------------------------------------------------------------------------- #
# Public API


def _text_and_lang(text: Any, lang: Optional[str]) -> tuple[str, str]:
    if isinstance(text, dict):
        note = text
        text = note.get("text") or note.get("message") or ""
        lang = note.get("language") or lang
    if text is None:
        text = ""
    return str(text), (lang or "fi")


def parse_note(text: str, lang: str = "fi",
               request: Optional[dict] = None) -> Dict[str, Any]:
    """Read one dealer note.  `request` supplies the arrival day and windows."""
    if isinstance(lang, dict) and request is None:
        request, lang = lang, "fi"
    if text is None and isinstance(request, dict):
        note = request.get("note")
        if isinstance(note, dict):
            text, lang = note.get("text") or "", note.get("language") or lang
    text, lang = _text_and_lang(text, lang)
    arrival = _arrival_day(request)
    now = request.get("arrivedAt") if isinstance(request, dict) else None
    return _parse(text, lang or "fi", request, arrival,
                  now if isinstance(now, int) else None)


def parse_order_note(text: str, lang: str = "fi",
                     request: Optional[dict] = None,
                     at: Optional[int] = None) -> Dict[str, Any]:
    """Read an `order_changed` message.  `at` is the event minute.

    Days in a change of plan count from the minute the message arrives; the
    order's own windows are used only when the message names no day.
    """
    if isinstance(lang, dict) and request is None:
        request, lang = lang, "fi"
    if text is None and isinstance(request, dict):
        order = request.get("order") if isinstance(request.get("order"), dict) else {}
        text = request.get("message") or order.get("message") or ""
    text, lang = _text_and_lang(text, lang)
    if at is None and isinstance(request, dict):
        for key in ("now", "at", "arrivedAt"):
            value = request.get(key)
            if isinstance(value, int):
                at = value
                break
    base = (at // DAY) if isinstance(at, int) else _arrival_day(request)
    return _parse(text, lang or "fi", request, base,
                  at if isinstance(at, int) else None)


def _main() -> None:  # pragma: no cover - a demonstration, not a test
    import json
    import pathlib
    here = pathlib.Path(__file__).with_name("study1.json")
    data = json.loads(here.read_text())
    rows = [r for r in data["requests"] if r.get("note")]
    parsed = []
    for r in rows:
        note = r["note"]
        parsed.append((r, parse_note(note["text"], note.get("language", "fi"), r)))
    print(f"{len(parsed)} notes parsed, {sum(1 for _, o in parsed if o)} with constraints")
    sample = [pair for pair in parsed if pair[1]][:15]
    for r, out in sample:
        print(f"{r['id']}  {r['note']['text']}")
        print(f"   -> {out}")


if __name__ == "__main__":  # pragma: no cover
    _main()
