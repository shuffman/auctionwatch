# AuctionWatch — Complete Specification

> Use this document to reconstruct the project from scratch.

---

## Overview

AuctionWatch is a Python/Flask web app that aggregates used car listings from eleven sources in real time. The user enters a search query; results stream in from all sites simultaneously and are displayed in a filterable, sortable card grid with a day/night theme (day default).

**Sources:** Cars & Bids, Bring a Trailer, Hagerty, PCar Market, Craigslist, Cars.com, Porsche Finder, CarMax, Carvana, eBay Motors, Hemmings

**Stack:** Python 3.9+, Flask, Playwright (Chromium), asyncio, SQLite, vanilla JS + CSS

---

## File Structure

```
auctionwatch.py   CLI entry point and scraping orchestrator
web.py            Flask server + full single-page UI (HTML/CSS/JS inlined)
scrapers.py       All eleven Playwright-based scrapers + JS extractor
models.py         Listing dataclass + source color maps
store.py          JSON (local) and SQLite (web) persistence
version.py        __version__ = "vX.Y.Z" — bumped every release, shown in the UI header
```

---

## Data Model

```python
@dataclass
class Listing:
    title: str          # "2014 Porsche 911 Carrera S"
    url: str            # canonical listing URL
    source: str         # "Cars & Bids" | "Bring a Trailer" | "Hagerty" | "PCar Market" |
                        # "Craigslist" | "Cars.com" | "Porsche Finder" | "CarMax" |
                        # "Carvana" | "eBay Motors" | "Hemmings"
    price: str = ""     # "$42,500" or ""
    mileage: str = ""   # "23,456 mi" (Cars.com, PF, CarMax, Carvana)
    location: str = ""  # "Portland, OR"
    status: str = ""    # unused
    time_left: str = "" # "2:14:33" | "3 days" | "1D 11H 18M" | "2D 6H" | "Ended" | ""
    image_url: str = ""
    bid_count: str = "" # unused

    @property
    def short_id(self) -> str:
        """First 8 hex chars of SHA-256(url_path). Stable cross-session.
        4 chars before v1.7.19; stored 4-char IDs still match as prefixes."""

    @property
    def is_active(self) -> bool | None:
        """True if time_left has digits and no ended/sold/closed. None if ""."""
```

**Source badge colors (HTML):**
```
Cars & Bids   → #00bcd4  (cyan)
Bring a Trailer → #4caf50 (green)
Hagerty       → #2196f3  (blue)
PCar Market   → #9c27b0  (magenta)
Craigslist    → #ff9800  (orange)
Cars.com      → #e91e63  (pink)
Porsche Finder → #d5001c (Porsche red)
CarMax        → #c9201f  (red)
Carvana       → #00a78e  (teal)
eBay Motors   → #e43137  (red)
Hemmings      → #b22222  (firebrick)
```

---

## Scrapers

All scrapers share the signature `scrape_x(page, query, debug, zip_code="", radius=0) -> list[Listing]`. Cars & Bids and Bring a Trailer use the shared `_JS_EXTRACT` extractor; Hagerty, Cars.com, Craigslist, eBay, and CarMax have site-specific JS extractors; PCar Market, Porsche Finder, Carvana, and Hemmings read embedded JSON / internal APIs. `_num()` coerces string prices/mileage (`"56,900.00"`) before numeric formatting so one malformed listing can't kill a scraper.

### Universal JS Extractor (`_JS_EXTRACT`)

Injected via `page.evaluate(_JS_EXTRACT, linkSelector)`. Returns list of dicts.

**Algorithm:**
1. Query all `a` elements matching `linkSelector`
2. Skip links with empty `textContent`
3. For each link, `findCard(link)`:
   - Walk up DOM from the link
   - At each ancestor, count distinct `href` values matching the selector
   - If count > 1, return the previous (just-below) element — that's the card boundary
   - Stop early at `<li>` or `<article>` tags
   - Give up after 8 levels, return last visited element
