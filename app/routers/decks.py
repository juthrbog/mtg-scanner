"""Build and save Commander decks."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..db import get_db
from ..deck import DECK_SIZE, can_be_commander, categorise, curve, identity_of, validate
from ..deck_import import COMMANDER, SIDEBOARD, fold, front_face
from ..deck_import import parse as parse_list
from ..export import FORMATS, render
from ..search import parse
from ..templating import templates
from .collection import collection_totals

router = APIRouter()

# Everything the rules engine and the templates need about a card.
CARD_FIELDS = """
    sc.id AS scryfall_id, sc.oracle_id, sc.name, sc.type_line, sc.oracle_text, sc.mana_cost,
    sc.color_identity, sc.colors, sc.commander_legal, sc.cmc, sc.rarity,
    sc.set_code, sc.set_name, sc.collector_number, sc.image_small, sc.image_normal,
    sc.price_usd, sc.manapool_nm_cents, sc.manapool_cents
"""



# Rank exact and prefix name matches above alphabetical order. Searching
# "Lightning Bolt" otherwise surfaced "Emeritus of Conflict // Lightning Bolt"
# first, purely because it sorts earlier — and the top hit is the one people
# click.
def _relevance_order(q: str) -> tuple:
    bare = " ".join(t for t in q.split() if ":" not in t).strip()
    if not bare:
        return "sc.name COLLATE NOCASE", []
    return (
        "CASE WHEN LOWER(sc.name) = LOWER(?) THEN 0 "
        "     WHEN LOWER(sc.name) LIKE LOWER(?) THEN 1 "
        "     ELSE 2 END, LENGTH(sc.name), sc.name COLLATE NOCASE",
        [bare, f"{bare}%"],
    )


def _deck_row(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM deck WHERE id = ?", (deck_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Deck not found")
    return row


def _card(conn: sqlite3.Connection, scryfall_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"SELECT {CARD_FIELDS}, 1 AS quantity FROM scryfall_card sc WHERE sc.id = ?",
        (scryfall_id,),
    ).fetchone()


def _deck_cards(conn: sqlite3.Connection, deck_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT {CARD_FIELDS}, dc.quantity, dc.id AS entry_id
        FROM deck_card dc JOIN scryfall_card sc ON sc.id = dc.scryfall_id
        WHERE dc.deck_id = ?
        ORDER BY sc.name COLLATE NOCASE
        """,
        (deck_id,),
    ).fetchall()


def _owned_counts(conn: sqlite3.Connection) -> dict:
    """How many copies of each card the collection holds, keyed by oracle id.

    Keyed by oracle rather than printing because a Sol Ring is a Sol Ring
    whichever set it came from — Scryfall lists 140 printings — and a deck
    cares about the card, not the art.
    """
    return {
        r["oracle_id"]: r["qty"]
        for r in conn.execute(
            """SELECT sc.oracle_id, SUM(ce.quantity) AS qty
               FROM collection_entry ce JOIN scryfall_card sc ON sc.id = ce.scryfall_id
               GROUP BY sc.oracle_id"""
        )
    }


# Deck building searches the collection itself rather than the whole card
# database filtered by ownership. Searching everything and then asking "do you
# own any printing of this?" returns whichever printing the group happened to
# collapse to — which showed a showcase Hama Pashar to someone holding the
# regular one. Selecting from collection_entry means every row offered is a
# card in hand, with its own art.
#
# MAX(ce.id) is not decorative: SQLite fills bare columns from the row that
# produced the min/max, so this makes the chosen printing the most recently
# added one instead of arbitrary.
OWNED_SOURCE = """
    FROM collection_entry ce
    JOIN scryfall_card sc ON sc.id = ce.scryfall_id
"""
OWNED_PICK = "MAX(ce.id) AS _pick, SUM(ce.quantity) AS owned_qty"


def _touch(conn: sqlite3.Connection, deck_id: int) -> None:
    conn.execute("UPDATE deck SET updated_at = datetime('now') WHERE id = ?", (deck_id,))


