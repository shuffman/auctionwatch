# AuctionWatch — Architecture

## Overview

AuctionWatch is a single-file Python script (`auctionwatch.py`) that drives a headless Chromium browser via Playwright to scrape four JavaScript-heavy auction SPAs simultaneously. It supports three output modes: terminal table, static HTML, and an interactive web UI.

## High-level flow

```
main()
  ├─ parse args
  ├─ load ~/.auctionwatch.json  (ignored IDs, star marker, starred IDs)
  ├─ --serve  →  serve_web()        (Flask + SSE, blocking)
  └─ otherwise → asyncio.run(run())
                    └─ _scrape_all()
                         └─ async_playwright → browser → N pages in parallel
                              ├─ scrape_carsandbids()
                              ├─ scrape_bat()
                              ├─ scrape_hagerty()
                              └─ scrape_pcarmarket()
                    filter (ignored, active/inactive)
                    sort by time remaining
                    display_terminal()  /  generate_html()  /  JSON stdout
```

## Scraping strategy

All four sites are single-page apps that render content via JavaScript. Playwright loads each site in a real Chromium tab, waits for listing anchor elements to appear in the DOM, then runs a JavaScript extractor (`_JS_EXTRACT`) inside the browser context.

### `_JS_EXTRACT` — client-side JS injected via `page.evaluate()`

Rather than depending on minified or generated CSS class names, the extractor anchors on URL patterns and walks the DOM:

1. **`findCard(link)`** — walks up from a known anchor (`<a href="/auctions/...">`) until it reaches a node that contains more than one distinct listing URL. The node just below that boundary is the card container. This is robust to class name changes.

