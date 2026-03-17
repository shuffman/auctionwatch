# AuctionWatch

Search Cars & Bids, Bring a Trailer, Hagerty Marketplace, and PCar Market simultaneously from a single command.

## Installation

```bash
pip install playwright rich flask
playwright install chromium
```

## Usage

### Interactive web UI (recommended)

```bash
python auctionwatch.py "porsche 911" --serve
```

Opens a browser tab at `http://127.0.0.1:5173` with a live card grid. Results stream in per-site as they load. Use `--port` to change the port.

### Terminal

```bash
python auctionwatch.py "bmw m3 e46"
```

Prints a formatted table sorted by time remaining. Active auctions appear first; ended listings are pushed to the bottom.

### HTML file

```bash
python auctionwatch.py "ferrari 308" --html
python auctionwatch.py "ferrari 308" --html --open   # open in browser immediately
```

Saves a self-contained dark-themed HTML card grid to `auctionwatch_ferrari_308.html`.

### JSON output

```bash
python auctionwatch.py "alfa romeo" --json
```

Prints raw JSON to stdout (in addition to the terminal table).

## Site filters

By default all four sites are searched. Use flags to search only specific sites:

```bash
python auctionwatch.py "porsche 911" --cab           # Cars & Bids only
python auctionwatch.py "porsche 911" --bat           # Bring a Trailer only
python auctionwatch.py "porsche 911" --hagerty       # Hagerty Marketplace only
python auctionwatch.py "porsche 911" --pcar          # PCar Market only
python auctionwatch.py "porsche 911" --cab --bat     # multiple sites
```

## Status filters

```bash
python auctionwatch.py "land rover defender" --active    # bidding open only
python auctionwatch.py "land rover defender" --inactive  # ended/sold only
```

In the web UI, the **Active only** toggle does the same thing instantly without re-searching.

## Listing IDs

Every listing gets a stable 4-character hex ID derived from its URL (e.g. `a3f2`). IDs are consistent across searches and are shown in the terminal table and on each web card.

### Ignoring listings

```bash
python auctionwatch.py --ignore a3f2
```

The listing is permanently hidden from all future results. Ignored IDs are stored in `~/.auctionwatch.json`. In the web UI, click the **✕** button on any card.

### Start marker

```bash
python auctionwatch.py --start a3f2
```

Sets a "seen below" divider at that listing. Everything above is new; everything at or below it is dimmed. In the web UI, this is set automatically by the **↓ start here** action (available via the JS API).

You can combine store operations with a search:

```bash
python auctionwatch.py "porsche 911" --ignore a3f2
```

### Starring listings

In the web UI, click the **★** button on any card to star it. Starred cards get a gold border and persist across sessions.

## Web UI controls

| Control | Effect |
|---|---|
| Site pills (C&B, BaT, Hagerty, PCar) | Instantly show/hide cards from that source |
| Active only toggle | Instantly hide ended listings |
| **✕** button on card | Ignore listing permanently |
| **★** button on card | Toggle star (gold border, persisted) |
| Click card | Open listing in new tab |

## Debugging

```bash
python auctionwatch.py "porsche 911" --debug
```

Runs Chromium with a visible window and saves `debug_*.html` snapshots of each site's page for inspecting selectors.

## Persistent store

`~/.auctionwatch.json` stores ignored IDs, the start marker, and starred IDs. It is a plain JSON file and can be edited by hand.
