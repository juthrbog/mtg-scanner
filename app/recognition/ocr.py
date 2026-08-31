"""Read a card's printed name and use it to check the image match.

The two signals fail in different places, which is the point of having both:
perceptual hashing degrades gracefully as a photo gets soft, while OCR is
either right or silent. Measured on progressively worse captures, OCR read the
title perfectly until the image went soft *and* dim, then returned nothing —
it does not quietly return a wrong name. That makes it a good confirmer: when
it reads something, it is worth trusting; when it doesn't, nothing is lost.

OCR therefore only ever *reorders* the candidates the hash already found. It
never introduces a card of its own, so a misread cannot invent a match.
"""
from __future__ import annotations

import difflib
import re
from typing import List, Optional

import cv2
import numpy as np

from .detect import title_strip

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


def _prepare(strip: np.ndarray) -> np.ndarray:
    """Upscale and normalise local contrast before reading.

    This is what makes OCR usable on a dim, softly focused webcam frame: on
    raw pixels the title was unreadable at that quality, and with CLAHE it
    came back.
    """
    up = cv2.resize(strip, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    contrast = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    return cv2.cvtColor(contrast, cv2.COLOR_GRAY2BGR)


def read_title(card: np.ndarray) -> Optional[str]:
    """Best-effort read of the name from an already-deskewed card."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        result, _ = engine(_prepare(title_strip(card)))
    except Exception:  # noqa: BLE001
        return None
    if not result:
        return None
    return " ".join(item[1] for item in result).strip() or None


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
