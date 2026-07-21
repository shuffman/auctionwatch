from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse


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
        """8-char hex ID derived from SHA-256 of the URL path (no query params).

        Was 4 chars before v1.7.19 — too few bits: a 200-listing result set had a
        ~26% chance of a collision, which made stars/ignores bleed across listings.
        Stored 4-char IDs are still honored as prefix matches for compatibility.
        """
        path = urlparse(self.url).path.rstrip("/")
        return hashlib.sha256(path.encode()).hexdigest()[:8]


SOURCE_COLORS_RICH = {
    "Cars & Bids": "cyan",
    "Bring a Trailer": "green",
    "Hagerty": "blue",
    "PCar Market": "magenta",
    "Craigslist": "orange1",
    "Cars.com": "yellow",
    "Porsche Finder": "red",
    "CarMax": "red",
    "Carvana": "cyan",
    "eBay Motors": "red",
    "Hemmings": "red",
}

SOURCE_COLORS_HTML = {
    "Cars & Bids": "#00bcd4",
    "Bring a Trailer": "#4caf50",
    "Hagerty": "#2196f3",
    "PCar Market": "#9c27b0",
    "Craigslist": "#ff9800",
    "Cars.com":   "#e91e63",
    "Porsche Finder": "#d5001c",
    "CarMax":  "#c9201f",
    "Carvana": "#00a78e",
}
