"""Central paths and settings for the app. One file, no env-var ceremony —
edit these directly if you need to relocate things."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "mtg.db"
SCAN_CACHE_DIR = DATA_DIR / "scans"

DATA_DIR.mkdir(exist_ok=True)
SCAN_CACHE_DIR.mkdir(exist_ok=True)

SCRYFALL_API = "https://api.scryfall.com"

# Recognition tuning
#
# PHASH_SIZE is the side of the DCT block imagehash keeps, so the fingerprint
# is PHASH_SIZE**2 bits: 8 -> 64 bits, 16 -> 256 bits.
#
# 64 bits is too few to separate 111k cards once an image is degraded.
# Measured on tilted, off-centre cards with a glare patch, comparing the
# distance to the correct card against the distance to the nearest wrong one:
#
#     64-bit   margin -0.141  ->  0/8 correct   (wrong card is *closer*)
#    256-bit   margin +0.074  ->  6/8 correct
#
# On clean captures both score 8/8, but 256 bits still carries a wider margin
# (0.251 vs 0.164), which is the headroom that absorbs bad lighting.
#
# Changing this invalidates every stored hash — re-run:
#     python -m app.scryfall.hashing --rehash
PHASH_SIZE = 16
PHASH_BITS = PHASH_SIZE * PHASH_SIZE
PHASH_HEX_LEN = PHASH_BITS // 4

# Where the *wrong* cards live. Measured over 1200 real card images: the
# nearest incorrect card sits at a Hamming distance of ~70 of 256 bits (0.276),
# and that barely moves under camera noise, soft focus, dim light, or JPEG
# recompression — while the correct card stays under 4. So this is the point
# at which a match carries no information, and the natural zero for a
# confidence scale.
HASH_NOISE_FLOOR = int(PHASH_BITS * 0.27)

# Beyond this a match is worth flagging as weak. Sits just under the noise
# floor: a real photograph of a card — soft focus, room lighting, a phone or
# laptop sensor — lands around 50 even when the crop is perfect and the match
# is right. Anything stricter cries wolf on ordinary, correct scans.
HASH_MATCH_THRESHOLD = int(PHASH_BITS * 0.235)

# Bands used to describe a match in words. Percentages imply a precision this
# doesn't have; "Strong" and "Weak" travel better. Set from observed real
# captures: a correct match off a webcam lands near d50, so that has to read
# as good rather than as a warning.
HASH_STRONG = int(PHASH_BITS * 0.16)   # d41
HASH_GOOD = int(PHASH_BITS * 0.225)    # d57

TOP_N_CANDIDATES = 3


def match_verdict(distance: int) -> str:
    if distance <= HASH_STRONG:
        return "Strong"
    if distance <= HASH_GOOD:
        return "Good"
    if distance < HASH_NOISE_FLOOR:
        return "Weak"
    return "Unreliable"


# The scale is deliberately not linear. Photographing a physical card is far
# harsher than any synthetic degradation of Scryfall's art: gloss, room
# lighting, a soft webcam focus and a real sensor together put a *correct*
# match around d50, while unrelated cards sit near d66-70. Measured on real
# captures, not simulations — every synthetic test put correct matches under
# d5, which is why an earlier linear scale reported ordinary good scans as
# "28%".
#
# So the useful signal is squeezed into a narrow band just below the noise
# floor, and the curve stretches that band out: d50 reads as a solid match,
# d66 as a poor one, which is what those numbers actually mean in practice.
CONFIDENCE_CURVE = 3.3


def confidence_from_distance(distance: int) -> int:
    """Map a Hamming distance to a 0-100 score.

    0 means "no better than a random card", 100 means pixel-identical.
    """
    if HASH_NOISE_FLOOR <= 0:
        return 0
    ratio = min(1.0, max(0.0, distance / HASH_NOISE_FLOOR))
    return max(0, min(100, round(100 * (1 - ratio ** CONFIDENCE_CURVE))))
