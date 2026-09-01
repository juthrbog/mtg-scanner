"""Locate the card in a captured frame and produce candidate crops to match against.

The browser uploads the whole camera frame, so finding the card is entirely
this module's job. Rather than betting everything on one guess,
`card_candidates()` returns several plausible crops; the matcher hashes each
and keeps whichever scores best, so one bad outline can't sink the scan.

Two searches run over the same frame because they fail in opposite conditions:

* **Contour following** needs the card's boundary to be closed. That holds for
  a black-bordered card on a plain surface, where it is also the more precise
  of the two.
* **Line fitting** does not. A borderless or full-art card held in the hand has
  a boundary that is straight but broken, and measured on real captures no
  four-sided contour was found around any of them — every quad the earlier code
  returned came from `minAreaRect` on some unrelated blob.

Candidates from both are then *scored* rather than ordered by size. Preferring
the largest is what made detection return the camera frame itself: with a
borderless card the frame outline was the only strong closed contour, and
nothing in the old gates excluded it.
"""
from __future__ import annotations

import itertools
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
# Stated as an explicit range rather than a tolerance band, because the band
# form hid a bug worth remembering. It was written as ±32% around 1.397, which
# works out to 0.95–1.84; since the ratio is computed as max/min it can never
# be below 1.0, so the gate really accepted 1.00–1.84 — every camera frame,
# every torso, every face. Detection duly returned the frame itself whenever a
# card's own outline was faint, which is exactly what borderless cards do.
#
# The range still has to allow foreshortening: tilting a card about its long
# axis squashes the height and the ratio falls, about its short axis the width
# goes and it rises. cos(40°) = 0.77 bounds both directions.
ASPECT_MIN = 1.12
ASPECT_MAX = 1.85

# A card held up to a webcam fills perhaps a third of the view. Anything
# covering most of the frame is the frame, the wall, or the person holding it.
# This is the ceiling the old code lacked entirely.
MAX_AREA_FRACTION = 0.72

# How much of a quad's outline must sit on a real intensity edge, on its
# *weakest* side. This is the test that separates a card from a torso: both
# are roughly rectangular and roughly the right size, but a spurious quad is
# only supported on the two or three sides that happen to follow a shadow,
# whereas a card has all four. Measured on real captures below.
EDGE_SUPPORT_MIN = 0.12

# Line-based detection: how many of the longest lines to keep per direction,
# and how long a line must be to count. Every pair from one direction is
# crossed with every pair from the other, so cost is quartic in this — 12 gives
# 66 pairs a side and 4356 candidate quads at ~170ms a frame. Measured on the
# real captures: 8 costs 81ms and locates the card to 6.9%, 12 costs 173ms for
# 3.7%, and 14 and 18 buy nothing further while costing 289ms and 694ms. The
# cheap geometric gates reject almost every candidate before the expensive
# edge-support test runs, which is what keeps this affordable at all.
HOUGH_MAX_LINES = 12
HOUGH_MIN_LENGTH_FRACTION = 0.18

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
# and keeps the best, so offering runners-up costs little — and now that quads
# are ranked by how card-like they are rather than by size, the right one is
# usually at or near the top instead of buried behind the frame outline.
MAX_QUAD_CANDIDATES = 4

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


def _quad_aspect(quad: np.ndarray) -> float:
    """Aspect from the quad's own sides rather than its bounding box.

    A tilted card's bounding box is close to square even when the card itself
    is plainly card-shaped, so measuring the box throws away the very signal
    the gate depends on.
    """
    q = _order_corners(quad)
    width = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    height = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    if width < 2 or height < 2:
        return 0.0
    return float(max(width, height) / min(width, height))


def _corner_regularity(quad: np.ndarray) -> float:
    """1.0 when every corner is square, falling to 0 by 45° of skew."""
    q = _order_corners(quad)
    worst = 0.0
    for i in range(4):
        a, b, c = q[(i - 1) % 4], q[i], q[(i + 1) % 4]
        v1, v2 = a - b, c - b
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 < 2 or n2 < 2:
            return 0.0
        cos = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
        worst = max(worst, abs(np.degrees(np.arccos(cos)) - 90.0))
    return max(0.0, 1.0 - worst / 45.0)


