#!/usr/bin/env python3
"""
AuctionWatch - Search multiple car auction sites simultaneously

Usage:
    python auctionwatch.py "porsche 911"
    python auctionwatch.py "ferrari 308" --html
    python auctionwatch.py "bmw m3 e46" --html --open
    python auctionwatch.py "alfa romeo" --json
    python auctionwatch.py "land rover defender" --debug
"""

import argparse
import hashlib
import re
import asyncio
import json
import os
import secrets
import sqlite3
import sys
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
import queue
import threading
from urllib.parse import quote, quote_plus, urlparse

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Page
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    from rich import box as rich_box
    _console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    _console = None


# ─── Persistent Store ─────────────────────────────────────────────────────────

STORE_PATH = Path.home() / ".auctionwatch.json"
_DATA_DIR = Path(os.environ.get("DATA_DIR", Path.home()))
DB_PATH = _DATA_DIR / ".auctionwatch.db"
SECRET_KEY_PATH = _DATA_DIR / ".auctionwatch.secret"


def _load_store() -> dict:
    try:
        return json.loads(STORE_PATH.read_text()) if STORE_PATH.exists() else {}
    except Exception:
        return {}


def _save_store(data: dict):
    STORE_PATH.write_text(json.dumps(data, indent=2))


def store_ignore(listing_id: str):
    data = _load_store()
    ignored = set(data.get("ignored", []))
    ignored.add(listing_id)
    data["ignored"] = sorted(ignored)
    _save_store(data)
    return data["ignored"]


def store_set_start(listing_id: str):
    data = _load_store()
    data["start"] = listing_id
    _save_store(data)


def store_get_ignored() -> set[str]:
    return set(_load_store().get("ignored", []))


def store_set_ignored(listing_id: str, ignored: bool):
    data = _load_store()
    s = set(data.get("ignored", []))
    if ignored:
        s.add(listing_id)
    else:
        s.discard(listing_id)
    data["ignored"] = sorted(s)
    _save_store(data)


def store_get_start() -> str:
    return _load_store().get("start", "")


def store_set_starred(listing_id: str, starred: bool):
    data = _load_store()
    s = set(data.get("starred", []))
    if starred:
        s.add(listing_id)
    else:
        s.discard(listing_id)
    data["starred"] = sorted(s)
    _save_store(data)


def store_get_starred() -> set[str]:
    return set(_load_store().get("starred", []))


# ─── Auth / Multi-user DB ─────────────────────────────────────────────────────

def _get_secret_key() -> bytes:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    SECRET_KEY_PATH.write_bytes(key)
    return key

