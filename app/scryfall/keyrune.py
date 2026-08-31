"""Which set codes Keyrune actually has a symbol glyph for.

Keyrune ships ~353 set symbols, but Scryfall knows ~791 set codes (promos,
Secret Lairs, foreign-language printings, and so on). Rendering `ss-<code>`
for a set Keyrune doesn't know produces a silently blank icon, so the UI
checks this set first and falls back to a plain text badge.

Regenerate after upgrading Keyrune:
    python -m app.scryfall.keyrune --refresh
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

KEYRUNE_CSS_URL = "https://cdn.jsdelivr.net/npm/keyrune@latest/css/keyrune.css"
_DATA_FILE = Path(__file__).with_name("keyrune_sets.txt")


def supported_sets() -> frozenset[str]:
    if not _DATA_FILE.exists():
        return frozenset()
    return frozenset(
        line.strip().lower()
        for line in _DATA_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


SUPPORTED_SETS = supported_sets()


def refresh() -> int:
    import httpx

    css = httpx.get(KEYRUNE_CSS_URL, timeout=30, follow_redirects=True).text
    codes = sorted(set(re.findall(r"\.ss-([a-z0-9]+):before", css)))
    _DATA_FILE.write_text(
        "# Set codes Keyrune has a symbol for. Regenerate:\n"
        "#   python -m app.scryfall.keyrune --refresh\n" + "\n".join(codes) + "\n",
        encoding="utf-8",
    )
    return len(codes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh the Keyrune supported-set list.")
    parser.add_argument("--refresh", action="store_true", help="Re-download Keyrune CSS and rewrite the list")
    args = parser.parse_args()
    if args.refresh:
        print(f"Wrote {refresh()} set codes to {_DATA_FILE}")
    else:
        print(f"{len(SUPPORTED_SETS)} set codes currently known. Use --refresh to update.")
