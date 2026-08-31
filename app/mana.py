"""Render Scryfall mana symbols as Mana-font icons.

Scryfall writes costs and rules text with brace tokens — `{5}{R}{R}`,
`{W/U}`, `{T}` — which are unreadable in a UI. This turns each token into a
Mana font element (https://mana.andrewgioia.com), leaving the surrounding
text alone.

Everything outside a token is HTML-escaped, so card text is never treated as
markup; only the icon elements this module generates are trusted.
"""
from __future__ import annotations

import re
from pathlib import Path

from markupsafe import Markup, escape

TOKEN_RE = re.compile(r"\{([^{}]+)\}")

_MANA_CSS = Path(__file__).parent / "static" / "mana.css"


def _available_classes() -> frozenset[str]:
    """Symbol classes the vendored Mana font actually defines.

    Emitting `ms-<something the font lacks>` renders a silently blank icon,
    which is worse than showing the token text — so every mapping below is
    checked against this before it's used.
    """
    if not _MANA_CSS.exists():
        return frozenset()
    return frozenset(re.findall(r"\.ms-([a-z0-9-]+)::before", _MANA_CSS.read_text(encoding="utf-8")))


AVAILABLE = _available_classes()

# Tokens whose Mana class isn't just the lowercased token text.
_SPECIAL = {
    "T": "tap",
    "Q": "untap",
    "∞": "infinity",
    "½": "half",
    "PW": "planeswalker",
    "CHAOS": "chaos",
    "A": "acorn",       # Unfinity acorn
    "HW": "half",       # half-white (Unglued)
    "HR": "half",       # half-red
    "C/P": "p",         # colorless phyrexian; font has only the generic phyrexian symbol
}


def _symbol_class(token: str) -> str | None:
    """Map one Scryfall token's inner text to a Mana font class suffix."""
    raw = token.strip()
    if not raw:
        return None

    upper = raw.upper()
    if upper in _SPECIAL:
        return _SPECIAL[upper]

    # Hybrid, phyrexian, and split tokens: {W/U}, {2/W}, {W/P}, {C/W}.
    # Mana font names these by concatenation, in the same order: ms-wu, ms-2w.
    if "/" in raw:
        return "".join(part.strip().lower() for part in raw.split("/"))

    # Plain numbers ({0}-{20}, {100}, {1000000}) and single letters
    # ({W}, {X}, {C}, {S}, {E}) both just lowercase.
    if raw.isdigit() or (len(raw) <= 2 and raw.isalpha()):
        return raw.lower()

    return None


def symbol_class(token: str) -> str | None:
    """Mana class for a token, or None if the font can't render it."""
    cls = _symbol_class(token)
    if cls and (not AVAILABLE or cls in AVAILABLE):
        return cls
    return None


def render_symbols(text: str | None, cost_style: bool = True) -> Markup:
    """Replace `{...}` tokens in `text` with Mana font icons.

    `cost_style` adds the rounded grey background Mana uses for costs; turn it
    off for inline symbols that should sit flush with surrounding prose.
    """
    if not text:
        return Markup("")

    extra = " ms-cost" if cost_style else ""
    out: list[str] = []
    last = 0

    for match in TOKEN_RE.finditer(text):
        out.append(str(escape(text[last:match.start()])))
        cls = symbol_class(match.group(1))
        if cls:
            title = escape(match.group(0))
            out.append(f'<i class="ms ms-{escape(cls)}{extra}" title="{title}"></i>')
        else:
            # Unrecognised token — show it as written rather than dropping it.
            out.append(str(escape(match.group(0))))
        last = match.end()

    out.append(str(escape(text[last:])))
    return Markup("".join(out))
