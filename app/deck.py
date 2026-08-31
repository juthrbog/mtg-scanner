"""Commander deck rules.

Kept as plain functions over plain rows so the rules can be exercised without
a request, a database session, or a template.

The format's constraints, and how each is checked here:

* **100 cards**, counting the commander.
* **Singleton** — one copy of any card, except basic lands and the handful of
  cards that say otherwise in their own text.
* **Colour identity** — every card's identity must fit inside the commander's.
  Identity counts mana symbols anywhere on the card, not just its cost, which
  is why `color_identity` is stored rather than derived from `mana_cost`.
* **Legality** — Commander has its own banned list, taken from Scryfall rather
  than maintained here.
* **The commander** must be a legendary creature, or a card that explicitly
  says it can be one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

DECK_SIZE = 100

# Cards allowed in any quantity. Basic lands are the rule everyone knows; the
# rest say so in their own rules text, which is what `unlimited_allowed`
# actually checks for.
BASIC_LAND_NAMES = {
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Snow-Covered Wastes",
}

_ANY_NUMBER_RE = re.compile(
    r"a deck can have any number of cards named|"
    r"any number of cards named",
    re.I,
)


def unlimited_allowed(card) -> bool:
    """True if singleton doesn't apply to this card."""
    if card["name"] in BASIC_LAND_NAMES:
        return True
    if "Basic" in (card["type_line"] or "") and "Land" in (card["type_line"] or ""):
        return True
    return bool(_ANY_NUMBER_RE.search(card["oracle_text"] or ""))


def can_be_commander(card) -> bool:
    """Legendary creatures, plus anything that says it can lead a deck.

    The text check covers planeswalkers printed with "can be your commander"
    and the Backgrounds/partner-style cards, without hard-coding a list that
    would go stale with every set.
    """
    type_line = (card["type_line"] or "")
    text = (card["oracle_text"] or "")
    if "Legendary" in type_line and "Creature" in type_line:
        return True
    return "can be your commander" in text.lower()


def identity_of(card) -> set:
    raw = card["color_identity"]
    if raw is None:
        raw = card["colors"] or ""
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


@dataclass
class Problem:
    kind: str          # "size" | "singleton" | "identity" | "legality" | "commander"
    message: str
    cards: List[str] = field(default_factory=list)


@dataclass
class DeckReport:
    total: int = 0
    nonland: int = 0
    identity: set = field(default_factory=set)
    problems: List[Problem] = field(default_factory=list)
    owned: int = 0
    missing: int = 0

    @property
    def legal(self) -> bool:
        return not self.problems

    @property
    def remaining(self) -> int:
        """Cards still needed to reach 100 — negative if over."""
        return DECK_SIZE - self.total


def validate(commander, partner, cards: Iterable, owned_ids: Optional[set] = None) -> DeckReport:
    """Check a deck against the format.

    `cards` are the non-commander rows, each needing name, type_line,
    oracle_text, color_identity/colors, commander_legal and quantity.
    """
    cards = list(cards)
    report = DeckReport()
    owned_ids = owned_ids or set()

    leaders = [c for c in (commander, partner) if c is not None]
    for leader in leaders:
        if not can_be_commander(leader):
            report.problems.append(Problem(
                "commander",
                f"{leader['name']} can't be a commander — it isn't a legendary "
                "creature and doesn't say it can lead a deck.",
                [leader["name"]],
            ))

    report.identity = set().union(*(identity_of(l) for l in leaders)) if leaders else set()

    report.total = len(leaders) + sum(c["quantity"] for c in cards)
    report.nonland = sum(
        c["quantity"] for c in cards if "Land" not in (c["type_line"] or "")
    )

    for c in cards:
        if c["scryfall_id"] in owned_ids:
            report.owned += c["quantity"]
        else:
            report.missing += c["quantity"]

    if report.total != DECK_SIZE:
        over = report.total > DECK_SIZE
        report.problems.append(Problem(
            "size",
            f"{report.total} cards — {abs(DECK_SIZE - report.total)} "
            f"too {'many' if over else 'few'}.",
        ))

    dupes = [c["name"] for c in cards if c["quantity"] > 1 and not unlimited_allowed(c)]
    if dupes:
        report.problems.append(Problem(
            "singleton",
            "More than one copy of a card that isn't a basic land.",
            sorted(set(dupes)),
        ))

    if leaders:
        outside = sorted({
            c["name"] for c in cards if not identity_of(c) <= report.identity
        })
        if outside:
            report.problems.append(Problem(
                "identity",
                "Outside the commander's colour identity.",
                outside,
            ))

    banned = sorted({
        c["name"] for c in list(cards) + leaders
        if c["commander_legal"] == 0
    })
    if banned:
        report.problems.append(Problem(
            "legality", "Not legal in Commander.", banned,
        ))

    return report


# Grouping for the deck list. Order matters: a card matches the first bucket
# whose keyword appears in its type line, so "Artifact Creature" lands under
# Creatures rather than Artifacts, which is how deck lists are conventionally
# organised.
CATEGORIES = [
    ("Creatures", "Creature"),
    ("Planeswalkers", "Planeswalker"),
    ("Instants", "Instant"),
    ("Sorceries", "Sorcery"),
    ("Artifacts", "Artifact"),
    ("Enchantments", "Enchantment"),
    ("Battles", "Battle"),
    ("Lands", "Land"),
]


def categorise(cards: Iterable) -> List[dict]:
    buckets = {name: [] for name, _ in CATEGORIES}
    buckets["Other"] = []
    for card in cards:
        type_line = card["type_line"] or ""
        for name, keyword in CATEGORIES:
            if keyword in type_line:
                buckets[name].append(card)
                break
        else:
            buckets["Other"].append(card)
    return [
        {"name": name, "cards": rows, "count": sum(c["quantity"] for c in rows)}
        for name, rows in buckets.items()
        if rows
    ]


def curve(cards: Iterable) -> List[dict]:
    """Mana curve over non-land cards.

    Lands are excluded: they have no mana value to speak of and would pile a
    third of the deck onto the zero column, flattening the shape the curve
    exists to show.
    """
    counts: dict = {}
    for card in cards:
        if "Land" in (card["type_line"] or ""):
            continue
        value = int(card["cmc"] or 0)
        bucket = min(value, 7)
        counts[bucket] = counts.get(bucket, 0) + card["quantity"]
    peak = max(counts.values(), default=0)
    return [
        {
            "mv": mv,
            "label": "7+" if mv == 7 else str(mv),
            "count": counts.get(mv, 0),
            "share": (counts.get(mv, 0) / peak) if peak else 0,
        }
        for mv in range(8)
    ]