4. Extract from card:
   - **title** via `findTitle(link, card)`:
     - Prefer first `h2/h3/h4/strong` whose full text is not in `UI_NOISE`
     - Fall back to longest leaf text node: 8–120 chars, no time/price/year-only patterns, not in `UI_NOISE`
     - Last resort: link's own text
   - **price** via `findPrice(link, card)` (4-tier cascade — see below)
   - **timeLeft** via `findTimeLeft(card)` (priority order — see below)
   - **location** via `findLocation(card)`: leaf matching `/^[A-Z][a-z]+,\s*[A-Z]{2}$/`
   - **imageUrl**: first `img` in card — check `src`, `data-src`, `data-lazy-src`; skip data URIs
5. Dedup by URL, keeping the entry with the longest title

**UI_NOISE** (exact-match strings that are skipped as titles):
```
Bid, Watch, Share, Login, Sign, Save Listing, Save, Register, Submit, Buy, Sell,
View, More, Details, Photo, Image, Gallery, Featured, Premium, No Reserve, Ready,
Learn, Contact, Make Offer, Ends In, High Bid, Sold For, Starting Bid
```

**Price extraction cascade:**
1. Leaf whose entire text is `/^\s*\$[\d,]+(\.\d{2})?\s*$/`
2. `$` leaf + adjacent sibling that is all digits (Hagerty split rendering)
3. Link text containing `/Bid\s*\$\s*(\d{1,3}(?:,\d{3})*)/i` (comma-aware)
4. Short leaf (<20 chars) loosely containing `/\$[\d,]+/`

**Time-left extraction (priority):**
1. `/\b(\d{1,2}:\d{2}:\d{2})\b/` → HH:MM:SS
2. `/\b(\d+\s+(?:days?|hours?|hrs?|minutes?|mins?))\b/i` → natural language
3. `/^((?:\d+D\s+)?\d+H\s+\d+M)$/i` → PCar compact format
4. `/\b(sold|ended|closed|completed)\b/i` → "Ended"
5. Default: `""`

---

### Cars & Bids

- **URL:** `https://carsandbids.com/search?q={query}`
- **Selector:** `a[href*="/auctions/"]`
- **Wait:** `domcontentloaded` (React SPA, never reaches networkidle)
- **Post-load:** Repeatedly find and click "Load More"/"Show More" buttons + scroll until no more appear
- **Title quirk:** Extracted from URL slug `/auctions/{id}/{year-make-model}` → `title()` cased. DOM title unreliable.
- **time_left:** Defaults to `"Ended"` if not found (auction-only site)

---

### Bring a Trailer

- **URL:** `https://bringatrailer.com/search/?s={query}` (redirects to `/saab/` etc.)
- **Selector:** `a[href*="/listing/"]`
- **Wait:** `load` (full page load), then 2s pause for Knockout.js to initialize
- **Post-load:** Scroll to bottom, then `wait_for_load_state("networkidle")` to let Knockout.js AJAX-load completed listings
- **Card structures:** Two types:
  - Live listings: `<div class="listing-card">` with `<a class="image-overlay">` (no text, skipped) + `<h3><a>title</a></h3>`
  - Completed listings: `<a class="listing-card">` (the card IS the link) with `<h3 data-bind="html: title">` inside
- **Year fallback:** If extracted title lacks `/\b(?:19[5-9]\d|20[0-2]\d)\b/`, prepend year from URL slug `/listing/YYYY-make-model/`
- **time_left:** Defaults to `"Ended"` if not found (auction-only site). Live auctions have `<span class="countdown-text">HH:MM:SS</span>` or `"X days"` text.

---

### Hagerty

