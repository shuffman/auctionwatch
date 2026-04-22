from __future__ import annotations

import json as _json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Page
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from playwright_stealth import Stealth
    _STEALTH = Stealth()
    HAS_STEALTH = True
except ImportError:
    _STEALTH = None
    HAS_STEALTH = False


def stealth_playwright():
    """Return a playwright context manager with stealth applied if available."""
    pw = async_playwright()
    return _STEALTH.use_async(pw) if HAS_STEALTH else pw

try:
    from rich.console import Console
    _console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    _console = None

from models import Listing


def _log(msg: str, level: str = "info"):
    if HAS_RICH:
        styles = {"info": "dim", "warning": "yellow", "error": "red bold"}
        _console.print(f"  {msg}", style=styles.get(level, ""))
    else:
        print(f"  [{level.upper()}] {msg}", file=sys.stderr)


def _save_debug(content: str, name: str):
    path = f"debug_{name}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _log(f"Debug HTML saved to {path}", "info")


async def _text(el) -> str:
    """Safely get inner text from an element."""
    if el is None:
        return ""
    try:
        return (await el.inner_text()).strip()
    except Exception:
        return ""


async def _attr(el, attr: str) -> str:
    """Safely get an attribute from an element."""
    if el is None:
        return ""
    try:
        return (await el.get_attribute(attr) or "").strip()
    except Exception:
        return ""


async def _get_img_src(el) -> str:
    """Try src, then data-src, then data-lazy-src for lazy-loaded images."""
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        val = await _attr(el, attr)
        if val and not val.startswith("data:"):
            return val
    return ""


