# AuctionWatch — Architecture

## Overview

AuctionWatch drives a headless Chromium browser via Playwright to scrape eleven JavaScript-heavy car-listing sites simultaneously. It supports three output modes: terminal table, static HTML, and an interactive multi-user web UI.

## Modules

```
auctionwatch.py   CLI entry point, ALL_SITES registry, scrape orchestration, terminal/HTML output
scrapers.py       All eleven Playwright scrapers, the shared _JS_EXTRACT extractor, CL_METROS
web.py            Flask server, SSE streaming, auth, and the full single-page UI (inlined)
models.py         Listing dataclass + source color maps
store.py          JSON persistence (CLI) and SQLite persistence (web, per-user)
version.py        __version__, bumped every release; shown in the UI header
```

## High-level flow

```
main()
  ├─ parse args
  ├─ --ignore/--start  →  update ~/.auctionwatch.json
  ├─ --serve  →  serve_web()        (Flask + SSE, blocking)
  └─ otherwise → asyncio.run(run())
                    └─ _scrape_all()
                         └─ async_playwright → browser → one page per site, in parallel
                              ├─ scrape_carsandbids()   ├─ scrape_pf()
                              ├─ scrape_bat()           ├─ scrape_carmax()
                              ├─ scrape_hagerty()       ├─ scrape_carvana()
                              ├─ scrape_pcarmarket()    ├─ scrape_ebay()
                              ├─ scrape_craigslist()    └─ scrape_hemmings()
                              └─ scrape_cars_com()
                    filter (ignored, active/inactive)
                    sort by time remaining
                    display_terminal()  /  generate_html()  /  JSON stdout
```

Every scraper has the same signature: `scrape_x(page, query, debug, zip_code="", radius=0) -> list[Listing]`. ZIP/radius are honored by Cars.com, eBay Motors, and CarMax; other scrapers ignore them.

## Scraping strategy

Three families of scrapers:

1. **DOM-walking via `_JS_EXTRACT`** (Cars & Bids, Bring a Trailer, Carvana fallback) — a JavaScript extractor injected with `page.evaluate()` that anchors on listing-URL patterns and walks the DOM, avoiding minified class names.
2. **Site-specific JS extractors** (Hagerty `_HAGERTY_JS`, Cars.com `_CARS_COM_JS`, Craigslist `_CL_JS`, eBay `_EBAY_JS`, CarMax `_CARMAX_JS`) — used where the generic walker is unreliable.
3. **Embedded JSON / internal APIs** (PCar Market preloaded JSON, Porsche Finder JSON-LD, Carvana JSON-LD, CarMax search API, Hemmings search API) — the most robust option where available.

### `_JS_EXTRACT` — client-side JS injected via `page.evaluate()`

1. **`findCard(link)`** — walks up from a known anchor (`<a href="/auctions/...">`) until it reaches a node containing more than one distinct listing URL; the node just below that boundary is the card container.
2. **`findPrice(link, card)`** — four-tier cascade: strict `$XX,XXX` leaf → `$`+digits sibling pair (Hagerty split rendering) → comma-aware `"Bid $X,XXX"` in link text → loose short leaf with a `$` pattern.
3. **`findTitle(link, card)`** — prefers a semantic heading (`h2/h3/h4/strong`); falls back to the most title-like leaf (8–120 chars, not a price/time/UI string per the `UI_NOISE` regex).
4. **`findTimeLeft(card)`** — `HH:MM:SS` countdown → natural language (`"3 days"`) → PCar compact (`"1D 11H 18M"`) → `"Ended"` on sold/ended/closed words.
5. **`findLocation(card)`** — matches a `"City, ST"` leaf.

Dedupes by URL, keeping the entry with the longest title.

### Per-site notes

| Site | Strategy | Notes |
|---|---|---|
| Cars & Bids | `_JS_EXTRACT` on `a[href*="/auctions/"]` | Title rebuilt from URL slug; clicks "Load More" (capped at 20 rounds); no countdown ⇒ "Ended" |
| Bring a Trailer | `_JS_EXTRACT` on `a[href*="/listing/"]` | 2s pause for Knockout.js, scroll, networkidle; year prepended from slug if missing |
| Hagerty | `_HAGERTY_JS` | Matches auction/classified/listings links; parses "5 days Bid…", "Sold for…", "Bid to…" card text |
| PCar Market | `#__PRELOADED_AUCTIONS_LIST__` JSON | Paginates by clicking Next and waiting for the JSON blob to change; client-side query-word filter |
| Craigslist | `_CL_JS` per metro | ~40 major US metros, fresh page each, 4 concurrently; 20000px viewport; image URLs sniffed from network responses; dedup by pid then (title, price) |
| Cars.com | `_CARS_COM_JS` on `fuse-card` | Pages 1–10, stops on no new listings; zip/radius params |
| Porsche Finder | JSON-LD `ItemList` | Model keyword → targeted URL; **waits for the ItemList JSON-LD** (the site rewrites its URL with a geo `position` param a few seconds after load — a fixed pause loses that race) and retries the extract if the JS context is torn down |
| CarMax | `_CARMAX_JS` + internal `/cars/api/search/run` | Page 1 from embedded script constants, then API pagination (~5 pages); client-side title match |
| Carvana | JSON-LD, `_JS_EXTRACT` fallback | Waits up to 10s for Cloudflare Turnstile; **raises** "Blocked by Cloudflare challenge" if it never clears (hosting-provider IPs) |
| eBay Motors | `_EBAY_JS` on `li.s-card` | Category 6001, 3 pages; "2d 6h left" normalized to "2D 6H"; zip via `_stpos`/`_sadis` |
| Hemmings | Internal `api.hemmings.com/v2/search/listings` | Auth headers sniffed from the page's own initial-load API call (handler registered **before** goto); search-box typing is the fallback; raises on Cloudflare block |

