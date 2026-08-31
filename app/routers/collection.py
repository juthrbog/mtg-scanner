"""Browse, search, and edit the local collection."""
from __future__ import annotations

import sqlite3
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ..db import get_db
from ..search import describe, parse
from ..templating import templates

router = APIRouter()


def collection_totals(conn: sqlite3.Connection) -> dict:
    """Estimated value of the whole collection, per marketplace.

    Foil copies are valued at foil prices where a foil price exists. Cards a
    marketplace has no price for contribute nothing, so `priced` reports how
    many of your copies each total actually covers — a total over 40 of 50
    cards shouldn't read as though it covered all 50.
    """
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(ce.quantity), 0) AS copies,
          COALESCE(SUM(ce.quantity * CASE
              WHEN ce.foil AND sc.price_usd_foil IS NOT NULL THEN sc.price_usd_foil
              ELSE sc.price_usd END), 0) AS tcg_total,
          COALESCE(SUM(CASE WHEN (CASE WHEN ce.foil AND sc.price_usd_foil IS NOT NULL
                                       THEN sc.price_usd_foil ELSE sc.price_usd END)
                            IS NOT NULL THEN ce.quantity ELSE 0 END), 0) AS tcg_priced,
          COALESCE(SUM(ce.quantity * CASE
              WHEN ce.foil AND sc.manapool_foil_cents IS NOT NULL THEN sc.manapool_foil_cents
              ELSE COALESCE(sc.manapool_nm_cents, sc.manapool_cents) END), 0) / 100.0 AS mp_total,
          COALESCE(SUM(CASE WHEN (CASE WHEN ce.foil AND sc.manapool_foil_cents IS NOT NULL
                                       THEN sc.manapool_foil_cents
                                       ELSE COALESCE(sc.manapool_nm_cents, sc.manapool_cents) END)
                            IS NOT NULL THEN ce.quantity ELSE 0 END), 0) AS mp_priced
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        """
    ).fetchone()
    return {
        "copies": row["copies"],
        "tcg_total": row["tcg_total"],
        "tcg_priced": row["tcg_priced"],
        "mp_total": row["mp_total"],
        "mp_priced": row["mp_priced"],
    }


# Foil-aware price, reused by sorting and by the totals above.
_PRICE = """CASE WHEN ce.foil AND sc.price_usd_foil IS NOT NULL
                 THEN sc.price_usd_foil ELSE sc.price_usd END"""

# Sort key -> (label, ORDER BY clause). Rarity needs an explicit order because
# alphabetically "common" would outrank "mythic".
SORTS = {
    "name":      ("Name (A–Z)",        "sc.name COLLATE NOCASE ASC"),
    "name_desc": ("Name (Z–A)",        "sc.name COLLATE NOCASE DESC"),
    "value_desc":("Value (high first)", f"{_PRICE} IS NULL, {_PRICE} DESC"),
    "value_asc": ("Value (low first)",  f"{_PRICE} IS NULL, {_PRICE} ASC"),
    "rarity":    ("Rarity (mythic first)",
                  "CASE sc.rarity WHEN 'mythic' THEN 0 WHEN 'rare' THEN 1 "
                  "WHEN 'uncommon' THEN 2 WHEN 'common' THEN 3 ELSE 4 END, sc.name COLLATE NOCASE"),
    "set":       ("Set",
                  "sc.set_name COLLATE NOCASE, CAST(sc.collector_number AS INTEGER), sc.collector_number"),
    "quantity":  ("Quantity (most first)", "ce.quantity DESC, sc.name COLLATE NOCASE"),
    "added":     ("Recently added",        "ce.id DESC"),
}
DEFAULT_SORT = "name"


def collection_keywords(conn: sqlite3.Connection) -> List[dict]:
    """Keywords present in the collection, with how many cards carry each.

    Listing what you actually own beats a generic glossary: every entry here
    is guaranteed to return results when clicked.
    """
    rows = conn.execute(
        """
        SELECT sc.keywords, ce.quantity
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        WHERE sc.keywords IS NOT NULL AND sc.keywords != ''
        """
    ).fetchall()
    counts: dict = {}
    for row in rows:
        for kw in row["keywords"].split(","):
            kw = kw.strip()
            if kw:
                counts[kw] = counts.get(kw, 0) + row["quantity"]
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _query_collection(conn: sqlite3.Connection, q: str, color: str, rarity: str,
                      sort: str = DEFAULT_SORT) -> List[sqlite3.Row]:
    sql = """
        SELECT ce.id AS entry_id, ce.quantity, ce.foil, ce.condition,
               sc.id AS scryfall_id, sc.name, sc.set_code, sc.set_name,
               sc.rarity, sc.colors, sc.mana_cost, sc.type_line, sc.keywords,
               sc.image_small, sc.image_normal,
               sc.price_usd, sc.price_usd_foil
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        WHERE 1 = 1
    """
    params: list = []
    parsed = parse(q)
    if parsed.sql:
        sql += f" AND ({parsed.sql})"
        params.extend(parsed.params)
    if color:
        sql += " AND (',' || sc.colors || ',') LIKE ?"
        params.append(f"%,{color},%")
    if rarity:
        sql += " AND sc.rarity = ?"
        params.append(rarity)
    # Look the clause up rather than interpolating user input into SQL.
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])[1]
    sql += f" ORDER BY {order}"
    return conn.execute(sql, params).fetchall()


@router.get("/", response_class=HTMLResponse)
def collection_page(request: Request, q: str = "", color: str = "", rarity: str = "",
                    sort: str = DEFAULT_SORT, conn=Depends(get_db)):
    cards = _query_collection(conn, q, color, rarity, sort)
    return templates.TemplateResponse(
        request,
        "collection.html",
        {"cards": cards, "q": q, "color": color, "rarity": rarity,
         "search_summary": describe(parse(q)),
         "sort": sort if sort in SORTS else DEFAULT_SORT, "sorts": SORTS,
         "keywords": collection_keywords(conn),
         "totals": collection_totals(conn), "oob": False},
    )


@router.get("/grid", response_class=HTMLResponse)
def collection_grid(request: Request, q: str = "", color: str = "", rarity: str = "",
                    sort: str = DEFAULT_SORT, conn=Depends(get_db)):
    cards = _query_collection(conn, q, color, rarity, sort)
    return templates.TemplateResponse(
        request,
        "partials/grid.html",
        {"cards": cards, "search_summary": describe(parse(q))},
    )


@router.get("/{entry_id}", response_class=HTMLResponse)
def card_detail(request: Request, entry_id: int, conn=Depends(get_db)):
    row = conn.execute(
        """
        SELECT ce.*, sc.name, sc.set_name, sc.set_code, sc.collector_number,
               sc.rarity, sc.mana_cost, sc.type_line, sc.oracle_text, sc.colors,
               sc.image_normal, sc.price_usd, sc.price_usd_foil,
               sc.tcgplayer_url, sc.manapool_cents, sc.manapool_nm_cents,
               sc.manapool_foil_cents, sc.manapool_url
        FROM collection_entry ce JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        WHERE ce.id = ?
        """,
        (entry_id,),
    ).fetchone()
    return templates.TemplateResponse(
        request,
        "partials/card_detail.html",
        {"card": row})


@router.put("/{entry_id}", response_class=HTMLResponse)
def update_entry(
    request: Request,
    entry_id: int,
    quantity: int = Form(...),
    condition: str = Form("NM"),
    foil: bool = Form(False),
    notes: str = Form(""),
    conn=Depends(get_db),
):
    if quantity <= 0:
        conn.execute("DELETE FROM collection_entry WHERE id = ?", (entry_id,))
    else:
        conn.execute(
            "UPDATE collection_entry SET quantity = ?, condition = ?, foil = ?, notes = ? WHERE id = ?",
            (quantity, condition, int(foil), notes, entry_id),
        )
    cards = _query_collection(conn, "", "", "")
    return templates.TemplateResponse(
        request,
        "partials/grid.html",
        {"cards": cards, "totals": collection_totals(conn), "oob": True},
    )


@router.delete("/{entry_id}", response_class=HTMLResponse)
def delete_entry(request: Request, entry_id: int, conn=Depends(get_db)):
    conn.execute("DELETE FROM collection_entry WHERE id = ?", (entry_id,))
    cards = _query_collection(conn, "", "", "")
    return templates.TemplateResponse(
        request,
        "partials/grid.html",
        {"cards": cards, "totals": collection_totals(conn), "oob": True},
    )