def _init_db():
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ignored (
                user_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                PRIMARY KEY (user_id, listing_id)
            );
            CREATE TABLE IF NOT EXISTS starred (
                user_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                PRIMARY KEY (user_id, listing_id)
            );
            CREATE TABLE IF NOT EXISTS user_start (
                user_id INTEGER PRIMARY KEY,
                listing_id TEXT NOT NULL
            );
        """)

def _db_create_user(username: str, password: str) -> bool:
    from werkzeug.security import generate_password_hash
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         (username.strip(), generate_password_hash(password)))
        return True
    except sqlite3.IntegrityError:
        return False

def _db_check_user(username: str, password: str) -> int | None:
    from werkzeug.security import check_password_hash
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE username=?",
                           (username.strip(),)).fetchone()
    if row and check_password_hash(row[1], password):
        return row[0]
    return None

def _db_get_ignored(user_id: int) -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT listing_id FROM ignored WHERE user_id=?", (user_id,)).fetchall()
    return {r[0] for r in rows}

def _db_set_ignored(user_id: int, listing_id: str, ignored: bool):
    with sqlite3.connect(DB_PATH) as conn:
        if ignored:
            conn.execute("INSERT OR IGNORE INTO ignored VALUES (?,?)", (user_id, listing_id))
        else:
            conn.execute("DELETE FROM ignored WHERE user_id=? AND listing_id=?", (user_id, listing_id))

def _db_get_starred(user_id: int) -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT listing_id FROM starred WHERE user_id=?", (user_id,)).fetchall()
    return {r[0] for r in rows}

def _db_set_starred(user_id: int, listing_id: str, starred: bool):
    with sqlite3.connect(DB_PATH) as conn:
        if starred:
            conn.execute("INSERT OR IGNORE INTO starred VALUES (?,?)", (user_id, listing_id))
        else:
            conn.execute("DELETE FROM starred WHERE user_id=? AND listing_id=?", (user_id, listing_id))

def _db_get_start(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT listing_id FROM user_start WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else ""

def _db_set_start(user_id: int, listing_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO user_start VALUES (?,?)", (user_id, listing_id))


# ─── Data Model ───────────────────────────────────────────────────────────────

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
    def is_active(self) -> bool | None:
        """True = bidding open, False = ended/sold, None = unknown (e.g. classified)."""
        t = self.time_left.lower()
        if re.search(r'\d', t) and not re.search(r'ended|sold|closed', t):
            return True
        if re.search(r'ended|sold|closed', t):
            return False
        return None

    @property
    def short_id(self) -> str:
        """4-char hex ID derived from SHA-256 of the URL path (no query params)."""
        path = urlparse(self.url).path.rstrip("/")
        return hashlib.sha256(path.encode()).hexdigest()[:4]


SOURCE_COLORS_RICH = {
    "Cars & Bids": "cyan",
    "Bring a Trailer": "green",
    "Hagerty": "blue",
    "PCar Market": "magenta",
}

SOURCE_COLORS_HTML = {
    "Cars & Bids": "#00bcd4",
    "Bring a Trailer": "#4caf50",
    "Hagerty": "#2196f3",
    "PCar Market": "#9c27b0",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _time_left_minutes(time_left: str) -> float:
    """Convert a time-left string to minutes for sorting. Ended/unknown → infinity."""
    t = (time_left or "").strip()
    if not t or re.search(r'ended|sold|closed', t, re.I):
        return float("inf")
    total = 0
    m = re.search(r'(\d+)\s*D', t, re.I)
    if m: total += int(m.group(1)) * 1440
    m = re.search(r'(\d+)\s*H', t, re.I)
    if m: total += int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*M', t, re.I)
    if m: total += int(m.group(1))
    if not total:
        # HH:MM:SS format (C&B, BaT)
        m = re.search(r'(\d+):(\d{2}):\d{2}', t)
        if m: total = int(m.group(1)) * 60 + int(m.group(2))
    return total if total > 0 else float("inf")


def _log(msg: str, level: str = "info"):
    if HAS_RICH:
        styles = {"info": "dim", "warning": "yellow", "error": "red bold"}
        _console.print(f"  {msg}", style=styles.get(level, ""))
    else:
        print(f"  [{level.upper()}] {msg}", file=sys.stderr)


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


def _save_debug(content: str, name: str):
    path = f"debug_{name}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _log(f"Debug HTML saved to {path}", "info")


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

        await _scroll_to_bottom(page)
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
        await page.wait_for_selector('#__PRELOADED_AUCTIONS_LIST__', timeout=15000)

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


# ─── Output: Terminal ─────────────────────────────────────────────────────────

def display_terminal(listings: list[Listing], query: str, start_id: str = ""):
    if not listings:
        msg = f"No results found for '{query}'"
        if HAS_RICH:
            _console.print(f"\n[yellow]{msg}[/yellow]")
        else:
            print(f"\n{msg}")
        return

    # Find position of start marker (everything at/after this index is "seen")
    start_idx = next((i for i, l in enumerate(listings) if l.short_id == start_id), None) if start_id else None

    new_count = start_idx if start_idx is not None else len(listings)

    if HAS_RICH:
        _console.print(
            f"\n[bold]Found [cyan]{len(listings)}[/cyan] result(s) for "
            f"[bold white]'{query}'[/bold white][/bold]"
            + (f"  [dim]({new_count} new)[/dim]" if start_idx is not None else "")
            + "\n"
        )

        # Compute column widths from actual content: max(header, widest value) + 2
        def _w(header: str, values: list[str], cap: int = 999) -> int:
            return min(max(len(header), max((len(v) for v in values), default=0)), cap) + 2

        time_vals   = [l.time_left or "Ended" for l in listings]
        price_vals  = [l.price or "–" for l in listings]
        source_vals = [l.source for l in listings]
        title_vals  = [l.title for l in listings]
        url_vals    = [urlparse(l.url)._replace(query="", fragment="").geturl() for l in listings]

        col_widths = dict(
            id=6,
            source=_w("Source", source_vals),
            title=_w("Title", title_vals, cap=50),
            price=_w("Price", price_vals),
            time=_w("Time Left", time_vals),
            url=_w("URL", url_vals, cap=60),
        )

        def _make_table(show_header: bool) -> Table:
            t = Table(
                show_header=show_header,
                header_style="bold white on grey23",
                box=rich_box.ROUNDED,
                expand=False,
                show_lines=True,
                padding=(0, 1),
            )
            t.add_column("ID",        width=col_widths["id"],     justify="center", no_wrap=True)
            t.add_column("Source",    style="bold",               width=col_widths["source"], no_wrap=True)
            t.add_column("Title",     width=col_widths["title"],  no_wrap=True, overflow="ellipsis")
            t.add_column("Price",     width=col_widths["price"],  justify="right", no_wrap=True)
            t.add_column("Time Left", width=col_widths["time"],   justify="center", no_wrap=True)
            t.add_column("URL",       style="dim",                width=col_widths["url"], no_wrap=True, overflow="ellipsis")
            return t

        def _add_row(table: Table, l: Listing, dim: bool):
            color = SOURCE_COLORS_RICH.get(l.source, "white")
            t = l.time_left
            if dim:
                time_text = Text(t or "Ended", style="dim")
            elif l.is_active is True:
                time_text = Text(t, style="green")
            elif l.is_active is False:
                time_text = Text(t or "Ended", style="dim red")
            else:
                time_text = Text("–", style="dim")
            display_url = urlparse(l.url)._replace(query="", fragment="").geturl()
            s = "dim" if dim else ""
            table.add_row(
                Text(l.short_id, style="dim" if dim else "dim yellow"),
                Text(l.source,   style="dim" if dim else f"bold {color}"),
                Text(l.title,    style=s),
                Text(l.price or "–", style=s),
                time_text,
                Text(display_url, style="dim"),
            )

        new_table = _make_table(show_header=True)
        for l in (listings[:start_idx] if start_idx is not None else listings):
            _add_row(new_table, l, dim=False)
        _console.print(new_table)

        if start_idx is not None and start_idx < len(listings):
            _console.print("[dim]─── seen below ───[/dim]")
            seen_table = _make_table(show_header=False)
            for l in listings[start_idx:]:
                _add_row(seen_table, l, dim=True)
            _console.print(seen_table)

    else:
        print(f"\nFound {len(listings)} result(s) for '{query}'"
              + (f"  ({new_count} new)" if start_idx is not None else "") + "\n")
        print(f"{'ID':<5} {'Source':<18} {'Title':<42} {'Price':<14} {'Time Left':<12} URL")
        print("─" * 120)
        for i, l in enumerate(listings):
            if i == start_idx:
                print("─── seen below " + "─" * 105)
            title = l.title[:39] + "..." if len(l.title) > 42 else l.title
            tl = l.time_left or ("–" if l.is_active is None else ("Ended" if l.is_active is False else "–"))
            print(f"{l.short_id:<5} {l.source:<18} {title:<42} {(l.price or '–'):<14} {tl:<12} {l.url}")


# ─── Output: HTML ─────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def generate_html(listings: list[Listing], query: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    count = len(listings)

    source_counts: dict[str, int] = {}
    for l in listings:
        source_counts[l.source] = source_counts.get(l.source, 0) + 1
    summary = " &nbsp;·&nbsp; ".join(
        '<span style="color:{}">{} {}</span>'.format(SOURCE_COLORS_HTML.get(s, "#aaa"), c, _esc(s))
        for s, c in source_counts.items()
    )

    if not listings:
        cards_html = '<p class="no-results">No results found.</p>'
    else:
        card_parts = []
        for l in listings:
            color = SOURCE_COLORS_HTML.get(l.source, "#888")
            img_html = (
                f'<img src="{_esc(l.image_url)}" alt="" loading="lazy" '
                f'onerror="this.parentElement.innerHTML=\'<div class=no-img>No image</div>\'">'
                if l.image_url else '<div class="no-img">No image</div>'
            )
            price_html = f'<span class="price">{_esc(l.price)}</span>' if l.price else ""
            if l.is_active is True:
                tl_html = f'<span class="time-left active">{_esc(l.time_left)}</span>'
            elif l.is_active is False:
                tl_html = f'<span class="time-left ended">{_esc(l.time_left) or "Ended"}</span>'
            else:
                tl_html = ""
            id_html = f'<span class="listing-id">{_esc(l.short_id)}</span>'
            card_parts.append(f"""
        <div class="card">
          <a href="{_esc(l.url)}" target="_blank" rel="noopener" class="card-link">
            <div class="card-img">{img_html}</div>
            <div class="card-body">
              <div class="card-header-row">
                {id_html}
                <div class="source-badge" style="background:{color}">{_esc(l.source)}</div>
              </div>
              <div class="title">{_esc(l.title)}</div>
              {price_html}
              {tl_html}
            </div>
          </a>
        </div>""")
        cards_html = "\n".join(card_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AuctionWatch: {_esc(query)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0d0d0d;
      color: #e0e0e0;
      min-height: 100vh;
    }}
    header {{
      background: #141414;
      border-bottom: 1px solid #222;
      padding: 1.25rem 2rem;
      display: flex;
      align-items: baseline;
      gap: 1.25rem;
      flex-wrap: wrap;
    }}
    header h1 {{
      font-size: 1.3rem;
      font-weight: 700;
      color: #fff;
      letter-spacing: -0.02em;
    }}
    header h1 .query {{ color: #00bcd4; }}
    .meta {{ color: #555; font-size: 0.8rem; }}
    .summary-bar {{
      background: #141414;
      border-bottom: 1px solid #1e1e1e;
      padding: 0.6rem 2rem;
      font-size: 0.82rem;
      color: #888;
    }}
    .summary-bar strong {{ color: #ddd; margin-right: 0.5rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
      gap: 1.25rem;
      padding: 2rem;
    }}
    .card {{
      background: #1a1a1a;
      border: 1px solid #252525;
      border-radius: 10px;
      overflow: hidden;
      transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
    }}
    .card:hover {{
      transform: translateY(-4px);
      box-shadow: 0 12px 32px rgba(0,0,0,0.5);
      border-color: #333;
    }}
    .card-link {{ display: block; text-decoration: none; color: inherit; height: 100%; }}
    .card-img {{
      height: 175px;
      overflow: hidden;
      background: #111;
      position: relative;
    }}
    .card-img img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      transition: transform 0.3s ease;
    }}
    .card:hover .card-img img {{ transform: scale(1.04); }}
    .no-img {{
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #333;
      font-size: 0.8rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .card-body {{ padding: 0.875rem 1rem 1rem; }}
    .source-badge {{
      display: inline-block;
      padding: 0.18rem 0.5rem;
      border-radius: 4px;
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: #fff;
    }}
    .title {{
      font-size: 0.93rem;
      font-weight: 600;
      color: #f0f0f0;
      line-height: 1.38;
      margin-bottom: 0.4rem;
    }}
    .price {{
      display: block;
      font-size: 1.1rem;
      font-weight: 700;
      color: #00e676;
      margin-bottom: 0.25rem;
    }}
    .time-left {{
      display: inline-block;
      font-size: 0.78rem;
      font-weight: 600;
      margin: 0.25rem 0;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
    }}
    .time-left.active {{ background: rgba(0,230,118,0.15); color: #00e676; }}
    .time-left.ended  {{ background: rgba(255,82,82,0.12);  color: #666; }}
    .card-header-row {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.5rem;
    }}
    .listing-id {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      color: #e6c84a;
      background: rgba(230,200,74,0.1);
      border: 1px solid rgba(230,200,74,0.25);
      border-radius: 3px;
      padding: 0.1rem 0.4rem;
    }}
    .no-results {{
      grid-column: 1 / -1;
      text-align: center;
      color: #444;
      padding: 5rem 2rem;
      font-size: 1.05rem;
    }}
    footer {{
      text-align: center;
      padding: 2rem;
      color: #333;
      font-size: 0.78rem;
      border-top: 1px solid #1a1a1a;
      margin-top: 1rem;
    }}
    @media (max-width: 600px) {{
      header {{ padding: 1rem; }}
      .grid {{ padding: 1rem; gap: 1rem; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>AuctionWatch: <span class="query">"{_esc(query)}"</span></h1>
    <span class="meta">Searched {now}</span>
  </header>
  <div class="summary-bar">
    <strong>{count} result{"s" if count != 1 else ""}</strong>{(" &nbsp;·&nbsp; " + summary) if summary else ""}
  </div>
  <div class="grid">
    {cards_html}
  </div>
  <footer>Generated by AuctionWatch &nbsp;·&nbsp; {now}</footer>
</body>
</html>"""


