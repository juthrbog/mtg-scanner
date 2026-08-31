"""Refresh market prices from Mana Pool.

TCGplayer prices already arrive with the Scryfall sync (Scryfall's `usd` /
`usd_foil` are TCGplayer market prices), so this only fetches Mana Pool.

Mana Pool publishes every single's price in one public, unauthenticated dump
(`GET /api/v1/prices/singles`, ~50MB) keyed by Scryfall ID, which joins
straight onto our card table. Pulling the whole file once is far kinder to
their API than a request per card.

Run with:
    python -m app.scryfall.prices
"""
from __future__ import annotations

import argparse

import httpx

from ..db import db_session, init_db

PRICES_URL = "https://manapool.com/api/v1/prices/singles"
HEADERS = {"User-Agent": "weatherlight/0.1 (local personal collection tool)", "Accept": "application/json"}
BATCH = 2000


def refresh_manapool() -> tuple[int, int]:
    """Returns (rows_seen, rows_matched_in_local_db)."""
    print(f"Fetching {PRICES_URL} ...")
    with httpx.Client(timeout=180, headers=HEADERS, follow_redirects=True) as client:
        payload = client.get(PRICES_URL).json()

    as_of = payload.get("meta", {}).get("as_of", "unknown")
    base = payload.get("meta", {}).get("base_url", "https://manapool.com")
    cards = payload.get("data", [])
    print(f"  {len(cards)} priced singles, as of {as_of}")

    seen = matched = 0
    batch: list[tuple] = []
    with db_session() as conn:
        for c in cards:
            sid = c.get("scryfall_id")
            if not sid:
                continue
            seen += 1
            url = c.get("url") or ""
            if url.startswith("/"):
                url = base + url
            batch.append((
                c.get("price_cents"),
                c.get("price_cents_nm"),
                c.get("price_cents_foil"),
                url or None,
                sid,
            ))
            if len(batch) >= BATCH:
                matched += _flush(conn, batch)
                batch = []
                print(f"\r  matched {matched}", end="", flush=True)
        if batch:
            matched += _flush(conn, batch)
    print()
    return seen, matched


def _flush(conn, batch: list[tuple]) -> int:
    cur = conn.executemany(
        """
        UPDATE scryfall_card
           SET manapool_cents = ?, manapool_nm_cents = ?, manapool_foil_cents = ?, manapool_url = ?
         WHERE id = ?
        """,
        batch,
    )
    conn.commit()
    return cur.rowcount


def main() -> None:
    argparse.ArgumentParser(description="Refresh Mana Pool prices into the local database.").parse_args()
    init_db()
    seen, matched = refresh_manapool()
    print(f"Done. {matched} of your {seen} priced singles matched a card in the local database.")


if __name__ == "__main__":
    main()
