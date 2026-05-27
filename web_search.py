"""
Web search and on-demand page fetcher.

google_search(query) — constructs a Google search URL, scrapes the results
                       page, and returns a list of {title, url, snippet} dicts.

fetch_and_index(url) — fetches a URL, extracts clean text, chunks and caches
                       it in SQLite. Results cached for 6 hours.
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

_GOOGLE_SKIP = re.compile(
    r"google\.|youtube\.com|webcache\.googleusercontent|accounts\."
    r"|support\.google|maps\.google|play\.google|policies\.google"
)

SEARCH_CACHE_TTL = 60 * 30  # 30 minutes for search result lists


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
        CREATE TABLE IF NOT EXISTS search_cache (
            query    TEXT PRIMARY KEY,
            results  TEXT,
            fetched  INTEGER
        );
    """)
    conn.commit()
    return conn


# ── Google search scraper ─────────────────────────────────────────────────────

def google_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Constructs https://www.google.com/search?q=<query>, scrapes organic
    results, and returns a list of {title, url, snippet} dicts.
    Results are cached for 30 minutes.
    """
    import json as _json

    conn = open_web_db()
    cutoff = int(time.time()) - SEARCH_CACHE_TTL
    row = conn.execute(
        "SELECT results FROM search_cache WHERE query = ? AND fetched > ?",
        (query, cutoff),
    ).fetchone()
    if row:
        conn.close()
        return _json.loads(row[0])

    search_url = "https://www.google.com/search?" + urllib.parse.urlencode({
        "q": query, "num": 10, "hl": "en", "gl": "us",
    })
    req = urllib.request.Request(search_url, headers={
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(MAX_PAGE_BYTES)

    soup = BeautifulSoup(raw, "lxml")
    results: list[dict] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]

        # decode Google redirect URLs (/url?q=https://...)
        if href.startswith("/url?"):
            parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = parsed_qs.get("q", [""])[0]

        if not href.startswith("http"):
            continue
        if _GOOGLE_SKIP.search(href):
            continue

        # title is the nearest h3
        h3 = a.find("h3")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        if not title:
            continue

        # snippet: largest text block in the enclosing result div
        snippet = ""
        parent = a.parent
        for _ in range(5):          # walk up at most 5 levels
            if parent is None:
                break
            for div in parent.find_all("div", recursive=False):
                text = div.get_text(" ", strip=True)
                if 40 < len(text) < 400 and text != title:
                    snippet = text
                    break
            if snippet:
                break
            parent = parent.parent

        if href not in {r["url"] for r in results}:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    conn.execute(
        "INSERT OR REPLACE INTO search_cache(query, results, fetched) VALUES (?, ?, ?)",
        (query, _json.dumps(results), int(time.time())),
    )
    conn.commit()
    conn.close()
    return results


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
