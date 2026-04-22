#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if ! python3 -c "import playwright" 2>/dev/null; then
  echo "Installing dependencies..."
  pip3 install playwright rich flask playwright-stealth
fi

python3 -m playwright install chromium

python3 auctionwatch.py --serve
