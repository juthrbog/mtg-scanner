"""Build the perceptual-hash match index by downloading card art and hashing it.

This is the slow step — one small image fetched per card. Safe to interrupt
(Ctrl-C) and re-run later: already-hashed cards are skipped, so it resumes
where it left off.

Run with:
    python -m app.scryfall.hashing                 # hash everything still missing a hash
    python -m app.scryfall.hashing --limit 500      # quick smoke test
"""
from __future__ import annotations

import argparse
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import cv2
import httpx
import imagehash
import numpy as np
from PIL import Image

from ..recognition.detect import art_window

from ..config import DATA_DIR, PHASH_SIZE
from ..db import db_session

HEADERS = {"User-Agent": "mtg-scanner/0.1 (local personal collection tool)"}

# Card art is cached here so a re-hash (after changing PHASH_SIZE, say) costs
# CPU rather than another 100k downloads from Scryfall's CDN.
CACHE_DIR = DATA_DIR / "art_cache"


def _cache_path(scryfall_id: str):
    # Shard by first two characters; a single directory with 100k entries is
    # slow to enumerate on some filesystems.
    d = CACHE_DIR / scryfall_id[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{scryfall_id}.jpg"


def _fetch_and_hash(client: httpx.Client, scryfall_id: str, url: str) -> tuple[str, Optional[str], Optional[str]]:
    try:
        path = _cache_path(scryfall_id)
        if path.exists():
            data = path.read_bytes()
        else:
            resp = client.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.content
            path.write_bytes(data)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        whole = str(imagehash.phash(img, hash_size=PHASH_SIZE))
        # The art window is hashed from the same source image, so the
        # index and a live scan crop the same region the same way.
        bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        art = Image.fromarray(cv2.cvtColor(art_window(bgr), cv2.COLOR_BGR2RGB))
        return scryfall_id, whole, str(imagehash.phash(art, hash_size=PHASH_SIZE))
    except Exception as exc:  # noqa: BLE001 — one bad image shouldn't stop the run
        print(f"  ! {scryfall_id}: {exc}")
        return scryfall_id, None, None


def build_index(limit: Optional[int] = None, workers: int = 8, rehash: bool = False) -> None:
    from ..config import PHASH_HEX_LEN

    with db_session() as conn:
        if rehash:
            # Everything, regardless of what's already stored.
            rows = conn.execute(
                "SELECT id, image_small FROM scryfall_card WHERE image_small IS NOT NULL"
            ).fetchall()
        else:
            # Anything missing a hash, plus anything hashed at a different
            # size — those can't be compared against current fingerprints.
            rows = conn.execute(
                "SELECT id, image_small FROM scryfall_card "
                "WHERE image_small IS NOT NULL AND (phash IS NULL OR length(phash) != ?)",
                (PHASH_HEX_LEN,),
            ).fetchall()

    if limit:
        rows = rows[:limit]

    if not rows:
        print("Nothing to do — every card already has a hash.")
        return

    print(f"Hashing {len(rows)} card images with {workers} concurrent workers ...")
    done = 0
    with db_session() as conn, httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_and_hash, client, row["id"], row["image_small"]): row["id"]
                for row in rows
            }
            for future in as_completed(futures):
                scryfall_id, phash, art = future.result()
                if phash:
                    conn.execute(
                        "UPDATE scryfall_card SET phash = ?, art_phash = ? WHERE id = ?",
                        (phash, art, scryfall_id),
                    )
                done += 1
                if done % 200 == 0:
                    conn.commit()
                    print(f"  {done}/{len(rows)}")
        conn.commit()
    print(f"Done. {done} cards processed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local pHash match index from Scryfall art.")
    parser.add_argument("--limit", type=int, default=None, help="Only hash the first N un-hashed cards")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent downloads (be reasonable — this hits Scryfall's image CDN)")
    parser.add_argument("--rehash", action="store_true",
                        help="Recompute every hash, not just missing ones. Art already in data/art_cache "
                             "is reused, so this is CPU-bound rather than another full download.")
    args = parser.parse_args()
    build_index(limit=args.limit, workers=args.workers, rehash=args.rehash)


if __name__ == "__main__":
    main()