**Numeric coercion** — `_num()` coerces JSON prices/mileage that may arrive as strings (`"56,900.00"`) before `:,.0f` formatting, so one odd listing can't kill a whole scraper.

### `_scrape_all()`

Opens a single browser context, creates one tab per enabled site, and runs all scrapers concurrently with `asyncio.gather()`. The web server variant runs Craigslist in a **separate browser** so its huge viewport and image traffic don't starve the auction scrapers.

## Data model

```python
@dataclass
class Listing:
    title: str
    url: str
    source: str
    price: str = ""
    mileage: str = ""
    location: str = ""
    status: str = ""
    time_left: str = ""
    image_url: str = ""
    bid_count: str = ""

    @property
    def short_id(self) -> str:
        # SHA-256 of the URL path (no query params), first 8 hex chars.
        # 8 chars since v1.7.19 (4 collided at realistic result counts);
        # stored 4-char IDs are still honored as prefix matches everywhere.

    @property
    def is_active(self) -> bool | None:
        # True = has a numeric time remaining, False = ended/sold, None = unknown (classifieds)
```

## Sorting

`_time_left_minutes(time_left)` (Python) and `tlMinutes()` (JS) convert time-left strings to minutes, parsing D/H/M components and falling back to `HH:MM:SS` (seconds included, so a `0:00:45` auction sorts first rather than as ended). Ended/unknown → infinity. Both track whether *anything* parsed rather than testing `total > 0`.

## Persistence

**CLI** — `~/.auctionwatch.json`:

```json
{ "ignored": ["a3f2c91b"], "start": "c7de1902", "starred": ["bb01aa23"] }
```

**Web** — SQLite at `$DATA_DIR/.auctionwatch.db` (defaults to `~`); tables `users(id, username, password_hash)`, `ignored(user_id, listing_id)`, `starred(user_id, listing_id)`, `user_start(user_id, listing_id)`, `searches(user_id, query, searched_at)`. Passwords are pbkdf2-hashed (werkzeug; pbkdf2 rather than scrypt for Python 3.9 compatibility). Accounts created before passwords existed are claimed by their first successful login. The Flask session key is stored in `$DATA_DIR/.auctionwatch.secret` with mode 600.

Un-ignoring/un-starring also deletes any legacy 4-char row for the same listing.

## Output modes

### Terminal (`display_terminal`)

Rich table; column widths fit content with caps. Listings before `start_id` print normally, then a `─── seen below ───` divider and dimmed rows. Start-ID matching uses `startswith` so legacy 4-char markers still work.

### Static HTML (`generate_html`)

A self-contained dark-themed card grid rendered as an f-string template. No JavaScript; purely static. (Not themed — the interactive UI's day/night mode does not apply here.)

### Interactive web UI (`serve_web`)

Flask application:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the single-page app |
| `/login` | GET/POST | Username+password sign-in; registers new usernames |
| `/logout` | GET | Clears the session |
| `/api/search/stream` | GET | SSE stream; `site` event per scraper, then `done` |
| `/api/ignore` | POST | Toggle ignore for the signed-in user |
| `/api/start` | POST | Set the start marker |
| `/api/star` | POST | Toggle star |
| `/api/store` | GET | Current ignored/start/starred state |
| `/api/searches` | GET | 10 most recent queries |

The three write endpoints return `{"ok": true, "saved": bool}` — `saved` is false for guests, and the client shows a one-time "sign in to keep stars & ignores" notice.

**Async-to-sync bridge** — Flask is synchronous but Playwright needs asyncio. `search_stream` starts a daemon thread with a fresh event loop, runs the scrape, and pushes per-site results into a `queue.Queue`; the Flask generator yields SSE frames from the queue.

## Web UI (embedded in `_WEB_HTML`)

A complete single-page app in one raw string. No build step; no external dependencies.

**Theming** — all colors are CSS variables. `:root` holds the day palette (default); `:root[data-theme="dark"]` holds the night palette. A ☾/☀ header button toggles and persists the choice in `localStorage` (`aw-theme`); a one-line `<head>` script applies it before first paint. The login page shares the mechanism.

Key client-side state:

```js
let st = {
  bysite: {},          // site_key → Listing[]  (full unfiltered results per site)
  siteData: {},        // site_key → {stats, error} for status-pill tooltips
  serverStart: '',     // short_id of seen-divider
  starred: new Set(),  // may contain legacy 4-char IDs
  ignored: new Set(),
  tagState: new Map(), // tag → 'require' | 'prohibit'
  es: null,            // active EventSource
};
```

`inSet(set, id)` checks membership by full ID **or** 4-char prefix (legacy compatibility).

`allListings()` derives the visible list on every render: site pills → cars-only → active-only → ignored/starred → year/price ranges → tag filters (with a `_preTag` snapshot for the tag bar) → sort. All filter state round-trips through the URL query string.

**Card actions:**
- **✕** (ignore) — fades the card out, POSTs `/api/ignore`
- **⚑** (start marker) — sets the "seen below" divider at this card, POSTs `/api/start`
- **★** (star) — toggles gold border in place, POSTs `/api/star`

## Deployment

`Dockerfile` (python:3.12-slim + Chromium) with `railway.toml`; Railway auto-deploys on push to main. `DATA_DIR=/data` expects a mounted volume. Carvana and Hemmings are Cloudflare-blocked on hosting-provider IPs and surface that as scraper errors rather than empty results.