def _deck_context(conn: sqlite3.Connection, deck_id: int) -> dict:
    deck = _deck_row(conn, deck_id)
    commander = _card(conn, deck["commander_id"]) if deck["commander_id"] else None
    partner = _card(conn, deck["partner_id"]) if deck["partner_id"] else None
    cards = _deck_cards(conn, deck_id)
    owned = _owned_counts(conn)
    report = validate(commander, partner, cards, owned_counts=owned)
    return {
        "deck": deck,
        "commander": commander,
        "partner": partner,
        "groups": categorise(cards),
        "curve": curve(cards),
        "report": report,
        "owned_counts": owned,
        "deck_size": DECK_SIZE,
    }


@router.get("/", response_class=HTMLResponse)
def deck_list(request: Request, conn=Depends(get_db)):
    rows = conn.execute(
        """
        SELECT d.*, sc.name AS commander_name, sc.image_small, sc.image_normal,
               sc.color_identity,
               COALESCE((SELECT SUM(quantity) FROM deck_card WHERE deck_id = d.id), 0) AS card_count
        FROM deck d LEFT JOIN scryfall_card sc ON sc.id = d.commander_id
        ORDER BY d.updated_at DESC
        """
    ).fetchall()
    decks = []
    for r in rows:
        # +1 for the commander, which lives on the deck row rather than in
        # deck_card, so it is never counted twice or lost on edit.
        total = r["card_count"] + (1 if r["commander_id"] else 0)
        decks.append({**dict(r), "total": total, "remaining": DECK_SIZE - total})
    return templates.TemplateResponse(
        request, "decks.html",
        {"decks": decks, "deck_size": DECK_SIZE,
         "totals": collection_totals(conn), "oob": False},
    )


@router.get("/new", response_class=HTMLResponse)
def new_deck(request: Request, q: str = "", conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "deck_new.html",
        {"q": q, "commanders": _commander_search(conn, q),
         "totals": collection_totals(conn), "oob": False},
    )


def _commander_search(conn: sqlite3.Connection, q: str, limit: int = 24) -> List[sqlite3.Row]:
    """Cards that could lead a deck, matching the query.

    Restricted at the SQL level rather than filtered afterwards, so paging
    through thousands of non-commanders never happens.
    """
    if not q or not q.strip():
        return []
    parsed = parse(q)
    where = [
        "sc.commander_legal = 1",
        "sc.image_small IS NOT NULL",
        "((sc.type_line LIKE '%Legendary%' AND sc.type_line LIKE '%Creature%')"
        " OR sc.oracle_text LIKE '%can be your commander%')",
    ]
    params: list = []
    if parsed.sql:
        where.append(f"({parsed.sql})")
        params.extend(parsed.params)
    order, order_params = _relevance_order(q)
    return conn.execute(
        f"""SELECT {CARD_FIELDS}, {OWNED_PICK}
            {OWNED_SOURCE}
            WHERE {' AND '.join(where)}
            GROUP BY sc.oracle_id
            ORDER BY {order} LIMIT ?""",
        [*params, *order_params, limit],
    ).fetchall()


@router.post("/")
def create_deck(name: str = Form(...), commander_id: str = Form(...), conn=Depends(get_db)):
    commander = _card(conn, commander_id)
    if commander is None:
        raise HTTPException(status_code=400, detail="Unknown commander")
    cur = conn.execute(
        "INSERT INTO deck (name, commander_id) VALUES (?, ?)",
        (name.strip() or commander["name"], commander_id),
    )
    return RedirectResponse(url=f"/decks/{cur.lastrowid}", status_code=303)


# ---------------------------------------------------------------- importing

def _owned_index(conn: sqlite3.Connection) -> tuple:
    """Every owned printing, keyed by the names a deck list might call it.

    Two keys per printing — the full name and the front face — so a list from
    Arena ("Obyra's Attendants") and one from Moxfield ("Obyra's Attendants //
    Desperate Parry") both land on the same card.

    Ordered by collection_entry id so the last candidate for a name is the most
    recently added printing, which is the same tie-break the deck search uses.
    """
    by_name: dict = {}
    owned: dict = {}
    for row in conn.execute(
        f"""SELECT {CARD_FIELDS}, ce.quantity AS entry_qty
            {OWNED_SOURCE} ORDER BY ce.id"""
    ):
        owned[row["oracle_id"]] = owned.get(row["oracle_id"], 0) + row["entry_qty"]
        for key in {fold(row["name"]), fold(front_face(row["name"]))}:
            by_name.setdefault(key, []).append(row)
    return by_name, owned


