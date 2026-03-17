FROM python:3.12-slim

# Install Playwright + Chromium system dependencies, then clean up apt cache
RUN pip install --no-cache-dir playwright rich flask \
 && playwright install chromium --with-deps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py .

# Persistent data (SQLite DB + secret key) lives here.
# Mount a Railway volume at /data so it survives redeploys.
ENV DATA_DIR=/data

# Railway injects $PORT at runtime; the app reads it automatically.
EXPOSE 8080

CMD ["python", "auctionwatch.py", "--serve"]