2. **`findPrice(link, card)`** — four-tier cascade:
   - Leaf element whose entire text is `$XX,XXX` (strict match)
   - `$` element adjacent to a digit-only sibling (Hagerty's split rendering)
   - Link text containing `"Bid $X,XXX"` with comma-aware regex
   - Short leaf element loosely containing a `$` pattern

3. **`findTitle(link, card)`** — prefers a semantic heading (`h2/h3/h4/strong`); falls back to the most title-like leaf element (8–120 chars, not a price, not a UI string). A `UI_NOISE` regex excludes button labels like "Save Listing", "Ends In", "High Bid", etc.

4. **`findTimeLeft(card)`** — tries three formats in order:
   - `HH:MM:SS` countdown (Cars & Bids, Bring a Trailer)
   - Natural language: `"3 days"`, `"2 hours"` (Bring a Trailer)
   - PCar Market compact format: `"1D 11H 18M"` / `"11H 17M"`
   - Falls back to `"Ended"` if a sold/ended/closed word is found

5. **`findLocation(card)`** — matches `"City, ST"` (US city+state abbreviation) leaf pattern.

The extractor deduplicates by URL, keeping the entry with the longest title (since multiple links may point to the same listing).

### Per-site scraper details

| Site | Search URL | Link selector | Title source | Notes |
|---|---|---|---|---|
| Cars & Bids | `/search?q=` | `a[href*="/auctions/"]` | URL slug (path segment 2) | `wait_until="domcontentloaded"` |
| Bring a Trailer | `/search/?s=` | `a[href*="/listing/"]` | JS extractor | |
| Hagerty | `/marketplace/search?searchQuery=&type=classifieds` | `a[href*="/marketplace/auction/"]` | JS extractor | Filters "Why Hagerty Marketplace?" promo entries |
| PCar Market | `/auctions` (no query param) | `a[href*="/auction/"]` | URL slug with trailing `-{digits}` stripped | Selects "All Vehicles" category; client-side query-word filter; iterates all pagination pages via "Next" button |

**PCar Market pagination** — the site uses JS-driven pagination with no URL changes. After scraping page 1, the scraper checks for a `button:has-text("Next")` that is not disabled, captures the first listing URL, clicks Next, and waits for that URL to change before scraping the new page. Repeats until no Next button or button is disabled.

**PCar Market category filter** — on load, clicks `"All Categories"` → `"All Vehicles"` and waits for the dropdown button label to confirm the selection before proceeding.

### `_scrape_all()`

Opens a single browser context, creates one tab per enabled site, and runs all scrapers concurrently with `asyncio.gather()`. Accepts an optional `on_site_done` async callback used by the web server to stream results as they arrive.

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
        # SHA-256 of the URL path (no query params), first 4 hex chars
        # Deterministic and stable — same listing always gets the same ID
        path = urlparse(self.url).path.rstrip("/")
        return hashlib.sha256(path.encode()).hexdigest()[:4]

    @property
    def is_active(self) -> bool | None:
        # True = has a numeric time remaining, False = ended/sold, None = unknown
```

## Sorting

`_time_left_minutes(time_left)` converts time-left strings to minutes (parsing D/H/M components) and returns `float("inf")` for ended or unknown listings. Used as the sort key in both the terminal output and the web UI's `allListings()` JS function.

## Persistent store

`~/.auctionwatch.json` is a plain JSON file with three keys:

```json
{
  "ignored": ["a3f2", "bb01"],
  "start": "c7de",
  "starred": ["bb01"]
}
```

- **ignored** — listing short IDs permanently hidden from results
- **start** — short ID of the "seen below" divider; listings at or after this position are dimmed
- **starred** — short IDs the user has starred (gold border in web UI)

All mutations go through atomic read-modify-write helpers (`store_ignore`, `store_set_start`, `store_set_starred`, etc.).

## Output modes

### Terminal (`display_terminal`)

Uses the `rich` library. Column widths are computed as `min(max(header_len, max_content_len), cap) + 2` — never wider than content. Listings before `start_id` are printed normally; a `─── seen below ───` separator precedes the rest, which are dimmed. Query params are stripped from displayed URLs.

### Static HTML (`generate_html`)

A self-contained dark-themed card grid rendered as an f-string template. No JavaScript; purely static.

### Interactive web UI (`serve_web`)

Flask application with four routes:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the single-page app HTML |
| `/api/search/stream` | GET | SSE stream; scrapes and emits `site` events as each scraper finishes, then a `done` event |
| `/api/ignore` | POST | Adds ID to ignored store |
| `/api/start` | POST | Sets start marker in store |
| `/api/star` | POST | Toggles starred state for an ID |
| `/api/store` | GET | Returns current ignored/start/starred state |

**Async-to-sync bridge** — Flask is synchronous but Playwright requires an asyncio event loop. `search_stream` starts a daemon `threading.Thread` that creates a fresh event loop (`asyncio.new_event_loop()`), runs the scrape coroutine, and pushes results into a `queue.Queue`. The Flask generator function reads from the queue and yields SSE frames. This avoids running Playwright inside Flask's request thread.

**SSE streaming** — each site fires a `site` event with `{site, listings[], error?}` as it completes. The client inserts cards immediately. A final `done` event carries the `start_id` from the store so the seen-divider is placed correctly.

## Web UI (embedded in `_WEB_HTML`)

The frontend is a ~170-line raw string constant containing a complete single-page application. No build step; no external dependencies.

Key client-side state:

```js
let st = {
  bysite: {},        // site_key → Listing[]  (full unfiltered results per site)
  serverStart: '',   // short_id of seen-divider
  starred: new Set(),// short_ids that are starred
  lastQ: '',
  lastT: '',
  es: null,          // active EventSource
};
```

`allListings()` derives the visible, filtered, sorted list from `st.bysite` on every render call:
1. Filter to enabled site pills
2. Filter to active-only if that toggle is on
3. Sort by `tlMinutes(time_left)` ascending

Toggling any pill or filter immediately calls `render()` — no round-trip to the server.

**Card actions:**
- **✕** (ignore) — removes card with fade animation, deletes from `st.bysite`, POSTs to `/api/ignore`
- **★** (star) — toggles `.starred` class and `.on` on the button in-place (no full re-render), POSTs to `/api/star`
