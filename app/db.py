"""SQLite connection and schema management. One file (`data/mtg.db`) holds
the Scryfall cache and your collection side by side."""
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS scryfall_card (
    id                TEXT PRIMARY KEY,      -- Scryfall's own card id (one per printing)
    oracle_id         TEXT,
    name              TEXT NOT NULL,
    set_code          TEXT NOT NULL,
    set_name          TEXT,
    collector_number  TEXT NOT NULL,
    rarity            TEXT,
    mana_cost         TEXT,
    type_line         TEXT,
    oracle_text       TEXT,
    colors            TEXT,                  -- comma-joined, e.g. "W,U"
    image_small       TEXT,
    image_normal      TEXT,
    price_usd         REAL,
    price_usd_foil    REAL,
    phash             TEXT                   -- perceptual hash of image_small, filled in by hashing.py
);
CREATE INDEX IF NOT EXISTS idx_scryfall_card_name ON scryfall_card(name);
CREATE INDEX IF NOT EXISTS idx_scryfall_card_set ON scryfall_card(set_code);

CREATE TABLE IF NOT EXISTS collection_entry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id   TEXT NOT NULL REFERENCES scryfall_card(id),
    quantity      INTEGER NOT NULL DEFAULT 1,
    foil          INTEGER NOT NULL DEFAULT 0,
    condition     TEXT DEFAULT 'NM',
    language      TEXT DEFAULT 'en',
    acquired_at   TEXT DEFAULT (date('now')),
    notes         TEXT,
    scan_image    TEXT
);
CREATE INDEX IF NOT EXISTS idx_collection_scryfall_id ON collection_entry(scryfall_id);

CREATE TABLE IF NOT EXISTS scan_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
    matched_id    TEXT,
    confidence    REAL,
    image_path    TEXT,
    accepted      INTEGER
);
"""


def get_connection() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI's async routes resolve sync dependencies
    # in a worker thread but run the route body on the event loop thread, so a
    # connection handed in via Depends(get_db) can cross threads within one
    # request. Safe here — each request gets its own fresh connection, never
    # shared or reused concurrently across requests.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the original schema shipped. Applied with ALTER TABLE so
# an existing database (and the collection in it) is upgraded in place rather
# than rebuilt.
_MIGRATIONS = {
    "scryfall_card": {
        "tcgplayer_url": "TEXT",
        "manapool_cents": "INTEGER",       # ManaPool lowest listing, in cents
        "manapool_nm_cents": "INTEGER",    # near-mint specifically
        "manapool_foil_cents": "INTEGER",
        "manapool_url": "TEXT",
        # Perceptual hash of the illustration window alone. Optional:
        # only populated when hashing runs with art hashing enabled.
        "art_phash": "TEXT",
        # What colours a card actually represents, including mana it
        # produces. A Swamp has no mana cost, so `colors` is empty for it
        # while `color_identity` is black — which is what "how much black
        # do I own" should count.
        "color_identity": "TEXT",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def get_db():
    """FastAPI dependency — one fresh connection per request."""
    with db_session() as conn:
        yield conn
