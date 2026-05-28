# Flounder

A local search server that gives AI agents free web search and page reading without any API keys. Runs on `localhost:8000` and is used as a tool backend by [Trout](https://github.com/Pkill-MyDaemons/trout).

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)

## What it does

- **Google search** — constructs a Google search URL from a keyword, scrapes the results page, and returns titles, URLs, and snippets. No API key. Results cached for 30 minutes.
- **Page fetching** — given any URL, fetches it like a browser, strips navigation/scripts/ads, extracts clean text, and returns the most relevant chunks. Cached for 6 hours.
- **Local file index** — indexes your own Markdown and JSON files via SQLite FTS5 full-text search.
- **LLM-optimized output** — all responses are XML-wrapped `<source>` or `<result>` blocks with relevance scores, ready to inject directly into a prompt.

## Quick start

```bash
git clone https://github.com/Pkill-MyDaemons/flounder
cd flounder
./start.sh
```

`start.sh` creates a virtualenv, installs dependencies, re-indexes `data/`, and starts the server on `http://localhost:8000`.

Requires Python 3.11+.

## Endpoints

### `GET /search/web?q=<query>`
Search Google and return organic results.

```
GET /search/web?q=golang+http+server&format=llm
```

Returns XML-formatted result blocks:
```xml
Google search results for: golang http server

<result rank="1" url="https://pkg.go.dev/net/http">
  <title>net/http - Go Packages</title>
  <snippet>Package http provides HTTP client and server implementations...</snippet>
</result>
```

### `GET /fetch?url=<url>[&q=<query>]`
Fetch a URL and return its text content. If `q` is provided, returns only the chunks most relevant to the query (FTS5 ranked). Otherwise returns the first few chunks.

```
GET /fetch?url=https://pkg.go.dev/net/http&q=ListenAndServe&format=llm
```

Returns XML-wrapped source chunks:
```xml
Found 3 relevant snippet(s) for: ListenAndServe

<source path="https://pkg.go.dev/net/http" chunk="2" relevance="1.0">
func ListenAndServe(addr string, handler Handler) error
ListenAndServe listens on the TCP network address addr and then calls
Serve with handler to handle requests on incoming connections.
</source>
```

### `GET /search?q=<query>`
Full-text search over local files indexed from the `data/` directory.

### `POST /index`
Re-index all `.md` and `.json` files in `data/`. Returns `{"files": N, "chunks": M}`.

### `GET /health`
Returns `{"status": "ok", "index_exists": true/false}`.

## Query parameters

All search endpoints accept:

| Parameter | Default | Options |
|-----------|---------|---------|
| `format` | `llm` | `llm` (XML blocks), `json` |
| `limit` | `5` | 1–20 |

## Local file indexing

Drop `.md` or `.json` files into the `data/` directory, then hit `POST /index` or restart the server. Files are:

1. **Cleaned** — YAML frontmatter, HTML comments, and base64 image strings are stripped
2. **Chunked** — split into ~800 character chunks with 100 character overlap, breaking on newlines
3. **Indexed** — stored in SQLite FTS5 with porter stemmer tokenization

## Token guard

All endpoints cap combined response content at 4,000 characters so context windows don't overflow. Chunks are dropped from the bottom of the ranked list once the ceiling is reached.

## Caching

| Cache | TTL | Storage |
|-------|-----|---------|
| Google search results | 30 minutes | `web_cache.db` |
| Fetched page content | 6 hours | `web_cache.db` |
| Local file index | Persistent (manual re-index) | `search.db` |

## Project structure

```
flounder/
├── main.py          # FastAPI server and endpoints
├── indexer.py       # Local file indexer (clean, chunk, FTS5)
├── web_search.py    # Google scraper and page fetcher
├── requirements.txt
├── start.sh         # Setup and launch script
└── data/            # Put your .md and .json files here
```

## Dependencies

- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) — HTTP server
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) + lxml — HTML parsing
- SQLite FTS5 — full-text search (stdlib, no extra install)
