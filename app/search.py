"""Parse a collection search query into a SQL fragment.

The syntax deliberately mirrors Scryfall's, because anyone with a Magic
collection already types that: `t:creature`, `s:mbs`, `kw:flying`, quoted
phrases, and `-` to exclude. A bare word searches the things you would expect
to search — name, type line, set, and keywords — so the syntax is available
without being required.

Oracle text is *not* in the bare-word search. Including it makes half the
collection match common words like "creature" or "target"; it stays available
behind `o:` for when that is what you actually want.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Tuple

# field prefix -> the SQL expression a term is matched against
FIELDS = {
    "name": "sc.name",
    "n": "sc.name",
    "type": "sc.type_line",
    "t": "sc.type_line",
    "set": "(sc.set_name || ' ' || sc.set_code)",
    "s": "(sc.set_name || ' ' || sc.set_code)",
    "e": "(sc.set_name || ' ' || sc.set_code)",   # Scryfall also accepts e:
    "keyword": "sc.keywords",
    "kw": "sc.keywords",
    "oracle": "sc.oracle_text",
    "o": "sc.oracle_text",
    "rarity": "sc.rarity",
    "r": "sc.rarity",
    "artist": "sc.name",   # not stored; degrade to name rather than error
}

# What a bare word searches. The four things the box promises, and no more.
BARE_FIELDS = [
    "sc.name",
    "sc.type_line",
    "(sc.set_name || ' ' || sc.set_code)",
    "sc.keywords",
]

TERM_RE = re.compile(r"^(?P<neg>-)?(?:(?P<field>[a-zA-Z]+):)?(?P<value>.*)$", re.S)


@dataclass
class ParsedQuery:
    sql: str = ""
    params: List[str] = field(default_factory=list)
    terms: List[dict] = field(default_factory=list)   # for explaining the search back
    unknown_fields: List[str] = field(default_factory=list)


def _tokenize(raw: str) -> List[str]:
    """Split on whitespace but keep quoted phrases together.

    shlex handles the quoting rules; it raises on an unbalanced quote, which
    happens constantly while someone is still typing, so fall back to a plain
    split rather than showing an error mid-keystroke.
    """
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def parse(raw: str) -> ParsedQuery:
    parsed = ParsedQuery()
    if not raw or not raw.strip():
        return parsed

    clauses: List[str] = []
    for token in _tokenize(raw):
        if not token:
            continue
        m = TERM_RE.match(token)
        if not m:
            continue
        negate = bool(m.group("neg"))
        prefix = (m.group("field") or "").lower()
        value = m.group("value").strip()
        if not value:
            continue

        if prefix and prefix in FIELDS:
            columns: Tuple[str, ...] = (FIELDS[prefix],)
            label = prefix
        else:
            # An unrecognised prefix is far more likely to be part of what the
            # user meant (a name containing a colon) than a typo'd field, so
            # search the whole token instead of silently dropping it.
            if prefix:
                parsed.unknown_fields.append(prefix)
                value = f"{prefix}:{value}"
            columns = tuple(BARE_FIELDS)
            label = "any"

        ors = " OR ".join(f"COALESCE({c}, '') LIKE ?" for c in columns)
        clauses.append(f"{'NOT ' if negate else ''}({ors})")
        parsed.params.extend([f"%{value}%"] * len(columns))
        parsed.terms.append({"field": label, "value": value, "negate": negate})

    # Terms are ANDed: each one narrows the result, which is how search boxes
    # are expected to behave and how Scryfall behaves.
    parsed.sql = " AND ".join(clauses)
    return parsed


def describe(parsed: ParsedQuery) -> str:
    """Plain-language echo of what the query is doing, for the UI."""
    if not parsed.terms:
        return ""
    bits = []
    for t in parsed.terms:
        where = "anywhere" if t["field"] == "any" else t["field"]
        bits.append(f"{'not ' if t['negate'] else ''}{where} contains “{t['value']}”")
    return " and ".join(bits)
