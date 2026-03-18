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
import asyncio
import json
import os
import sys
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

from models import Listing, SOURCE_COLORS_RICH, SOURCE_COLORS_HTML
from store import (
    store_ignore, store_set_start, store_get_ignored, store_get_start,
)
from scrapers import (
    HAS_RICH, _console, _log,
    scrape_carsandbids, scrape_bat, scrape_hagerty, scrape_pcarmarket, scrape_craigslist,
)


# ─── Main Runner ──────────────────────────────────────────────────────────────

ALL_SITES = {
    "cab":     ("Cars & Bids",    "cyan",    scrape_carsandbids),
    "bat":     ("Bring a Trailer","green",   scrape_bat),
    "hagerty": ("Hagerty",        "blue",    scrape_hagerty),
    "pcar":    ("PCar Market",    "magenta", scrape_pcarmarket),
    "cl":      ("Craigslist",     "orange1", scrape_craigslist),
}


def _listing_json(l: Listing) -> dict:
    """Serialize a Listing to JSON-safe dict including computed properties."""
    d = asdict(l)
    d["short_id"] = l.short_id
    d["is_active"] = l.is_active
    return d


def _time_left_minutes(time_left: str) -> float:
    """Convert a time-left string to minutes for sorting. Ended/unknown → infinity."""
    import re
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


def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


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
        from rich.table import Table
        from rich.text import Text
        from rich import box as rich_box

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
    site_group.add_argument("--cl",      dest="sites", action="append_const", const="cl",
                            help="Search Craigslist (west coast metros)")

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
        from web import serve_web
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
