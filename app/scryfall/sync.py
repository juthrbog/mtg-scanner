"""Download Scryfall bulk card data and load it into the local database.

This is the fast step — metadata only, no images. Scryfall explicitly invites
caching this locally rather than hitting the live API per-lookup. As of 2026
bulk files are gzip-compressed JSONL (one card object per line), which we
stream through rather than loading the whole thing into memory at once.

Run with:
    python -m app.scryfall.sync                          # full default_cards sync
    python -m app.scryfall.sync --bulk-type unique_artwork --limit 500   # quick smoke test
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Iterator, Optional

import httpx

from ..config import DATA_DIR, SCRYFALL_API
from ..db import db_session, init_db

BULK_INFO_URL = f"{SCRYFALL_API}/bulk-data"
HEADERS = {"User-Agent": "mtg-scanner/0.1 (local personal collection tool)", "Accept": "application/json"}

# Layouts that aren't real, ownable printings — skip them.
SKIP_LAYOUTS = {"art_series", "token", "double_faced_token", "emblem", "planar", "scheme", "vanguard"}

BATCH_SIZE = 2000


def _find_bulk_uri(bulk_type: str) -> tuple[str, str]:
    resp = httpx.get(BULK_INFO_URL, timeout=30, headers=HEADERS)
    resp.raise_for_status()
    for entry in resp.json()["data"]:
        if entry["type"] == bulk_type:
            return entry["jsonl_download_uri"], entry["updated_at"]
    raise SystemExit(f"No bulk data of type {bulk_type!r} found. Options: default_cards, unique_artwork, oracle_cards.")


def download_bulk_file(bulk_type: str) -> Path:
    uri, updated_at = _find_bulk_uri(bulk_type)
    dest = DATA_DIR / f"{bulk_type}.jsonl.gz"
    print(f"Downloading {bulk_type} (Scryfall last updated it {updated_at}) ...")
    with httpx.stream("GET", uri, timeout=None, headers=HEADERS) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        written = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                written += len(chunk)
                if total:
                    print(f"\r  {written / total:.0%}", end="", flush=True)
    print("\nSaved to", dest)
    return dest


def _extract_image_uris(card: dict) -> dict:
    if card.get("image_uris"):
        return card["image_uris"]
    faces = card.get("card_faces") or [{}]
    return faces[0].get("image_uris") or {}


def _iter_cards(bulk_path: Path) -> Iterator[dict]:
    """Stream cards one at a time from a gzip-compressed JSONL bulk file."""
    with gzip.open(bulk_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _to_row(card: dict) -> tuple:
    image_uris = _extract_image_uris(card)
    prices = card.get("prices") or {}
    return (
        card["id"],
        card.get("oracle_id"),
        card["name"],
        card.get("set", ""),
        card.get("set_name", ""),
        card.get("collector_number", ""),
        card.get("rarity"),
        card.get("mana_cost"),
        card.get("type_line"),
        card.get("oracle_text"),
        ",".join(card.get("colors", [])),
        ",".join(card.get("color_identity", [])),
        ",".join(card.get("keywords", [])),
        image_uris.get("small"),
        image_uris.get("normal"),
        float(prices["usd"]) if prices.get("usd") else None,
        float(prices["usd_foil"]) if prices.get("usd_foil") else None,
        (card.get("purchase_uris") or {}).get("tcgplayer"),
    )


UPSERT_SQL = """
    INSERT INTO scryfall_card (
        id, oracle_id, name, set_code, set_name, collector_number,
        rarity, mana_cost, type_line, oracle_text, colors, color_identity, keywords,
        image_small, image_normal, price_usd, price_usd_foil, tcgplayer_url
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        name=excluded.name, set_code=excluded.set_code, set_name=excluded.set_name,
        collector_number=excluded.collector_number, rarity=excluded.rarity,
        mana_cost=excluded.mana_cost, type_line=excluded.type_line,
        oracle_text=excluded.oracle_text, colors=excluded.colors,
        image_small=excluded.image_small, image_normal=excluded.image_normal,
        price_usd=excluded.price_usd, price_usd_foil=excluded.price_usd_foil,
        tcgplayer_url=excluded.tcgplayer_url,
        color_identity=excluded.color_identity, keywords=excluded.keywords
"""


def load_into_db(bulk_path: Path, limit: Optional[int] = None) -> int:
    print(f"Parsing {bulk_path} ...")
    total = 0
    batch: list[tuple] = []
    with db_session() as conn:
        for card in _iter_cards(bulk_path):
            if card.get("layout") in SKIP_LAYOUTS:
                continue
            batch.append(_to_row(card))
            total += 1
            if len(batch) >= BATCH_SIZE:
                conn.executemany(UPSERT_SQL, batch)
                conn.commit()
                print(f"\r  {total} cards inserted", end="", flush=True)
                batch = []
            if limit and total >= limit:
                break
        if batch:
            conn.executemany(UPSERT_SQL, batch)
    print()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Scryfall bulk card data into the local database.")
    parser.add_argument(
        "--bulk-type", default="default_cards",
        choices=["default_cards", "unique_artwork", "oracle_cards"],
        help="default_cards = every printing (best for a real collection); unique_artwork is much smaller and good for a first test run.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only load the first N cards (quick smoke test)")
    parser.add_argument("--skip-download", action="store_true", help="Reuse a previously downloaded bulk file")
    args = parser.parse_args()

    init_db()

    bulk_path = DATA_DIR / f"{args.bulk_type}.jsonl.gz"
    if not args.skip_download or not bulk_path.exists():
        bulk_path = download_bulk_file(args.bulk_type)

    count = load_into_db(bulk_path, limit=args.limit)
    print(f"Done. {count} cards in the local database.")
    print("Next: python -m app.scryfall.hashing   (builds the image match index — the slow step)")


if __name__ == "__main__":
    main()