def _gradient(channel: np.ndarray) -> np.ndarray:
    """Edge strength, scaled so the threshold means the same on any image.

    Normalised against a high percentile rather than the maximum: a single
    specular highlight sets the maximum and would push every real edge down to
    a fraction of full scale.
    """
    gx = cv2.Scharr(channel, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(channel, cv2.CV_32F, 0, 1)
    mag = cv2.magnitude(gx, gy)
    scale = float(np.percentile(mag, 99)) or 1.0
    return np.clip(mag / scale, 0.0, 1.0)


def _edge_support(quad: np.ndarray, mag: np.ndarray, samples: int = 32) -> float:
    """How well the quad's weakest side is backed by an actual edge.

    The decisive test for a card with no black border. A quad around the frame,
    a forearm or a shadow is supported on the sides that happen to follow
    something and unsupported on the rest; a card is supported all the way
    round. Scoring the weakest side is what tells them apart — an average would
    let three strong sides carry one that rests on nothing.
    """
    q = _order_corners(quad)
    h, w = mag.shape[:2]
    weakest = 1.0
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        # Skip the corners: they are the least reliably placed part of a quad.
        ts = np.linspace(0.08, 0.92, samples)[:, None]
        pts = a[None, :] + (b - a)[None, :] * ts
        xs = np.clip(pts[:, 0].astype(int), 1, w - 2)
        ys = np.clip(pts[:, 1].astype(int), 1, h - 2)
        # Strongest response in a 3x3 window, so an edge a pixel or two off the
        # fitted line still counts as support.
        best = np.zeros(len(ts), dtype=np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                best = np.maximum(best, mag[ys + dy, xs + dx])
        weakest = min(weakest, float(best.mean()))
    return weakest


def _is_frame_outline(quad: np.ndarray, padded_shape, pad: int) -> bool:
    """True if this quad is just the edge of the padded image.

    Padding with a flat colour is what lets a card at the edge of the view
    close its outline, but it necessarily draws a perfect rectangle around the
    whole picture — larger than any card and, before quads were scored, always
    the winner. A real card can have a corner or two out at the boundary; all
    four means it is the boundary.
    """
    h, w = padded_shape[:2]
    q = np.asarray(quad, dtype=np.float32)
    slack = max(4.0, pad * 0.35)
    on_edge = (
        (np.abs(q[:, 0] - pad) < slack) | (np.abs(q[:, 0] - (w - pad)) < slack)
    ) & (
        (np.abs(q[:, 1] - pad) < slack) | (np.abs(q[:, 1] - (h - pad)) < slack)
    )
    return bool(on_edge.sum() >= 4)


def _score_quad(quad: np.ndarray, mag: np.ndarray, frame_area: float,
                pad: int, padded_shape) -> float | None:
    """How card-like a quad is, or None if it is not a plausible card at all."""
    area = abs(cv2.contourArea(quad.astype(np.float32)))
    if not (frame_area * MIN_AREA_FRACTION <= area <= frame_area * MAX_AREA_FRACTION):
        return None
    aspect = _quad_aspect(quad)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return None
    if _is_frame_outline(quad, padded_shape, pad):
        return None
    regularity = _corner_regularity(quad)
    if regularity < 0.25:
        return None
    support = _edge_support(quad, mag)
    if support < EDGE_SUPPORT_MIN:
        return None

    rect = cv2.minAreaRect(quad.astype(np.float32))
    rect_area = rect[1][0] * rect[1][1]
    fill = min(area / rect_area, 1.0) if rect_area > 1 else 0.0
    closeness = 1.0 - min(abs(aspect - CARD_ASPECT) / 0.45, 1.0)

    # Prefer the larger candidate, saturating at a plausible card size. A card
    # is full of smaller rectangles — the text box, the art window, the mana
    # cost — and they are *more* perfectly card-shaped than the card itself,
    # which is bounded by rounded corners and a hand. Without this a 7%-of-frame
    # inner box outranked the real outline. Note this is not the old
    # "biggest wins" ordering: it only breaks ties between quads that already
    # passed the aspect, size, frame and edge-support gates, and it stops
    # rewarding growth well before the ceiling.
    size = min(area / (frame_area * 0.30), 1.0)

    # Edge support carries the most weight: it is the only term that checks the
    # quad against the image rather than against its own geometry.
    return 2.0 * support + 1.2 * closeness + 1.0 * size + 0.8 * regularity + 0.6 * fill


def _channel_edges(channel: np.ndarray) -> np.ndarray:
    """Edges in one channel, from two thresholdings.

    One threshold pair cannot serve both cases: fixed values find the crisp
    outline of a black-bordered card, while thresholds derived from the image
    median are what pick up the low-contrast boundary of a borderless card
    lying against skin or a pale wall.
    """
    blurred = cv2.GaussianBlur(channel, (5, 5), 0)
    median = float(np.median(blurred))
    return cv2.Canny(blurred, 40, 120) | cv2.Canny(
        blurred, int(max(0, 0.66 * median)), int(min(255, 1.33 * median))
    )


def _quads_from_contours(edges: np.ndarray, mag: np.ndarray, frame_area: float,
                         pad: int, shape) -> List[tuple]:
    """Scored outlines from contour following.

    Works when the card's boundary is closed — a bordered card on a plain
    surface. It is kept alongside the line-based search below because when it
    does work it is the more precise of the two.
    """
    # One dilation, not two: two closes the gap between a card and the hand
    # holding it, merging them into a single blob whose outline is neither.
    edges = cv2.dilate(edges, None, iterations=1)
    # RETR_LIST rather than RETR_EXTERNAL — a borderless card whose edge merges
    # with the background is not an outermost contour, so asking only for
    # outermost ones is asking for everything except the card.
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    scored: List[tuple] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:MAX_CONTOURS_CONSIDERED]:
        if cv2.contourArea(contour) < frame_area * MIN_AREA_FRACTION:
            break  # sorted by area, so everything after this is smaller too

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            quad = approx.reshape(4, 2).astype("float32")
        else:
            quad = cv2.boxPoints(cv2.minAreaRect(contour)).astype("float32")

        score = _score_quad(quad, mag, frame_area, pad, shape)
        if score is not None:
            scored.append((score, quad))
    return scored


def _to_line(x1: float, y1: float, x2: float, y2: float):
    """A segment as the coefficients of the infinite line through it."""
    a, b = y2 - y1, x1 - x2
    return a, b, a * x1 + b * y1


def _intersect(l1, l2):
    a1, b1, c1 = l1
    a2, b2, c2 = l2
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-6:
        return None  # parallel
    return ((b2 * c1 - b1 * c2) / det, (a1 * c2 - a2 * c1) / det)


def _quads_from_lines(edges: np.ndarray, mag: np.ndarray, frame_area: float,
                      pad: int, shape) -> List[tuple]:
    """Scored outlines rebuilt from long straight lines.

    This is the case contour following cannot reach. Following a contour needs
    the boundary to be *closed*, and a borderless card against skin has a
    boundary that is straight but broken — measured on real captures, no
    four-sided contour was found around any of them, and every quad the old
    code returned came from `minAreaRect` on some other blob entirely.

    A Hough transform does not care about closure: every fragment of an edge
    votes for the same infinite line, so four dashed-looking sides still
    produce four strong lines. Taking two lines from each of the two dominant
    directions and intersecting them reconstructs the quad.
    """
    height, width = edges.shape[:2]
    min_len = int(HOUGH_MIN_LENGTH_FRACTION * min(height, width))
    segments = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=55,
                               minLineLength=min_len, maxLineGap=18)
    if segments is None:
        return []
    segments = segments.reshape(-1, 4).astype(np.float64)

    angles = np.degrees(np.arctan2(segments[:, 3] - segments[:, 1],
                                   segments[:, 2] - segments[:, 0])) % 180
    lengths = np.hypot(segments[:, 2] - segments[:, 0], segments[:, 3] - segments[:, 1])

    # Length-weighted dominant direction, computed on doubled angles so that
    # 1° and 179° — the same direction — average to 0° instead of 90°.
    doubled = np.radians(angles * 2)
    dominant = np.degrees(np.arctan2((lengths * np.sin(doubled)).sum(),
                                     (lengths * np.cos(doubled)).sum())) / 2 % 180

    def offset(angle: float, reference: float) -> float:
        d = abs(angle - reference) % 180
        return min(d, 180 - d)

    def family(reference: float) -> list:
        members = [i for i in range(len(segments)) if offset(angles[i], reference) < 30]
        members.sort(key=lambda i: -lengths[i])
        return [_to_line(*segments[i]) for i in members[:HOUGH_MAX_LINES]]

    side_a = family(dominant)
    side_b = family((dominant + 90) % 180)

    scored: List[tuple] = []
    for a1, a2 in itertools.combinations(side_a, 2):
        for b1, b2 in itertools.combinations(side_b, 2):
            corners = [_intersect(a, b) for a in (a1, a2) for b in (b1, b2)]
            if any(c is None for c in corners):
                continue
            # Ordered round the quad, not in the order the pairs produced.
            quad = np.array([corners[0], corners[1], corners[3], corners[2]],
                            dtype="float32")
            if not np.all(np.isfinite(quad)):
                continue
            score = _score_quad(quad, mag, frame_area, pad, shape)
            if score is not None:
                scored.append((score, quad))
    return scored


