"""Collection statistics."""
from __future__ import annotations

import sqlite3
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..colors import MONO, parse_colors
from ..db import get_db
from ..templating import templates
from .collection import collection_totals

router = APIRouter()

# WUBRG order — the order Magic itself prints costs in, so it is the order
# players read. Colourless trails it as a separate bucket rather than a colour.
COLOR_ORDER = ["W", "U", "B", "R", "G"]


def _color_breakdown(conn: sqlite3.Connection) -> List[dict]:
    """Cards per colour, plus a colourless bucket.

    Counts by *colour identity*, not by mana cost. A Swamp has no mana cost, so
    its `colors` is empty and it would otherwise land under "colourless" —
    which is not what someone asking how much black they own means. Identity
    includes the mana a card produces, so basic lands sit under their colour.

    A multicolour card counts once under *each* of its colours, because the
    question this answers is "how much red do I own", not "how many cards are
    exactly red". The colour totals therefore sum to more than the collection
    size, which the page says out loud rather than leaving to be discovered.
    """
    rows = conn.execute(
        """
        SELECT ce.quantity,
               COALESCE(NULLIF(sc.color_identity, ''), sc.colors) AS colors,
               CASE WHEN ce.foil AND sc.price_usd_foil IS NOT NULL
                    THEN sc.price_usd_foil ELSE sc.price_usd END AS tcg,
               CASE WHEN ce.foil AND sc.manapool_foil_cents IS NOT NULL
                    THEN sc.manapool_foil_cents
                    ELSE COALESCE(sc.manapool_nm_cents, sc.manapool_cents) END AS mp_cents
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        """
    ).fetchall()

    buckets = {c: {"cards": 0, "unique": 0, "tcg": 0.0, "mp": 0.0} for c in COLOR_ORDER}
    buckets["C"] = {"cards": 0, "unique": 0, "tcg": 0.0, "mp": 0.0}

    for row in rows:
        letters = parse_colors(row["colors"]) or {"C"}
        for letter in letters:
            b = buckets.get(letter)
            if b is None:
                continue
            b["cards"] += row["quantity"]
            b["unique"] += 1
            if row["tcg"]:
                b["tcg"] += row["tcg"] * row["quantity"]
            if row["mp_cents"]:
                b["mp"] += row["mp_cents"] * row["quantity"] / 100

    names = {**{k: v for k, v in ((next(iter(s)), n) for s, n in MONO.items())}, "C": "Colourless"}
    peak = max((b["cards"] for b in buckets.values()), default=0)

    out = []
    for letter in COLOR_ORDER + ["C"]:
        b = buckets[letter]
        out.append({
            "letter": letter,
            "symbol": letter.lower(),
            "name": names.get(letter, letter),
            "cards": b["cards"],
            "unique": b["unique"],
            "tcg": b["tcg"],
            "mp": b["mp"],
            # Share of the largest bucket, for the bar length.
            "share": (b["cards"] / peak) if peak else 0,
        })
    return out


def collection_stats(conn: sqlite3.Connection) -> dict:
    totals = collection_totals(conn)
    counts = conn.execute(
        """
        SELECT COUNT(*) AS printings,
               COUNT(DISTINCT sc.oracle_id) AS distinct_cards,
               COALESCE(SUM(ce.foil), 0) AS foil_entries
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        """
    ).fetchone()

    rarity = conn.execute(
        """
        SELECT COALESCE(sc.rarity, 'unknown') AS rarity,
               SUM(ce.quantity) AS cards, COUNT(*) AS unique_printings
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        GROUP BY sc.rarity
        """
    ).fetchall()
    order = {"mythic": 0, "rare": 1, "uncommon": 2, "common": 3}
    rarity = sorted((dict(r) for r in rarity), key=lambda r: order.get(r["rarity"], 9))

    return {
        "totals": totals,
        "printings": counts["printings"],
        "distinct_cards": counts["distinct_cards"],
        "foil_entries": counts["foil_entries"],
        "colors": _color_breakdown(conn),
        "rarity": rarity,
    }


@router.get("/", response_class=HTMLResponse)
def stats_page(request: Request, conn=Depends(get_db)):
    stats = collection_stats(conn)
    return templates.TemplateResponse(
        "stats.html",
        {"request": request, "stats": stats, "totals": stats["totals"], "oob": False},
    )