def _abs_url(href: str, base: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


# Porsche model keyword → URL key for finder.porsche.com
_PF_MODELS: list[tuple[str, str]] = [
    ("cayenne", "cayenne"),
    ("macan", "macan"),
    ("taycan", "taycan"),
    ("panamera", "panamera"),
    ("boxster", "718"),
    ("cayman", "718"),
    ("spyder", "718"),
    ("718", "718"),
    ("911", "911"),
    ("carrera", "911"),
    ("targa", "911"),
    ("gt3", "911"),
    ("gt2", "911"),
]


# ─── Scrapers ─────────────────────────────────────────────────────────────────

# JavaScript run inside the browser to extract listing data by walking up from
# known anchor links. This avoids depending on minified/generated class names.
_JS_EXTRACT = """
(linkSelector) => {
    function cardText(el) { return el ? el.textContent.trim().replace(/\\s+/g, ' ') : ''; }

    function findCard(link) {
        // Walk up, stopping just before a container that holds MULTIPLE DISTINCT listings.
        // A card may have several links to the same listing URL (image + title), so we
        // count distinct hrefs rather than total links.
        // prev starts as the link itself so if the very first parent is already a
        // multi-listing container (e.g. Hagerty wraps each card in <a>), we return
        // the link element itself as the card.
        let prev = link;
        let el   = link.parentElement;
        for (let i = 0; i < 8; i++) {
            if (!el || el === document.body) break;
            const distinctHrefs = new Set(
                Array.from(el.querySelectorAll(linkSelector)).map(a => a.href)
            );
            if (distinctHrefs.size > 1) return prev; // gone too far
            const tag = el.tagName.toLowerCase();
            if (tag === 'li' || tag === 'article') return el;
            prev = el;
            el = el.parentElement;
        }
        return prev;
    }

    function findPrice(link, card) {
        // 1. Leaf element whose entire text is a price: "$XX,XXX"
        const strict = Array.from(card.querySelectorAll('*')).find(el =>
            el.children.length === 0 &&
            /^\\s*\\$[\\d,]+(\\.[\\d]{2})?\\s*$/.test(el.textContent)
        );
        if (strict) return cardText(strict);

        // 2. Adjacent siblings: "$" element next to a number element (e.g. Hagerty)
        const dollarEl = Array.from(card.querySelectorAll('*')).find(el =>
            el.children.length === 0 && el.textContent.trim() === '$'
        );
        if (dollarEl && dollarEl.parentElement) {
            const siblings = Array.from(dollarEl.parentElement.children);
            const idx = siblings.indexOf(dollarEl);
            const next = siblings[idx + 1];
            if (next && /^[\\d,]+$/.test(next.textContent.trim())) {
                return '$' + next.textContent.trim();
            }
        }

        // 3. Link text contains "Bid $X,XXX" — use comma-aware regex to avoid
        //    grabbing trailing digits (e.g. "Bid $17,2512002" → "$17,251")
        const lt = link.textContent;
        const bidM = lt.match(/Bid\\s*\\$\\s*(\\d{1,3}(?:,\\d{3})*)/i);
        if (bidM) return '$' + bidM[1];

        // 4. Short leaf element containing a price (cap at 20 chars to avoid descriptions)
        const loose = Array.from(card.querySelectorAll('*')).find(el =>
            el.children.length === 0 &&
            el.textContent.trim().length < 20 &&
            /\\$[\\d,]+/.test(el.textContent)
        );
        return loose ? cardText(loose) : '';
    }

    // UI strings to exclude from title detection
    const UI_NOISE = /^(Bid|Watch|Share|Login|Sign|Save Listing|Save|Register|Submit|Buy|Sell|View|More|Details|Photo|Image|Gallery|Featured|Premium|No Reserve|Ready|Learn|Contact|Make Offer|Ends In|High Bid|Sold For|Starting Bid)$/i;

    function findTitle(link, card) {
        // Prefer a semantic heading
        const h = card.querySelector('h2, h3, h4, strong');
        if (h && !UI_NOISE.test(h.textContent.trim())) return cardText(h);
        // Otherwise find the most "title-like" leaf
        const leaves = Array.from(card.querySelectorAll('*'))
            .filter(el => el.children.length === 0);
        const t = leaves.find(el => {
            const s = el.textContent.trim();
            return s.length > 8 && s.length < 120 &&
                   !/^\\d{1,2}:\\d{2}/.test(s) &&
                   !/^\\$/.test(s) &&
                   !/^[\\d,\\.]+$/.test(s) &&
                   !UI_NOISE.test(s);
        });
        return t ? cardText(t) : cardText(link);
    }

    function findTimeLeft(card) {
        const leaves = Array.from(card.querySelectorAll('*'))
            .filter(el => el.children.length === 0);
        // HH:MM:SS countdown (e.g. C&B, BaT)
        for (const el of leaves) {
            const m = el.textContent.match(/\\b(\\d{1,2}:\\d{2}:\\d{2})\\b/);
            if (m) return m[1];
        }
        // "X days/hours" (natural language)
        for (const el of leaves) {
            const m = el.textContent.match(/\\b(\\d+\\s+(?:days?|hours?|hrs?|minutes?|mins?))\\b/i);
            if (m) return m[1];
        }
        // PCar Market format: "1D 11H 18M" or "11H 17M"
        for (const el of leaves) {
            const t = el.textContent.trim();
            const m = t.match(/^((?:\\d+D\\s+)?\\d+H\\s+\\d+M)$/i);
            if (m) return m[1];
        }
        for (const el of leaves) {
            if (/\\b(sold|ended|closed|completed)\\b/i.test(el.textContent)) return 'Ended';
        }
        return '';
    }

    function findLocation(card) {
        const el = Array.from(card.querySelectorAll('*')).find(el =>
            el.children.length === 0 &&
            /^[A-Z][a-z]+,\\s*[A-Z]{2}$/.test(el.textContent.trim())
        );
        return el ? cardText(el) : '';
    }

    const best = new Map(); // href -> best result (longest title wins)

    document.querySelectorAll(linkSelector).forEach(link => {
        const href = link.href;
        if (!href || !link.textContent.trim()) return;

        const card  = findCard(link);
        const title = findTitle(link, card);
        const img   = card.querySelector('img');
        const imageUrl = img
            ? (img.src || img.dataset.src || img.dataset.lazySrc || '')
            : '';

        if (title) {
            const entry = {
                title,
                url: href,
                price:    findPrice(link, card),
                timeLeft: findTimeLeft(card),
                location: findLocation(card),
                imageUrl: imageUrl.startsWith('data:') ? '' : imageUrl,
            };
            const existing = best.get(href);
            if (!existing || title.length > existing.title.length) {
                best.set(href, entry);
            }
        }
    });

    return Array.from(best.values());
}
"""


async def _eval_listings(page: Page, link_selector: str) -> list[dict]:
    """Run the JS extractor in the page and return raw dicts."""
    try:
        return await page.evaluate(_JS_EXTRACT, link_selector) or []
    except Exception:
        return []


async def _scroll_to_bottom(page: Page, pause_ms: int = 800, max_scrolls: int = 15):
    """Scroll incrementally to trigger lazy-loaded content."""
    for _ in range(max_scrolls):
        prev = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
        curr = await page.evaluate("document.body.scrollHeight")
        if curr == prev:
            break


async def scrape_carsandbids(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Cars & Bids"
    base = "https://carsandbids.com"
    url = f"{base}/search?q={quote_plus(query)}"
    listings = []

    try:
        _log(f"[{source}] Fetching {url}")
        # Use domcontentloaded — C&B is a React SPA that never reaches networkidle
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_selector('a[href*="/auctions/"]', timeout=20000)
        _log(f"[{source}] Page loaded, extracting listings")

        if debug:
            _save_debug(await page.content(), "carsandbids")

        # Click "Load More" until exhausted, scrolling between clicks to trigger lazy loads
        while True:
            await _scroll_to_bottom(page)
            btn = await page.query_selector('button:has-text("Load More"), button:has-text("Show More"), a:has-text("Load More")')
            if not btn:
                break
            await btn.click()
            await page.wait_for_timeout(1500)

        for item in (await _eval_listings(page, 'a[href*="/auctions/"]')):
            url = item.get("url", "")
            if not url:
                continue
            # Derive title from URL slug: /auctions/{id}/{year-make-model}
            parts = urlparse(url).path.strip("/").split("/")
            title = parts[2].replace("-", " ").title() if len(parts) >= 3 else item.get("title", "")
            if title:
                # C&B is auctions-only: no countdown = auction ended
                time_left = item.get("timeLeft", "") or "Ended"
                listings.append(Listing(
                    title=title, url=url, source=source,
                    price=item.get("price", ""), time_left=time_left,
                    image_url=item.get("imageUrl", ""),
                ))
        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


async def scrape_bat(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Bring a Trailer"
    base = "https://bringatrailer.com"
    url = f"{base}/search/?s={quote_plus(query)}"
    listings = []

    try:
        _log(f"[{source}] Fetching {url}")
        await page.goto(url, wait_until="load", timeout=30000)
        await page.wait_for_selector('a[href*="/listing/"]', timeout=20000)
        # Give Knockout.js / lazy-load scripts time to initialize before scrolling
        await page.wait_for_timeout(2000)
        _log(f"[{source}] Page loaded, extracting listings")

        await _scroll_to_bottom(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            pass

        if debug:
            _save_debug(await page.content(), "bat")
        for item in (await _eval_listings(page, 'a[href*="/listing/"]')):
            if item.get("title") and item.get("url"):
                title = item["title"]
                url   = item["url"]
                # If the extracted title is missing a year, pull it from the URL slug
                # (BaT slugs: /listing/2003-saab-9-3-5/ → prepend "2003")
                if not re.search(r'\b(?:19[5-9]\d|20[0-2]\d)\b', title):
                    m = re.search(r'/listing/(\d{4})-', url)
                    if m:
                        title = m.group(1) + ' ' + title
                # BaT is auctions-only: no countdown = auction ended
                time_left = item.get("timeLeft", "") or "Ended"
                listings.append(Listing(
                    title=title, url=url, source=source,
                    price=item.get("price", ""), time_left=time_left,
                    location=item.get("location", ""), image_url=item.get("imageUrl", ""),

                ))
        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


_HAGERTY_LINK_SEL = (
    'a[href*="/marketplace/auction/"], '
    'a[href*="/marketplace/classified/"], '
    'a[href*="/marketplace/listings/"]'
)

_HAGERTY_LINK_SEL_ESC = _HAGERTY_LINK_SEL.replace("'", "\\'")
_HAGERTY_JS = f"""() => {{
    const LINK_SEL = '{_HAGERTY_LINK_SEL_ESC}';
    const YEAR_RE  = /\\b(19[5-9]\\d|20[0-3]\\d)\\b/;
    const results  = [];
    const seen     = new Set();

    document.querySelectorAll(LINK_SEL).forEach(link => {{
        const url = link.href.split('?')[0];
        if (!url || seen.has(url)) return;
        seen.add(url);

        // Walk up to card boundary (stop when parent contains multiple distinct links)
        let card = link, el = link.parentElement;
        for (let i = 0; i < 8; i++) {{
            if (!el || el === document.body) break;
            const hrefs = new Set(Array.from(el.querySelectorAll(LINK_SEL)).map(a => a.href.split('?')[0]));
            if (hrefs.size > 1) break;
            card = el; el = el.parentElement;
        }}

        const cardText = card.textContent.replace(/\\s+/g, ' ').trim();

        // Title: prefer h4 > h3 > h2 > strong, then fall back to largest text span
        const h = card.querySelector('h4, h3, h2, strong');
        let title = h ? h.textContent.trim() : '';
        if (!title) title = link.textContent.trim();
        // Prepend year if missing
        if (title && !YEAR_RE.test(title)) {{
            const m = cardText.match(YEAR_RE);
            if (m) title = m[1] + ' ' + title;
        }}

        // Price: look for "$X,XXX" leaf, or adjacent $ + number spans
        let price = '';
        const priceEl = Array.from(card.querySelectorAll('*')).find(e =>
            e.children.length === 0 && /^\\$[\\d,]+$/.test(e.textContent.trim())
        );
        if (priceEl) {{
            price = priceEl.textContent.trim();
        }} else {{
            const dollarEl = Array.from(card.querySelectorAll('*')).find(e =>
                e.children.length === 0 && e.textContent.trim() === '$'
            );
            if (dollarEl?.parentElement) {{
                const sibs = Array.from(dollarEl.parentElement.children);
                const next = sibs[sibs.indexOf(dollarEl) + 1];
                if (next && /^[\\d,]+$/.test(next.textContent.trim()))
                    price = '$' + next.textContent.trim();
            }}
        }}

        // Time left: parse card text patterns
        //   Active:   "5 days Bid ..."  or  "3 hours Bid ..."
        //   Ended:    "Bid to $ X on MM/DD/YY ..."
        //   Sold:     "Sold for $ X on MM/DD/YY ..."
        //   Classified: "Asking price $ X ..."  (no time)
        let timeLeft = '';
        const dayM  = cardText.match(/^(\\d+)\\s*days?\\b/i);
        const hourM = cardText.match(/^(\\d+)\\s*hours?\\b/i);
        const minM  = cardText.match(/^(\\d+)\\s*min/i);
        if (dayM)       timeLeft = dayM[1]  + 'D';
        else if (hourM) timeLeft = hourM[1] + 'H';
        else if (minM)  timeLeft = minM[1]  + 'M';
        else if (/Sold for/i.test(cardText))  timeLeft = 'Sold';
        else if (/Bid to/i.test(cardText))    timeLeft = 'Ended';
        // "Asking price" = classified, leave timeLeft = ''

        const img = card.querySelector('img');
        const imageUrl = img ? (img.src || img.dataset.src || '') : '';

        results.push({{ url, title, price, timeLeft,
            imageUrl: imageUrl.startsWith('data:') ? '' : imageUrl }});
    }});
    return results;
}}"""


async def scrape_hagerty(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Hagerty"
    base = "https://www.hagerty.com"
    listings = []

    hagerty_url = f"{base}/marketplace/search?searchQuery={quote_plus(query)}&sortBy=recommended"
    try:
        _log(f"[{source}] Fetching {hagerty_url}")
        await page.goto(hagerty_url, wait_until="domcontentloaded", timeout=30000)

        if debug:
            _save_debug(await page.content(), "hagerty")

        await page.wait_for_selector(_HAGERTY_LINK_SEL, timeout=20000)
        _log(f"[{source}] Page loaded, extracting listings")

        await _scroll_to_bottom(page)
        raw = await page.evaluate(_HAGERTY_JS) or []

        seen_urls: set[str] = set()
        for item in raw:
            title = item.get("title", "")
            url   = item.get("url", "")
            if not title or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if re.search(r'why hagerty|hagerty marketplace\?', title, re.IGNORECASE):
                continue
            listings.append(Listing(
                title=title, url=url, source=source,
                price=item.get("price", ""),
                time_left=item.get("timeLeft", ""),
                image_url=item.get("imageUrl", ""),
            ))
        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


_CARS_COM_JS = """() => {
    const results = [];
    document.querySelectorAll('fuse-card[id^="vehicle-card-"]').forEach(card => {
        const link    = card.querySelector('a[href*="/vehicledetail/"]');
        if(!link) return;
        const title   = card.querySelector('h2')?.textContent?.trim() || '';
        if(!title) return;
        // spark-body-larger is the listed price; avoid monthly-payment elements
        const price   = card.querySelector('span.spark-body-larger, p.spark-body-larger')
                            ?.textContent?.trim() || '';
        const mileage = card.querySelector('.datum-icon.mileage')?.textContent?.trim() || '';
        // .datum-icon without sub-class is the dealer location; .datum-icon.mileage is mileage
        const location= card.querySelector('.datum-icon:not(.mileage):not(.price-drop):not(.review-star)')
                            ?.textContent?.trim() || '';
        const img     = card.querySelector('img');
        results.push({
            url:      link.href.split('?')[0],
            title,
            price,
            mileage,
            location,
            imageUrl: img?.src || '',
        });
    });
    return results;
}"""


async def scrape_cars_com(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Cars.com"
    listings = []
    seen_urls: set[str] = set()
    loc = f"&zip={zip_code}&maximum_distance={radius or 'all'}" if zip_code else "&maximum_distance=all"
    base_url = (
        f"https://www.cars.com/shopping/results/"
        f"?keyword={quote_plus(query)}&stock_type=all{loc}&sort=list_price_asc"
    )
    try:
        for page_num in range(1, 11):  # cap at 10 pages (~200 results)
            url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
            _log(f"[{source}] Fetching page {page_num}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector('fuse-card[id^="vehicle-card-"]', timeout=15000)
            except PlaywrightTimeout:
                _log(f"[{source}] No results on page {page_num}, stopping")
                break
            if debug and page_num == 1:
                _save_debug(await page.content(), "cars_com")
            items = await page.evaluate(_CARS_COM_JS) or []
            if not items:
                break
            new_count = 0
            for item in items:
                title = item.get("title", "").strip()
                item_url = item.get("url", "")
                if not title or not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_count += 1
                listings.append(Listing(
                    title=title,
                    url=item_url,
                    source=source,
                    price=item.get("price", ""),
                    mileage=item.get("mileage", ""),
                    location=item.get("location", ""),
                    time_left="",
                    image_url=item.get("imageUrl", ""),
                ))
            _log(f"[{source}] Page {page_num}: {new_count} new listings (total {len(listings)})")
            if new_count == 0:
                break  # no new results — we've exhausted the pages
        _log(f"[{source}] Done — {len(listings)} listings")
    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise
    return listings


CL_METROS = [
    # Pacific Northwest — Washington
    ("Seattle",        "seattle"),
    ("Spokane",        "spokane"),
    ("Bellingham",     "bellingham"),
    ("Olympia",        "olympia"),
    ("Yakima",         "yakima"),
    ("Tri-Cities",     "tricities"),
    ("Wenatchee",      "wenatchee"),
    # Pacific Northwest — Oregon
    ("Portland",       "portland"),
    ("Eugene",         "eugene"),
    ("Salem",          "salem"),
    ("Bend",           "bend"),
    ("Medford",        "medford"),
    # Pacific Northwest — Idaho
    ("Boise",          "boise"),
    # Southwest
    ("Los Angeles",    "losangeles"),
    ("San Francisco",  "sfbay"),
    ("Las Vegas",      "lasvegas"),
    ("Phoenix",        "phoenix"),
    ("Salt Lake City", "saltlake"),
]

_CL_JS = """() => {
    const results = [];
    document.querySelectorAll('.cl-search-result').forEach(div => {
        const pid = div.dataset.pid || '';
        const a = div.querySelector('a.posting-title');
        if (!a || !a.href) return;
        const title = a.textContent.trim();
        if (!title) return;
        const price = div.querySelector('.priceinfo')?.textContent?.trim() || '';
        const img = div.querySelector('img');
        const imgSrc = img ? (img.src || '') : '';
        results.push({
            pid, url: a.href, title, price,
            imageUrl: imgSrc.startsWith('data:') ? '' : imgSrc,
        });
    });
    return results;
}"""


async def scrape_craigslist(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Craigslist"
    listings = []
    seen_pids: set[str] = set()
    seen_titles: set[str] = set()

    # Each metro gets its own page and pid_to_img dict.
    # _on_response is a factory that closes over the per-metro dict.
    def _on_response(pid_to_img: dict):
        def handler(response):
            url = response.url
            if "images.craigslist.org/d/" in url and "empty.png" not in url:
                try:
                    pid = url.split("/d/")[1].split("/")[0]
                    pid_to_img.setdefault(pid, url)
                except Exception:
                    pass
        return handler

    ctx = page.context

    for city_name, subdomain in CL_METROS:
        url = f"https://{subdomain}.craigslist.org/search/cta?query={quote_plus(query)}&srchType=T&bundleDuplicates=1"
        # Fresh page per metro: clears cookies/session so CL can't correlate
        # requests across subdomains and rate-limit after the first hit.
        p = await ctx.new_page()
        pid_to_img: dict[str, str] = {}
        p.on("response", _on_response(pid_to_img))
        try:
            # Large viewport set BEFORE navigation so IntersectionObserver fires
            # for all results that fit within 20 000 px (~160-170 listings).
            await p.set_viewport_size({"width": 1280, "height": 20000})
            await p.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for image requests triggered by the initial render to settle
            try:
                await p.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeout:
                pass
            if debug:
                _save_debug(await p.content(), f"cl_{subdomain}")
            items = await p.evaluate(_CL_JS) or []
            for item in items:
                pid = item.get("pid", "")
                dedup_key = pid or item.get("url", "")
                if not dedup_key or dedup_key in seen_pids:
                    continue
                title = item.get("title", "").strip()
                if not title:
                    continue
                title_key = title.lower()
                if title_key in seen_titles:
                    continue
                item_url = item.get("url", "")
                if "vancouver.craigslist.org" in item_url:
                    continue
                seen_pids.add(dedup_key)
                seen_titles.add(title_key)
                dom_img = item.get("imageUrl", "")
                image_url = pid_to_img.get(pid, "") or (dom_img if not dom_img.startswith("data:") else "")
                listings.append(Listing(
                    title=title,
                    url=item.get("url", ""),
                    source=source,
                    price=item.get("price", ""),
                    time_left="",
                    location=city_name,
                    image_url=image_url,
                ))
        except PlaywrightTimeout:
            _log(f"[{source}] Timed out: {city_name}", "warning")
        except Exception as e:
            _log(f"[{source}] Error {city_name}: {e}", "error")
        finally:
            await p.close()

    return listings


def _fmt_pcar_time(seconds) -> str:
    """Convert PCar Market time_remaining (seconds) to a sortable time string."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return "Ended"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d: parts.append(f"{d}D")
    if h: parts.append(f"{h}H")
    if m or not parts: parts.append(f"{m}M")
    return " ".join(parts)


async def scrape_pcarmarket(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "PCar Market"
    base = "https://www.pcarmarket.com"
    listings = []
    seen_slugs: set[str] = set()
    query_words = [w.lower() for w in query.split() if len(w) > 2]

    def _ingest(results: list[dict]):
        for item in results:
            slug = item.get("slug", "")
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            title = item.get("title", "")
            if not title:
                continue
            if query_words and not any(w in title.lower() for w in query_words):
                continue
            status = (item.get("status") or "").lower()
            if status in ("ended", "sold", "closed"):
                time_left = "Ended"
            else:
                time_left = _fmt_pcar_time(item.get("time_remaining", 0))
            listings.append(Listing(
                title=title,
                url=f"{base}/auction/{slug}",
                source=source,
                price=item.get("current_bid", ""),
                time_left=time_left,
                location=item.get("location", ""),
                image_url=item.get("featured_image_url", ""),
            ))

    pcar_url = f"{base}/auctions"
    try:
        _log(f"[{source}] Fetching {pcar_url}")
        await page.goto(pcar_url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_selector('#__PRELOADED_AUCTIONS_LIST__', state="attached", timeout=15000)
        _log(f"[{source}] Page loaded, extracting listings")

        if debug:
            _save_debug(await page.content(), "pcarmarket")

        # Read page 1 from the embedded JSON the server injects into the page
        api_data = await page.evaluate(
            '() => { const el = document.getElementById("__PRELOADED_AUCTIONS_LIST__"); '
            'return el ? JSON.parse(el.textContent) : null; }'
        )
        if not api_data:
            return listings

        _ingest(api_data.get("results", []))

        # Paginate by clicking Next and waiting for __PRELOADED_AUCTIONS_LIST__ to update
        while True:
            next_btn = await page.query_selector('button.pcar-pagination__nav[aria-label="Next page"]')
            if not next_btn or await next_btn.get_attribute("disabled") is not None:
                break
            prev_content = await page.evaluate(
                '() => document.getElementById("__PRELOADED_AUCTIONS_LIST__")?.textContent || ""'
            )
            await next_btn.click()
            try:
                await page.wait_for_function(
                    '(prev) => { const el = document.getElementById("__PRELOADED_AUCTIONS_LIST__"); '
                    'return el && el.textContent !== prev; }',
                    arg=prev_content,
                    timeout=8000,
                )
            except PlaywrightTimeout:
                break
            api_data = await page.evaluate(
                '() => { const el = document.getElementById("__PRELOADED_AUCTIONS_LIST__"); '
                'return el ? JSON.parse(el.textContent) : null; }'
            )
            if not api_data:
                break
            _ingest(api_data.get("results", []))

        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out waiting for listings", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


_CARMAX_JS = """() => {
    let cars = [], totalCount = 0, zipCode = '', requestedUrl = '/cars';
    for (const s of document.scripts) {
        const t = s.textContent;
        const cm = t.match(/const cars = ([\\s\\S]*?\\]);\\n/);
        if (cm) { try { cars = JSON.parse(cm[1]); } catch(e) {} }
        const tm = t.match(/totalCount = (\\d+)/);
        if (tm && !totalCount) totalCount = parseInt(tm[1]);
        const zm = t.match(/"zipCode":"(\\d{5})"/);
        if (zm && !zipCode) zipCode = zm[1];
        const rm = t.match(/"requestedUrl":"([^"]+)"/);
        if (rm && requestedUrl === '/cars') requestedUrl = rm[1];
    }
    return { cars, totalCount, zipCode, requestedUrl };
}"""


def _carmax_listing(car: dict, base: str, source: str) -> "Listing | None":
    stock = str(car.get("stockNumber", ""))
    if not stock:
        return None
    year  = car.get("year", "")
    make  = car.get("make", "")
    model = car.get("model", "")
    trim  = car.get("trim") or ""
    title = " ".join(str(p) for p in [year, make, model, trim] if p).strip()
    if not title:
        return None
    price_val = car.get("basePrice")
    price     = f"${price_val:,.0f}" if price_val else ""
    miles_val = car.get("mileage")
    mileage   = f"{int(miles_val):,} mi" if miles_val else ""
    city      = car.get("storeCity", "")
    state     = car.get("stateAbbreviation", "")
    location  = f"{city}, {state}" if city and state else city or state
    return Listing(
        title=title, url=f"{base}/car/{stock}", source=source,
        price=price, mileage=mileage, location=location,
        image_url=car.get("heroImageUrl", "") or "",
    )


async def scrape_carmax(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "CarMax"
    base   = "https://www.carmax.com"
    listings: list[Listing] = []
    seen_stocks: set[str] = set()

    query_words = [w.lower() for w in query.split() if len(w) > 1]

    def _matches(title: str) -> bool:
        t = title.lower()
        return all(w in t for w in query_words)

    def _ingest(cars: list[dict]):
        for car in cars:
            stock = str(car.get("stockNumber", ""))
            if not stock or stock in seen_stocks:
                continue
            seen_stocks.add(stock)
            lst = _carmax_listing(car, base, source)
            if lst and _matches(lst.title):
                listings.append(lst)

    search_url = f"{base}/cars?search={quote_plus(query)}" + (f"&zip={zip_code}" if zip_code else "")
    try:
        _log(f"[{source}] Fetching {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        if debug:
            _save_debug(await page.content(), "carmax")

        page_data = await page.evaluate(_CARMAX_JS) or {}
        cars         = page_data.get("cars", []) or []
        total_count  = page_data.get("totalCount", 0) or 0
        zip_code     = page_data.get("zipCode", "") or ""
        requested_url = page_data.get("requestedUrl", "/cars") or "/cars"

        _ingest(cars)
        _log(f"[{source}] Page 1: {len(cars)} items (total ~{total_count})")

        # Paginate via internal search API (same endpoint the page uses for "See more")
        if len(cars) >= 24 and total_count > 24 and zip_code and requested_url:
            import uuid as _uuid
            visitor_id = str(_uuid.uuid4())
            skip = 24
            max_pages = 4  # up to 5 pages total (~120 results)

            while skip < total_count and skip < 24 * (max_pages + 1):
                api_url = (
                    f"{base}/cars/api/search/run"
                    f"?uri={quote_plus(requested_url)}&skip={skip}&take=24"
                    f"&zipCode={zip_code}&shipping=-1&sort=bestmatch"
                    f"&visitorID={visitor_id}"
                )
                api_data = await page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials:'include'});
                        return r.ok ? await r.json() : null;
                    }""",
                    api_url,
                )
                if not api_data:
                    break
                batch = api_data.get("items") or []
                before = len(listings)
                _ingest(batch)
                new = len(listings) - before
                page_num = skip // 24 + 1
                _log(f"[{source}] Page {page_num}: {len(batch)} items ({new} new)")
                if len(batch) < 24:
                    break
                skip += 24
                await page.wait_for_timeout(400)

        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


async def scrape_carvana(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Carvana"
    base   = "https://www.carvana.com"
    listings: list[Listing] = []
    seen_urls: set[str] = set()

    search_url = f"{base}/cars?search={quote_plus(query)}"
    try:
        _log(f"[{source}] Fetching {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        # Carvana uses Cloudflare Turnstile — wait up to 10s for it to clear
        for _ in range(10):
            title = await page.title()
            if "Just a moment" not in title:
                break
            await page.wait_for_timeout(1000)
        else:
            _log(f"[{source}] Blocked by Cloudflare challenge — skipping", "warning")
            return listings

        await page.wait_for_timeout(2000)

        if debug:
            _save_debug(await page.content(), "carvana")

        # Extract via JSON-LD (Carvana embeds Car structured data like CarMax)
        items = await page.evaluate("""() => {
            const results = [];
            for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d['@type'] === 'Car' || d['@type'] === 'Vehicle') results.push(d);
                    if (d['@type'] === 'ItemList') {
                        for (const el of (d.itemListElement || [])) results.push(el);
                    }
                } catch(e) {}
            }
            return results;
        }""")

        if not items:
            # Fallback: generic extractor for /vehicle/ links
            raw = await _eval_listings(page, 'a[href*="/vehicle/"]')
            for item in raw:
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                listings.append(Listing(
                    title=item.get("title", ""), url=url, source=source,
                    price=item.get("price", ""), image_url=item.get("imageUrl", ""),
                ))
        else:
            for item in items:
                offers   = item.get("offers") or {}
                item_url = offers.get("url") or item.get("url", "")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                year   = str(item.get("vehicleModelDate") or item.get("modelDate") or "")[:4]
                name   = item.get("name") or ""
                config = item.get("vehicleConfiguration") or ""
                title  = name or f"{year} {config}".strip()
                price_val = offers.get("price")
                price  = f"${price_val:,.0f}" if price_val else ""
                mileage_raw = item.get("mileageFromOdometer")
                miles_val = mileage_raw.get("value") if isinstance(mileage_raw, dict) else mileage_raw
                mileage = f"{int(miles_val):,} mi" if miles_val and int(miles_val) > 0 else ""
                listings.append(Listing(
                    title=title, url=item_url, source=source,
                    price=price, mileage=mileage,
                    image_url=item.get("image") or "",
                ))

        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


async def scrape_pf(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Porsche Finder"
    base = "https://finder.porsche.com/us/en-US"
    listings: list[Listing] = []
    seen_urls: set[str] = set()

    # Detect Porsche model from query for targeted URL
    q_lower = query.lower()
    model_key: str | None = None
    for keyword, key in _PF_MODELS:
        if re.search(rf'\b{re.escape(keyword)}\b', q_lower):
            model_key = key
            break

    # Words to filter client-side (everything except "porsche" and short words)
    filter_words = [w for w in re.split(r'\W+', q_lower)
                    if len(w) > 2 and w not in ('porsche', 'the', 'and', 'for')]

    search_base = f"{base}/search/{model_key}?model={model_key}" if model_key else f"{base}/search"

    try:
        for page_num in range(1, 6):  # max 5 pages = 75 results
            url = f"{search_base}&page={page_num}" if page_num > 1 else search_base
            _log(f"[{source}] Fetching page {page_num}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(1500)

            if debug and page_num == 1:
                _save_debug(await page.content(), "pf")

            items = await page.evaluate("""() => {
                for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                    try {
                        const d = JSON.parse(s.textContent);
                        if (d['@type'] === 'ItemList') return d.itemListElement || [];
                    } catch(e) {}
                }
                return [];
            }""")

            if not items:
                if debug:
                    _log(f"[{source}] No JSON-LD ItemList on page {page_num} — check debug_pf.html", "warning")
                break

            for item in items:
                offers   = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                raw_url  = offers.get("url", "") or item.get("url", "")
                clean_url = raw_url.split("?")[0]  # strip position/model params
                if not clean_url or clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)

                year   = (item.get("modelDate") or item.get("vehicleModelDate") or "")[:4]
                config = item.get("vehicleConfiguration") or item.get("name") or ""
                title  = f"{year} Porsche {config}".strip()

                # Client-side query filter
                if filter_words:
                    tl = title.lower()
                    if not all(w in tl for w in filter_words):
                        continue

                price_val = offers.get("price")
                price     = f"${price_val:,.0f}" if price_val else ""

                miles_val = (item.get("mileageFromOdometer") or {}).get("value")
                mileage   = f"{int(miles_val):,} mi" if miles_val and int(miles_val) > 0 else ""

                address  = (offers.get("seller") or {}).get("address") or {}
                location = address.get("addressLocality", "")

                image = item.get("image") or ""
                if isinstance(image, list):
                    image = image[0] if image else ""

                listings.append(Listing(
                    title=title, url=clean_url, source=source,
                    price=price, mileage=mileage, location=location,
                    image_url=image,
                ))

            _log(f"[{source}] Page {page_num}: {len(items)} items")
            if len(items) < 15:
                break  # last page
            await page.wait_for_timeout(400)

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    _log(f"[{source}] Done — {len(listings)} listings")
    return listings


# ─── eBay Motors ──────────────────────────────────────────────────────────────

_EBAY_JS = """() => {
    const results = [];
    document.querySelectorAll('li.s-card').forEach(card => {
        const link = card.querySelector('.s-card__link:not(.image-treatment)');
        if (!link) return;
        const rawUrl = link.href || '';
        if (!rawUrl.includes('/itm/')) return;
        const url = rawUrl.split('?')[0];

        const titleEl = card.querySelector('.s-card__title .su-styled-text');
        const title = titleEl ? titleEl.textContent.trim() : '';
        if (!title || title === 'Shop on eBay') return;

        const priceEl = card.querySelector('.s-card__price');
        const price = priceEl ? priceEl.textContent.trim() : '';

        const timeEl = card.querySelector('.s-card__time-left');
        const timeLeft = timeEl ? timeEl.textContent.trim() : '';

        const imgEl = card.querySelector('img.s-card__image, .image-treatment img');
        const imageUrl = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';

        results.push({ url, title, price, timeLeft, imageUrl: imageUrl.startsWith('data:') ? '' : imageUrl });
    });
    return results;
}"""


async def scrape_ebay(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source  = "eBay Motors"
    base    = "https://www.ebay.com"
    listings: list[Listing] = []
    seen_urls: set[str] = set()

    query_words = [w.lower() for w in query.split() if len(w) > 1]

    def _ingest(items: list[dict]):
        for item in items:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = item.get("title", "")
            if not title:
                continue
            if not all(w in title.lower() for w in query_words):
                continue
            time_left = item.get("timeLeft", "")
            # "2d 6h left" → "2D 6H"; no time = classified (no time_left)
            if time_left:
                time_left = re.sub(r'\s+left.*', '', time_left, flags=re.IGNORECASE).upper().strip()
            listings.append(Listing(
                title=title, url=url, source=source,
                price=item.get("price", ""), time_left=time_left,
                image_url=item.get("imageUrl", ""),
            ))

    try:
        loc_params = (f"&_stpos={zip_code}" + (f"&_sadis={radius}" if radius else "")) if zip_code else ""
        for page_num in range(1, 4):   # cap at 3 pages (~180 results)
            url = (
                f"{base}/sch/i.html?_nkw={quote_plus(query)}"
                f"&_sacat=6001&_sop=12&_pgn={page_num}{loc_params}"
            )
            _log(f"[{source}] Fetching page {page_num}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            if debug and page_num == 1:
                _save_debug(await page.content(), "ebay")

            items = await page.evaluate(_EBAY_JS) or []
            if not items:
                break
            before = len(listings)
            _ingest(items)
            new = len(listings) - before
            _log(f"[{source}] Page {page_num}: {len(items)} items ({new} new)")
            if new == 0:
                break

        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


# ─── Hemmings ─────────────────────────────────────────────────────────────────

def _hemmings_time_left(end_date: str | None) -> str:
    """Convert Hemmings end_date ISO string to a time-left string."""
    if not end_date:
        return ""
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = end - now
        if delta.total_seconds() <= 0:
            return "Ended"
        d, rem = divmod(int(delta.total_seconds()), 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        parts = []
        if d: parts.append(f"{d}D")
        if h: parts.append(f"{h}H")
        if m or not parts: parts.append(f"{m}M")
        return " ".join(parts)
    except Exception:
        return ""


async def scrape_hemmings(page: Page, query: str, debug: bool = False, zip_code: str = "", radius: int = 0) -> list[Listing]:
    source = "Hemmings"
    base   = "https://www.hemmings.com"
    listings: list[Listing] = []
    seen_ids: set[str] = set()

    query_words = [w.lower() for w in query.split() if len(w) > 1]

    def _ingest(results: list[dict]):
        for item in results:
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            title = item.get("title", "") or ""
            if not title:
                continue
            if not all(w in title.lower() for w in query_words):
                continue
            url = item.get("url", "") or ""
            if not url:
                continue
            # Price: classified use price, auction use current_bid
            price = item.get("current_bid") or item.get("current_price") or item.get("price") or ""
            # Time left: derived from end_date for auctions
            end_date = item.get("end_date")
            status   = (item.get("status") or "").lower()
            if status in ("sold", "ended", "expired"):
                time_left = "Ended"
            else:
                time_left = _hemmings_time_left(end_date)
            location = item.get("location") or ""
            thumb = (item.get("thumbnail") or {}).get("md") or {}
            image_url = thumb.get("4:3") or thumb.get("3:2") or thumb.get("full") or ""
            listings.append(Listing(
                title=title, url=url, source=source,
                price=str(price) if price else "",
                time_left=time_left, location=location, image_url=image_url,
            ))

    try:
        _log(f"[{source}] Loading search page")
        await page.goto(f"{base}/classifieds/cars-for-sale", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Capture auth headers from the first API call (triggered by typing)
        api_headers: dict = {}
        first_url: list[str] = []

        async def _on_request(req):
            if "api.hemmings.com/v2/search/listings" in req.url and not api_headers:
                api_headers.update(req.headers)
                first_url.append(req.url)

        page.on("request", _on_request)

        search = await page.query_selector('[placeholder="Keyword Search"]')
        if not search:
            _log(f"[{source}] Search input not found", "warning")
            return listings
        await search.fill(query)
        await search.press("Enter")
        # Wait for the API call to fire and response to arrive
        await page.wait_for_timeout(4000)

        if not api_headers:
            _log(f"[{source}] API headers not captured", "warning")
            return listings

        if debug:
            _save_debug(await page.content(), "hemmings")

        # Build the canonical paginated URL from the captured base
        hem_secret = api_headers.get("hemmings-secret", "")
        csrf_token  = api_headers.get("x-csrf-token", "")
        api_base = (
            f"https://api.hemmings.com/v2/search/listings"
            f"?adtype=cars-for-sale&q={quote_plus(query)}"
            f"&per_page=30&sort_by=recommended&members_preview=false"
        )

        for page_num in range(1, 6):  # up to 5 pages = 150 results
            api_url = f"{api_base}&page={page_num}"
            api_data = await page.evaluate(
                """async (params) => {
                    const r = await fetch(params.url, {
                        headers: {
                            'hemmings-secret': params.secret,
                            'x-csrf-token':    params.csrf,
                            'hemmings-client': '1',
                            'x-requested-with': 'XMLHttpRequest',
                            'accept': 'application/json',
                        }
                    });
                    return r.ok ? await r.json() : null;
                }""",
                {"url": api_url, "secret": hem_secret, "csrf": csrf_token},
            )
            if not api_data:
                break
            results = api_data.get("results", [])
            total   = api_data.get("total_count", 0)
            before  = len(listings)
            _ingest(results)
            new = len(listings) - before
            _log(f"[{source}] Page {page_num}: {len(results)} items ({new} new, total ~{total})")
            if not results or len(results) < 30:
                break

        _log(f"[{source}] Done — {len(listings)} listings")

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
        raise
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")
        raise

    return listings