- **URL:** `https://www.hagerty.com/marketplace/search?searchQuery={query}&sortBy=recommended`
- **Selector:** `a[href*="/marketplace/auction/"], a[href*="/marketplace/classified/"], a[href*="/marketplace/listings/"]`
- **Custom extractor `_HAGERTY_JS`** (not `_JS_EXTRACT`): walks up to the card boundary, prefers `h4/h3/h2/strong` titles, prepends year from card text when missing, and parses card text for time state: `"5 days Bid…"` → `5D`, `"Sold for…"` → `Sold`, `"Bid to…"` → `Ended`, `"Asking price…"` → classified (empty time)
- **Wait:** `domcontentloaded` + scroll to bottom
- **Post-filter:** Drop entries matching `/why hagerty|hagerty marketplace\?/i`

---

### PCar Market

- **URL:** `https://www.pcarmarket.com/auctions` (no query in URL)
- **Strategy:** Does NOT use `_JS_EXTRACT`. Instead:
  1. Navigate to `/auctions`
  2. Wait for `#__PRELOADED_AUCTIONS_LIST__` script element
  3. Read and parse JSON from that element
  4. Apply client-side query filter: all query words (>2 chars) must appear in listing title
  5. Pagination: check for enabled "Next page" button → click → wait for DOM content to change → repeat
- **Time formatting:** `_fmt_pcar_time(seconds)` → `"XD YH ZM"` or `"YH ZM"`
- **Dedup:** By listing `slug` field

---

### Craigslist

- **URL:** `https://{metro}.craigslist.org/search/cta?query={query}&srchType=T&bundleDuplicates=1`
  - `srchType=T` = titles only
  - `bundleDuplicates=1` = suppress cross-posts
- **Metros (42 total, see `CL_METROS`):**
  - **PNW:** seattle, spokane, bellingham, olympic, yakima, kpr, wenatchee, portland, eugene, salem, bend, medford, boise
  - **West/SW:** losangeles, orangecounty, inlandempire, sandiego, sfbay, sacramento, lasvegas, phoenix, saltlake, denver
  - **Texas:** dallas, houston, austin, sanantonio
  - **Midwest:** chicago, detroit, minneapolis, stlouis, kansascity
  - **South/East:** atlanta, nashville, charlotte, miami (South Florida), tampa, orlando, newyork, philadelphia, washingtondc, boston

Gotchas encoded in the list: `olympia.craigslist.org` does not exist (Olympia proper is a Seattle-site subarea), and `tricities.craigslist.org` is Tri-Cities **Tennessee** — Washington's is `kpr` (Kennewick-Pasco-Richland).

- **Browser:** Separate Playwright browser instance from auction sites (prevents viewport/I/O starvation)
- **Per-metro:** Fresh page per metro (avoids session-based rate limiting); metros run 4 at a time via `asyncio.Semaphore` — ~40 metros complete in roughly the time 18 sequential ones did
- **Attribution note:** cross-metro dedup keeps the first copy seen, so per-metro counts are first-come under concurrency
- **Viewport:** 1280×20000 before navigation (forces IntersectionObserver to load all ~160 results)
- **Image capture:** Intercept `response` events for `images.craigslist.org/d/{pid}/...` → build `pid → image_url` map
- **Selector:** `.cl-search-result` → `a.posting-title`
- **Dedup:** First by PID, then by case-insensitive (title, price) pair — same car cross-posted to several metros shares both; distinct cars sharing a title usually differ in price. Filter out `vancouver.craigslist.org` URLs.

---

### Cars.com

- **Base URL:** `https://www.cars.com/shopping/results/?keyword={query}&stock_type=all&maximum_distance=all&sort=list_price_asc`
- **Pagination:** Pages 1–10 (append `&page={n}`)
- **Selector:** `fuse-card[id^="vehicle-card-"]` (web component)
- **Custom extractor `_CARS_COM_JS`** (not `_JS_EXTRACT`):
  - Price from `span.spark-body-larger`
  - Mileage from `.datum-icon.mileage`
  - Location from `.datum-icon` not matching mileage/price-drop/review-star classes
- **Stops** when a page returns 0 new (deduplicated by URL) listings
- **Location:** `&zip={zip}&maximum_distance={radius|all}` when a ZIP is given

