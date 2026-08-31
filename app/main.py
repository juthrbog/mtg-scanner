"""FastAPI app entrypoint.

Run with:  uvicorn app.main:app --reload
Then open: http://localhost:8000
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import SCAN_CACHE_DIR
from .db import init_db
from .recognition.match import art_index, index
from .routers import collection, scan, stats

app = FastAPI(title="Weatherlight")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/data/scans", StaticFiles(directory=str(SCAN_CACHE_DIR)), name="scans")

app.include_router(collection.router, prefix="/collection", tags=["collection"])
app.include_router(scan.router, prefix="/scan", tags=["scan"])
app.include_router(stats.router, prefix="/stats", tags=["stats"])


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    n = index.reload()
    print(f"Loaded {n} card hashes into the match index.")
    if index.stale:
        print(f"  ! {index.stale} card(s) still hashed at the old fingerprint size and are being")
        print("    ignored. Re-run:  python -m app.scryfall.hashing")
    if n == 0:
        print("  (empty — run `python -m app.scryfall.sync` then `python -m app.scryfall.hashing`)")

    art_n = art_index.reload(column="art_phash")
    if art_n:
        print(f"Loaded {art_n} art-window hashes (optional art matching available).")
    else:
        print("No art-window hashes yet — re-run `python -m app.scryfall.hashing --rehash` to enable art matching.")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/collection")