def _channels(frame: np.ndarray) -> List[np.ndarray]:
    """The single-channel views detection searches, and why each is there.

      * **Brightness** is the natural choice and works on an ordinary desk.
      * **Saturation** carries the card when brightness cannot. Glare adds
        white light, which is nearly colourless, so a reflection that erases
        the card's edges in the grey channel leaves them intact in saturation
        — measured, a glare patch that pushed grey-channel detection 43px off
        left saturation detection accurate to 2px.
      * **Lab a and b** separate by hue where the other two see nothing. A
        borderless card held in the hand is the hard case precisely because it
        has no dark rim to find, but skin against green or blue card art is a
        large, clean step in a/b even when brightness and saturation are
        nearly continuous across the boundary.
    """
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    return [
        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
        cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[:, :, 1],
        lab[:, :, 1],
        lab[:, :, 2],
    ]


def _detect_quads(frame: np.ndarray, limit: int = MAX_QUAD_CANDIDATES, pad: int = 0) -> List[np.ndarray]:
    """Find believable card outlines, most card-like first.

    Every channel's candidates are scored on the same scale and ranked
    together, rather than each channel contributing its own largest few. Size
    is deliberately not the ranking: the biggest quad in a handheld shot is
    reliably the frame, the wall, or the person, and ordering by area is what
    made detection return those.

    `pad` is the border width to add around the frame so a card touching the
    edge of the view still forms a closed contour. Each channel is padded with
    *its own* median: padding the colour image first and deriving saturation
    from that gives the border a saturation of its own, which merges with the
    background and swallows the card.

    Returned corners are in the coordinate space of the padded image, matching
    what `card_candidates` warps from.
    """
    frame_area = frame.shape[0] * frame.shape[1]

    per_channel: List[np.ndarray] = []
    union: np.ndarray | None = None
    strongest: np.ndarray | None = None
    for channel in _channels(frame):
        if pad:
            channel = cv2.copyMakeBorder(
                channel, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=int(np.median(channel))
            )
        edges = _channel_edges(channel)
        per_channel.append(edges)
        union = edges if union is None else (union | edges)
        gradient = _gradient(cv2.GaussianBlur(channel, (5, 5), 0))
        strongest = gradient if strongest is None else np.maximum(strongest, gradient)

    shape = per_channel[0].shape
    scored: List[tuple] = []
    for edges in per_channel:
        scored.extend(_quads_from_contours(edges, strongest, frame_area, pad, shape))
    # Lines are searched over the union: a card whose left edge only shows in
    # saturation and whose top only shows in Lab-a still has four sides here,
    # and it is the four together that make the quad.
    scored.extend(_quads_from_lines(union, strongest, frame_area, pad, shape))

    scored.sort(key=lambda s: s[0], reverse=True)

    # Different channels routinely find the same card. Keep the best-scoring
    # version of each distinct outline so the candidate slots go to genuinely
    # different guesses instead of four copies of one.
    kept: List[np.ndarray] = []
    for _, quad in scored:
        centre = _order_corners(quad).mean(axis=0)
        if any(np.linalg.norm(centre - _order_corners(k).mean(axis=0)) < 0.05 * max(frame.shape[:2])
               for k in kept):
            continue
        kept.append(quad)
        if len(kept) >= limit:
            break
    return kept


