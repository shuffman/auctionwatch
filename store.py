import json
import os
import secrets
import sqlite3
from pathlib import Path


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
                password_hash TEXT NOT NULL DEFAULT ''
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

def _db_get_or_create_user(username: str) -> int:
    """Return the user_id for username, creating the user if needed."""
    username = username.strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, '')",
            (username,),
        )
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    return row[0]

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