# ─── Web UI ───────────────────────────────────────────────────────────────────

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AuctionWatch — Sign in</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0d0d0d; color: #e0e0e0; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; }
    .box { width: 340px; background: #141414; border: 1px solid #222; border-radius: 12px; padding: 2rem; }
    .brand { font-size: 1.2rem; font-weight: 700; color: #00bcd4; text-align: center; margin-bottom: 1.75rem; }
    .tabs { display: flex; border-bottom: 1px solid #222; margin-bottom: 1.5rem; }
    .tab { flex: 1; text-align: center; padding: .55rem; font-size: .85rem; cursor: pointer;
           color: #555; border-bottom: 2px solid transparent; transition: all .15s; }
    .tab.on { color: #e0e0e0; border-bottom-color: #00bcd4; }
    .form-section { display: none; }
    .form-section.on { display: block; }
    label { display: block; font-size: .78rem; color: #888; margin-bottom: .3rem; }
    input[type=text], input[type=password] {
      width: 100%; background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
      padding: .5rem .75rem; color: #e0e0e0; font-size: .9rem; outline: none; margin-bottom: 1rem;
    }
    input:focus { border-color: #00bcd4; }
    button[type=submit] {
      width: 100%; background: #00bcd4; border: none; border-radius: 6px;
      padding: .55rem; color: #000; font-weight: 700; font-size: .9rem; cursor: pointer;
    }
    button[type=submit]:hover { background: #26c6da; }
    .error { color: #ff5252; font-size: .78rem; margin-bottom: .9rem; min-height: 1.1rem; }
  </style>
</head>
<body>
<div class="box">
  <div class="brand">AuctionWatch</div>
  <div class="tabs">
    <div class="tab on" id="t-login" onclick="show('login')">Sign in</div>
    <div class="tab"    id="t-reg"   onclick="show('reg')">Create account</div>
  </div>
  <div class="error" id="err">{{error}}</div>

  <div class="form-section on" id="s-login">
    <form method="post" action="/login">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" autofocus required>
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>

  <div class="form-section" id="s-reg">
    <form method="post" action="/register">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" required>
      <label>Password</label>
      <input type="password" name="password" autocomplete="new-password" required>
      <button type="submit">Create account</button>
    </form>
  </div>
</div>
<script>
function show(tab){
  document.getElementById('s-login').classList.toggle('on', tab==='login');
  document.getElementById('s-reg').classList.toggle('on',   tab==='reg');
  document.getElementById('t-login').classList.toggle('on', tab==='login');
  document.getElementById('t-reg').classList.toggle('on',   tab==='reg');
}
// If server flagged a register error, show that tab
if(document.getElementById('err').textContent.includes('taken')) show('reg');
</script>
</body>
</html>"""

_WEB_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AuctionWatch</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
    :root {
      --bg: #0d0d0d; --bg2: #141414; --bg3: #1a1a1a;
      --border: #252525; --text: #e0e0e0; --dim: #555;
      --green: #00e676; --red: #ff5252; --yellow: #e6c84a; --accent: #00bcd4;
    }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: var(--bg); color: var(--text); }
    header {
      background: var(--bg2); border-bottom: 1px solid #222;
      padding: 0.9rem 1.5rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
    }
    .brand { font-size: 1.1rem; font-weight: 700; color: var(--accent); white-space: nowrap; }
    #sf { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; flex: 1; }
    #q {
      flex: 1; min-width: 180px; background: #1e1e1e; border: 1px solid #333;
      border-radius: 6px; padding: 0.42rem 0.75rem; color: var(--text); font-size: 0.9rem; outline: none;
    }
    #q:focus { border-color: var(--accent); }
    .pills { display: flex; gap: 0.3rem; flex-wrap: wrap; }
    .pill {
      padding: 0.28rem 0.6rem; border-radius: 20px; font-size: 0.73rem; font-weight: 600;
      border: 1px solid #333; color: var(--dim); cursor: pointer; user-select: none; transition: all 0.15s;
    }
    .pill.on { color: #fff; }
    .pill[data-site="cab"].on  { color: #00bcd4; border-color: #00bcd4; }
    .pill[data-site="bat"].on  { color: #4caf50; border-color: #4caf50; }
    .pill[data-site="hagerty"].on { color: #2196f3; border-color: #2196f3; }
    .pill[data-site="pcar"].on { color: #9c27b0; border-color: #9c27b0; }
    .pill[data-site].prohibit { color: var(--red); border-color: rgba(255,82,82,0.45); }
    .pill[data-filter="active"].on  { color: var(--green);  border-color: var(--green); }
    .pill[data-filter="starred"].on { color: var(--yellow); border-color: var(--yellow); }
    .pill[data-filter="ignored"].on { color: var(--red);    border-color: var(--red); }
    #search-btn {
      padding: 0.38rem 1rem; background: var(--accent); border: none; border-radius: 6px;
      color: #000; font-weight: 700; font-size: 0.85rem; cursor: pointer; white-space: nowrap;
    }
    #search-btn:hover { background: #26c6da; }
    #search-btn:disabled { opacity: 0.45; cursor: not-allowed; }
    #statusbar {
      padding: 0.45rem 1.5rem; background: var(--bg2); border-bottom: 1px solid #1e1e1e;
      font-size: 0.78rem; color: var(--dim); display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
    }
    .sc { color: var(--text); font-weight: 600; }
    .nb { background: rgba(0,230,118,0.15); color: var(--green); padding: 0.08rem 0.38rem; border-radius: 4px; font-weight: 700; }
    #site-status { display: flex; gap: 0.6rem; flex-wrap: wrap; padding: 0.75rem 1.5rem; min-height: 2.5rem; }
    .spill {
      display: flex; align-items: center; gap: 0.35rem; padding: 0.28rem 0.6rem;
      border-radius: 20px; font-size: 0.72rem; font-weight: 600; border: 1px solid #2a2a2a; color: var(--dim);
      transition: all 0.2s;
    }
    .spill.loading { animation: pulse 1.2s infinite; }
    .spill.done   { color: var(--green); border-color: rgba(0,230,118,0.35); }
    .spill.error  { color: var(--red);   border-color: rgba(255,82,82,0.35); }
    .spin { width: 9px; height: 9px; border: 2px solid #333; border-top-color: currentColor;
            border-radius: 50%; animation: spin 0.7s linear infinite; }
    @keyframes spin  { to { transform: rotate(360deg); } }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 1rem; padding: 1.25rem 1.5rem; }
    .seen-div {
      grid-column: 1/-1; display: flex; align-items: center; gap: 0.6rem;
      color: #2e2e2e; font-size: 0.7rem; letter-spacing: 0.14em; text-transform: uppercase; padding: 0.1rem 0;
    }
    .seen-div::before, .seen-div::after { content:''; flex:1; border-top: 1px solid #202020; }
    .card {
      background: var(--bg3); border: 1px solid var(--border); border-radius: 10px;
      overflow: hidden; transition: transform .15s, box-shadow .15s, border-color .15s, opacity .15s;
      position: relative;
    }
    .card:hover { transform: translateY(-3px); box-shadow: 0 10px 28px rgba(0,0,0,.55); border-color: #333; }
    .card.seen { opacity: 0.38; }
    .card.seen:hover { opacity: 0.72; }
    .card.out { animation: fadeout .22s ease forwards; pointer-events: none; }
    .card.starred { border-color: rgba(230,200,74,.45); }
    .card.starred:hover { border-color: rgba(230,200,74,.75); box-shadow: 0 10px 28px rgba(230,200,74,.1); }
    .card.is-ignored { opacity: 0.55; }
    .card.is-ignored:hover { opacity: 0.85; }
    .card.is-ignored .abtn.ign:hover { border-color: #4a4; color: #6c6; }
    @keyframes fadeout { to { opacity:0; transform:scale(.93); } }
    .cactions {
      position: absolute; top: 7px; right: 7px; display: flex; gap: 5px; z-index: 5;
    }
    .abtn {
      width: 26px; height: 26px; background: rgba(8,8,8,.82); border: 1px solid #3a3a3a;
      border-radius: 50%; color: #555; cursor: pointer; font-size: 0.8rem;
      display: flex; align-items: center; justify-content: center;
      transition: all .1s; padding: 0;
    }
    .abtn:hover { background: #1c1c1c; color: #fff; border-color: #555; }
    .abtn.ign:hover { border-color: #c44; color: #e66; }
    .abtn.str.on { color: #e6c84a; border-color: #555; background: rgba(8,8,8,.82); text-shadow: 0 0 6px rgba(230,200,74,.8); }
    .abtn.str:not(.on):hover { border-color: #e6c84a; color: #e6c84a; }
    .clink { display: block; text-decoration: none; color: inherit; }
    .cimg { height: 165px; overflow: hidden; background: #111; }
    .cimg img { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform .3s; }
    .card:hover .cimg img { transform: scale(1.04); }
    .noimg { height: 100%; display: flex; align-items: center; justify-content: center;
             color: #222; font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
    .cbody { padding: .75rem .85rem .85rem; }
    .cmeta { display: flex; align-items: center; gap: .35rem; margin-bottom: .4rem; flex-wrap: wrap; }
    .sbadge { padding: .13rem .42rem; border-radius: 4px; font-size: .63rem; font-weight: 700;
              text-transform: uppercase; letter-spacing: .06em; color: #fff; }
    .lid { font-family: monospace; font-size: .65rem; color: var(--yellow);
           background: rgba(230,200,74,.1); border: 1px solid rgba(230,200,74,.2);
           border-radius: 3px; padding: .08rem .32rem; }
    .ctitle { font-size: .88rem; font-weight: 600; color: #f0f0f0; line-height: 1.35; margin-bottom: .3rem; }
    .cprice { display: block; font-size: 1.02rem; font-weight: 700; color: var(--green); margin-bottom: .18rem; }
    .tl { display: inline-block; font-size: .72rem; font-weight: 600;
          padding: .1rem .38rem; border-radius: 4px; margin-top: .12rem; }
    .tl.active { background: rgba(0,230,118,.14); color: var(--green); }
    .tl.ended  { background: rgba(255,82,82,.1);  color: #4a4a4a; }
    .empty { grid-column:1/-1; text-align:center; color:#2e2e2e; padding:4rem; font-size:.95rem; }
    #tag-bar {
      padding: 0.45rem 1.5rem; border-bottom: 1px solid #1a1a1a;
      display: none; flex-wrap: wrap; gap: 0.3rem; align-items: center;
    }
    .tpill {
      padding: 0.22rem 0.6rem; border-radius: 20px; font-size: 0.7rem; font-weight: 600;
      border: 1px solid #252525; color: #3a3a3a; cursor: pointer; user-select: none;
      transition: all 0.12s;
    }
    .tpill:hover { border-color: #444; color: #666; }
    .tpill.require { color: var(--green); border-color: rgba(0,230,118,0.45); background: rgba(0,230,118,0.07); }
    .tpill.prohibit { color: var(--red);   border-color: rgba(255,82,82,0.45);  background: rgba(255,82,82,0.07); }
    @media(max-width:600px) { .grid{padding:1rem;gap:.8rem} header{padding:.7rem 1rem} }
    .range-row {
      display: flex; align-items: center; gap: 0.35rem;
      font-size: 0.72rem; color: var(--dim); white-space: nowrap;
    }
    .range-row label { color: #666; }
    .range-row input[type=number] {
      width: 68px; background: #1e1e1e; border: 1px solid #333; border-radius: 5px;
      padding: 0.28rem 0.45rem; color: var(--text); font-size: 0.72rem; outline: none;
    }
    .range-row input[type=number]:focus { border-color: var(--accent); }
    .range-row input[type=number]::-webkit-inner-spin-button,
    .range-row input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; }
    .range-row input[type=number] { -moz-appearance: textfield; }
    .range-sep { color: #333; }
  </style>
</head>
<body>
<header>
  <div class="brand">AuctionWatch</div>
  <a href="/logout" style="margin-left:auto;font-size:.75rem;color:#444;text-decoration:none;white-space:nowrap" onmouseover="this.style.color='#888'" onmouseout="this.style.color='#444'">Sign out</a>
  <form id="sf">
    <input id="q" type="text" placeholder="Search auctions..." autocomplete="off">
    <div class="pills" id="spills">
      <div class="pill on" data-site="cab" data-label="C&amp;B">C&amp;B</div>
      <div class="pill on" data-site="bat" data-label="BaT">BaT</div>
      <div class="pill on" data-site="hagerty" data-label="Hagerty">Hagerty</div>
      <div class="pill on" data-site="pcar" data-label="PCar">PCar</div>
    </div>
    <div class="pills">
      <div class="pill on" data-filter="active">Active only</div>
      <div class="pill" data-filter="starred">★ Starred</div>
      <div class="pill" data-filter="ignored">✕ Ignored</div>
    </div>
    <div class="range-row">
      <label>Year</label>
      <input type="number" id="year-lo" placeholder="Min" min="1900" max="2030" step="1">
      <span class="range-sep">–</span>
      <input type="number" id="year-hi" placeholder="Max" min="1900" max="2030" step="1">
    </div>
    <div class="range-row">
      <label>Price $</label>
      <input type="number" id="price-lo" placeholder="Min" min="0" step="500">
      <span class="range-sep">–</span>
      <input type="number" id="price-hi" placeholder="Max" min="0" step="500">
    </div>
    <button type="submit" id="search-btn">Search</button>
  </form>
</header>
<div id="statusbar">Ready — enter a search query above</div>
<div id="site-status"></div>
<div id="tag-bar"></div>
<div class="grid" id="grid"></div>

<script>
const SC = {'Cars & Bids':'#00bcd4','Bring a Trailer':'#4caf50','Hagerty':'#2196f3','PCar Market':'#9c27b0'};
const SN = {cab:'C&B', bat:'BaT', hagerty:'Hagerty', pcar:'PCar'};
let st = { bysite:{}, serverStart:'', lastQ:'', lastT:'', starred:new Set(), ignored:new Set(), tagState:new Map() };

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }

function activeSites(){ return [...document.querySelectorAll('#spills .pill')].filter(p=>!p.classList.contains('prohibit')).map(p=>p.dataset.site) }

function tlMinutes(tl){
  const t=(tl||'').trim();
  if(!t||/ended|sold|closed/i.test(t)) return Infinity;
  let m=0;
  const d=t.match(/(\d+)\s*D/i); if(d) m+=parseInt(d[1])*1440;
  const h=t.match(/(\d+)\s*H/i); if(h) m+=parseInt(h[1])*60;
  const mn=t.match(/(\d+)\s*M/i); if(mn) m+=parseInt(mn[1]);
  if(!m){
    // HH:MM:SS format (C&B, BaT)
    const ts=t.match(/(\d+):(\d{2}):\d{2}/);
    if(ts) m=parseInt(ts[1])*60+parseInt(ts[2]);
  }
  return m||Infinity;
}

function isActiveOnly()  { return !!document.querySelector('[data-filter="active"].on');  }
function isStarredOnly() { return !!document.querySelector('[data-filter="starred"].on'); }
function isIgnoredOnly() { return !!document.querySelector('[data-filter="ignored"].on'); }

function extractYear(title) {
  const m = title.match(/\b(19[0-9]{2}|20[0-2][0-9])\b/);
  return m ? parseInt(m[1]) : null;
}

function parsePrice(priceStr) {
  if(!priceStr) return null;
  const n = parseInt(priceStr.replace(/[^0-9]/g, ''));
  return isNaN(n) ? null : n;
}

['year-lo','year-hi','price-lo','price-hi'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
});

const STOP = new Set([
  'a','an','the','and','or','with','for','in','on','at','by','to','of','is','as','no','not',
  'its','this','that','are','was','has','had','been','will','but','via','my','our','your',
  'their','all','both','each','from','into','over','than','then','when','where','which',
  'who','how','why','what','one','two','three','per','sale','auction','reserve','bid',
  'car','auto','vehicle','used','new','amp','very','only','just','also','well','great',
  'nice','good','clean','rare','low','high','long','time','see','more','less',
]);

function tokenizeTitle(title) {
  return [...new Set(
    title.split(/[\s\/,.()\[\]&+#@!?:;'"]+/)
      .map(t => t.toLowerCase().replace(/[^a-z0-9-]/g, ''))
      .filter(t => t.length >= 2)
      .filter(t => !/^(19|20)\d{2}$/.test(t))
      .filter(t => !STOP.has(t))
  )];
}

function buildTagCounts() {
  const counts = new Map();
  for(const l of Object.values(st.bysite).flat().filter(l=>!st.ignored.has(l.short_id))) {
    for(const t of tokenizeTitle(l.title)) counts.set(t, (counts.get(t)||0) + 1);
  }
  return counts;
}

function renderTagBar() {
  const all = Object.values(st.bysite).flat();
  const bar = document.getElementById('tag-bar');
  if(all.length < 2) { bar.style.display='none'; return; }
  const counts = buildTagCounts();
  const tags = [...counts.entries()]
    .filter(([,n]) => n >= 2 && n < all.length)
    .sort((a,b) => b[1]-a[1])
    .slice(0, 35)
    .map(([t]) => t);
  if(!tags.length) { bar.style.display='none'; return; }
  bar.style.display = 'flex';
  bar.innerHTML = tags.map(t => {
    const s = st.tagState.get(t)||null;
    const cls = s ? ' '+s : '';
    const suffix = s==='require' ? ' ✓' : s==='prohibit' ? ' ✕' : '';
    return `<span class="tpill${cls}" data-tag="${esc(t)}">${esc(t)}${suffix}</span>`;
  }).join('');
}

document.getElementById('tag-bar').addEventListener('click', e => {
  const pill = e.target.closest('.tpill');
  if(!pill) return;
  const tag = pill.dataset.tag;
  const cur = st.tagState.get(tag)||null;
  const next = cur===null ? 'require' : cur==='require' ? 'prohibit' : null;
  if(next===null) st.tagState.delete(tag); else st.tagState.set(tag, next);
  renderTagBar();
  render();
});

function allListings(){
  const activeOnly  = isActiveOnly();
  const starredOnly = isStarredOnly();
  const ignoredOnly = isIgnoredOnly();
  const siteKey = {'Cars & Bids':'cab','Bring a Trailer':'bat','Hagerty':'hagerty','PCar Market':'pcar'};
  const reqSites  = new Set([...document.querySelectorAll('#spills .pill.on')].map(p=>p.dataset.site));
  const probSites = new Set([...document.querySelectorAll('#spills .pill.prohibit')].map(p=>p.dataset.site));
  let all = ['cab','bat','hagerty','pcar'].filter(k=>st.bysite[k]).flatMap(k=>st.bysite[k]);
  all = all.filter(l => {
    const k = siteKey[l.source]||'';
    if(probSites.has(k)) return false;
    if(reqSites.size > 0 && !reqSites.has(k)) return false;
    return true;
  });
  if(activeOnly)  all = all.filter(l => { const t=l.time_left||''; return /\d/.test(t) && !/ended|sold|closed/i.test(t); });
  if(ignoredOnly) all = all.filter(l =>  st.ignored.has(l.short_id));
  else            all = all.filter(l => !st.ignored.has(l.short_id));
  if(starredOnly) all = all.filter(l => st.starred.has(l.short_id));
  // Year filter
  const yloV = document.getElementById('year-lo').value;
  const yhiV = document.getElementById('year-hi').value;
  if(yloV || yhiV) {
    all = all.filter(l => {
      const y = extractYear(l.title); if(y===null) return true;
      if(yloV && y < parseInt(yloV)) return false;
      if(yhiV && y > parseInt(yhiV)) return false;
      return true;
    });
  }
  // Price filter
  const ploV = document.getElementById('price-lo').value;
  const phiV = document.getElementById('price-hi').value;
  if(ploV || phiV) {
    all = all.filter(l => {
      const p = parsePrice(l.price); if(p===null) return true;
      if(ploV && p < parseInt(ploV)) return false;
      if(phiV && p > parseInt(phiV)) return false;
      return true;
    });
  }
  // Tag filters
  for(const [tag, state] of st.tagState) {
    const re = new RegExp('\\b' + tag.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '\\b', 'i');
    if(state==='require')  all = all.filter(l => re.test(l.title));
    if(state==='prohibit') all = all.filter(l => !re.test(l.title));
  }
  return all.sort((a,b)=>tlMinutes(a.time_left)-tlMinutes(b.time_left));
}

function startIdx(listings){
  if(!st.serverStart) return null;
  const i = listings.findIndex(l=>l.short_id===st.serverStart);
  return i>=0 ? i : null;
}

function tlHtml(l){
  if(!l.time_left) return '';
  const t=l.time_left.toLowerCase(), cls=/ended|sold|closed/.test(t)?'ended':/\d/.test(t)?'active':'';
  return cls ? `<span class="tl ${cls}">${esc(l.time_left)}</span>` : '';
}

function cardHtml(l, seen){
  const c=SC[l.source]||'#888';
  const starred = st.starred.has(l.short_id);
  const ignored = st.ignored.has(l.short_id);
  const img=l.image_url
    ? `<img src="${esc(l.image_url)}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=noimg>No image</div>'">`
    : '<div class="noimg">No image</div>';
  return `<div class="card${seen?' seen':''}${starred?' starred':''}${ignored?' is-ignored':''}" data-id="${l.short_id}">
  <div class="cactions">
    <button class="abtn ign" onclick="toggleIgnore('${l.short_id}',event)" title="${ignored?'Unignore':'Ignore'}">✕</button>
    <button class="abtn str${starred?' on':''}" onclick="starCard('${l.short_id}',event)" title="Star">★</button>
  </div>
  <a class="clink" href="${esc(l.url)}" target="_blank" rel="noopener">
    <div class="cimg">${img}</div>
    <div class="cbody">
      <div class="cmeta">
        <span class="lid">${l.short_id}</span>
        <span class="sbadge" style="background:${c}">${esc(l.source)}</span>
      </div>
      <div class="ctitle">${esc(l.title)}</div>
      ${l.price?`<span class="cprice">${esc(l.price)}</span>`:''}
      ${tlHtml(l)}
    </div>
  </a>
</div>`;
}

function render(){
  const listings = allListings();
  const si = startIdx(listings);
  const grid = document.getElementById('grid');
  if(!listings.length){ grid.innerHTML=''; return; }
  let html='';
  for(let i=0;i<listings.length;i++){
    if(si!==null && i===si) html+='<div class="seen-div"><span>seen below</span></div>';
    html+=cardHtml(listings[i], si!==null && i>=si);
  }
  grid.innerHTML=html;
  const newN = si!==null ? si : listings.length;
  const bar = document.getElementById('statusbar');
  bar.innerHTML = `<span class="sc">${listings.length} result${listings.length!==1?'s':''}</span>`
    + (si!==null ? ` <span class="nb">${newN} new</span>` : '')
    + (st.lastQ ? ` &nbsp;for <em>"${esc(st.lastQ)}"</em>` : '')
    + (st.lastT ? ` &nbsp;&middot; ${st.lastT}` : '');
}

function setSitePill(site, cls, text){
  const ss=document.getElementById('site-status');
  let el=ss.querySelector(`[data-s="${site}"]`);
  if(!el){ el=document.createElement('div'); el.dataset.s=site; ss.appendChild(el); }
  el.className=`spill ${cls}`;
  el.innerHTML=cls==='loading'?`<div class="spin"></div> ${text}`:text;
}

function doSearch(e){
  if(e) e.preventDefault();
  const q=document.getElementById('q').value.trim();
  if(!q) return;
  const sites=activeSites();
  if(!sites.length) return;
  if(st.es){ st.es.close(); st.es=null; }
  st.bysite={}; st.lastQ=q; st.lastT=''; st.tagState=new Map();
  document.getElementById('search-btn').disabled=true;
  document.getElementById('grid').innerHTML='';
  document.getElementById('site-status').innerHTML='';
  document.getElementById('tag-bar').style.display='none';
  const activeOnly=!!document.querySelector('[data-filter="active"].on');
  const sp=sites.map(s=>`sites=${encodeURIComponent(s)}`).join('&');
  const url=`/api/search/stream?q=${encodeURIComponent(q)}&${sp}${activeOnly?'&active=1':''}`;
  sites.forEach(s=>setSitePill(s,'loading',SN[s]));
  const es=new EventSource(url);
  st.es=es;
  es.addEventListener('site',ev=>{
    const d=JSON.parse(ev.data);
    st.bysite[d.site]=d.listings||[];
    setSitePill(d.site, d.error?'error':'done', (d.error?'✕ ': d.listings.length+' · ')+SN[d.site]);
    renderTagBar();
    render();
  });
  es.addEventListener('done',ev=>{
    const d=JSON.parse(ev.data);
    st.serverStart=d.start_id||'';
    st.ignored=new Set(d.ignored||[]);
    st.lastT=new Date().toLocaleTimeString();
    es.close(); st.es=null;
    document.getElementById('search-btn').disabled=false;
    renderTagBar();
    render();
  });
  es.onerror=()=>{ es.close(); st.es=null; document.getElementById('search-btn').disabled=false; };
}

async function toggleIgnore(id, e){
  e.preventDefault(); e.stopPropagation();
  const nowIgnored = !st.ignored.has(id);
  if(nowIgnored) st.ignored.add(id); else st.ignored.delete(id);
  // Card disappears from current view if it no longer matches the filter
  const willDisappear = isIgnoredOnly() ? !nowIgnored : nowIgnored;
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if(card && willDisappear){
    card.classList.add('out');
    setTimeout(()=>render(), 230);
  } else {
    render();
  }
  await fetch('/api/ignore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,ignored:nowIgnored})});
}

async function setStart(id, e){
  e.preventDefault(); e.stopPropagation();
  st.serverStart=id;
  await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  render();
}

async function starCard(id, e){
  e.preventDefault(); e.stopPropagation();
  const nowStarred = !st.starred.has(id);
  if(nowStarred) st.starred.add(id); else st.starred.delete(id);
  const card=document.querySelector(`.card[data-id="${id}"]`);
  if(card){
    card.classList.toggle('starred', nowStarred);
    const btn=card.querySelector('.abtn.str');
    if(btn) btn.classList.toggle('on', nowStarred);
  }
  await fetch('/api/star',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,starred:nowStarred})});
}

document.getElementById('sf').addEventListener('submit', doSearch);

// Site pills: three-state cycle require(.on) → neutral → prohibit → require
document.querySelectorAll('#spills .pill').forEach(p=>p.addEventListener('click',()=>{
  const cur = p.classList.contains('on') ? 'require' : p.classList.contains('prohibit') ? 'prohibit' : 'neutral';
  const next = cur==='require' ? 'neutral' : cur==='neutral' ? 'prohibit' : 'require';
  p.classList.remove('on','prohibit');
  if(next==='require') p.classList.add('on');
  if(next==='prohibit') p.classList.add('prohibit');
  p.textContent = p.dataset.label + (next==='require' ? ' ✓' : next==='prohibit' ? ' ✕' : '');
  render();
}));

// Filter pills: simple toggle
document.querySelectorAll('.pill[data-filter]').forEach(p=>p.addEventListener('click',()=>{
  p.classList.toggle('on');
  // Starred and Ignored are mutually exclusive
  if(p.dataset.filter==='starred' && p.classList.contains('on'))
    document.querySelector('[data-filter="ignored"]')?.classList.remove('on');
  else if(p.dataset.filter==='ignored' && p.classList.contains('on'))
    document.querySelector('[data-filter="starred"]')?.classList.remove('on');
  render();
}));

// Always pre-load ignored/starred so they're available before the first search completes
const initQ=new URLSearchParams(location.search).get('q')||'';
fetch('/api/store').then(r=>r.json()).then(d=>{
  st.serverStart=d.start||''; st.starred=new Set(d.starred||[]); st.ignored=new Set(d.ignored||[]);
  if(initQ){ document.getElementById('q').value=initQ; doSearch(null); }
});
</script>
</body>
</html>
"""


# ─── Main Runner ──────────────────────────────────────────────────────────────

ALL_SITES = {
    "cab":     ("Cars & Bids",    "cyan",    scrape_carsandbids),
    "bat":     ("Bring a Trailer","green",   scrape_bat),
    "hagerty": ("Hagerty",        "blue",    scrape_hagerty),
    "pcar":    ("PCar Market",    "magenta", scrape_pcarmarket),
}


def _listing_json(l: Listing) -> dict:
    """Serialize a Listing to JSON-safe dict including computed properties."""
    d = asdict(l)
    d["short_id"] = l.short_id
    d["is_active"] = l.is_active
    return d


async def _scrape_all(
    query: str,
    site_keys: list[str],
    debug: bool = False,
    on_site_done=None,          # optional callback(site_key, listings_or_exception)
) -> list[Listing]:
    """Run all scrapers in parallel; return combined listings."""
    active = {k: v for k, v in ALL_SITES.items() if k in site_keys}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not debug)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        pages = await asyncio.gather(*[context.new_page() for _ in active])

        async def _one(i, key, scraper_fn):
            try:
                result = await scraper_fn(pages[i], query, debug)
            except Exception as e:
                result = e
            if on_site_done:
                await on_site_done(key, result)
            return result

        results = await asyncio.gather(*[
            _one(i, k, fn)
            for i, (k, (_, _, fn)) in enumerate(active.items())
        ])
        await browser.close()

    listings: list[Listing] = []
    for k, result in zip(active.keys(), results):
        name = active[k][0]
        if isinstance(result, Exception):
            _log(f"[{name}] Failed: {result}", "error")
        else:
            listings.extend(result)
    return listings


async def run(
    query: str,
    output_html: bool,
    output_json: bool,
    debug: bool,
    open_browser: bool,
    sites: list[str],
    only_active: bool = False,
    only_inactive: bool = False,
    ignored: set[str] | None = None,
    start_id: str = "",
):
    # sites is a list of keys from ALL_SITES; empty means all
    active = {k: v for k, v in ALL_SITES.items() if not sites or k in sites}

    if HAS_RICH:
        _console.print(
            f"\n[bold cyan]AuctionWatch[/bold cyan]  "
            f"searching for [bold white]\"{query}\"[/bold white]...\n"
        )
        for name, color, _ in active.values():
            _console.print(f"  [dim]→[/dim] [{color}]{name}[/{color}]")
        _console.print()
    else:
        print(f"\nSearching for '{query}' across {len(active)} site(s)...\n")

    listings = await _scrape_all(query, list(active.keys()), debug)

    if only_active:
        listings = [l for l in listings if l.is_active is not False]
    elif only_inactive:
        listings = [l for l in listings if l.is_active is False]

    # Filter ignored listings
    if ignored:
        listings = [l for l in listings if l.short_id not in ignored]

    # Sort by time remaining (active first, ended last)
    listings.sort(key=lambda l: _time_left_minutes(l.time_left))

    # Always show terminal output
    display_terminal(listings, query, start_id=start_id)

    # JSON output
    if output_json:
        print(json.dumps([asdict(l) for l in listings], indent=2))

    # HTML output
    if output_html:
        html_content = generate_html(listings, query)
        safe_query = "".join(c if c.isalnum() or c in "-_ " else "_" for c in query)
        outfile = f"auctionwatch_{safe_query.replace(' ', '_')}.html"
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(html_content)
        if HAS_RICH:
            _console.print(f"\n[green]✓[/green] HTML saved → [bold]{outfile}[/bold]")
        else:
            print(f"\nHTML saved to: {outfile}")
        if open_browser:
            webbrowser.open(f"file://{os.path.abspath(outfile)}")

    return listings


def serve_web(initial_query: str = "", port: int = 5173):
    try:
        from flask import Flask, Response, request as freq, jsonify
    except ImportError:
        _log("flask not installed — run: pip install flask", "error")
        sys.exit(1)

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = _get_secret_key()
    _init_db()

    def _uid():
        """Return current user_id from session, or None."""
        from flask import session as fsession
        return fsession.get("user_id")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        from flask import request as freq2, session as fsession, redirect
        if freq2.method == "POST":
            uid = _db_check_user(freq2.form.get("username",""), freq2.form.get("password",""))
            if uid:
                fsession["user_id"] = uid
                fsession["username"] = freq2.form.get("username","").strip()
                return redirect("/")
            return _LOGIN_HTML.replace("{{error}}", "Invalid username or password")
        return _LOGIN_HTML.replace("{{error}}", "")

    @app.route("/register", methods=["POST"])
    def register():
        from flask import request as freq2, session as fsession, redirect
        username = freq2.form.get("username", "").strip()
        password = freq2.form.get("password", "")
        if not username or not password:
            return _LOGIN_HTML.replace("{{error}}", "Username and password are required")
        if _db_create_user(username, password):
            uid = _db_check_user(username, password)
            fsession["user_id"] = uid
            fsession["username"] = username
            return redirect("/")
        return _LOGIN_HTML.replace("{{error}}", "Username already taken")

    @app.route("/logout")
    def logout():
        from flask import session as fsession, redirect
        fsession.clear()
        return redirect("/login")

    @app.route("/")
    def index():
        from flask import redirect
        if not _uid():
            return redirect("/login")
        return _WEB_HTML

    @app.route("/api/search/stream")
    def search_stream():
        q       = freq.args.get("q", "").strip()
        sites   = freq.args.getlist("sites") or list(ALL_SITES.keys())
        act_only = freq.args.get("active") == "1"

        if not q:
            return jsonify({"error": "no query"}), 400

        uid = _uid()
        if not uid:
            return jsonify({"error": "not authenticated"}), 401

        ignored  = _db_get_ignored(uid)
        start_id = _db_get_start(uid)

        result_q: queue.Queue = queue.Queue()

        def _run():
            async def _scrape():
                active = {k: v for k, v in ALL_SITES.items() if k in sites}
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    ctx = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                    )
                    pages = await asyncio.gather(*[ctx.new_page() for _ in active])

                    async def _one(i, key, name, scraper_fn):
                        try:
                            listings = await scraper_fn(pages[i], q, False)
                            if act_only:
                                listings = [l for l in listings if l.is_active is not False]
                            result_q.put({"site": key, "listings": [_listing_json(l) for l in listings]})
                        except Exception as exc:
                            result_q.put({"site": key, "listings": [], "error": str(exc)})

                    await asyncio.gather(*[
                        _one(i, k, name, fn)
                        for i, (k, (name, _, fn)) in enumerate(active.items())
                    ])
                    await browser.close()
                result_q.put(None)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_scrape())
            finally:
                loop.close()

        threading.Thread(target=_run, daemon=True).start()

        def _generate():
            while True:
                item = result_q.get()
                if item is None:
                    done = json.dumps({"start_id": start_id, "ignored": list(ignored)})
                    yield f"event: done\ndata: {done}\n\n"
                    break
                yield f"event: site\ndata: {json.dumps(item)}\n\n"

        return Response(
            _generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/ignore", methods=["POST"])
    def api_ignore():
        uid = _uid()
        if not uid: return jsonify({"error": "not authenticated"}), 401
        lid     = (freq.json or {}).get("id", "")
        ignored = (freq.json or {}).get("ignored", True)
        if lid:
            _db_set_ignored(uid, lid, ignored)
        return jsonify({"ok": True})

    @app.route("/api/start", methods=["POST"])
    def api_start():
        uid = _uid()
        if not uid: return jsonify({"error": "not authenticated"}), 401
        lid = (freq.json or {}).get("id", "")
        if lid:
            _db_set_start(uid, lid)
        return jsonify({"ok": True})

    @app.route("/api/star", methods=["POST"])
    def api_star():
        uid = _uid()
        if not uid: return jsonify({"error": "not authenticated"}), 401
        lid     = (freq.json or {}).get("id", "")
        starred = (freq.json or {}).get("starred", True)
        if lid:
            _db_set_starred(uid, lid, starred)
        return jsonify({"ok": True})

    @app.route("/api/store")
    def api_store():
        uid = _uid()
        if not uid: return jsonify({"error": "not authenticated"}), 401
        return jsonify({"ignored": list(_db_get_ignored(uid)),
                        "start":   _db_get_start(uid),
                        "starred": list(_db_get_starred(uid))})

    # In a server environment (Railway etc.) PORT is set; bind publicly and skip browser open
    server_port = int(os.environ.get("PORT", port))
    is_server   = "PORT" in os.environ
    host        = "0.0.0.0" if is_server else "127.0.0.1"

    url = f"http://{host}:{server_port}"
    if HAS_RICH:
        _console.print(f"\n[bold cyan]AuctionWatch[/bold cyan] → [bold]{url}[/bold]   (Ctrl+C to stop)\n")
    else:
        print(f"\nServing at {url}  (Ctrl+C to stop)")

    if not is_server:
        launch_url = f"http://127.0.0.1:{server_port}" + (f"?q={quote_plus(initial_query)}" if initial_query else "")
        webbrowser.open(launch_url)

    app.run(host=host, port=server_port, debug=False, threaded=True)


def main():
    parser = argparse.ArgumentParser(
        description="Search car auction sites simultaneously",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python auctionwatch.py "porsche 911"
  python auctionwatch.py "ferrari 308" --html
  python auctionwatch.py "bmw m3 e46" --html --open
  python auctionwatch.py "alfa romeo" --json
  python auctionwatch.py "porsche 911" --cab --bat
  python auctionwatch.py "land rover defender" --debug
        """,
    )
    parser.add_argument("query", nargs="?", help="Search query (e.g. 'porsche 911 turbo')")
    parser.add_argument("--html", action="store_true", help="Save results as HTML file")
    parser.add_argument("--open", action="store_true", help="Open HTML in browser (implies --html)")
    parser.add_argument("--json", action="store_true", help="Also print raw JSON to stdout")
    parser.add_argument(
        "--debug", action="store_true",
        help="Show browser UI and save page HTML dumps for debugging selectors"
    )
    parser.add_argument("--serve", action="store_true", help="Start interactive web UI")
    parser.add_argument("--port",  type=int, default=int(os.environ.get("PORT", 5173)), metavar="PORT",
                        help="Port for --serve (default: 5173, or $PORT env var)")
    parser.add_argument(
        "--ignore", metavar="ID",
        help="Permanently hide listing with this 4-char ID from future results"
    )
    parser.add_argument(
        "--start", metavar="ID",
        help="Mark listing ID as 'seen'; future results show a divider here"
    )

    status_group = parser.add_mutually_exclusive_group()
    status_group.add_argument(
        "--active", action="store_true",
        help="Show only active auctions (time remaining)"
    )
    status_group.add_argument(
        "--inactive", action="store_true",
        help="Show only ended/sold auctions"
    )

    # Site filters — if none specified, all sites are queried
    site_group = parser.add_argument_group(
        "site filters (default: all sites)"
    )
    site_group.add_argument("--cab",     dest="sites", action="append_const", const="cab",
                            help="Search Cars & Bids")
    site_group.add_argument("--bat",     dest="sites", action="append_const", const="bat",
                            help="Search Bring a Trailer")
    site_group.add_argument("--hagerty", dest="sites", action="append_const", const="hagerty",
                            help="Search Hagerty Marketplace")
    site_group.add_argument("--pcar",    dest="sites", action="append_const", const="pcar",
                            help="Search PCar Market")

    args = parser.parse_args()

    # Handle --ignore and --start (store updates, no search required)
    if args.ignore:
        new_list = store_ignore(args.ignore)
        if HAS_RICH:
            _console.print(f"[green]✓[/green] Ignored [dim yellow]{args.ignore}[/dim yellow]  "
                           f"[dim]({len(new_list)} total ignored)[/dim]")
        else:
            print(f"Ignored {args.ignore} ({len(new_list)} total ignored)")
        if not args.query:
            return

    if args.start:
        store_set_start(args.start)
        if HAS_RICH:
            _console.print(f"[green]✓[/green] Start marker set to [dim yellow]{args.start}[/dim yellow]")
        else:
            print(f"Start marker set to {args.start}")
        if not args.query:
            return

    if args.serve:
        serve_web(initial_query=args.query or "", port=args.port)
        return

    if not args.query:
        parser.error("a search query is required unless using --ignore or --start alone")

    if args.open:
        args.html = True

    asyncio.run(run(
        query=args.query,
        output_html=args.html,
        output_json=args.json,
        debug=args.debug,
        open_browser=args.open,
        sites=args.sites or [],
        only_active=args.active,
        only_inactive=args.inactive,
        ignored=store_get_ignored(),
        start_id=store_get_start(),
    ))


if __name__ == "__main__":
    main()