# --- searching for the card's shape directly -----------------------------
#
# Everything above hunts for edges and then asks whether what it found happens
# to be card-shaped. This does the opposite: it scans the one shape we are
# looking for — a 63:88 rectangle — over position, size and rotation, and
# scores how well its outline sits on image edges.
#
# That inversion is what makes it steady. Assembling a quad from detected lines
# is all-or-nothing: lose one faint edge and the quad is a different shape
# entirely, which is why the outline jumped between frames and sometimes landed
# on things that were not card-shaped at all. Measured across six
# near-identical frames — a few pixels of camera shake, a little noise — the
# line-and-contour search moved its answer by 53px on average and up to 105px,
# and found nothing at all in 2 frames of 36. This moves by 8px and never
# comes back empty.
#
# It is also about 25x cheaper, because for a fixed size and rotation the score
# for *every* position is four integral-image lookups, which is array slicing
# rather than a loop over candidates.

POSE_WORK_WIDTH = 320
POSE_ANGLES = np.arange(-30, 31, 5)          # how far a hand-held card tilts
POSE_HEIGHT_FRACTIONS = np.linspace(0.28, 0.95, 14)
POSE_BAND = 3                                 # thickness of the sampled edge band

# A card is full of smaller rectangles — the text box, the art window — whose
# edges are crisper than the card's own outline against skin. Without a pull
# toward the larger fit the search settles on one of them.
POSE_SIZE_WEIGHT = 0.15

