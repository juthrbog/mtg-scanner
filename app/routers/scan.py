"""Capture a card photo, match it against the local index, and let the user
confirm a match into the collection."""
from __future__ import annotations

import json
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from ..config import (
    HASH_MATCH_THRESHOLD,
    SCAN_CACHE_DIR,
    TOP_N_CANDIDATES,
    confidence_from_distance,
    match_verdict,
)
from ..db import get_db
from ..recognition import ocr as ocr_mod
from ..recognition.detect import art_window, card_candidates
from ..recognition.match import art_index, hash_frame, index
from .collection import collection_totals
from ..templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def scan_page(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "scan.html",
        {"index_size": len(index), "totals": collection_totals(conn), "oob": False},
    )


@router.post("/capture", response_class=HTMLResponse)
async def capture(
    request: Request,
    photo: UploadFile = File(...),
    corners: str = Form(None),
    use_art: bool = Form(False),
    use_ocr: bool = Form(False),
    conn=Depends(get_db),
):
    raw = await photo.read()
    frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

    # The browser sends the outline its live overlay settled on, when it has
    # one. It watched the card across many frames; this endpoint sees a single
    # still, so that outline is often the better answer.
    client_corners = None
    if corners:
        try:
            client_corners = json.loads(corners)
        except ValueError:
            client_corners = None

    # Detection can't reliably tell the card's outer edge from its inner
    # borders, so hand the matcher several plausible crops and let the best
    # score win rather than betting the scan on one guess.
    crops = card_candidates(frame, corners=client_corners)

    matches, winner = index.best_matches_multi_with_index(
        [hash_frame(c) for c in crops], top_n=TOP_N_CANDIDATES
    )

    # Matching on the illustration alone separates cards further apart than the
    # whole card does, but assumes a standard frame — so it is consulted
    # alongside the whole-card result, never instead of it, and whichever
    # scores closer wins.
    if use_art and len(art_index):
        art_matches, art_winner = art_index.best_matches_multi_with_index(
            [hash_frame(art_window(c)) for c in crops], top_n=TOP_N_CANDIDATES
        )
        if art_matches and (not matches or art_matches[0].distance < matches[0].distance):
            matches, winner = art_matches, art_winner

    # Save the crop that actually won, so the thumbnail shows what the match
    # was computed from rather than detection's first guess.
    scan_id = uuid.uuid4().hex[:12]
    image_path = SCAN_CACHE_DIR / f"{scan_id}.jpg"
    cv2.imwrite(str(image_path), crops[winner] if crops else frame)

    candidates = []
    if matches:
        placeholders = ",".join("?" * len(matches))
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, name, set_name, set_code, rarity, collector_number, image_small, image_normal "
                f"FROM scryfall_card WHERE id IN ({placeholders})",
                [m.scryfall_id for m in matches],
            ).fetchall()
        }
        for m in matches:
            card = rows.get(m.scryfall_id)
            if not card:
                continue
            candidates.append({
                "card": card,
                "distance": m.distance,
                "confidence": confidence_from_distance(m.distance),
                "verdict": match_verdict(m.distance),
            })

    # OCR can only reorder what the hash already found, so a misread can never
    # introduce a card that wasn't a visual match in the first place.
    ocr_title = None
    if use_ocr and crops:
        ocr_title = ocr_mod.read_title(crops[winner])
        candidates = ocr_mod.rerank(candidates, ocr_title)

    conn.execute(
        "INSERT INTO scan_event (matched_id, confidence, image_path, accepted) VALUES (?, ?, ?, NULL)",
        (
            candidates[0]["card"]["id"] if candidates else None,
            candidates[0]["confidence"] if candidates else None,
            str(image_path),
        ),
    )

    return templates.TemplateResponse(
        request,
        "partials/scan_result.html",
        {"candidates": candidates,
            "low_confidence": not candidates or candidates[0]["distance"] > HASH_MATCH_THRESHOLD,
            "ocr_title": ocr_title,
            "scan_image": f"/data/scans/{image_path.name}",
        },
    )


@router.post("/confirm", response_class=HTMLResponse)
def confirm(request: Request, scryfall_id: str = Form(...), foil: bool = Form(False), conn=Depends(get_db)):
    # A copy you already own is the same entry only if it's the same printing
    # *and* the same finish — a foil is worth differently from a non-foil, so
    # they're tracked separately rather than merged.
    existing = conn.execute(
        "SELECT id, quantity FROM collection_entry WHERE scryfall_id = ? AND foil = ?",
        (scryfall_id, int(foil)),
    ).fetchone()
    if existing:
        conn.execute("UPDATE collection_entry SET quantity = quantity + 1 WHERE id = ?", (existing["id"],))
        quantity = existing["quantity"] + 1
    else:
        conn.execute("INSERT INTO collection_entry (scryfall_id, foil) VALUES (?, ?)", (scryfall_id, int(foil)))
        quantity = 1

    conn.execute("UPDATE scan_event SET accepted = 1 WHERE matched_id = ?", (scryfall_id,))
    card = conn.execute("SELECT name, set_name FROM scryfall_card WHERE id = ?", (scryfall_id,)).fetchone()
    return templates.TemplateResponse(
        request,
        "partials/scan_confirmed.html",
        {"card": card, "quantity": quantity, "foil": foil,
         "totals": collection_totals(conn), "oob": True},
    )