def _pick_printing(candidates: list, set_code: Optional[str],
                   collector: Optional[str]) -> sqlite3.Row:
    """The copy to use for a list line.

    A list that names a printing is asking for that printing, so honour it when
    the collection holds it — that is the whole point of exporting with set and
    collector number. Otherwise fall back to the most recently added copy.
    """
    if set_code:
        code = set_code.lower()
        exact = [c for c in candidates if (c["set_code"] or "").lower() == code]
        if collector:
            numbered = [c for c in exact
                        if (c["collector_number"] or "").lstrip("0") == collector.lstrip("0")]
            if numbered:
                return numbered[-1]
        if exact:
            return exact[-1]
    return candidates[-1]


def _known_to_scryfall(conn: sqlite3.Connection, names: List[str]) -> set:
    """Which of these names exist as cards at all.

    The distinction matters more than it looks: "you don't own this" and "no
    such card" ask the user for completely different things — go scan it in,
    versus check the spelling.
    """
    if not names:
        return set()
    lowered = [n.lower() for n in names]
    found = {
        fold(r["name"])
        for r in conn.execute(
            f"SELECT name FROM scryfall_card WHERE LOWER(name) IN "
            f"({','.join('?' * len(lowered))})",
            lowered,
        )
    }
    # Anything still missing may be a double-faced card given by its front face,
    # which no equality test will find. Few enough by now to check one at a time.
    for name in names:
        if fold(name) in found:
            continue
        hit = conn.execute(
            "SELECT 1 FROM scryfall_card WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
            (f"{name} //%",),
        ).fetchone()
        if hit:
            found.add(fold(name))
    return found


def _resolve_import(conn: sqlite3.Connection, text: str) -> dict:
    """Match a pasted list against the collection.

    Nothing is written here. The same function backs both the preview and the
    create, so what the user is shown is exactly what gets built — rather than
    a preview that re-derives the answer a second, subtly different way.
    """
    parsed = parse_list(text)
    by_name, owned = _owned_index(conn)

    rows: List[dict] = []
    unresolved: List[str] = []
    # Copies already spoken for, by oracle id. Clamping each line against the
    # full owned count independently would let a list that spreads a card over
    # several lines — "1 Forest" three times, which hand-written lists do —
    # claim three copies of the one Forest in the collection.
    used: dict = {}
    for line in parsed.lines:
        candidates = by_name.get(fold(line.name)) or by_name.get(fold(front_face(line.name)))
        if not candidates:
            unresolved.append(line.name)
            rows.append({"line": line, "card": None, "quantity": 0,
                         "owned": 0, "status": "missing"})
            continue
        card = _pick_printing(candidates, line.set_code, line.collector_number)
        have = owned.get(card["oracle_id"], 0)
        if line.section == SIDEBOARD:
            status = "sideboard"
            quantity = 0
        else:
            # Clamped rather than added and flagged: a deck in this app can
            # never ask for more copies than the collection holds, and an
            # import is not the place to break that.
            quantity = max(0, min(line.quantity, have - used.get(card["oracle_id"], 0)))
            used[card["oracle_id"]] = used.get(card["oracle_id"], 0) + quantity
            status = "ok" if quantity == line.quantity else "clamped"
        rows.append({"line": line, "card": card, "quantity": quantity,
                     "owned": have, "status": status})

    known = _known_to_scryfall(conn, unresolved)
    for row in rows:
        if row["status"] == "missing":
            row["status"] = "unowned" if fold(row["line"].name) in known else "unknown"

    # Anything that could lead the deck. A list with a Commander section says
    # so outright; without one, the user picks from whatever qualifies.
    named = [r for r in rows if r["card"] and r["line"].section == COMMANDER]
    candidates = [r for r in rows if r["card"] and can_be_commander(r["card"])]
    suggested = named[0]["card"]["scryfall_id"] if named else (
        candidates[0]["card"]["scryfall_id"] if len(candidates) == 1 else None)

    return {
        "rows": rows,
        "unreadable": parsed.unreadable,
        "commander_candidates": candidates,
        "suggested_commander_id": suggested,
        "counts": {
            "ok": sum(1 for r in rows if r["status"] == "ok"),
            "clamped": sum(1 for r in rows if r["status"] == "clamped"),
            "unowned": sum(1 for r in rows if r["status"] == "unowned"),
            "unknown": sum(1 for r in rows if r["status"] == "unknown"),
            "sideboard": sum(1 for r in rows if r["status"] == "sideboard"),
        },
        "total_cards": sum(r["quantity"] for r in rows),
    }


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "deck_import.html",
        {"totals": collection_totals(conn), "oob": False},
    )