---

### Porsche Finder

- **URL:** `https://finder.porsche.com/us/en-US/search/{model}?model={model}` when a model keyword (911, cayman, taycan, …) is detected in the query via `_PF_MODELS`; plain `/search` otherwise. Pagination appends `page={n}` with `&` or `?` as appropriate; 5 pages max, stop early when a page has <15 items.
- **Strategy:** JSON-LD — first `<script type="application/ld+json">` with `@type: ItemList`
- **Critical wait:** the site hydrates client-side and rewrites its URL with a geo `position` param a few seconds after load. `wait_for_function` polls for the ItemList JSON-LD (15s), and the extract retries up to 3× if the JS context is destroyed by that navigation. A fixed pause loses this race on slow containers.
- **Client filter:** all significant query words (minus "porsche"/stopwords) must appear in the title
- **Title:** `"{modelDate} Porsche {vehicleConfiguration|name}"`

---

### CarMax

- **URL:** `https://www.carmax.com/cars?search={query}` (+ `&zip=` when given)
- **Page 1:** `_CARMAX_JS` regex-extracts `const cars = [...]`, `totalCount`, `zipCode`, `requestedUrl` from inline scripts
- **Pages 2+:** internal API `GET /cars/api/search/run?uri=…&skip=N&take=24&zipCode=…&visitorID={uuid4}` via in-page `fetch` (credentials included); up to 5 pages total
- **Client filter:** every query word must appear in the built title (`year make model trim`)
- **Dedup:** by `stockNumber`; listing URL is `/car/{stockNumber}`

---

### Carvana

- **URL:** `https://www.carvana.com/cars?search={query}`
- **Cloudflare:** waits up to 10s for the Turnstile challenge to clear; if the title still says "Just a moment", **raises** `Blocked by Cloudflare challenge (hosting-provider IP)` so the UI shows an error pill. This always happens on datacenter IPs (e.g. Railway); works on residential IPs.
- **Strategy:** JSON-LD (`Car`/`Vehicle`/`ItemList` entries); `_JS_EXTRACT` on `a[href*="/vehicle/"]` as fallback
- **Price/mileage:** from `offers.price` / `mileageFromOdometer` (dict or scalar), coerced with `_num()`

---

### eBay Motors

- **URL:** `https://www.ebay.com/sch/i.html?_nkw={query}&_sacat=6001&_sop=12&_pgn={n}` (+ `&_stpos={zip}&_sadis={radius}`), 3 pages max
- **Custom extractor `_EBAY_JS`:** `li.s-card` cards; skips "Shop on eBay" placeholders
- **time_left:** `"2d 6h left"` → `"2D 6H"`; empty for fixed-price listings
- **Client filter:** every query word must appear in the title

---

### Hemmings

- **URL flow:** load `https://www.hemmings.com/classifieds/cars-for-sale`, sniff auth headers (`hemmings-secret`, `x-csrf-token`) from the page's own call to `api.hemmings.com/v2/search/listings`. The request handler is registered **before** `goto` — the page calls the API on initial load, no interaction needed. Typing into the "Keyword Search" box is the fallback.
- **Then:** direct API pagination via in-page `fetch`: `?adtype=cars-for-sale&q={query}&per_page=30&page={n}`, 5 pages max
- **Cloudflare:** raises `Blocked by Cloudflare challenge (hosting-provider IP)` when the page title is "Just a moment..." (hosting IPs)
- **time_left:** derived from `end_date` ISO timestamp (`_hemmings_time_left`); statuses sold/ended/expired → "Ended"

---

## Web Server (Flask)

### Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Serve SPA (HTML/CSS/JS all inline) |
| `/login` | GET/POST | Username + password form; registers new usernames on first sign-in |
| `/logout` | GET | Clear session |
| `/api/search/stream` | GET | SSE stream of scraper results |
| `/api/ignore` | POST | Toggle ignore on a listing short_id |
| `/api/star` | POST | Toggle star on a listing short_id |
| `/api/start` | POST | Set "seen below" start marker |
| `/api/store` | GET | Return starred, ignored, start_id |
| `/api/searches` | GET | Return recent search history (10 most recent) |

