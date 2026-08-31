# MTG Scanner

A local app for scanning and cataloging a Magic: The Gathering collection.
Point a webcam at a card, confirm the match, and it's in your collection —
searchable and browsable from the same page. No cloud service, no account,
one SQLite file on your own disk.

Stack: **FastAPI + HTMX + Tailwind/DaisyUI + SQLite**, card data from
**[Scryfall](https://scryfall.com/docs/api)**, recognition via OpenCV card
detection + perceptual hashing, set symbols from
**[Keyrune](https://keyrune.andrewgioia.com/)**.

No Node required. Tailwind is compiled by its standalone binary, and every
front-end asset is served from `app/static/` — the app styles correctly
offline and can't be broken by a slow or unreachable CDN.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

./fetch-assets.sh              # Tailwind CLI + DaisyUI/Keyrune/htmx (once)
./build-css.sh                 # compile app/static/tailwind.css
```

### Styling

The front end uses Tailwind, compiled by Tailwind's **standalone CLI** — a
single binary, no Node or npm. `fetch-assets.sh` downloads it to `bin/`.

After editing anything in `app/templates/`, recompile:

```bash
./build-css.sh                 # or: ./build-css.sh --watch
```

Two notes on why it's set up this way:

- **Not the Play CDN.** `cdn.tailwindcss.com` ships a compiler that generates
  styles in the browser at load time. It's documented as development-only,
  and when it loads slowly or is blocked, every utility disappears at once —
  grids stop being grids and the page collapses into full-size stacked
  elements. The standalone CLI scans template *files* and emits a static
  stylesheet, so this can't happen. It also means utilities used only inside
  HTMX-injected partials compile correctly, which the DOM-scanning CDN did
  not do reliably.
- **`app/static/app.css`** holds hand-written CSS for page structure and the
  card-art hover preview. Component looks still come from DaisyUI.

## First run

Card recognition needs a local copy of Scryfall's card data. This is two
separate steps on purpose — the first is fast, the second is slow, and you
don't need to wait for the slow one to start browsing.

**1. Sync card metadata** (a minute or two):

```bash
python -m app.scryfall.sync --bulk-type unique_artwork --limit 500   # quick smoke test
```

Once you've confirmed things work, do the real sync (every printing, so
foils/sets/collector numbers are all distinguishable — this downloads a
few hundred MB from Scryfall):

```bash
python -m app.scryfall.sync --bulk-type default_cards
```

**2. Build the image match index.** One image fetched per card; roughly
3 minutes for a full `default_cards` sync at 16 workers. Safe to interrupt
and re-run — it resumes where it left off, and art is cached under
`data/art_cache/` so later re-hashes cost CPU rather than another download:

```bash
python -m app.scryfall.hashing --workers 16
```

Cards hashed at a different fingerprint size are picked up automatically and
recomputed; `--rehash` forces every card to be redone.

**3. Run the app:**

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000**. Camera access works out of the box on
`localhost` — browsers treat it as a secure context. If you want to scan
from a phone over your LAN instead, you'll need to serve over HTTPS or use
your browser's flag for trusting an insecure origin on your local network.

## How it's organized

```
app/
  main.py              FastAPI app, startup (loads the hash index into memory)
  config.py            paths and recognition tuning, one place to edit
  db.py                SQLite schema + connection helpers
  scryfall/
    sync.py            bulk metadata download → scryfall_card table
    hashing.py         downloads art, computes pHash → fills scryfall_card.phash
    prices.py          refreshes Mana Pool prices (TCGplayer arrives with sync.py)
    keyrune.py         which set codes have a Keyrune symbol (--refresh to update)
  mana.py              renders Scryfall {R} tokens as Mana font symbols
  recognition/
    detect.py           OpenCV: find the card in a frame, warp it flat
    match.py             pHash comparison against the in-memory index
  static/detect-live.js  the same detection running live in the browser,
                         drawing the outline over the camera preview
  routers/
    collection.py       browse / search / edit / delete
    scan.py               capture → match → confirm
  templates/            Jinja2 + HTMX; Tailwind and DaisyUI loaded from CDN, no build step
  static/scan.js         the one hand-written bit of client JS (camera capture)
data/                    gitignored — mtg.db, downloaded bulk files, scan captures
```

## Prices

Each card shows market prices from two marketplaces, linked to that card's
page on each:

- **TCGplayer** — arrives with the Scryfall sync; Scryfall's `usd`/`usd_foil`
  fields *are* TCGplayer market prices, so no extra source is needed. The
  outbound link is Scryfall's own affiliate URL, exactly as its API supplies it.
- **[Mana Pool](https://manapool.com)** — its `/api/v1/prices/singles`
  endpoint is public and unauthenticated, and returns every single's price
  keyed by Scryfall ID. Refresh with:

```bash
python -m app.scryfall.prices
```

Prices move constantly, so re-run that whenever you want current numbers.
Roughly 84k of the 111k cards carry a Mana Pool price; cards with none simply
show the TCGplayer chip (or "No market price available" if neither has one).

## Your collection is safe

Everything lives in `data/mtg.db`, an ordinary SQLite file on disk. Stopping
the app, rebooting, and re-running the sync scripts all leave your collection
untouched — schema changes are applied as in-place `ALTER TABLE` migrations
rather than rebuilds. To back it up, copy that one file.

## Notes

- **Re-syncing:** Scryfall updates card data continuously. Re-run `sync.py`
  occasionally (it's an upsert, safe to repeat) and `hashing.py` after —
  new cards won't have a hash yet. Both preserve your collection and existing
  hashes; `sync.py --skip-download` re-parses the file already on disk.
- **Scanning:** the browser uploads the whole camera frame and the server
  locates the card within it, so the card does not have to be centred or
  square to the lens. `detect-live.js` runs the same detection live and draws
  the outline over the preview, turning green once it has held steady — that
  outline is what the server will work from, because both run the same
  algorithm with the same constants. If you change a threshold in
  `detect.py`, change it in `detect-live.js` too or the preview will start
  lying about what the server sees.

- **Live detection needs `app/static/opencv.js`** (~10MB, fetched by
  `fetch-assets.sh`). It loads asynchronously and the page works without it —
  capture just loses the on-screen outline, since the server detects
  independently either way.

- **Recognition quality** depends far more on your scanning rig than the
  algorithm: a straight-down camera angle and even, diffuse lighting will
  outperform a nicer camera held at an angle. Cards leaning back are still the
  weak spot — see `detect.py` for the thresholds.
- **Fingerprint size matters more than camera resolution.** Measured against
  the full index, capture resolution barely changes the hit rate above about
  1280px — pHash downsamples internally, so extra pixels buy margin, not new
  matches. The fingerprint length does change it: at 64 bits a glare patch
  made the *wrong* card closer than the right one (margin −0.141, 0/8
  correct); at 256 bits the margin turns positive and matches come back.
  `PHASH_SIZE` in `config.py` controls this; changing it invalidates every
  stored hash, and the app says so at startup.

- **Auto-capture** (toggle on the scan page, remembered between visits) fires
  the shutter once a card has been held still for about 0.85s. It disarms
  after firing and re-arms only when the card leaves the view, so swapping in
  the next card drives the rhythm rather than a timer. Taking the card away
  also dismisses an already-confirmed result — but never pending candidates
  you haven't acted on. It captures only; confirming a match stays manual,
  since auto-adding a wrong card is worse than one extra click.

- **Two optional matching aids**, toggled on the scan page and remembered
  between visits:

  - **Art matching** compares the illustration window on its own as well as
    the whole card. Every card shares the same frame furniture, so the art is
    the part that actually distinguishes them — measured, this widened the gap
    between the right card and the nearest wrong one from 64 to 84. It assumes
    a standard frame, so full-art and showcase printings can miss; it is
    consulted *alongside* whole-card matching and only wins when it scores
    closer. Needs `art_phash`, filled in by `hashing.py`.

  - **Name check** reads the printed card name with OCR and uses it to
    re-rank the image matches. The two signals fail differently: hashing
    degrades gradually as a photo softens, while OCR is either right or
    silent — on progressively worse captures it read titles perfectly until
    the image went both soft and dim, then returned nothing rather than
    guessing. It can only reorder candidates the image match already found,
    so a misread cannot invent a card. Adds roughly 300ms per scan and needs
    `rapidocr-onnxruntime`.

- **Detection runs over two channels**, brightness and saturation. Glare is
  nearly colourless, so a reflection that erases the card's edges in
  brightness leaves them visible in saturation. Both sets of candidates are
  scored and the best wins.

- **Matching is pure pHash** for now, no ML dependencies. If glare/angle
  tolerance becomes a real problem, the natural upgrade is swapping
  `recognition/match.py` to compare CLIP/SigLIP embeddings instead of
  Hamming distance on a hash — see the pretrained embeddings at
  `TrevorJS/mtg-scryfall-cropped-art-embeddings-siglip-so400m-patch14-384`
  on Hugging Face rather than training anything yourself.
