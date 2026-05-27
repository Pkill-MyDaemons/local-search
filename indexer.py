#!/usr/bin/env python3
"""
Scans data/ for .md and .json files, cleans them, chunks them,
and builds a SQLite FTS5 index in search.db (one row per chunk).
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "search.db"
DATA_DIR = Path(__file__).parent / "data"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MAX_CHUNK_CHARS = 1200  # hard ceiling per chunk stored in DB


def create_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS docs;
        CREATE VIRTUAL TABLE docs USING fts5(
            path       UNINDEXED,
            chunk_idx  UNINDEXED,
            title,
            content,
            tokenize = 'porter unicode61 remove_diacritics 1'
        );
    """)
    conn.commit()


# ── cleaning ──────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BASE64_RE = re.compile(r"data:[a-z]+/[a-z+]+;base64,[A-Za-z0-9+/=]{50,}")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_text(text: str, is_markdown: bool) -> str:
    if is_markdown:
        text = _FRONTMATTER_RE.sub("", text)
        text = _HTML_COMMENT_RE.sub("", text)
        text = _BASE64_RE.sub("[image]", text)
        text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# ── chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        # try to break on a newline rather than mid-word
        if end < len(text):
            nl = chunk.rfind("\n")
            if nl > size // 2:
                end = start + nl + 1
                chunk = text[start:end]
        chunks.append(chunk[:MAX_CHUNK_CHARS])
        start = end - overlap
    return [c for c in chunks if c.strip()]


# ── title inference ───────────────────────────────────────────────────────────

def infer_title(path: Path, content: str) -> str:
    if path.suffix == ".md":
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


# ── indexing ──────────────────────────────────────────────────────────────────

def index_file(conn: sqlite3.Connection, path: Path, root: Path) -> int:
    rel = str(path.relative_to(root))
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  skip {rel}: {e}", file=sys.stderr)
        return 0

    is_md = path.suffix == ".md"
    if path.suffix == ".json":
        try:
            raw = json.dumps(json.loads(raw), indent=2)
        except json.JSONDecodeError:
            pass

    text = clean_text(raw, is_md)
    title = infer_title(path, text)
    chunks = chunk_text(text)

    for i, chunk in enumerate(chunks):
        conn.execute(
            "INSERT INTO docs(path, chunk_idx, title, content) VALUES (?, ?, ?, ?)",
            (rel, i, title, chunk),
        )
    return len(chunks)


def main() -> None:
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True)
        print(f"Created {DATA_DIR} — add .md and .json files there, then re-run.")
        return

    conn = sqlite3.connect(DB_PATH)
    create_db(conn)

    files, chunks = 0, 0
    for dirpath, _, filenames in os.walk(DATA_DIR):
        for fname in filenames:
            if Path(fname).suffix in {".md", ".json"}:
                n = index_file(conn, Path(dirpath) / fname, DATA_DIR)
                if n:
                    files += 1
                    chunks += n

    conn.commit()
    conn.close()
    print(f"Indexed {files} file(s) → {chunks} chunk(s) → {DB_PATH}")


if __name__ == "__main__":
    main()
