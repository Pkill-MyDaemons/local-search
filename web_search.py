"""
On-demand page fetcher: given a URL, fetches it like a browser,
extracts clean text, chunks it, and caches in SQLite.
No external search engine involved.
"""
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

import indexer as idx

WEB_DB_PATH = Path(__file__).parent / "web_cache.db"
CACHE_TTL_SECONDS = 60 * 60 * 6   # 6 hours — re-fetch stale pages
FETCH_TIMEOUT = 10
MAX_PAGE_BYTES = 512 * 1024

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_STRIP_TAGS = {
    "script", "style", "nav", "header", "footer",
    "aside", "noscript", "svg", "iframe", "form",
    "button", "select", "input", "textarea",
}


# ── DB setup ──────────────────────────────────────────────────────────────────

def open_web_db() -> sqlite3.Connection:
    conn = sqlite3.connect(WEB_DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS web_meta (
            url      TEXT PRIMARY KEY,
            title    TEXT,
            fetched  INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS web_docs USING fts5(
            url        UNINDEXED,
            chunk_idx  UNINDEXED,
            title,
            content,
            tokenize = 'porter unicode61 remove_diacritics 1'
        );
    """)
    conn.commit()
    return conn


def _is_cached(conn: sqlite3.Connection, url: str) -> bool:
    cutoff = int(time.time()) - CACHE_TTL_SECONDS
    row = conn.execute(
        "SELECT 1 FROM web_meta WHERE url = ? AND fetched > ?", (url, cutoff)
    ).fetchone()
    return row is not None


def _evict(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM web_docs WHERE url = ?", (url,))
    conn.execute("DELETE FROM web_meta WHERE url = ?", (url,))
    conn.commit()


# ── page fetcher + text extractor ────────────────────────────────────────────

def fetch_and_index(url: str) -> tuple[sqlite3.Connection, str]:
    """
    Fetches url if not cached, indexes its text, returns (conn, title).
    Caller is responsible for closing conn.
    """
    conn = open_web_db()
    conn.row_factory = sqlite3.Row

    if _is_cached(conn, url):
        title = conn.execute("SELECT title FROM web_meta WHERE url = ?", (url,)).fetchone()["title"]
        return conn, title

    _evict(conn, url)

    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            raise ValueError(f"not an HTML page ({content_type})")
        raw = resp.read(MAX_PAGE_BYTES)

    soup = BeautifulSoup(raw, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    body = (
        soup.find("article")
        or soup.find("main")
        or soup.find(id=re.compile(r"content|article|main", re.I))
        or soup.find(class_=re.compile(r"content|article|post|entry", re.I))
        or soup.find("body")
        or soup
    )

    text = body.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = idx.clean_text(text, is_markdown=False)

    chunks = idx.chunk_text(text)
    for i, chunk in enumerate(chunks):
        conn.execute(
            "INSERT INTO web_docs(url, chunk_idx, title, content) VALUES (?, ?, ?, ?)",
            (url, i, title, chunk),
        )
    conn.execute(
        "INSERT OR REPLACE INTO web_meta(url, title, fetched) VALUES (?, ?, ?)",
        (url, title, int(time.time())),
    )
    conn.commit()
    return conn, title


def search_page(url: str, query: str, limit: int = 5) -> list[sqlite3.Row]:
    """Fetch+index url, then return FTS5-ranked chunks matching query."""
    conn, _ = fetch_and_index(url)
    rows = conn.execute(
        """
        SELECT url, chunk_idx, title, content, rank
        FROM web_docs
        WHERE web_docs MATCH ? AND url = ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, url, limit),
    ).fetchall()
    conn.close()
    return rows


def get_page(url: str, limit: int = 3) -> list[sqlite3.Row]:
    """Fetch+index url, then return the first N chunks (no query filter)."""
    conn, _ = fetch_and_index(url)
    rows = conn.execute(
        "SELECT url, chunk_idx, title, content FROM web_docs WHERE url = ? ORDER BY chunk_idx LIMIT ?",
        (url, limit),
    ).fetchall()
    conn.close()
    return rows
