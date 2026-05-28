#!/usr/bin/env python3
"""
FastAPI search server.

Endpoints:
  GET /search?q=<query>[&format=json|llm][&limit=5]                    — local file index
  GET /search/web?q=<query>[&format=json|llm]                          — Google search (scraped, no API key)
  GET /fetch?url=<url>[&q=<query>][&format=json|llm][&limit=5]         — fetch a URL, return chunks
  POST /index   — re-index the data/ directory
  GET /health
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

import indexer as idx
import web_search as ws

DB_PATH = idx.DB_PATH
MAX_TOTAL_CHARS = 4000

app = FastAPI(title="flounder", version="3.0.0")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Index not built yet — POST /index first")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "index_exists": DB_PATH.exists()}


@app.post("/index")
def rebuild_index() -> dict:
    import os
    conn = sqlite3.connect(DB_PATH)
    idx.create_db(conn)
    files, chunks = 0, 0
    for dirpath, _, filenames in os.walk(idx.DATA_DIR):
        for fname in filenames:
            if Path(fname).suffix in {".md", ".json"}:
                n = idx.index_file(conn, Path(dirpath) / fname, idx.DATA_DIR)
                if n:
                    files += 1
                    chunks += n
    conn.commit()
    conn.close()
    return {"files": files, "chunks": chunks}


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    format: str = Query("llm", pattern="^(json|llm)$"),
    limit: int = Query(5, ge=1, le=20),
):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT path, chunk_idx, title, content, rank FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (q, limit),
        ).fetchall()

    kept = _apply_token_guard(rows, "content")
    if format == "llm":
        return _as_llm(q, kept, source_key="path")
    return _as_json_local(q, kept)


@app.get("/search/web")
def search_web(
    q: str = Query(..., min_length=1),
    format: str = Query("llm", pattern="^(json|llm)$"),
):
    try:
        results = ws.google_search(q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if format == "llm":
        return _as_llm_search(q, results)
    return {"query": q, "count": len(results), "results": results}


@app.get("/fetch")
def fetch(
    url: str = Query(..., min_length=1),
    q: str = Query(None),
    format: str = Query("llm", pattern="^(json|llm)$"),
    limit: int = Query(5, ge=1, le=20),
):
    try:
        rows = ws.search_page(url, q, limit=limit) if q else ws.get_page(url, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    kept = _apply_token_guard(rows, "content")
    label = q or url
    if format == "llm":
        return _as_llm(label, kept, source_key="url")
    return _as_json_web(label, kept)


# ── helpers ───────────────────────────────────────────────────────────────────

def _apply_token_guard(rows, content_key: str) -> list:
    kept, total = [], 0
    for r in rows:
        content = r[content_key]
        if total + len(content) > MAX_TOTAL_CHARS and kept:
            break
        kept.append(r)
        total += len(content)
    return kept


def _relevance(i: int) -> float:
    return round(1.0 / (1.0 + i * 0.15), 2)


def _as_llm(query: str, rows, source_key: str) -> PlainTextResponse:
    if not rows:
        body = f'No results for "{query}".'
    else:
        lines = [f"Found {len(rows)} relevant snippet(s) for: {query}\n"]
        for i, r in enumerate(rows):
            rel = _relevance(i)
            src = r[source_key]
            chunk = r["chunk_idx"]
            lines.append(f'<source path="{src}" chunk="{chunk}" relevance="{rel}">')
            lines.append(r["content"].strip())
            lines.append("</source>\n")
        body = "\n".join(lines)
    return PlainTextResponse(content=body, media_type="text/plain")


def _as_json_local(query: str, rows) -> dict:
    return {
        "query": query,
        "count": len(rows),
        "results": [
            {"path": r["path"], "chunk": r["chunk_idx"], "title": r["title"],
             "excerpt": r["content"], "relevance": _relevance(i)}
            for i, r in enumerate(rows)
        ],
    }


def _as_json_web(query: str, rows) -> dict:
    return {
        "query": query,
        "count": len(rows),
        "results": [
            {"url": r["url"], "chunk": r["chunk_idx"], "title": r["title"],
             "excerpt": r["content"], "relevance": _relevance(i)}
            for i, r in enumerate(rows)
        ],
    }


def _as_llm_search(query: str, results: list[dict]) -> PlainTextResponse:
    if not results:
        body = f'No Google results for "{query}".'
    else:
        lines = [f"Google search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f'<result rank="{i}" url="{r["url"]}">')
            lines.append(f'  <title>{r["title"]}</title>')
            if r.get("snippet"):
                lines.append(f'  <snippet>{r["snippet"]}</snippet>')
            lines.append("</result>\n")
        body = "\n".join(lines)
    return PlainTextResponse(content=body, media_type="text/plain")