# Below this, the best pose is not resting on anything and is not offered.
POSE_MIN_SCORE = 0.04


def _axis_energy(frame: np.ndarray) -> tuple:
    """Edge strength split by direction, strongest response across channels.

    Split because it is what makes the search specific to a card rather than to
    rectangles in general: horizontal edges are scored with |d/dy| and vertical
    ones with |d/dx|, so a line of rules text running parallel to the card's
    top edge cannot support it.
    """
    gx = gy = None
    for channel in _channels(frame):
        blurred = cv2.GaussianBlur(channel, (5, 5), 0)
        ax = np.abs(cv2.Scharr(blurred, cv2.CV_32F, 1, 0))
        ay = np.abs(cv2.Scharr(blurred, cv2.CV_32F, 0, 1))
        gx = ax if gx is None else np.maximum(gx, ax)
        gy = ay if gy is None else np.maximum(gy, ay)

    def scale(g):
        return np.clip(g / (float(np.percentile(g, 99)) or 1.0), 0.0, 1.0)

    return scale(gx), scale(gy)


def _best_position(ii_x, ii_y, h: int, w: int, band: int):
    """Weakest-side score at every top-left position, as one array op."""
    ny, nx = ii_x.shape[0] - h, ii_x.shape[1] - w
    if ny <= 0 or nx <= 0:
        return None, None

    def rect(ii, y0, y1, x0, x1):
        return (ii[y1:y1 + ny, x1:x1 + nx] - ii[y0:y0 + ny, x1:x1 + nx]
                - ii[y1:y1 + ny, x0:x0 + nx] + ii[y0:y0 + ny, x0:x0 + nx])

    top = rect(ii_y, 0, band, 0, w) / (w * band)
    bottom = rect(ii_y, h - band, h, 0, w) / (w * band)
    left = rect(ii_x, 0, h, 0, band) / (h * band)
    right = rect(ii_x, 0, h, w - band, w) / (h * band)
    # Weakest side, for the same reason `_edge_support` uses it: three strong
    # sides must not carry a fourth resting on nothing.
    score = np.minimum(np.minimum(top, bottom), np.minimum(left, right))
    flat = int(np.argmax(score))
    return float(score.flat[flat]), divmod(flat, nx)


_POSE_OFFSETS = np.arange(-9, 10, 1.5)
_POSE_SLOPES = np.linspace(-0.14, 0.14, 9)


