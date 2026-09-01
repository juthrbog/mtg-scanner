"""Names for Magic's colour combinations.

Two-colour pairs are named for the Ravnica guilds, three-colour sets for the
Alara shards (allied) and Tarkir wedges (enemy), and four-colour sets for the
Guildpact nephilim. See:
https://www.dicebreaker.com/games/magic-the-gathering-game/how-to/mtg-colour-combinations-explained

Keys are frozensets, so colour order in the database never matters.
"""
from __future__ import annotations

MONO = {
    frozenset("W"): "White",
    frozenset("U"): "Blue",
    frozenset("B"): "Black",
    frozenset("R"): "Red",
    frozenset("G"): "Green",
}

GUILDS = {
    frozenset("WU"): "Azorius",
    frozenset("UB"): "Dimir",
    frozenset("BR"): "Rakdos",
    frozenset("RG"): "Gruul",
    frozenset("GW"): "Selesnya",
    frozenset("WB"): "Orzhov",
    frozenset("UR"): "Izzet",
    frozenset("BG"): "Golgari",
    frozenset("RW"): "Boros",
    frozenset("GU"): "Simic",
}

SHARDS = {  # three allied colours
    frozenset("GWU"): "Bant",
    frozenset("WUB"): "Esper",
    frozenset("UBR"): "Grixis",
    frozenset("BRG"): "Jund",
    frozenset("RGW"): "Naya",
}

WEDGES = {  # three colours spanning an enemy pair
    frozenset("WBG"): "Abzan",
    frozenset("URW"): "Jeskai",
    frozenset("BGU"): "Sultai",
    frozenset("RWB"): "Mardu",
    frozenset("GUR"): "Temur",
}

NEPHILIM = {  # four colours, named for the Guildpact nephilim
    frozenset("WUBR"): "Yore-Tiller",
    frozenset("UBRG"): "Glint-Eye",
    frozenset("BRGW"): "Dune-Brood",
    frozenset("RGWU"): "Ink-Treader",
    frozenset("GWUB"): "Witch-Maw",
}

# Five colours has no lore name the way the smaller combinations do; players
# just spell out the colour letters.
FIVE = {frozenset("WUBRG"): "WUBRG"}

# What kind of grouping each name belongs to — used for the tooltip.
_KIND = [
    (MONO, "mono-colour"),
    (GUILDS, "guild"),
    (SHARDS, "shard"),
    (WEDGES, "wedge"),
    (NEPHILIM, "nephilim"),
    (FIVE, "five-colour"),
]

ALL_COMBOS = {k: v for table, _ in _KIND for k, v in table.items()}


def parse_colors(colors: str | None) -> frozenset[str]:
    """Turn the stored 'W,U' column into a set of colour letters."""
    if not colors:
        return frozenset()
    return frozenset(c.strip().upper() for c in colors.split(",") if c.strip())


def combo_name(colors: str | None) -> str | None:
    """Badge text for a card's colour combination.

    Single-colour cards read simply "Mono" — which colour it is, is already
    obvious from the mana pips shown beside the badge.

    Colourless cards read "Colorless", Magic's own spelling and its own
    category: it is not the absence of an answer but a real one, which is why
    they get a badge rather than the blank space that made a Heart of Kiran
    look like a card whose colours hadn't loaded.
    """
    letters = parse_colors(colors)
    if not letters:
        return "Colorless"
    if len(letters) == 1:
        return "Mono"
    return ALL_COMBOS.get(letters)


def combo_kind(colors: str | None) -> str | None:
    """Which family the combination belongs to ('guild', 'shard', ...)."""
    letters = parse_colors(colors)
    if not letters:
        return None
    for table, kind in _KIND:
        if letters in table:
            return kind
    return None


def combo_detail(colors: str | None) -> str | None:
    """The secondary label shown next to the badge.

    Nothing gets one any more. Mono cards used to be spelled out in full
    ("Mono" + "White"), but the mana pips sit directly above the badge and
    already say which colour it is, so the word only repeated them. Multicolour
    cards never had one: the family term (guild, shard, wedge, nephilim) is
    Magic jargon that doesn't explain itself at a glance, so it lives in the
    badge's tooltip.

    Kept as a filter rather than removed from the templates so the hook is
    still there if some combination later needs a word beside its badge.
    """
    return None


def combo_full_name(colors: str | None) -> str | None:
    """Spelled-out name for tooltips, e.g. "Mono White" or "Jeskai — wedge".

    The family term is explained here rather than on screen, so the jargon is
    available on hover without cluttering the card.
    """
    letters = parse_colors(colors)
    if not letters:
        return "Colorless"
    if len(letters) == 1:
        # The badge says only "Mono"; the colour it is lives here, so hovering
        # still spells it out for anyone who wants it.
        return f"Mono {MONO[letters]}"
    name = ALL_COMBOS.get(letters)
    if not name:
        return None
    kind = combo_kind(colors)
    return f"{name} — {kind}" if kind and kind != "five-colour" else name
