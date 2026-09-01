"""Match a captured card image against the local pHash index.

The index is loaded into memory once (reload() is called on app startup).
Hashes are kept as a packed numpy bit-matrix so comparing one query against
all ~111k cards is a single vectorised XOR + popcount rather than a Python
loop — fast enough to afford trying several candidate crops per scan.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import cv2
import imagehash
import numpy as np
from PIL import Image

from ..config import PHASH_HEX_LEN, PHASH_SIZE
from ..db import get_connection

# Number of bits set in each possible byte value — turns popcount into a
# table lookup over the XOR result.
_POPCOUNT = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1).sum(axis=1).astype(np.uint16)


@dataclass
class MatchCandidate:
    scryfall_id: str
    distance: int


def _hash_to_bytes(h: imagehash.ImageHash) -> np.ndarray:
    return np.packbits(h.hash.flatten())


class HashIndex:
    def __init__(self) -> None:
        self._ids: List[str] = []
        self._matrix: np.ndarray | None = None  # shape (n_cards, n_bytes), uint8
        self._pos: dict | None = None  # scryfall id -> row, built lazily
        self.stale = 0  # hashes skipped because they were stored at another size

    def reload(self, column: str = "phash") -> int:
        conn = get_connection()
        try:
            rows = conn.execute(
                f"SELECT id, {column} AS phash FROM scryfall_card WHERE {column} IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        # Hashes stored at a different PHASH_SIZE can't be compared with
        # freshly computed ones, so drop them rather than silently mixing
        # incompatible fingerprints. `stale` is reported at startup.
        usable = [r for r in rows if len(r["phash"]) == PHASH_HEX_LEN]
        self.stale = len(rows) - len(usable)

        self._ids = [r["id"] for r in usable]
        if usable:
            self._matrix = np.vstack([_hash_to_bytes(imagehash.hex_to_hash(r["phash"])) for r in usable])
        else:
            self._matrix = None
        self._pos = None
        return len(self._ids)

    def __len__(self) -> int:
        return len(self._ids)

    def _distances(self, query: imagehash.ImageHash) -> np.ndarray:
        assert self._matrix is not None
        return _POPCOUNT[np.bitwise_xor(self._matrix, _hash_to_bytes(query))].sum(axis=1)

    def distances_for(self, query: imagehash.ImageHash, scryfall_ids) -> dict:
        """Distance from one query to specific cards.

        Used to order the printings of a card the OCR named: the hash is a
        poor way to pick the card but a serviceable way to pick which printing
        of it is in front of the camera, because it is only separating a
        handful of images instead of a hundred thousand.
        """
        if self._matrix is None:
            return {}
        if self._pos is None:
            self._pos = {sid: i for i, sid in enumerate(self._ids)}
        distances = self._distances(query)
        return {sid: int(distances[self._pos[sid]])
                for sid in scryfall_ids if sid in self._pos}

    def best_matches(self, query: imagehash.ImageHash, top_n: int = 3) -> List[MatchCandidate]:
        if self._matrix is None:
            return []
        distances = self._distances(query)
        top_idx = np.argpartition(distances, min(top_n, len(distances) - 1))[:top_n]
        top_idx = top_idx[np.argsort(distances[top_idx])]
        return [MatchCandidate(self._ids[i], int(distances[i])) for i in top_idx]

    def best_matches_multi(self, queries: Sequence[imagehash.ImageHash], top_n: int = 3) -> List[MatchCandidate]:
        """Match several candidate crops and rank using whichever crop fit best.

        Detection can't always tell the card's outer edge from its inner
        borders, so we try each crop and pick the one whose closest match is
        closest overall — a crop that cut into the card matches nothing well
        and loses. Ranking then happens *within* that single winning crop:
        pooling distances across crops would pull every card's score down,
        not just the right one, and wash out the discrimination.
        """
        matches, _ = self.best_matches_multi_with_index(queries, top_n)
        return matches

    def best_matches_multi_with_index(
        self, queries: Sequence[imagehash.ImageHash], top_n: int = 3
    ) -> tuple[List[MatchCandidate], int]:
        """As above, but also reports *which* crop won.

        The caller saves that crop as the scan thumbnail, so what you see when
        a scan goes wrong is the image the match was actually computed from —
        not merely the first thing detection guessed.
        """
        if self._matrix is None or not queries:
            return [], 0
        per_crop = [self._distances(q) for q in queries]
        winner = min(range(len(per_crop)), key=lambda i: per_crop[i].min())
        best = per_crop[winner]
        top_idx = np.argpartition(best, min(top_n, len(best) - 1))[:top_n]
        top_idx = top_idx[np.argsort(best[top_idx])]
        return [MatchCandidate(self._ids[i], int(best[i])) for i in top_idx], winner


# One shared, process-wide index — rebuilt at startup in app/main.py.
index = HashIndex()


def hash_frame(frame_bgr: np.ndarray) -> imagehash.ImageHash:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return imagehash.phash(Image.fromarray(rgb), hash_size=PHASH_SIZE)
