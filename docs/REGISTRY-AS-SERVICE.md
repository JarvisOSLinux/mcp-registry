# Registry as a Service — Architectural Vision

This document describes the intended long-term evolution of the mcp-registry
from a static GitHub repository into a live HTTP search service backed by a
vector database.

The static-repo model is the current implementation and is described here so
that design decisions made today stay compatible with the migration path.

---

## Current Architecture (Static Repo)

```
mcp-registry (GitHub repository)
  registry.json          ← server index, embedding_spec
  servers/<id>/
    manifest.json        ← per-server metadata + pre-computed vectors

CI (GitHub Actions)
  On manifest change → runs generate_embeddings.py → commits updated vectors

dmcp (client CLI)
  dmcp sync-index        ← downloads full vector index to local disk
  dmcp browse --vector   ← brute-force cosine search on local file

JARVIS
  on startup             ← embeds intent with Ollama → sends vector to dmcp
  on search              ← receives top-k results from dmcp
```

### Problems with this model

| Problem | Root cause |
|---------|------------|
| Stale search results | Clients cache the index; re-sync is manual or boot-time only |
| Full index download on every sync | All vectors shipped as one JSON blob |
| O(n) brute-force search | No ANN index; degrades as server count grows |
| Client complexity | Each client reimplements caching, versioning, sync logic |
| CI embed latency | New server is unsearchable until CI finishes and client re-syncs |
| Privacy (future concern) | Client embeds query locally, but intent still leaves the machine |

---

## Target Architecture (Registry as a Service)

The registry becomes an HTTP service with a vector database backend. Clients
send a query vector (or raw text) and receive ranked results. No local index,
no sync step.

```
mcp-registry-service
  vector DB              ← stores manifest metadata + embedding vectors
  HTTP API               ← search, publish, fetch manifest

dmcp (client CLI)
  dmcp browse -q "..."   ← POST /search → ranked results, no local cache

JARVIS
  on search              ← embeds intent → POST /search → top-k results
```

### What this solves

| Problem | Solution |
|---------|----------|
| Stale results | Registry is the single source of truth; updates are instant |
| Full index download | Only results are transferred (top-k payloads) |
| O(n) search | ANN index (HNSW or similar) on the service; sub-millisecond at scale |
| Client complexity | Clients become thin HTTP callers; caching is optional |
| CI embed latency | Embed on publish, index immediately — new servers are searchable within seconds |

---

## API Design Sketch

All endpoints accept and return JSON. Authentication is bearer token (or open
for a public registry).

### `POST /search`

Find the most semantically similar servers to a query.

**Request**
```json
{
  "vector": [0.031, -0.184, ...],
  "top_k": 5,
  "min_score": 0.3,
  "filters": {
    "trust_status": ["vetted", "community"]
  }
}
```

Or, if the service embeds server-side:
```json
{
  "query": "read and patch files on local disk",
  "top_k": 5
}
```

**Response**
```json
{
  "results": [
    {
      "server_id": "org.modelcontextprotocol.server-filesystem",
      "server_name": "Filesystem MCP",
      "summary": "Secure local filesystem server",
      "score": 0.91,
      "tools": [
        { "name": "read", "description": "Read text file contents." }
      ]
    }
  ],
  "embedding_model": "nomic-embed-text",
  "latency_ms": 4
}
```

### `GET /servers/:id`

Fetch a single server's full manifest.

### `GET /servers`

List all servers (paginated). Used by `dmcp list` and initial bootstrap.

### `POST /servers` _(authenticated)_

Publish a new server or update an existing one. The service embeds the
canonical text immediately and makes the server searchable without a CI step.

```json
{
  "manifest": { ... }
}
```

### `GET /embedding-spec`

Return the embedding model the service uses. Clients that embed locally use
this to decide whether their vectors are compatible.

```json
{
  "model": "nomic-embed-text",
  "version": "1.5",
  "dimensions": 768,
  "provider": "ollama"
}
```

---

## Embedding Responsibility

Two sub-models are possible. Choose based on how much you trust the service.

### Client embeds, service searches

```
JARVIS → embed(query) locally → POST /search { vector } → results
```

- Query intent never leaves the machine as plain text.
- Client and service must use compatible models (`/embedding-spec` check).
- Client still needs a local embedding model (Ollama).

### Service embeds and searches

```
JARVIS → POST /search { query: "read files" } → results
```

- Clients need no local model; simpler setup.
- Query text is sent to the service (privacy trade-off).
- Model upgrades are transparent to clients.

The API design above supports both: if `vector` is provided the service skips
embedding; if only `query` is provided the service embeds it. This lets clients
migrate at their own pace.

---

## dmcp Changes Required

The `dmcp` CLI would need two changes:

1. **`browse`** — replace local `vector_index.json` search with `POST /search`.
   The `--vector` flag passes a pre-computed vector; a new `--query` flag sends
   raw text for server-side embedding.

2. **`sync-index`** — becomes a no-op (or removed). There is nothing to sync
   because there is no local cache.

The `index-server` and `embedding-spec` subcommands also become no-ops or
are removed.

Backward compatibility: if no service URL is configured, `dmcp browse` falls
back to keyword search against the locally installed `index.json`. This ensures
offline installs continue to work.

---

## JARVIS Changes Required

- `bootstrap_tool_index_nonfatal` — remove the `sync_index()` call entirely.
- `ensure_embedding_model` — still useful if client-side embedding is kept;
  remove if the service embeds.
- `discover_tools` — no change needed; it already calls `dispatch → dmcp` which
  would transparently use the new HTTP path.

---

## Vector Database Options

For a small-to-medium registry (hundreds of servers, thousands of tool entries)
any of these work:

| Option | Notes |
|--------|-------|
| **Qdrant** | Rust-native, easy self-host, full ANN (HNSW), filtering, gRPC+REST |
| **SQLite + sqlite-vss** | Zero infrastructure, file-based, good for single-machine deploys |
| **pgvector** | If you already run Postgres; production-grade |
| **Chroma** | Python-first, simple REST API, good for prototyping |

For the initial service implementation, SQLite + sqlite-vss is recommended:
no external dependencies, trivially deployable, and replaceable later without
changing the HTTP API.

---

## Migration Path

The static-repo model and the service model can coexist during transition.

1. **Phase 1 (now):** Static repo + client-side cache. `dmcp sync-index` on
   startup when index is empty. Manual re-sync after registry updates.

2. **Phase 2:** Service exposes `/search` and `/embedding-spec`. `dmcp browse`
   tries the service first, falls back to local cache if unreachable.

3. **Phase 3:** Local vector cache removed from `dmcp`. `sync-index` deprecated.
   CI embedding step removed from the registry repo.

The HTTP API defined above is stable across all three phases — only the backend
storage changes.

---

## Trade-offs to Accept

| | Static repo | Service |
|---|---|---|
| Infrastructure | Zero (GitHub) | Must host the service |
| Freshness | Stale between syncs | Real-time |
| Offline search | Yes (local cache) | No (or cache last results) |
| Search quality at scale | Degrades (brute-force) | Constant (ANN index) |
| Client complexity | High (sync, cache, versioning) | Low (one HTTP call) |
| Query privacy | Full (local embed + search) | Partial (query sent to service) |

For a personal or small-team deployment the infrastructure cost is low (a
single process, ~50 MB RAM for sqlite-vss at this scale). The freshness and
simplicity gains are worth it once the server count grows past ~50.
