"""Read a card's printed name, and find the card from it.

This began as a confirmer for the image hash and is now the primary signal,
because measured on real captures the hash does not work at all. Over 26
frames from an ordinary webcam the correct card was never the nearest
neighbour — not at 64-bit, 100-bit, 144-bit or 256-bit fingerprints, and not
with contrast or illumination normalisation. On a hand-cropped, pixel-accurate
warp of a plain black-bordered card the right answer still ranked 5367th of
111154. Perceptual hashing compares a photograph against Scryfall's *render*
of the same card, and glare, warm indoor light and a webcam lens leave too
little in common.

Reading the printed name identified 21 of 25 of those same frames, and named
a wrong card zero times — the failures were all silence.

The two signals still fail in different places, which is why both are kept.
OCR is either right or quiet; the hash degrades gracefully. So OCR may now
*introduce* candidates (`find_by_name`), held to a stricter similarity for the
privilege, while `rerank` remains for the case where it read something too
uncertain to look up but good enough to break a tie.
"""
from __future__ import annotations

import difflib
import re
from typing import List, Optional

import cv2
import numpy as np


_engine = None
_unavailable = False

# A name has to look this similar to a candidate before it counts as agreement.
NAME_SIMILARITY = 0.62


def available() -> bool:
    return not _unavailable


def _get_engine():
    """Load RapidOCR on first use — importing it costs a second or so."""
    global _engine, _unavailable
    if _engine is None and not _unavailable:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        except Exception:  # noqa: BLE001 — OCR is optional, never fatal
            _unavailable = True
    return _engine


def _prepare(image: np.ndarray) -> np.ndarray:
    """Upscale and normalise local contrast before reading.

    This is what makes OCR usable on a dim, softly focused webcam frame: on
    raw pixels the title was unreadable at that quality, and with CLAHE it
    came back.
    """
    up = cv2.resize(image, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    contrast = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    return cv2.cvtColor(contrast, cv2.COLOR_GRAY2BGR)


def read_title(card: np.ndarray) -> Optional[str]:
    """Best-effort read of the name from an already-deskewed card.

    Reads the *whole* card and takes the topmost line, rather than cropping to
    the title bar first. Measured on real captures, the narrow strip is much
    the worse input: the same Myojin that reads as "Myojin of Roaring Btades"
    from the full card comes back as "Myojn or KoJdrgBcue" from the strip.
    Detection is the reason — a crop that is a few percent off slices through
    the lettering, and a 42px-tall strip has no margin to lose, while the
    detector has plenty of room to be slightly wrong on a whole card. Feeding
    the detector's own output back through a second tight crop compounded both
    errors.
    """
    engine = _get_engine()
    if engine is None:
        return None
    try:
        result, _ = engine(_prepare(card))
    except Exception:  # noqa: BLE001
        return None
    if not result:
        return None
    # The name is the topmost text on a Magic card, in every frame layout.
    top = min(result, key=lambda item: min(point[1] for point in item[0]))
    return (top[1] or "").strip() or None


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def similarity(read: str, name: str) -> float:
    """How well an OCR read matches a card name, 0-1.

    Compares against the part before a comma too, so "Hama Pashar" still
    scores against "Hama Pashar, Ruin Seeker" when OCR clips the subtitle.
    """
    a = _normalise(read)
    if not a:
        return 0.0
    candidates = [name, name.split(",")[0]]
    return max(difflib.SequenceMatcher(None, a, _normalise(c)).ratio() for c in candidates)


# How close a read has to be to a real card name before it may *introduce*
# that card. Higher than NAME_SIMILARITY, which only governs reordering
# candidates the hash already proposed: a lookup that invents a card carries
# more risk than one that shuffles an existing list, so it has to be surer.
# Measured against the real captures, 0.72 is the point where the two reads
# that resolved to the wrong card ("ha,ScaFis" -> Pacifism at 0.62, a truncated
# "Sol'Kanar" -> Solfatara at 0.71) fall away while every correct read survives.
LOOKUP_SIMILARITY = 0.72


def _name_score(read: str, name: str) -> float:
    """Similarity that also rewards a correct but truncated read.

    OCR frequently clips a long name — "Sol'Kanar" for "Sol'Kanar the
    Tainted" — and plain sequence similarity punishes that hard enough to
    let an unrelated short name win. A read that is a clean prefix of a card
    name is strong evidence, so it is scored as such.
    """
    a, b = _normalise(read), _normalise(name)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) >= 6 and b.startswith(a):
        # Scaled by how much of the name was read, so a two-word prefix of a
        # long name still beats a coincidental full match on something short.
        ratio = max(ratio, 0.80 + 0.20 * (len(a) / len(b)))
    return ratio


def find_by_name(conn, read: Optional[str], limit: int = 8) -> List[dict]:
    """Printings whose card name matches an OCR read.

    This is the path that works when the hash cannot. Perceptual hashing
    compares a photograph against Scryfall's render of the same card, and on
    real captures — glare, warm indoor light, a phone-grade webcam — that
    comparison is close to uninformative: measured over 26 real frames the
    correct card was never the nearest neighbour, at any fingerprint size,
    while reading the printed name identified 21 of 25.

    Unlike `rerank`, this *introduces* cards, so it is deliberately stricter
    (see LOOKUP_SIMILARITY) and returns nothing rather than a poor guess.
    """
    if not read:
        return []
    folded = _normalise(read)
    if len(folded) < 4:
        return []

    rows = conn.execute(
        "SELECT DISTINCT name FROM scryfall_card WHERE phash IS NOT NULL"
    ).fetchall()
    scored = ((_name_score(read, r["name"]), r["name"]) for r in rows)
    best_score, best_name = max(scored, default=(0.0, None))
    if best_name is None or best_score < LOOKUP_SIMILARITY:
        return []

    printings = conn.execute(
        "SELECT id, name, set_name, set_code, rarity, collector_number, "
        "       image_small, image_normal "
        "FROM scryfall_card WHERE name = ? AND phash IS NOT NULL LIMIT ?",
        (best_name, limit),
    ).fetchall()
    return [{"card": row, "name_score": best_score} for row in printings]


def rerank(candidates: List[dict], title: Optional[str]) -> List[dict]:
    """Move the candidate whose name matches the OCR read to the front.

    Only reorders — the set of candidates is unchanged, so a bad read costs
    nothing beyond leaving the order as it was.
    """
    if not title or not candidates:
        return candidates
    for cand in candidates:
        cand["name_score"] = similarity(title, cand["card"]["name"])
    best = max(candidates, key=lambda c: c["name_score"])
    if best["name_score"] >= NAME_SIMILARITY:
        candidates.sort(key=lambda c: (-c["name_score"], c["distance"]))
        for cand in candidates:
            cand["ocr_agrees"] = cand is best
    return candidates
