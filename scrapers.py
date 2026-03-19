import re
import sys
from urllib.parse import quote_plus, urlparse

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Page
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

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


async def scrape_carsandbids(page: Page, query: str, debug: bool = False) -> list[Listing]:
    source = "Cars & Bids"
    base = "https://carsandbids.com"
    url = f"{base}/search?q={quote_plus(query)}"
    listings = []

    try:
        # Use domcontentloaded — C&B is a React SPA that never reaches networkidle
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_selector('a[href*="/auctions/"]', timeout=20000)

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

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")

    return listings


async def scrape_bat(page: Page, query: str, debug: bool = False) -> list[Listing]:
    source = "Bring a Trailer"
    base = "https://bringatrailer.com"
    url = f"{base}/search/?s={quote_plus(query)}"
    listings = []

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_selector('a[href*="/listing/"]', timeout=20000)

        if debug:
            _save_debug(await page.content(), "bat")

        await _scroll_to_bottom(page)
        for item in (await _eval_listings(page, 'a[href*="/listing/"]')):
            if item.get("title") and item.get("url"):
                # BaT is auctions-only: no countdown = auction ended
                time_left = item.get("timeLeft", "") or "Ended"
                listings.append(Listing(
                    title=item["title"], url=item["url"], source=source,
                    price=item.get("price", ""), time_left=time_left,
                    location=item.get("location", ""), image_url=item.get("imageUrl", ""),

                ))

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")

    return listings


async def scrape_hagerty(page: Page, query: str, debug: bool = False) -> list[Listing]:
    source = "Hagerty"
    base = "https://www.hagerty.com"
    listings = []

    try:
        # Navigate directly to the search URL (discovered via network inspection)
        await page.goto(
            f"{base}/marketplace/search?searchQuery={quote_plus(query)}&type=classifieds",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if debug:
            _save_debug(await page.content(), "hagerty")

        await page.wait_for_selector(
            'a[href*="/marketplace/auction/"]',
            timeout=20000,
        )

        await _scroll_to_bottom(page)
        for item in (await _eval_listings(page, 'a[href*="/marketplace/auction/"]')):
            title = item.get("title", "")
            url = item.get("url", "")
            if not title or not url:
                continue
            # Filter out Hagerty promotional/UI entries
            if re.search(r'why hagerty|hagerty marketplace\?', title, re.IGNORECASE):
                continue
            listings.append(Listing(
                title=title, url=url, source=source,
                price=item.get("price", ""), time_left=item.get("timeLeft", ""),
                location=item.get("location", ""), image_url=item.get("imageUrl", ""),
            ))

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out", "warning")
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")

    return listings


CL_METROS = [
    ("Seattle",        "seattle"),
    ("Portland",       "portland"),
    ("Boise",          "boise"),
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


async def scrape_craigslist(page: Page, query: str, debug: bool = False) -> list[Listing]:
    source = "Craigslist"
    listings = []
    seen_pids: set[str] = set()

    # Intercept image responses to capture URLs for lazy-loaded images.
    # CL images follow: https://images.craigslist.org/d/{pid}/{hash}_{size}.jpg
    pid_to_img: dict[str, str] = {}

    def _on_response(response):
        url = response.url
        if "images.craigslist.org/d/" in url and "empty.png" not in url:
            try:
                pid = url.split("/d/")[1].split("/")[0]
                pid_to_img.setdefault(pid, url)
            except Exception:
                pass

    page.on("response", _on_response)

    try:
        for city_name, subdomain in CL_METROS:
            url = f"https://{subdomain}.craigslist.org/search/cta?query={quote_plus(query)}"
            try:
                # Large viewport set BEFORE navigation so IntersectionObserver fires
                # for all results that fit within 20 000 px (~160-170 listings).
                # Do NOT resize after load: flooding the network with hundreds of
                # simultaneous image requests prevents networkidle from settling.
                await page.set_viewport_size({"width": 1280, "height": 20000})
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Wait for image requests triggered by the initial render to settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass
                if debug:
                    _save_debug(await page.content(), f"cl_{subdomain}")
                items = await page.evaluate(_CL_JS) or []
                for item in items:
                    pid = item.get("pid", "")
                    dedup_key = pid or item.get("url", "")
                    if not dedup_key or dedup_key in seen_pids:
                        continue
                    title = item.get("title", "").strip()
                    if not title:
                        continue
                    seen_pids.add(dedup_key)
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
        page.remove_listener("response", _on_response)

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


async def scrape_pcarmarket(page: Page, query: str, debug: bool = False) -> list[Listing]:
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

    try:
        await page.goto(f"{base}/auctions", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_selector('#__PRELOADED_AUCTIONS_LIST__', state="attached", timeout=15000)

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

    except PlaywrightTimeout:
        _log(f"[{source}] Timed out waiting for listings", "warning")
    except Exception as e:
        _log(f"[{source}] Error: {e}", "error")

    return listings
