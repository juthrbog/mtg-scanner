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
    PHASH_BITS,
    SCAN_CACHE_DIR,
    TOP_N_CANDIDATES,
    confidence_from_distance,
    match_verdict,
)
from ..db import get_db
from ..recognition import ocr as ocr_mod
from ..recognition.detect import card_candidates
from ..recognition.match import hash_frame, index
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

    # Save the crop that actually won, so the thumbnail shows what the match
    # was computed from rather than detection's first guess.
    scan_id = uuid.uuid4().hex[:12]
    image_path = SCAN_CACHE_DIR / f"{scan_id}.jpg"
    cv2.imwrite(str(image_path), crops[winner] if crops else frame)

    # Keep the frame detection ran *on*, alongside the crop it produced.
    # Without this a scan that goes wrong leaves only the wrong answer and no
    # way to reproduce it: tuning detection against saved crops means tuning
    # against its own output. These are the only real-world inputs there are,
    # so they are what any future change has to be measured on.
    cv2.imwrite(str(SCAN_CACHE_DIR / f"{scan_id}-frame.jpg"), frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 92])

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

    # Reading the printed name is not a tie-breaker on these captures, it is
    # the primary signal. Measured over 26 real frames the hash never once put
    # the correct card first — at any fingerprint size — because it compares a
    # photograph against Scryfall's render, and glare, warm light and a webcam
    # lens make that comparison close to uninformative. Reading the name
    # identified 21 of 25, and named a wrong card zero times.
    #
    # So OCR may now introduce candidates rather than only reorder them. It is
    # held to a stricter similarity for that, and stays silent when unsure.
    ocr_title = None
    if use_ocr and crops:
        for crop in crops[:3]:
            ocr_title = ocr_mod.read_title(crop)
            named = ocr_mod.find_by_name(conn, ocr_title)
            if named:
                # The hash is a poor way to pick the card but a fine way to
                # pick which *printing* of it is in shot — it only has to
                # separate a handful of images instead of a hundred thousand.
                query = hash_frame(crop)
                distances = index.distances_for(query, [n["card"]["id"] for n in named])
                for hit in named:
                    hit["distance"] = distances.get(hit["card"]["id"], PHASH_BITS)
                    hit["confidence"] = round(hit["name_score"] * 100)
                    hit["verdict"] = "Name"
                    hit["ocr_agrees"] = True
                named.sort(key=lambda h: h["distance"])

                seen = {h["card"]["id"] for h in named}
                candidates = named + [c for c in candidates if c["card"]["id"] not in seen]
                candidates = candidates[:TOP_N_CANDIDATES]
                break
        else:
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
            # A card identified by its printed name is not a weak match, even
            # though its hash distance is large — on a real photograph the
            # distance is large for the *right* card too, which is the whole
            # reason the name is being read.
            "low_confidence": not candidates or (
                candidates[0].get("verdict") != "Name"
                and candidates[0]["distance"] > HASH_MATCH_THRESHOLD
            ),
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