### Auth

- Passwords hashed with werkzeug `pbkdf2:sha256` (not scrypt — Python 3.9 builds may lack `hashlib.scrypt`)
- New username → account created; existing account with empty `password_hash` (pre-password era) → claimed by first successful login; otherwise `check_password_hash` must pass
- Session cookie signed with a 32-byte key persisted at `$DATA_DIR/.auctionwatch.secret`, chmod 600
- The signed-in username is HTML-escaped before interpolation into the header
- The three write endpoints return `{"ok": true, "saved": bool}` — `saved: false` for guests (nothing persisted); the client shows a one-time "sign in to keep stars & ignores" notice

### SSE Streaming (`/api/search/stream`)

Query params: `q` (query), `sites` (repeated: `sites=cab&sites=bat&…`; keys: `cab,bat,hagerty,pcar,pf,cl,carscom,carmax,carvana,ebay,hemmings`), `active=1` (server-side active-only filter), `zip`, `radius` (non-numeric radius falls back to 0)

1. Create `queue.Queue`
2. Spawn daemon thread with new asyncio event loop
3. Run the scrape coroutine:
   - Non-Craigslist scrapers share one Playwright browser (one page each)
   - Craigslist uses a separate Playwright browser
   - All scrapers run concurrently via `asyncio.gather`
   - Each result pushed to queue: `{"site": key, "listings": [...], "stats": {...}}`
   - Final item: `None` sentinel
4. Flask generator reads queue, yields SSE frames:
   - `event: site\ndata: {json}\n\n`
   - `event: done\ndata: {"start_id": "...", "ignored": [...]}\n\n`

---

## Persistence (`store.py`)

**Local mode** (CLI): `~/.auctionwatch.json`
```json
{
  "ignored": ["ab12cd34", "cd34ef56"],
  "starred": ["ef56ab12"],
  "start": "gh78cd12"
}
```

**Web mode** (Flask + SQLite): DB at `$DATA_DIR/.auctionwatch.db` (DATA_DIR defaults to `~`)

Tables:
- `users(id, username UNIQUE COLLATE NOCASE, password_hash)`
- `ignored(user_id, listing_id)` — PK (user_id, listing_id)
- `starred(user_id, listing_id)` — PK (user_id, listing_id)
- `user_start(user_id PK, listing_id)` — single row per user
- `searches(id, user_id, query, searched_at)` — pruned to 10 most recent per user

**Legacy ID compatibility:** stored 4-char IDs (pre-v1.7.19) are matched by prefix everywhere (`inSet()` client-side, `startswith`/`[:4]` checks server- and CLI-side). Un-ignoring/un-starring deletes both the 8-char and legacy 4-char rows.

---

## Frontend (Single-Page App in `web.py`)

All HTML, CSS, and JavaScript is inlined in a single Python string template served from `/` (`{{version}}` and `{{auth_link}}` placeholders substituted).

### Theming (day/night)

All colors are CSS variables: `:root` defines the **day palette (default)**, `:root[data-theme="dark"]` the night palette. A ☾/☀ header button (`#theme-toggle`) switches themes and persists the choice to `localStorage['aw-theme']`; a one-line `<head>` script applies the stored theme before first paint (no flash). The login page uses the same mechanism. Tinted backgrounds derive from the palette with `color-mix()`.

### Global State

```javascript
let st = {
  bysite: {},        // { "bat": [Listing, ...], "cab": [...], ... }
  siteData: {},      // { "bat": { elapsed, total, error, metros } }
  serverStart: '',   // short_id of the last-seen listing from previous session
  lastQ: '',         // last query string
  lastT: '',         // last timestamp string
  starred: new Set(),
  ignored: new Set(),
  tagState: new Map(), // tag → "require" | "prohibit"
  es: null,          // current EventSource
};
```

