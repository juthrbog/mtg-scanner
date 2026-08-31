"""Locate the card in a captured frame and produce candidate crops to match against.

The client (scan.js) already crops the camera feed to a card-shaped region,
so this module's job is to tighten that up — strip any remaining background
without cutting into the card itself.

Rather than betting everything on one contour guess, `card_candidates()`
returns several plausible crops. The matcher hashes each and keeps whichever
scores best, so a single bad contour can't sink the scan.
"""
from __future__ import annotations

from typing import List

import cv2
import numpy as np

CARD_ASPECT = 88 / 63  # MTG card height:width (63x88mm)
# Size of the deskewed crop handed to the hasher and saved as the scan
# thumbnail. pHash downsamples internally (to 64x64 at PHASH_SIZE=16), so this
# mainly governs how much detail survives the warp — and how sharp the saved
# capture looks when you inspect a scan that went wrong.
OUTPUT_WIDTH = 480
OUTPUT_HEIGHT = int(OUTPUT_WIDTH * CARD_ASPECT)

# A detected quad is only believable as the card outline if its aspect ratio
# is close to a real card's. Without this check, contour detection happily
# locks onto the art box or text box *inside* the card and crops into it,
# throwing away the parts that make the card identifiable.
#
# The allowance is generous because a card viewed at an angle is foreshortened:
# its apparent ratio shrinks with the cosine of the tilt, so a strict gate
# rejects genuinely tilted cards before they ever reach the warp.
ASPECT_TOLERANCE = 0.32

# Smallest share of the frame a contour may cover and still be considered the
# card. The client now uploads the whole camera frame rather than a tight
# centre crop, so the card legitimately occupies a much smaller fraction of it
# than it used to — a high threshold here silently rejects every real card and
# falls back to hashing the entire picture, background and all.
MIN_AREA_FRACTION = 0.035

# How many of the largest contours to consider. The single biggest is often a
# shadow, a table edge, or a play mat rather than the card — and a real photo
# (a hand, a patterned wall, hair) produces far more large contours than a
# clean synthetic frame does, so this needs headroom. The area threshold ends
# the scan early anyway, so a high ceiling costs nothing on simple scenes.
MAX_CONTOURS_CONSIDERED = 40

# How many detected quads to hand back as candidates. The matcher scores each
# and keeps the best, so offering a couple of runners-up costs little.
MAX_QUAD_CANDIDATES = 2

# A card held near the edge of the view touches the frame border, and a
# contour that runs off the image never closes — detection then locks onto the
# frame edge instead of the card. Padding the image first gives every card a
# complete outline no matter where it sits. Measured: cards flush against the
# top edge went from 2/6 matched to 6/6.
BORDER_PAD_FRACTION = 0.06


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Sort 4 points into top-left, top-right, bottom-right, bottom-left."""
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)
    return np.array([
        pts[np.argmin(s)],
        pts[np.argmin(diff)],
        pts[np.argmax(s)],
        pts[np.argmax(diff)],
    ], dtype="float32")


def _warp(frame: np.ndarray, corners: np.ndarray) -> np.ndarray:
    dst = np.array([
        [0, 0],
        [OUTPUT_WIDTH - 1, 0],
        [OUTPUT_WIDTH - 1, OUTPUT_HEIGHT - 1],
        [0, OUTPUT_HEIGHT - 1],
    ], dtype="float32")
    matrix = cv2.getPerspectiveTransform(_order_corners(corners), dst)
    return cv2.warpPerspective(frame, matrix, (OUTPUT_WIDTH, OUTPUT_HEIGHT))


def _resize_whole(frame: np.ndarray) -> np.ndarray:
    return cv2.resize(frame, (OUTPUT_WIDTH, OUTPUT_HEIGHT))


# Fractions of a deskewed card taken up by the art window and by the title
# bar. Standard card frames only — full-art, showcase and older borders differ,
# which is why anything using these is a supplement to whole-card matching
# rather than a replacement for it.
ART_BOX = (0.10, 0.57, 0.07, 0.93)     # top, bottom, left, right
TITLE_BOX = (0.035, 0.098, 0.06, 0.80)


def _sub_box(card: np.ndarray, box) -> np.ndarray:
    top, bottom, left, right = box
    h, w = card.shape[:2]
    return card[int(h * top):int(h * bottom), int(w * left):int(w * right)]


def art_window(card: np.ndarray) -> np.ndarray:
    """The illustration window of an already-deskewed card.

    Matching on this alone separates cards better than the whole card does —
    measured, the gap between the right card and the nearest wrong one widened
    from 64 to 84 — because every card shares the same frame furniture and
    only the art is unique.
    """
    return cv2.resize(_sub_box(card, ART_BOX), (256, 256))


def title_strip(card: np.ndarray) -> np.ndarray:
    """The name bar of an already-deskewed card, for OCR."""
    return _sub_box(card, TITLE_BOX)


def _aspect_is_card_like(w: float, h: float) -> bool:
    if w <= 1 or h <= 1:
        return False
    # Accept either orientation; a sideways card is still a card, and the
    # warp will square it up.
    ratio = max(w, h) / min(w, h)
    return abs(ratio - CARD_ASPECT) <= ASPECT_TOLERANCE * CARD_ASPECT


def _quads_from_channel(channel: np.ndarray, frame_area: float, limit: int) -> List[np.ndarray]:
    """Card-shaped outlines found in one single-channel image."""
    blurred = cv2.GaussianBlur(channel, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.dilate(edges, None, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    found: List[np.ndarray] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:MAX_CONTOURS_CONSIDERED]:
        if cv2.contourArea(contour) < frame_area * MIN_AREA_FRACTION:
            break  # sorted by area, so everything after this is smaller too

        rect = cv2.minAreaRect(contour)
        (_, (rw, rh), _) = rect
        if not _aspect_is_card_like(rw, rh):
            continue

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            found.append(approx.reshape(4, 2).astype("float32"))
        else:
            found.append(cv2.boxPoints(rect).astype("float32"))

        if len(found) >= limit:
            break
    return found


def _detect_quads(frame: np.ndarray, limit: int = MAX_QUAD_CANDIDATES, pad: int = 0) -> List[np.ndarray]:
    """Find believable card outlines, largest first.

    Runs edge detection twice, over two different channels, because they fail
    in different situations:

      * **Brightness** is the natural choice and works on an ordinary desk.
      * **Saturation** carries the card when brightness cannot. Glare adds
        white light, which is nearly colourless, so a reflection that erases
        the card's edges in the grey channel leaves them intact in saturation
        — measured, a glare patch that pushed grey-channel detection 43px off
        left saturation detection accurate to 2px.

    Both sets are returned. The matcher scores every candidate and keeps the
    best, so an extra wrong guess costs a few milliseconds while a right one
    rescues the scan.

    `pad` is the border width to add around the frame so a card touching the
    edge of the view still forms a closed contour. Each channel is padded with
    *its own* median: padding the colour image first and deriving saturation
    from that gives the border a saturation of its own, which merges with the
    background and swallows the card.

    Returned corners are in the coordinate space of the padded image, matching
    what `card_candidates` warps from.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[:, :, 1]

    frame_area = frame.shape[0] * frame.shape[1]

    found: List[np.ndarray] = []
    for channel in (gray, saturation):
        if pad:
            channel = cv2.copyMakeBorder(
                channel, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=int(np.median(channel))
            )
        found.extend(_quads_from_channel(channel, frame_area, limit))
    return found