def _refine_edge(energy: np.ndarray, fixed: float, lo: float, hi: float, horizontal: bool):
    """Slide and tilt one edge onto the strongest response.

    The search fits a rigid rectangle, but a card held in the hand is seen in
    perspective and its edges are not parallel. Refining each edge separately
    turns the rectangle back into the quadrilateral actually on screen.
    """
    span = lo + (hi - lo) * np.linspace(0.06, 0.94, 40)
    centre = (lo + hi) / 2
    height, width = energy.shape
    best, chosen = -1.0, (0.0, 0.0)
    for offset in _POSE_OFFSETS:
        for slope in _POSE_SLOPES:
            moving = fixed + offset + slope * (span - centre)
            xs, ys = (span, moving) if horizontal else (moving, span)
            value = energy[np.clip(ys.astype(np.int32), 0, height - 1),
                           np.clip(xs.astype(np.int32), 0, width - 1)].mean()
            if value > best:
                best, chosen = float(value), (float(offset), float(slope))
    offset, slope = chosen
    return (fixed + offset + slope * (lo - centre),
            fixed + offset + slope * (hi - centre)), best


def _edge_line(p, q):
    (x1, y1), (x2, y2) = p, q
    a, b = y2 - y1, x1 - x2
    return a, b, a * x1 + b * y1


def _refine_pose(gx, gy, x: int, y: int, w: int, h: int):
    (t1, t2), st = _refine_edge(gy, y, x, x + w, True)
    (b1, b2), sb = _refine_edge(gy, y + h, x, x + w, True)
    (l1, l2), sl = _refine_edge(gx, x, y, y + h, False)
    (r1, r2), sr = _refine_edge(gx, x + w, y, y + h, False)

    top = _edge_line((x, t1), (x + w, t2))
    bottom = _edge_line((x, b1), (x + w, b2))
    left = _edge_line((l1, y), (l2, y + h))
    right = _edge_line((r1, y), (r2, y + h))
    corners = [_intersect(top, left), _intersect(top, right),
               _intersect(bottom, right), _intersect(bottom, left)]
    if any(c is None for c in corners):
        return None, 0.0
    return np.array(corners, dtype="float32"), min(st, sb, sl, sr)


def card_pose(frame: np.ndarray) -> np.ndarray | None:
    """Corners of the best-fitting card-shaped rectangle, or None."""
    height0, width0 = frame.shape[:2]
    scale = POSE_WORK_WIDTH / width0
    small = cv2.resize(frame, (POSE_WORK_WIDTH, max(1, int(height0 * scale))))
    gx, gy = _axis_energy(small)
    h_s, w_s = gx.shape

    best_rank, best = -1.0, None
    for angle in POSE_ANGLES:
        rot = cv2.getRotationMatrix2D((w_s / 2, h_s / 2), float(angle), 1.0)
        rx = cv2.warpAffine(gx, rot, (w_s, h_s))
        ry = cv2.warpAffine(gy, rot, (w_s, h_s))
        ii_x, ii_y = cv2.integral(rx), cv2.integral(ry)
        for fraction in POSE_HEIGHT_FRACTIONS:
            h = int(h_s * fraction)
            w = int(h / CARD_ASPECT)
            if w < 24 or w >= w_s or h >= h_s:
                continue
            score, position = _best_position(ii_x, ii_y, h, w, POSE_BAND)
            if score is None:
                continue
            rank = score + POSE_SIZE_WEIGHT * fraction
            if rank > best_rank:
                best_rank, best = rank, (float(angle), position[0], position[1], w, h, score)

    if best is None or best[5] < POSE_MIN_SCORE:
        return None

    angle, y, x, w, h, score = best
    rot = cv2.getRotationMatrix2D((w_s / 2, h_s / 2), angle, 1.0)
    rx = cv2.warpAffine(gx, rot, (w_s, h_s))
    ry = cv2.warpAffine(gy, rot, (w_s, h_s))
    corners = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype="float32")
    refined, _ = _refine_pose(rx, ry, x, y, w, h)
    if refined is not None:
        corners = refined

    # Back out of the rotation, then out of the downscale.
    inverse = cv2.invertAffineTransform(rot)
    padded = np.hstack([corners, np.ones((4, 1), dtype="float32")])
    return ((padded @ inverse.T) / scale).astype("float32")


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

    # Then the shape search. It goes near the front because it is the steadiest
    # of the three and by far the cheapest; the edge-following searches below
    # stay because they succeed on frames it misses and vice versa — measured,
    # either alone reads 17-18 of 25 real captures and together they read 20.
    pose = card_pose(frame)
    if pose is not None:
        candidates.append(_warp(frame, pose))

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