### URL State

Encoded as query params, restored on page load:
- `q` — search query
- `s` — active site keys (comma-separated)
- `cars`, `active`, `starred`, `ignored` — filter pill states (0/1)
- `ylo`, `yhi`, `plo`, `phi` — year/price range inputs
- `sort` — sort key
- `tr` — required tags (comma-separated)
- `tp` — prohibited tags (comma-separated)

### Filter Pipeline (`allListings()`)

Applied in order:
1. Site filter (enabled site pills)
2. Cars only: `YEAR_RE = /\b(19[5-9]\d|20[0-2]\d)\b/` must match title
3. Active only: `time_left` contains digits and no `ended|sold|closed`
4. Ignored/starred toggles
5. Year range (extract from title with `/\b(19[0-9]{2}|20[0-2][0-9])\b/`)
6. Price range (strip non-digits, parseInt)
7. **Snapshot `_preTag`** (save pre-tag listings for tag bar)
8. Tag filters: require (title must match `\btag\b`) / prohibit (must not)
9. Sort

### Tag Bar (`renderTagBar`)

- Receives `listings._preTag` (pre-tag-filter snapshot) — so active tags don't collapse sibling tags
- Only non-Craigslist listings contribute to tag counts
- Only shows when ≥2 non-CL listings present
- Shows up to 60 tags, alphabetically sorted
- Threshold: tag must appear in ≥2 listings AND in <100% of listings (unless currently active)
- Active tags always shown even outside threshold
- Click cycles: off → require (✓) → prohibit (✕) → off

**`tokenizeTitle(title)`:**
- Split on `/[\s\/,()\[\]&+#@!?:;'"]+/`
- Lowercase, strip non-alphanumeric except `-` and `.`
- Filter: length ≥ 2, not a year (`/^(19|20)\d{2}$/`), not in stopword list
- Deduplicate

**Stopwords** (150+ items): common English words + car UI terms (bid, car, auto, vehicle, used, new, sale, auction, reserve, and/with/for/the/etc.)

### Card Rendering

Each card:
```
┌─────────────────────────┐
│    [image 165px tall]   │  lazy-loaded, onerror hides
│                 ✕  ⚑  ★ │  ignore / start-marker / star buttons
├─────────────────────────┤
│ [id] [SOURCE BADGE]     │
│ Title text              │
│ $price                  │
│ [time badge]            │
└─────────────────────────┘
```

- **⚑** sets the "seen below" start marker at this card (`setStart` → `/api/start`)
- Membership checks use `inSet(set, id)` — matches full 8-char ID or legacy 4-char prefix
- `short_id` shown in monospace gold
- Source badge: colored background per source
- Price: green text
- Time badge: green if active, gray if ended/unknown
- Ignored listings: dimmed (opacity 0.38) unless in "ignored only" mode
- Starred listings: gold border
- "Seen below" divider: `serverStart` listing position; below it all cards dimmed

### Site Status Pills

Each site pill shows:
- Spinner while loading
- `N · SiteName` when count = visible, or `visible/total · SiteName` when filtered
- Error state (red) on scraper failure
- Hover tooltip: site name, listing count, elapsed time, error details, metro count (CL only)

### Sort Options

| Value | Behavior |
|---|---|
| `time` | `tlMinutes(time_left)` ascending (default) |
| `price_asc` | `parsePrice()` ascending, nulls last |
| `price_desc` | `parsePrice()` descending, nulls last |
| `year_asc` | `extractYear()` ascending, nulls first |
| `year_desc` | `extractYear()` descending, nulls last |

`tlMinutes()` converts all time formats (HH:MM:SS, "X days", "XD YH ZM", "2D 6H") to minutes. "Ended"/unknown = Infinity (sorted to end). It tracks whether any component *matched* rather than testing `total > 0`, and parses seconds from HH:MM:SS — so a `0:00:45` countdown sorts first instead of being treated as ended. `_time_left_minutes()` in Python mirrors this exactly.

