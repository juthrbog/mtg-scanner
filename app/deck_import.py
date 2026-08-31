"""Parse a pasted deck list back into card lines.

The inverse of `export.py`, and deliberately more forgiving than it: a list
that is *written* has one shape, but a list that arrives has been through
whichever site the user copied it from. Rather than support a fixed set of
formats, this reads the union of what they all emit.

What it copes with, and where each convention comes from:

* ``1 Sol Ring``, ``1x Sol Ring``, or a bare ``Sol Ring`` — the last because
  hand-written lists routinely drop the count on singletons.
* ``1 Sol Ring (ECC) 57`` — Arena, and Moxfield's "with printing" export.
* Section headers (``Commander``, ``Deck``, ``Sideboard``) and type headers
  (``Creatures (30)``) that TappedOut and Archidekt insert.
* ``SB:`` line prefixes, from MTGO's .dec format.
* Trailing ``*F*`` (Moxfield foil) and ``[Category]`` (Archidekt) tags.
* ``//`` and ``#`` comments.
* Our own CSV export, so a round trip through a spreadsheet comes back.

Parsing stays free of the database so it can be exercised on a string. Turning
a name into a card the collection actually holds is the caller's job.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

# Where a line ends up. Commander decks have no sideboard, but lists exported
# from a site often carry one, and silently folding those cards into the deck
# would inflate it — they are parsed, marked, and reported as skipped.
COMMANDER, DECK, SIDEBOARD = "commander", "deck", "sideboard"

_SECTION_WORDS = {
    "commander": COMMANDER,
    "commanders": COMMANDER,
    "deck": DECK,
    "decklist": DECK,
    "main": DECK,
    "maindeck": DECK,
    "mainboard": DECK,
    "sideboard": SIDEBOARD,
    "maybeboard": SIDEBOARD,
    "considering": SIDEBOARD,
    "companion": SIDEBOARD,
    "tokens": SIDEBOARD,
}

_COUNT_RE = re.compile(r"^(\d+)\s*[xX]?\s+(.+)$")

# A header is a line with no count that is either a known section word or ends
# in a parenthesised total — "Creatures (30)". Both tests are needed: without
# the count test, type headers become phantom cards; without the word list,
# a bare "Commander" does.
_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z /'-]*?)\s*(?:\((\d+)\))?\s*:?\s*$")

# A set code and optional collector number at the end of the line. The code is
# bounded to 2-6 alphanumerics with no spaces, which is what keeps it off card
# names that genuinely end in brackets — "Erase (Not the Urza's Legacy One)"
# has spaces inside the parentheses and is left alone.
_PRINTING_RE = re.compile(r"\s*\(([A-Za-z0-9]{2,6})\)\s*([A-Za-z0-9★-]+)?\s*$")

_TRAILING_TAG_RE = re.compile(r"\s*(\[[^\]]*\]|\*[^*]*\*)\s*$")

_CSV_COUNT_HEADERS = {"count", "quantity", "qty", "amount"}


@dataclass
class ParsedLine:
    quantity: int
    name: str
    section: str = DECK
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    raw: str = ""


@dataclass
class ParseResult:
    lines: List[ParsedLine]
    # Lines that looked like content but could not be read at all. Kept with
    # their original text so the UI can show the user what was ignored.
    unreadable: List[str]


def fold(name: str) -> str:
    """A comparison key for a card name.

    Lists come from everywhere, so the same card arrives spelled several ways.
    This folds away the differences that never carry meaning: accents (a list
    typed on a US keyboard says "Lim-Dul's Vault"), curly quotes pasted out of
    a word processor, and runs of whitespace.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip().lower()


def front_face(name: str) -> str:
    """The front face of a double-faced card name.

    Arena exports only the front face, so a list that came from Arena says
    "Obyra's Attendants" where the database says "Obyra's Attendants //
    Desperate Parry". Matching has to reach both.
    """
    return name.split("//")[0].strip()


def _strip_tags(text: str) -> str:
    """Remove trailing ``[Category]`` and ``*F*`` markers.

    Looped because the two co-occur — Moxfield writes the foil marker and
    Archidekt the category, and a list that has been through both carries both.
    """
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_TAG_RE.sub("", text).rstrip()
    return text


def _parse_card_line(text: str, section: str) -> Optional[ParsedLine]:
    raw = text
    quantity = 1
    m = _COUNT_RE.match(text)
    if m:
        quantity = int(m.group(1))
        text = m.group(2)

    text = _strip_tags(text.strip())

    set_code = collector = None
    p = _PRINTING_RE.search(text)
    if p:
        set_code = p.group(1)
        collector = p.group(2)
        text = text[: p.start()].rstrip()

    name = _strip_tags(text).strip()
    if not name:
        return None
    return ParsedLine(quantity=quantity, name=name, section=section,
                      set_code=set_code, collector_number=collector, raw=raw.strip())


def _looks_like_csv(first_line: str) -> bool:
    fields = [f.strip().strip('"').lower() for f in first_line.split(",")]
    return "name" in fields and bool(_CSV_COUNT_HEADERS & set(fields))


def _parse_csv(text: str) -> ParseResult:
    lines: List[ParsedLine] = []
    unreadable: List[str] = []
    reader = csv.DictReader(io.StringIO(text))
    keys = {(k or "").strip().lower(): k for k in (reader.fieldnames or [])}
    count_key = next((keys[k] for k in _CSV_COUNT_HEADERS if k in keys), None)

    for row in reader:
        name = (row.get(keys.get("name", ""), "") or "").strip()
        if not name:
            continue
        try:
            quantity = int((row.get(count_key) or "1").strip()) if count_key else 1
        except ValueError:
            quantity = 1
        section = _SECTION_WORDS.get(
            (row.get(keys.get("section", ""), "") or "").strip().lower(), DECK)
        set_code = (row.get(keys.get("set code", ""), "") or "").strip() or None
        collector = (row.get(keys.get("collector number", ""), "") or "").strip() or None
        lines.append(ParsedLine(quantity=max(quantity, 1), name=name, section=section,
                                set_code=set_code, collector_number=collector,
                                raw=f"{quantity} {name}"))
    return ParseResult(lines=lines, unreadable=unreadable)


def parse(text: str) -> ParseResult:
    """Read a deck list of any of the supported shapes."""
    stripped = [ln for ln in text.splitlines() if ln.strip()]
    if stripped and _looks_like_csv(stripped[0]):
        return _parse_csv(text)

    lines: List[ParsedLine] = []
    unreadable: List[str] = []
    section = DECK

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        # MTGO marks sideboard per line rather than with a header.
        if line.lower().startswith("sb:"):
            parsed = _parse_card_line(line[3:].strip(), SIDEBOARD)
            (lines.append(parsed) if parsed else unreadable.append(line))
            continue

        if not _COUNT_RE.match(line):
            header = _HEADER_RE.match(line)
            if header:
                word = header.group(1).strip().lower()
                if word in _SECTION_WORDS:
                    section = _SECTION_WORDS[word]
                    continue
                # "Creatures (30)" — a type header, which isn't a card but does
                # end whatever section preceded it. TappedOut and Archidekt
                # write "Commander (1)" and then group the 99 under type
                # headings; leaving the section alone here would file the whole
                # deck as commanders.
                if header.group(2) is not None:
                    section = DECK
                    continue

        parsed = _parse_card_line(line, section)
        if parsed:
            lines.append(parsed)
        else:
            unreadable.append(line)

    return ParseResult(lines=lines, unreadable=unreadable)
