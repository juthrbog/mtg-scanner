# Weatherlight

> *"The ship was more than the sum of her parts."*

Named for the skyship of the Weatherlight Saga, whose crew spent the story
hunting down the scattered pieces of the Legacy — assembling a collection,
artifact by artifact, into something greater. Which is roughly what this does
with a shoebox of cards.


A local app for cataloguing a Magic: The Gathering collection. Point a webcam
at a card, confirm the match, and it's in your collection — searchable,
sortable, and browsable from the same page. Then build Commander decks out of
what you actually own, and export them to any of the usual deck sites. No cloud
service, no account, one SQLite file on your own disk.

**FastAPI + HTMX + Tailwind/DaisyUI + SQLite.** Card data from
[Scryfall](https://scryfall.com/docs/api); cards are located with OpenCV and
identified by reading their printed name with
[RapidOCR](https://github.com/RapidAI/RapidOCR), with perceptual hashing as a
supporting signal; set symbols from
[Keyrune](https://keyrune.andrewgioia.com/), mana symbols from
[Mana](https://mana.andrewgioia.com/).

No Node and no runtime CDN: Tailwind is compiled by its standalone binary and
every front-end asset is served from `app/static/`, so the app renders
correctly offline and can't be broken by a slow or blocked CDN.

---

## Quick start with Docker

```bash
docker compose up --build          # builds, then serves on http://localhost:8000
```

The first run needs card data. In another terminal:

```bash
docker compose exec app python -m app.scryfall.sync --bulk-type default_cards
docker compose exec app python -m app.scryfall.hashing --workers 16
docker compose exec app python -m app.scryfall.prices        # optional, market prices
```

Everything it downloads — the database, card art cache and bulk files — lives
in the `weatherlight-data` volume, so rebuilding the image never costs you the index or
your collection. (Verified: a `--no-cache` rebuild leaves the index, the hashes
and the collection intact.)

The image is ~660MB, most of it OpenCV, ONNX Runtime and the OCR models.
Dropping `rapidocr-onnxruntime` from `requirements.txt` roughly halves that,
but **don't**: reading the printed name is how cards are identified now, and
without it scanning falls back to image matching alone, which on a real webcam
photo almost never finds the right card. See [Scanning](#scanning) for the
measurements. Remove it only if you want the collection, deck and price
features and intend to add cards by search rather than by camera.

**Camera access needs a secure context.** `http://localhost:8000` counts as
one, so scanning works out of the box on the machine running Docker. Reaching
it from another device on your LAN does not — see [Scanning from another
device](#scanning-from-another-device).

## Running without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

./fetch-assets.sh                  # Tailwind CLI, DaisyUI, Keyrune, Mana, htmx, OpenCV.js
./build-css.sh                     # compile app/static/tailwind.css
```

Then load card data — two steps on purpose, since the first is quick and you
don't need the second to start browsing:

```bash
# 1. Card metadata. Every printing, so foils, sets and collector numbers are
#    all distinguishable. A few hundred MB from Scryfall.
python -m app.scryfall.sync --bulk-type default_cards

# 2. The image match index. One small image per card; about 3 minutes at 16
#    workers. Safe to interrupt and re-run — it resumes, and each image is
#    cached under data/art_cache/ so later re-hashes cost CPU, not another
#    download. Required for scanning even though the hash is no longer what
#    identifies a card: the name lookup searches the cards this step has
#    hashed, so an unhashed card cannot be scanned in.
python -m app.scryfall.hashing --workers 16

# 3. Market prices (optional).
python -m app.scryfall.prices
```

Run it:

```bash
uvicorn app.main:app --reload      # http://localhost:8000
```

To try things quickly without the full download, `sync` takes
`--bulk-type unique_artwork --limit 500`.

---

## Scanning

The browser uploads the **whole camera frame** and the server locates the card
within it, so a card doesn't have to be centred or square to the lens.
`detect-live.js` runs the same three searches live and draws the outline over
the preview, turning green once it has held steady. That outline is sent with
the capture, but only as one candidate among the server's own — a preview
slightly off cannot spoil the result.

> If you change a threshold in `detect.py`, change it in `detect-live.js` too,
> or the preview will start lying about what the server sees. The two files
> note the handful of places they deliberately differ, and why.

Two toggles on the scan page, both remembered between visits:

- **Auto-capture** fires the shutter once a card has been held still for about
  0.85s. It disarms after firing and re-arms only when the card leaves the
  view, so swapping in the next card drives the rhythm rather than a timer.
  Taking the card away also dismisses an already-confirmed result — but never
  pending candidates you haven't acted on. It only captures; confirming stays
  manual, because auto-adding a wrong card is worse than one extra click.

- **Name check** reads the printed name with OCR and looks the card up by it.
  **On by default, because it is the signal that works.** Measured over 26 real
  webcam captures, the image hash never once put the correct card first, while
  reading the name identified 21 of 25 and named a wrong card zero times — the
  four failures were silence, not a wrong answer. The two signals still fail
  differently, which is why both run: OCR is either right or quiet, the hash
  degrades gradually. Adds roughly 300ms per scan.

  Why the hash does so poorly on real photographs is worth stating plainly,
  since the rest of this README talks about it a lot: pHash compares a
  photograph against Scryfall's *render* of the same card, and glare, warm
  indoor light and a webcam lens leave too little in common. On a hand-cropped,
  pixel-accurate warp of a plain black-bordered card the right answer still
  ranked 5367th of 111154 — and that was the *best* case across 64-, 100-,
  144- and 256-bit fingerprints, with and without contrast and illumination
  normalisation. It is kept because it costs nothing to consult, it picks
  correctly between the handful of printings of a card the name identified,
  and it is the fallback when OCR stays silent.

Each result shows a verdict with the raw Hamming distance beside it. A card
identified by its name reads **Name**; one found by image alone reads Strong /
Good / Weak / Unreliable.

Don't read too much into the distance. On real webcam captures it is large even
for the right card — d84 to d104 is typical, against roughly d90 for the best
of 111k unrelated cards — which is the measurement that moved recognition onto
the printed name. It is shown because it still separates *printings* of a card
the name has already identified, and because a genuinely sharp, flat capture
(a scanner, or a phone straight down on a desk) does land much closer.

### Scanning from another device

Browsers only expose a camera in a secure context. `localhost` qualifies; a
LAN address does not. To scan from a phone you need HTTPS in front of the app,
or your browser's flag for trusting an insecure origin on your network.

---

## Searching

The search box uses a subset of Scryfall's syntax, because anyone with a Magic
collection already types that. A bare word matches **name, type, set and
keywords**; a prefix narrows it to one field.

| Query | Finds |
|---|---|
| `dragon` | name, type, set or keyword containing "dragon" |
| `n:bolt` | name only (`name:` also works) |
| `t:creature` | type line — `t:legendary`, `t:artifact` (`type:`) |
| `s:mbs` | set name or code (`set:`, and Scryfall's `e:`) |
| `kw:flying` | abilities, from Scryfall's structured keyword list (`keyword:`) |
| `o:destroy` | rules text (`oracle:`) |
| `r:mythic` | rarity (`rarity:`) |
| `"exact phrase"` | the phrase, kept together |
| `t:creature -kw:flying` | creatures that don't fly |

Terms combine with AND, so each one narrows the result. A prefix that isn't in
that list is searched as literal text rather than rejected, so a stray colon
never turns into an error.

Two deliberate choices:

- **Keywords come from Scryfall's `keywords` field**, not a text search of the
  rules box. Searching oracle text for "flying" also matches flavour text and
  reminder text.
- **Rules text is excluded from the bare-word search.** Words like "creature"
  or "target" appear in most rules boxes, so including it would make bare
  searches match nearly everything. It stays available behind `o:`.

The **Search tips** panel lists the keywords actually present in your
collection with counts — click one to search for it. Listing what you own
beats a generic glossary: every chip is guaranteed to return results.

Results sort by name, value, rarity, set, quantity or recently added, and sort
composes with the search and filters. **Reset** appears whenever anything is
active and clears all of it in one request.

---

## Commander decks

`/decks` builds and saves Commander decks **from cards you actually own**.
Only commanders in the collection are offered, card search returns only owned
cards, and a deck can never ask for more copies than the collection holds —
the Add button disappears once every copy is in the deck.

Ownership is counted by *oracle identity*, not by printing: a Sol Ring is a
Sol Ring whichever set it came from, and Scryfall lists 140 printings of it.
Own the same card in two sets and it appears once, with the copies combined.

The search reads the collection itself rather than the card database filtered
by ownership, so the art you see is the copy you actually hold — including the
right one when a set has both a regular and a showcase printing.

The deck view validates continuously and explains what is wrong rather than
just refusing:

- **100 cards**, counting the commander.
- **Singleton**, except basic lands and cards whose own text allows any number.
  That exemption is read from the card's rules text, so something like
  Persistent Petitioners is handled without a hard-coded list.
- **Colour identity** — every card must fit inside the commander's. Identity
  counts mana symbols anywhere on a card, which is why it comes from Scryfall's
  `color_identity` rather than from the mana cost.
- **Legality** — the Commander banned list, taken from Scryfall.
- **Availability** — re-checked on every render rather than assumed when a
  card was added, since a collection changes: trade a card away and the decks
  built on it say so.

Cards outside the identity or on the banned list can still be added; they are
flagged in the search results and named in the validation panel. Building a
deck you know is illegal is a normal step, and a button that silently refuses
teaches nothing.

Off-identity cards also carry the panel's warning colour on their own row in
the deck list. The panel names them, but finding those names again in a hundred
rows is the tedious part — especially after an import, where a list built for
someone else's commander can bring in a dozen at once.

Each deck also shows a mana curve over non-lands and cards grouped by type.

Deck rules live in `app/deck.py` as plain functions over card rows, so they can
be exercised without a request or a database.

### Exporting a deck

**Export** on a deck opens a preview you can copy, or download as a file. Every
mainstream deck tool parses roughly the same shape — a quantity, then a card
name, one per line — and differs mainly in whether it wants the exact printing
and how it marks the commander:

| Format | Looks like | For |
| --- | --- | --- |
| **Plain text** | `1 Sol Ring` | Moxfield, Archidekt, TappedOut, Cockatrice — and what to try when another import fails |
| **Moxfield / Archidekt** | the same, under `Commander` and `Deck` headers | filling the command zone on import |
| **MTG Arena** | `1 Sol Ring (ECC) 57` | pinning the exact printing you own |
| **CSV** | one row per card, with set and collector number | spreadsheets and collection trackers |

Two details that decide whether an import succeeds:

- **Arena wants the front face only.** `Obyra's Attendants // Desperate Parry`
  is rejected; `Obyra's Attendants` is not. The plain formats keep the full
  name, which is what the deck sites expect.
- **Arena's parser chokes on leading zeros** in collector numbers, so they are
  stripped.

Plain text has no `Commander` header on purpose — the sites that understand a
command zone read the header formats above, and the rest would treat the header
as a card name and fail the whole import. There the commander is just the first
line.

Rendering lives in `app/export.py`, separate from the route, so adding a format
means adding a branch and a `FORMATS` entry.

### Importing a deck list

**Import list** on `/decks` takes a pasted list or a `.txt`/`.csv` file. There
is no format to pick: a list that is *written* has one shape, but a list that
*arrives* has been through whichever site it was copied from, so the parser
reads the union of what they all emit —

- `1 Sol Ring`, `1x Sol Ring`, or a bare `Sol Ring` (hand-written lists drop
  the count on singletons)
- `1 Sol Ring (ECC) 57` — Arena and Moxfield's with-printing export
- section headers (`Commander`, `Deck`, `Sideboard`) and the type headers
  (`Creatures (30)`) TappedOut and Archidekt insert
- `SB:` line prefixes from MTGO's `.dec`
- trailing `*F*` (Moxfield foil) and `[Category]` (Archidekt) tags
- `//` and `#` comments, and the CSV this app exports

Names are matched loosely enough to survive the trip: accents, curly quotes and
stray whitespace are folded away, and a double-faced card matches on either its
front face or its full name — so an Arena list saying `Obyra's Attendants` finds
`Obyra's Attendants // Desperate Parry`.

Because decks here are built from cards you own, importing **matches against the
collection**, and the preview says exactly what will happen before anything is
saved:

| | |
| --- | --- |
| **short on copies** | the list wants 4, you own 1 — 1 is imported |
| **not in your collection** | a real card you don't own — listed, not added |
| **unrecognised** | no card by that name — usually a typo |
| **sideboard** | Commander has no sideboard, so those lines are skipped |

That last distinction is the one worth having: *you don't own this* and *no such
card* ask you for completely different things — go scan it in, versus check the
spelling.

The commander comes from a `Commander` section when the list has one, and
otherwise from a picker of every legendary creature in the list. Preview and
create run the *same* resolver, so what you are shown is what gets built.

Parsing lives in `app/deck_import.py` and never touches the database, so it can
be exercised on a string; turning a name into a card you own is the router's job.

---

## Stats

`/stats` summarises the collection: total cards, unique printings, distinct
cards (ignoring reprints), value from both marketplaces, and breakdowns by
colour and rarity.

Two things the page states rather than leaving you to infer:

- **Colours count by colour identity, not mana cost.** A Swamp has no mana
  cost, so counting by cost files every basic land under "colourless".
  Identity includes the mana a card produces, so lands sit under their colour.
- **A multicolour card counts under each of its colours**, so the colour rows
  sum to more than the collection size. That's the useful reading of "how much
  black do I own".

The colour bars use a single hue. The bar encodes magnitude; identity is
carried by the mana symbol and the colour's name beside it. Six hues would
encode identity twice over — and Magic's own colours put two confusable pairs
next to each other in WUBRG order (blue/black and red/green both fail
colour-vision separation checks).

---

## Prices

Each card shows market prices from two marketplaces, linked to that card's
page on each:

- **TCGplayer** — arrives with the Scryfall sync; Scryfall's `usd`/`usd_foil`
  fields *are* TCGplayer market prices. The outbound link is Scryfall's own
  affiliate URL, exactly as its API supplies it.
- **[Mana Pool](https://manapool.com)** — its `/api/v1/prices/singles`
  endpoint is public and unauthenticated, returning every single's price keyed
  by Scryfall ID.

Prices move constantly, so re-run `python -m app.scryfall.prices` whenever you
want current numbers. Roughly 84k of the 111k cards carry a Mana Pool price;
cards without one show only the TCGplayer chip.

---

## Your collection is safe

Everything lives in `data/mtg.db`, an ordinary SQLite file. Stopping the app,
rebooting, rebuilding the container and re-running the sync scripts all leave
your collection untouched — schema changes are applied as in-place
`ALTER TABLE` migrations rather than rebuilds. To back it up, copy that one
file.

A retired feature can take its column with it (`_DROPPED` in `db.py`), but only
ever a *derived* one — something recomputable from the card images, never
anything you entered. Collection rows, decks and scan history are only ever
added to.

---

## How it's organized

```
app/
  main.py               FastAPI app; startup loads the hash index into memory
  config.py             paths, fingerprint size, match thresholds — one place to edit
  db.py                 SQLite schema, connection helpers, in-place migrations
  search.py             parses the Scryfall-style query into a SQL fragment
  colors.py             colour-combination names (guilds, shards, wedges…)
  mana.py               renders Scryfall {R} tokens as Mana font symbols
  deck.py               Commander rules as plain functions over card rows
  export.py             renders a deck as text other Magic tools can import
  deck_import.py        reads a pasted deck list back, in whatever shape it came
  templating.py         shared Jinja environment, filters and globals
  scryfall/
    sync.py             bulk metadata download → scryfall_card
    hashing.py          downloads card images and computes their hashes
    prices.py           refreshes Mana Pool prices
    keyrune.py          which set codes have a Keyrune symbol
  recognition/
    detect.py           OpenCV: shape search + contours + lines; warps the card flat
    match.py            packed-bit Hamming search over the in-memory index
    ocr.py              reads the printed name and finds the card from it
  routers/
    collection.py       browse / search / sort / edit / delete
    scan.py             capture → match → confirm
    decks.py            build, validate and export Commander decks
    stats.py            totals, value, breakdowns
  templates/            Jinja2 + HTMX partials
  static/
    scan.js             camera capture, auto-capture, toggles
    detect-live.js      the same three searches, running live in the browser
    preview.js          hover-to-enlarge card art
    app.css             hand-written CSS for layout and anything HTMX injects
    src.css             Tailwind input, compiled to tailwind.css
data/                   gitignored — mtg.db, image cache, bulk files, scan captures
                        (each scan keeps both the crop and the frame it came from)
```

Third-party assets (`opencv.js`, DaisyUI, Keyrune, Mana, htmx) and the
Tailwind binary are gitignored and re-fetched with `./fetch-assets.sh`.

---

## Notes and tuning

- **Re-syncing.** Scryfall updates continuously. Re-run `sync.py` occasionally
  (it's an upsert, safe to repeat) and `hashing.py` after — new cards won't
  have a hash yet. Both preserve your collection and existing hashes.
  `sync.py --skip-download` re-parses the file already on disk, which is how
  new columns get populated without another few hundred MB.

- **`PHASH_SIZE` is not a lever worth pulling.** All 111k references were
  rehashed at 64, 100, 144 and 256 bits and re-ranked against a hand-cropped,
  pixel-accurate capture: the correct card placed 740th at 64 bits and 5367th
  at 256. No setting makes the hash identify a webcam photo. It stays at 256
  because that is what the stored hashes were built with; changing it
  invalidates every one of them, and the app says so at startup.

- **Capture resolution matters for reading, not for hashing.**
  `MAX_UPLOAD_EDGE` in `scan.js` is 1600; the measurement behind that number
  was about hash distance, and the binding constraint now is whether OCR can
  resolve the printed name. Re-running the same 25 captures downscaled: 21
  identified at 1280px, 20 at 960 and at 640, then 16 at 480. So there is
  headroom to shrink the upload, but the floor is the point where the title
  stops being legible, not anything to do with the hash.

- **Detection runs three searches over four channels.** The channels are
  brightness, saturation, and Lab a and b. Glare is nearly colourless, so a
  reflection that erases a card's edges in brightness leaves them in
  saturation; skin against green or blue card art is a clean step in a/b when
  the other two run continuous across the boundary. The searches are:

  1. **Shape search** (`card_pose`) scans a 63:88 rectangle over position,
     size and rotation and scores how well its outline sits on image edges,
     scoring horizontal edges with `|d/dy|` and vertical ones with `|d/dx|` so
     rules text parallel to the card's top edge cannot support it. This is the
     steady one: across near-identical frames it moves its answer ~10px, where
     the edge-following searches moved 53px and sometimes found nothing.
  2. **Contour following**, for a card whose outline is closed — the more
     precise answer when it works.
  3. **Line fitting**, for an outline that is straight but broken, which is
     what a borderless card against skin gives.

  All three feed the matcher, which scores every crop. Either family alone
  reads 17–18 of 25 real captures; together they read 20.

- **Rig beats algorithm.** A straight-down camera angle and even, diffuse
  lighting will outperform a better camera held at an angle. Dark or busy
  playmats are the worst case — a black-bordered card on a black mat has
  no edge to find.

- **Live detection needs `app/static/opencv.js`** (~10MB). It loads
  asynchronously and the page works without it; capture just loses the
  on-screen outline, since the server detects independently either way.

- **Styling.** `app/static/app.css` holds hand-written CSS for page structure
  and anything HTMX injects; DaisyUI supplies component looks. Two reasons it
  isn't the Tailwind Play CDN: that ships a compiler which generates styles in
  the browser, so when it loads slowly every utility vanishes at once and the
  page collapses into full-size stacked elements — and it misses utilities
  that only appear in HTML fetched later. The standalone CLI scans template
  *files* and emits a static stylesheet. Rebuild with `./build-css.sh`
  (or `--watch`) after editing templates.

- **The filter dropdowns use `appearance: base-select`.** A native `<select>`
  picker is drawn by the OS, so CSS on the element can't touch the open menu —
  it renders as system chrome against the app's theme. `base-select` makes the
  picker styleable while the element stays a real `<select>`, so keyboard
  navigation, form semantics and mobile behaviour are unchanged. Browsers
  without support ignore the rules and show their native picker.

- **Where matching should go next.** Reading the name now carries the scan and
  perceptual hashing contributes almost nothing on real photographs (see **Name
  check** above for the numbers). That makes the embedding upgrade the obvious
  next step rather than a speculative one: comparing CLIP/SigLIP embeddings
  instead of Hamming distance tolerates exactly the photograph-versus-render
  gap that defeats pHash. Pretrained embeddings for every Scryfall card exist
  at `TrevorJS/mtg-scryfall-cropped-art-embeddings-siglip-so400m-patch14-384`
  on Hugging Face, so nothing needs training.

  Until then the honest summary is: **the scanner identifies cards by reading
  them, not by looking at them.** Anything that hurts the printed name — a
  thumb over the title, heavy glare on the top of the card, a very soft frame —
  will lose the scan, and no amount of tuning the hash will rescue it.