---

## Known Workarounds

1. **C&B React SPA:** Use `domcontentloaded`, not `networkidle`. Clicks "Load More" buttons repeatedly.
2. **BaT Knockout.js:** 2s hardcoded pause post-load, then scroll, then `networkidle` wait.
3. **BaT dual card types:** Live = div card; Completed = anchor card. Both handled by `_JS_EXTRACT`.
4. **BaT year fallback:** Prepend year from URL slug when title lacks one.
5. **PCar JSON blob:** Reads `#__PRELOADED_AUCTIONS_LIST__` script element; no DOM scraping.
6. **CL separate browser:** Prevents 20000px viewport and 100+ image requests from starving auction site scrapers.
7. **CL fresh page per metro:** Avoids session-based rate limiting.
8. **CL image interception:** Captures `images.craigslist.org` response URLs to map PID → image.
9. **CL Vancouver filter:** Drop any result with `vancouver.craigslist.org` in URL.
10. **Hagerty promo filter:** Drop entries matching `/why hagerty|hagerty marketplace\?/i`.
11. **PCar client query filter:** Check all query words against title; drop mismatches.
12. **C&B title from slug:** DOM title unreliable; reconstruct from URL slug.
13. **Tag bar pre-tag snapshot:** `_preTag` attached to allListings result ensures selecting "turbo" doesn't hide "9-5" from tag bar.
14. **Price comma-aware regex:** `/Bid\s*\$\s*(\d{1,3}(?:,\d{3})*)` prevents grabbing "Bid $17,2512002".
15. **Longest-title dedup:** Multiple anchors per listing → keep longest title.
16. **PF geo-redirect race:** finder.porsche.com rewrites its URL with a `position` param seconds after load; wait for the ItemList JSON-LD itself and retry the extract on "Execution context was destroyed".
17. **Hemmings header sniffing:** register the request handler *before* `goto` — the page calls its search API on initial load, so no interaction is needed to capture auth headers.
18. **Cloudflare on hosting IPs:** Carvana and Hemmings serve a "Just a moment..." challenge to datacenter IPs that never clears headlessly; scrapers raise a descriptive error so the UI shows an error pill instead of a silent 0. Unfixable in code; a residential proxy would be required.
19. **C&B Load More cap:** the click loop is bounded (20 rounds) so a sticky button can't hang the scraper.
20. **String numerics:** JSON-LD/API prices and mileage may be strings with commas; `_num()` coerces before `:,.0f` formatting.

---

## CLI Usage

```bash
# Basic search
python auctionwatch.py "porsche 911"

# Output HTML file and open in browser
python auctionwatch.py "saab 9-3" --html --open

# JSON to stdout (includes short_id / is_active)
python auctionwatch.py "alfa romeo" --json

# Restrict sites
python auctionwatch.py "porsche 911" --cab --bat --pf

# ZIP/radius (Cars.com, eBay, CarMax)
python auctionwatch.py "porsche 911" --zip 98101 --radius 100

# Ignore a listing / set the seen-marker (IDs from the table)
python auctionwatch.py --ignore a3f2c91b
python auctionwatch.py --start a3f2c91b

# Debug mode (shows browser, saves debug_*.html files)
python auctionwatch.py "land rover defender" --debug

# Web server
python auctionwatch.py --serve            # 127.0.0.1:5173, opens browser
python auctionwatch.py --serve --port 8000 --host 0.0.0.0
```

**Dependencies:** playwright, playwright-stealth, flask, rich (see requirements.txt)

**Browser:** Chromium via Playwright. User-agent spoofed as Chrome 122 on macOS.

**Deployment:** Dockerfile (python:3.12-slim + Chromium) + railway.toml; Railway auto-deploys on push to main. `DATA_DIR=/data` expects a mounted volume; `$PORT` is honored and binds 0.0.0.0.