@router.post("/import/preview", response_class=HTMLResponse)
def import_preview(request: Request, text: str = Form(""), conn=Depends(get_db)):
    result = _resolve_import(conn, text) if text.strip() else None
    return templates.TemplateResponse(
        request, "partials/import_preview.html", {"result": result},
    )


@router.post("/import")
def import_deck(name: str = Form(""), text: str = Form(""),
                commander_id: str = Form(""), conn=Depends(get_db)):
    commander = _card(conn, commander_id) if commander_id else None
    if commander is None:
        raise HTTPException(status_code=400, detail="Pick a commander for the imported deck")

    result = _resolve_import(conn, text)
    cur = conn.execute(
        "INSERT INTO deck (name, commander_id) VALUES (?, ?)",
        (name.strip() or commander["name"], commander_id),
    )
    deck_id = cur.lastrowid

    # The commander lives on the deck row, so drop it from the list — a list
    # that names it in both a Commander section and the deck body (some sites
    # export it twice) would otherwise put it in the 99 as well.
    for row in result["rows"]:
        if not row["quantity"] or row["card"]["oracle_id"] == commander["oracle_id"]:
            continue
        conn.execute(
            """INSERT INTO deck_card (deck_id, scryfall_id, quantity) VALUES (?, ?, ?)
               ON CONFLICT(deck_id, scryfall_id) DO UPDATE SET quantity = quantity + excluded.quantity""",
            (deck_id, row["card"]["scryfall_id"], row["quantity"]),
        )
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.get("/{deck_id}", response_class=HTMLResponse)
def deck_detail(request: Request, deck_id: int, conn=Depends(get_db)):
    ctx = _deck_context(conn, deck_id)
    return templates.TemplateResponse(
        request, "deck_detail.html",
        {**ctx, "formats": FORMATS, "totals": collection_totals(conn), "oob": False},
    )


def _safe_filename(name: str, ext: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in name).strip()
    return f"{(keep or 'deck').replace(' ', '-').lower()}.{ext}"


@router.get("/{deck_id}/export")
def export_deck(deck_id: int, format: str = "text", download: bool = False, conn=Depends(get_db)):
    """Deck as importable text.

    Served as text/plain either way; `download` only switches the disposition,
    so the same URL can back both a preview and a Save-as.
    """
    if format not in FORMATS:
        raise HTTPException(status_code=400, detail="Unknown export format")
    deck = _deck_row(conn, deck_id)
    body = render(
        format, deck,
        _card(conn, deck["commander_id"]) if deck["commander_id"] else None,
        _card(conn, deck["partner_id"]) if deck["partner_id"] else None,
        _deck_cards(conn, deck_id),
    )
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{_safe_filename(deck["name"], FORMATS[format][1])}"'
        )
    return PlainTextResponse(body, headers=headers)


