"""Browse, search, and edit the local collection."""
from __future__ import annotations

import sqlite3
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ..db import get_db
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


def _query_collection(conn: sqlite3.Connection, q: str, color: str, rarity: str) -> List[sqlite3.Row]:
    sql = """
        SELECT ce.id AS entry_id, ce.quantity, ce.foil, ce.condition,
               sc.id AS scryfall_id, sc.name, sc.set_code, sc.set_name,
               sc.rarity, sc.colors, sc.mana_cost, sc.image_small, sc.image_normal,
               sc.price_usd, sc.price_usd_foil
        FROM collection_entry ce
        JOIN scryfall_card sc ON sc.id = ce.scryfall_id
        WHERE 1 = 1
    """
    params: list = []
    if q:
        sql += " AND sc.name LIKE ?"
        params.append(f"%{q}%")
    if color:
        sql += " AND (',' || sc.colors || ',') LIKE ?"
        params.append(f"%,{color},%")
    if rarity:
        sql += " AND sc.rarity = ?"
        params.append(rarity)
    sql += " ORDER BY sc.name"
    return conn.execute(sql, params).fetchall()


@router.get("/", response_class=HTMLResponse)
def collection_page(request: Request, q: str = "", color: str = "", rarity: str = "", conn=Depends(get_db)):
    cards = _query_collection(conn, q, color, rarity)
    return templates.TemplateResponse(
        "collection.html",
        {"request": request, "cards": cards, "q": q, "color": color, "rarity": rarity,
         "totals": collection_totals(conn), "oob": False},
    )


@router.get("/grid", response_class=HTMLResponse)
def collection_grid(request: Request, q: str = "", color: str = "", rarity: str = "", conn=Depends(get_db)):
    cards = _query_collection(conn, q, color, rarity)
    return templates.TemplateResponse("partials/grid.html", {"request": request, "cards": cards})


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
    return templates.TemplateResponse("partials/card_detail.html", {"request": request, "card": row})


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
        "partials/grid.html",
        {"request": request, "cards": cards, "totals": collection_totals(conn), "oob": True},
    )


@router.delete("/{entry_id}", response_class=HTMLResponse)
def delete_entry(request: Request, entry_id: int, conn=Depends(get_db)):
    conn.execute("DELETE FROM collection_entry WHERE id = ?", (entry_id,))
    cards = _query_collection(conn, "", "", "")
    return templates.TemplateResponse(
        "partials/grid.html",
        {"request": request, "cards": cards, "totals": collection_totals(conn), "oob": True},
    )