def _center_crop(frame: np.ndarray) -> np.ndarray:
    """Centre crop to card proportions.

    This reproduces the crop the browser used to apply before uploading, kept
    as a candidate so a well-centred card can never match worse than it did
    before the switch to full-frame capture.
    """
    h, w = frame.shape[:2]
    target_w_h = 1 / CARD_ASPECT  # width:height
    if w / h > target_w_h:
        cw = int(h * target_w_h)
        x0 = (w - cw) // 2
        cropped = frame[:, x0:x0 + cw]
    else:
        chh = int(w / target_w_h)
        y0 = (h - chh) // 2
        cropped = frame[y0:y0 + chh, :]
    return _resize_whole(cropped)


def warp_from_corners(frame: np.ndarray, corners) -> np.ndarray | None:
    """Deskew using corners supplied by the caller (the browser's live overlay).

    Returns None if the corners aren't four sane in-frame points, so a
    malformed payload falls back to server-side detection instead of raising.
    """
    try:
        pts = np.array(corners, dtype="float32").reshape(4, 2)
    except Exception:  # noqa: BLE001 — any malformed shape is simply unusable
        return None

    h, w = frame.shape[:2]
    margin = 0.25 * max(h, w)  # allow a little overshoot, reject nonsense
    if not np.all((pts[:, 0] > -margin) & (pts[:, 0] < w + margin)
                  & (pts[:, 1] > -margin) & (pts[:, 1] < h + margin)):
        return None
    if cv2.contourArea(pts) < h * w * MIN_AREA_FRACTION:
        return None
    return _warp(frame, pts)


def card_candidates(frame: np.ndarray, corners=None) -> List[np.ndarray]:
    """Return plausible card crops for the matcher to score.

    The browser now uploads the whole camera frame, so locating the card is
    entirely this module's job. Rather than commit to one guess, hand back
    several and let the matcher keep whichever scores best:

      1. each detected card-shaped outline, deskewed
      2. a centre crop at card proportions — what the browser used to upload,
         so a well-centred card can't do worse than it did before
      3. a slightly inset centre crop, for a card rimmed by a sliver of
         background or a sleeve edge too soft to detect as an edge

    The whole frame is deliberately *not* offered: at full-frame scale it is
    mostly desk, and hashing that only invites a confident wrong answer.
    """
    candidates: List[np.ndarray] = []

    # The browser's outline first, when it sent one: it was derived from many
    # frames of video rather than this single still.
    if corners is not None:
        from_client = warp_from_corners(frame, corners)
        if from_client is not None:
            candidates.append(from_client)

    # Detection pads internally so a card touching the edge of the view still
    # forms a closed contour, and returns corners in that padded space — so
    # warp from an identically padded colour frame.
    h, w = frame.shape[:2]
    pad = int(min(h, w) * BORDER_PAD_FRACTION)
    fill = int(np.median(frame))
    padded = cv2.copyMakeBorder(frame, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(fill, fill, fill))

    for quad in _detect_quads(frame, pad=pad):
        candidates.append(_warp(padded, quad))

    candidates.append(_center_crop(frame))

    inset = int(min(h, w) * 0.04)
    if h - 2 * inset > 10 and w - 2 * inset > 10:
        candidates.append(_center_crop(frame[inset:h - inset, inset:w - inset]))

    return candidates


def find_card(frame: np.ndarray) -> np.ndarray:
    """Single best-guess crop. Kept for callers that want just one image."""
    return card_candidates(frame)[0]