@router.get("/{deck_id}/search", response_class=HTMLResponse)
def card_search(request: Request, deck_id: int, q: str = "", conn=Depends(get_db)):
    """Cards to add, flagged against this deck's identity and contents."""
    deck = _deck_row(conn, deck_id)
    commander = _card(conn, deck["commander_id"]) if deck["commander_id"] else None
    partner = _card(conn, deck["partner_id"]) if deck["partner_id"] else None
    identity = set().union(*[identity_of(c) for c in (commander, partner) if c]) if commander else set()

    results = []
    if q and q.strip():
        parsed = parse(q)
        where = ["sc.image_small IS NOT NULL"]
        params: list = []
        if parsed.sql:
            where.append(f"({parsed.sql})")
            params.extend(parsed.params)
        order, order_params = _relevance_order(q)
        rows = conn.execute(
            f"""SELECT {CARD_FIELDS}, {OWNED_PICK}
                {OWNED_SOURCE}
                WHERE {' AND '.join(where)}
                GROUP BY sc.oracle_id
                ORDER BY {order} LIMIT 30""",
            [*params, *order_params],
        ).fetchall()
        in_deck = {r["oracle_id"]: r["quantity"] for r in _deck_cards(conn, deck_id)}
        owned = _owned_counts(conn)
        for r in rows:
            have = owned.get(r["oracle_id"], 0)
            used = in_deck.get(r["oracle_id"], 0)
            results.append({
                "card": r,
                # Flagged rather than hidden: a card outside the identity is
                # still worth showing, so the reason it can't go in is visible.
                "off_identity": bool(commander) and not identity_of(r) <= identity,
                "banned": r["commander_legal"] == 0,
                "owned": have,
                "used": used,
                # Every copy already in the deck; adding another would ask for
                # a card that isn't there.
                "exhausted": used >= have,
            })
    return templates.TemplateResponse(
        request, "partials/deck_search.html",
        {"results": results, "deck_id": deck_id, "q": q},
    )


@router.post("/{deck_id}/cards", response_class=HTMLResponse)
def add_card(request: Request, deck_id: int, scryfall_id: str = Form(...), conn=Depends(get_db)):
    _deck_row(conn, deck_id)
    card = _card(conn, scryfall_id)
    if card is None:
        raise HTTPException(status_code=400, detail="Unknown card")

    # Cap at what the collection holds. The button is hidden once a card is
    # used up, but a stale search panel could still post — the limit belongs
    # here, where it cannot be raced.
    have = _owned_counts(conn).get(card["oracle_id"], 0)
    used = sum(
        r["quantity"] for r in _deck_cards(conn, deck_id)
        if r["oracle_id"] == card["oracle_id"]
    )
    if used < have:
        conn.execute(
            """INSERT INTO deck_card (deck_id, scryfall_id, quantity) VALUES (?, ?, 1)
               ON CONFLICT(deck_id, scryfall_id) DO UPDATE SET quantity = quantity + 1""",
            (deck_id, scryfall_id),
        )
    _touch(conn, deck_id)
    return _deck_body(request, conn, deck_id)


@router.delete("/{deck_id}/cards/{entry_id}", response_class=HTMLResponse)
def remove_card(request: Request, deck_id: int, entry_id: int, conn=Depends(get_db)):
    conn.execute("DELETE FROM deck_card WHERE id = ? AND deck_id = ?", (entry_id, deck_id))
    _touch(conn, deck_id)
    return _deck_body(request, conn, deck_id)


@router.put("/{deck_id}/cards/{entry_id}", response_class=HTMLResponse)
def set_quantity(request: Request, deck_id: int, entry_id: int,
                 quantity: int = Form(...), conn=Depends(get_db)):
    if quantity <= 0:
        conn.execute("DELETE FROM deck_card WHERE id = ? AND deck_id = ?", (entry_id, deck_id))
    else:
        conn.execute(
            "UPDATE deck_card SET quantity = ? WHERE id = ? AND deck_id = ?",
            (quantity, entry_id, deck_id),
        )
    _touch(conn, deck_id)
    return _deck_body(request, conn, deck_id)


@router.delete("/{deck_id}")
def delete_deck(deck_id: int, conn=Depends(get_db)):
    conn.execute("DELETE FROM deck_card WHERE deck_id = ?", (deck_id,))
    conn.execute("DELETE FROM deck WHERE id = ?", (deck_id,))
    return HTMLResponse("", headers={"HX-Redirect": "/decks/"})


def _deck_body(request: Request, conn: sqlite3.Connection, deck_id: int) -> HTMLResponse:
    """The deck list plus its validation panel, re-rendered after any edit.

    Returned as one fragment because a card change can alter the legality
    summary, the curve and the counts at once — swapping them separately would
    let them disagree for a moment.
    """
    ctx = _deck_context(conn, deck_id)
    return templates.TemplateResponse(request, "partials/deck_body.html", ctx)
