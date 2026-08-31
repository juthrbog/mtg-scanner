"""Render a deck as text other Magic tools can import.

Every mainstream deck site parses roughly the same shape — a quantity, a card
name, one per line — and differs mainly in whether it wants the exact printing
and how it marks the commander. The formats here cover that spread:

* ``text``     — ``1 Card Name``. The lowest common denominator, and what to
                 reach for when an import fails somewhere else.
* ``moxfield`` — the same, with ``Commander`` and ``Deck`` headers, which
                 Moxfield and Archidekt read to fill the command zone.
* ``arena``    — ``1 Card Name (SET) 123``, pinned to exact printings.
* ``csv``      — for spreadsheets and collection trackers.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, List, Optional

FORMATS = {
    "text": ("Plain text", "txt", "Universal — Moxfield, Archidekt, TappedOut, Cockatrice"),
    "moxfield": ("Moxfield / Archidekt", "txt", "Adds Commander and Deck headers"),
    "arena": ("MTG Arena", "txt", "Pins the exact printing with set and collector number"),
    "csv": ("CSV", "csv", "Spreadsheets and collection trackers"),
}


def _front_face(name: str) -> str:
    """The front face of a double-faced card.

    Arena rejects "Front // Back" and wants only the front face; the plain
    formats keep the full name, which is what the deck sites expect.
    """
    return name.split("//")[0].strip()


def _arena_collector(number: Optional[str]) -> str:
    """Arena's importer chokes on leading zeros, so strip them."""
    n = (number or "").strip().lstrip("0")
    return n or (number or "").strip()


def _lines(cards: Iterable, printing: bool = False) -> List[str]:
    out = []
    for c in cards:
        if printing:
            out.append(
                f"{c['quantity']} {_front_face(c['name'])} "
                f"({(c['set_code'] or '').upper()}) {_arena_collector(c['collector_number'])}"
            )
        else:
            out.append(f"{c['quantity']} {c['name']}")
    return out


def render(fmt: str, deck, commander, partner, cards: Iterable) -> str:
    cards = list(cards)
    leaders = [c for c in (commander, partner) if c is not None]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Count", "Name", "Set code", "Set name", "Collector number", "Section"])
        for c in leaders:
            writer.writerow([1, c["name"], c["set_code"], c["set_name"],
                             c["collector_number"], "Commander"])
        for c in cards:
            writer.writerow([c["quantity"], c["name"], c["set_code"], c["set_name"],
                             c["collector_number"], "Deck"])
        return buf.getvalue()

    if fmt == "arena":
        blocks = []
        if leaders:
            blocks.append("Commander\n" + "\n".join(_lines(
                [{**dict(c), "quantity": 1} for c in leaders], printing=True)))
        blocks.append("Deck\n" + "\n".join(_lines(cards, printing=True)))
        return "\n\n".join(blocks) + "\n"

    if fmt == "moxfield":
        blocks = []
        if leaders:
            blocks.append("Commander\n" + "\n".join(
                f"1 {c['name']}" for c in leaders))
        blocks.append("Deck\n" + "\n".join(_lines(cards)))
        return "\n\n".join(blocks) + "\n"

    # Plain text: the commander is just another line. Sites that understand a
    # command zone read the header formats above; the rest would treat a
    # header as a card name and fail the import, so this one has none.
    return "\n".join(_lines([{**dict(c), "quantity": 1} for c in leaders] + list(cards))) + "\n"
