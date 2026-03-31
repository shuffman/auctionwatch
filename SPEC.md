# AuctionWatch — Complete Specification

> Use this document to reconstruct the project from scratch.

---

## Overview

AuctionWatch is a Python/Flask web app that aggregates used car listings from six sources in real time. The user enters a search query; results stream in from all sites simultaneously and are displayed in a filterable, sortable card grid.

**Stack:** Python 3.11+, Flask, Playwright (Chromium), asyncio, SQLite, vanilla JS + CSS

---

## File Structure

```
auctionwatch.py   CLI entry point and scraping orchestrator
web.py            Flask server + full single-page UI (HTML/CSS/JS inlined)
scrapers.py       All six Playwright-based scrapers + JS extractor
models.py         Listing dataclass
store.py          JSON (local) and SQLite (web) persistence
version.py        __version__ = "v1.7.3"
```

---

## Data Model

```python
@dataclass
class Listing:
    title: str          # "2014 Porsche 911 Carrera S"
    url: str            # canonical listing URL
    source: str         # "Cars & Bids" | "Bring a Trailer" | "Hagerty" |
                        # "PCar Market" | "Craigslist" | "Cars.com"
    price: str = ""     # "$42,500" or ""
    mileage: str = ""   # "23,456 miles" (Cars.com only)
    location: str = ""  # "Portland, OR"
    status: str = ""    # unused
    time_left: str = "" # "2:14:33" | "3 days" | "1D 11H 18M" | "Ended" | ""
    image_url: str = ""
    bid_count: str = "" # unused

    @property
    def short_id(self) -> str:
        """First 4 hex chars of SHA-256(url_path). Stable cross-session."""

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
```

---

## Scrapers

All scrapers share a Playwright page object and return `list[Listing]`. They use the shared `_JS_EXTRACT` JavaScript extractor except Cars.com (has its own `_CARS_COM_JS`) and PCar Market (reads a JSON blob).

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

- **URL:** `https://www.hagerty.com/marketplace/search?searchQuery={query}&type=classifieds`
- **Selector:** `a[href*="/marketplace/auction/"]`
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
- **Metros (18 total):**

| Name | Subdomain |
|---|---|
| Seattle | seattle |
| Spokane | spokane |
| Bellingham | bellingham |
| Olympia | olympia |
| Yakima | yakima |
| Tri-Cities | tricities |
| Wenatchee | wenatchee |
| Portland | portland |
| Eugene | eugene |
| Salem | salem |
| Bend | bend |
| Medford | medford |
| Boise | boise |
| Los Angeles | losangeles |
| San Francisco | sfbay |
| Las Vegas | lasvegas |
| Phoenix | phoenix |
| Salt Lake City | saltlake |

- **Browser:** Separate Playwright browser instance from auction sites (prevents viewport/I/O starvation)
- **Per-metro:** Fresh page per metro (avoids session-based rate limiting)
- **Viewport:** 1280×20000 before navigation (forces IntersectionObserver to load all ~160 results)
- **Image capture:** Intercept `response` events for `images.craigslist.org/d/{pid}/...` → build `pid → image_url` map
- **Selector:** `.cl-search-result` → `a.posting-title`
- **Dedup:** First by PID, then case-insensitive title. Filter out `vancouver.craigslist.org` URLs.

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

---

## Web Server (Flask)

### Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Serve SPA (HTML/CSS/JS all inline) |
| `/login` | GET/POST | Login form + auth |
| `/api/search/stream` | GET | SSE stream of scraper results |
| `/api/ignore` | POST | Toggle ignore on a listing short_id |
| `/api/star` | POST | Toggle star on a listing short_id |
| `/api/start` | POST | Set "seen below" start marker |
| `/api/store` | GET | Return starred, ignored, start_id |
| `/api/searches` | GET | Return recent search history |

### SSE Streaming (`/api/search/stream`)

Query params: `q` (query), `sites` (comma-separated: `cab,bat,hagerty,pcar,cl,carscom`)

1. Create `queue.Queue`
2. Spawn daemon thread with new asyncio event loop
3. Run `_scrape_all(query, sites, queue)`:
   - Auction scrapers share one Playwright browser (6 pages)
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
  "ignored": ["ab12", "cd34"],
  "starred": ["ef56"],
  "start_id": "gh78"
}
```

**Web mode** (Flask + SQLite): DB at `$DATA_DIR/auctionwatch.db` or `~/.auctionwatch.db`

Tables:
- `users(id, username, password_hash)`
- `ignored(user_id, short_id)`
- `starred(user_id, short_id)`
- `start_id(user_id, short_id)` (single row per user)
- `searches(user_id, query, ts)` — recent search history

---

## Frontend (Single-Page App in `web.py`)

All HTML, CSS, and JavaScript is inlined in a single Python f-string template served from `/`.

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
│                    ✕  ★ │  ignore / star buttons
├─────────────────────────┤
│ [id] [SOURCE BADGE]     │
│ Title text              │
│ $price                  │
│ [time badge]            │
└─────────────────────────┘
```

- `short_id` shown in monospace gray
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

`tlMinutes()` converts all time formats (HH:MM:SS, "X days", "XD YH ZM") to minutes. "Ended"/unknown = very large number (sorted to end).

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

---

## CLI Usage

```bash
# Basic search
python auctionwatch.py "porsche 911"

# Output HTML file and open in browser
python auctionwatch.py "saab 9-3" --html

# Debug mode (shows browser, saves debug_*.html files)
python auctionwatch.py "land rover defender" --debug

# Web server
python web.py
```

**Dependencies:** playwright, playwright-stealth (optional), flask, rich (optional)

**Browser:** Chromium via Playwright. User-agent spoofed as Chrome 122 on macOS.
