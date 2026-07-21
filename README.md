# AuctionWatch

Search car listings across eleven sites simultaneously — auctions and fixed-price — from one search box.

**Auction sites:** Cars & Bids, Bring a Trailer, Hagerty Marketplace, PCar Market, eBay Motors
**Fixed-price sites:** Craigslist (18 western metros), Cars.com, Porsche Finder, CarMax, Carvana, Hemmings

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

Or just run `./run.sh`, which installs dependencies and starts the web UI.

## Usage

### Interactive web UI (recommended)

```bash
python auctionwatch.py --serve
python auctionwatch.py "porsche 911" --serve   # start with an initial query
```

Opens a browser tab at `http://127.0.0.1:5173` with a live card grid. Results stream in per-site as they load. Use `--port` to change the port and `--host 0.0.0.0` for LAN access.

The UI defaults to **day mode**; the ☾/☀ button in the header switches to night mode (remembered per browser).

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

Prints raw JSON to stdout (in addition to the terminal table). Includes each listing's `short_id` and computed `is_active` flag.

## Site filters

By default all eleven sites are searched. Use flags to search only specific sites:

```bash
python auctionwatch.py "porsche 911" --cab           # Cars & Bids only
python auctionwatch.py "porsche 911" --bat           # Bring a Trailer only
python auctionwatch.py "porsche 911" --hagerty       # Hagerty Marketplace
python auctionwatch.py "porsche 911" --pcar          # PCar Market
python auctionwatch.py "porsche 911" --pf            # Porsche Finder (finder.porsche.com)
python auctionwatch.py "porsche 911" --cl            # Craigslist (west coast metros)
python auctionwatch.py "porsche 911" --carscom       # Cars.com
python auctionwatch.py "porsche 911" --carmax        # CarMax
python auctionwatch.py "porsche 911" --carvana       # Carvana
python auctionwatch.py "porsche 911" --ebay          # eBay Motors
python auctionwatch.py "porsche 911" --hemmings      # Hemmings
python auctionwatch.py "porsche 911" --cab --bat     # multiple sites
```

In the web UI, the **Auctions / Fixed Price / All** preset buttons toggle the corresponding site groups.

## Location search

```bash
python auctionwatch.py "porsche 911" --zip 98101 --radius 100
```

Cars.com, eBay Motors, and CarMax accept a ZIP code and search radius (miles). Other sites ignore these flags. The web UI has matching **Near ZIP / within** inputs.

## Status filters

```bash
python auctionwatch.py "land rover defender" --active    # bidding open only
python auctionwatch.py "land rover defender" --inactive  # ended/sold only
```

In the web UI, the **Active only** toggle does the same thing instantly without re-searching.

## Listing IDs

Every listing gets a stable 8-character hex ID derived from its URL (e.g. `a3f2c91b`). IDs are consistent across searches and are shown in the terminal table and on each web card. (IDs saved before v1.7.19 were 4 characters; they are still recognized.)

### Ignoring listings

```bash
python auctionwatch.py --ignore a3f2c91b
```

The listing is permanently hidden from all future results. Ignored IDs are stored in `~/.auctionwatch.json`. In the web UI, click the **✕** button on any card.

### Start marker

```bash
python auctionwatch.py --start a3f2c91b
```

Sets a "seen below" divider at that listing. Everything above is new; everything at or below it is dimmed. In the web UI, click the **⚑** button on a card to place the marker there.

You can combine store operations with a search:

```bash
python auctionwatch.py "porsche 911" --ignore a3f2c91b
```

### Starring listings

In the web UI, click the **★** button on any card to star it. Starred cards get a gold border and persist across sessions.

## Accounts (web UI)

Stars, ignores, the start marker, and recent-search history are saved per user. Click **Sign in**, enter a username and password — new usernames are registered on first sign-in. Without signing in you can still search and filter, but stars/ignores aren't saved (the UI tells you so).

## Web UI controls

| Control | Effect |
|---|---|
| Site pills (C&B, BaT, Hagerty, PCar, PF, CL, Cars.com, CarMax, Carvana, eBay, Hemmings) | Instantly show/hide cards from that source |
| Auctions / Fixed Price / All presets | Toggle whole site groups |
| Cars only / Active only / ★ Starred / ✕ Ignored pills | Instant client-side filters |
| Year and Price range inputs | Instant range filters |
| Sort dropdown | Time left, price ↑↓, year ↑↓ |
| Tag bar | Auto-generated keyword pills; click to require (✓) or prohibit (✕) |
| **✕** button on card | Ignore listing permanently |
| **⚑** button on card | Set the "seen below" start marker |
| **★** button on card | Toggle star (gold border, persisted) |
| ☾/☀ header button | Toggle day/night mode (day is default) |
| Click card | Open listing in new tab |

All filter state is encoded in the URL, so a filtered view can be bookmarked or shared.

## Debugging

```bash
python auctionwatch.py "porsche 911" --debug
```

Runs Chromium with a visible window and saves `debug_*.html` snapshots of each site's page for inspecting selectors.

## Persistent store

- **CLI:** `~/.auctionwatch.json` stores ignored IDs, the start marker, and starred IDs. Plain JSON, editable by hand.
- **Web UI:** SQLite at `$DATA_DIR/.auctionwatch.db` (defaults to `~`), with per-user tables for stars/ignores/start marker/search history and pbkdf2-hashed passwords. The Flask session key lives next to it in `.auctionwatch.secret` (chmod 600).

## Deployment

The included `Dockerfile` and `railway.toml` deploy the web UI to Railway (or any Docker host). Set a volume at `/data` (the image sets `DATA_DIR=/data`) so the database survives redeploys. `$PORT` is honored automatically.

**Note:** Carvana and Hemmings sit behind Cloudflare bot protection that blocks hosting-provider IPs. On such deployments those two sites report "Blocked by Cloudflare challenge" in their status pill; they work normally when running locally.
