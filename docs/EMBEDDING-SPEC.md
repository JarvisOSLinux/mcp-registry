# Embedding Spec — Semantic Server Discovery

This document specifies how pre-computed embedding vectors are stored in the
registry and used by consumers (JARVIS, dmcp) for semantic MCP server discovery.

## Why

Keyword search (`dmcp browse -k filesystem`) works for exact matches but fails
for natural-language queries like *"I need to read and patch local files"*.
Semantic similarity bridges that gap by comparing an intent vector against
pre-computed server vectors.

Pre-computing at registry-publish time has two advantages:

1. **No consumer dependency.** Consumers that have the same model locally get
   free similarity search. Consumers without a local model fall back to keyword
   search — nothing breaks.
2. **Consistent representation.** Every server is embedded from the same
   canonical text using the same model, so cosine similarity comparisons are
   apples-to-apples.

---

## Schema

### `registry.json` — top-level `embedding_spec`

Declares the model used to generate the pre-computed vectors stored in each
manifest. Consumers compare this against their local model to decide whether
the pre-computed vectors are reusable.

```json
{
  "version": "1.0",
  "updated": "2026-05-01T00:00:00Z",
  "embedding_spec": {
    "model": "nomic-embed-text",
    "dimensions": 768,
    "provider": "ollama",
    "canonical_fields": ["name", "summary", "keywords", "tools"]
  },
  "signing": { "keyringUrl": null, "signatures": [] },
  "servers": { ... }
}
```

| Field             | Type     | Description                                                                 |
|-------------------|----------|-----------------------------------------------------------------------------|
| `model`           | string   | Model identifier as passed to the embedding API (e.g. `nomic-embed-text`). |
| `dimensions`      | number   | Vector length. Used to sanity-check stored vectors at load time.            |
| `provider`        | string   | Where the model runs: `"ollama"`, `"openai"`, `"huggingface"`, etc.         |
| `canonical_fields`| string[] | Which manifest fields were combined to produce the embedding text.          |

`embedding_spec` is **omitted** (or `null`) when no pre-computed vectors are
present in the registry. Consumers treat its absence as "keyword-only" mode.

---

### `manifest.json` — per-server `embeddings`

Stores pre-computed vectors keyed by model name. The key matches
`embedding_spec.model` in the parent registry, but the dict structure allows
multiple models to coexist if the registry ever ships vectors for more than one.

```json
{
  "id": "org.modelcontextprotocol.server-filesystem",
  "name": "Filesystem MCP",
  "summary": "Secure local filesystem server: read, write, search, diff, patch, and manage files",
  "keywords": ["filesystem", "files", "read", "write", "diff", "patch"],
  "tools": [
    { "name": "read",  "description": "Read text file contents." },
    { "name": "write", "description": "Write file contents." }
  ],
  "embeddings": {
    "nomic-embed-text": {
      "v": [0.0312, -0.1847, 0.0091, ...],
      "hash": "<sha256 of the canonical text>"
    }
  }
}
```

Each model key maps to `{"v": [floats], "hash": "<sha256>"}` — `hash` is the
SHA-256 of the canonical text, used by `generate_embeddings.py` for
incremental regeneration (unchanged text is skipped).

Vector length should equal `embedding_spec.dimensions`. Consumers do not
hard-validate this at load time; dmcp infers dimensions from the fetched
vectors.

**Registry inline embeddings (primary consumer path):** in addition to the
per-manifest form above, `generate_embeddings.py` writes each `registry.json`
entry an `embeddings` object `{model, version, server: [vector], tools:
{tool-name → vector}}`. This inline form is what `dmcp sync-index` actually
fetches; the per-manifest form is the authoring/source-of-truth copy.

---

## Canonical Text Construction

The text fed to the embedding model is built deterministically from the
manifest. Order matters — the same recipe must be used every time, or vectors
cannot be compared.

```
<name>. <summary>. Keywords: <kw1>, <kw2>, .... Tools: <tool1_name>: <tool1_desc>; <tool2_name>: <tool2_desc>; ...
```

Example for the Filesystem server:

```
Filesystem MCP. Secure local filesystem server: read, write, search, diff, patch, and manage files. Keywords: filesystem, files, read, write, diff, patch, search, grep. Tools: roots: List allowed workspace roots.; ls: List directory contents.; find: Find files by glob pattern.; ...
```

Rules:
- All fields are stripped of leading/trailing whitespace.
- Missing or empty fields are omitted (no placeholder text).
- Tool list is truncated to the first 20 entries to keep text length bounded.
- `categories` are intentionally excluded — they are structural metadata, not
  descriptive prose, and skew similarity toward registry taxonomy rather than
  user intent.

---

## How JARVIS Uses Embeddings

JARVIS embeds natural-language task descriptions and finds the most semantically
relevant installed servers using cosine similarity.

```
user intent → embed locally → cosine_sim(intent_vec, server_vecs) → top-k servers
```

### Model matching

JARVIS reads `EMBED_MODEL` from the environment (default `nomic-embed-text`,
768 dimensions, via Ollama). At startup it checks the registry's
`embedding_spec.model`:

| Condition                                         | Action                                                        |
|---------------------------------------------------|---------------------------------------------------------------|
| `EMBED_MODEL` == `embedding_spec.model`           | Use pre-computed vectors from manifests directly.             |
| `EMBED_MODEL` != `embedding_spec.model` (or spec missing) | JARVIS adopts the registry's model: it sets its local Ollama embedding model to `embedding_spec.model` and pulls it if absent, so its query vectors match the registry's pre-computed vectors. |

Results are never written back to the manifest files — the registry is the
canonical source of pre-computed vectors.

### Fallback chain

```
1. Pre-computed registry vectors via dmcp sync-index   ← preferred
2. Local indexing of non-registry servers after install (dmcp index-server —
   JARVIS embeds server-id + tool docs)
3. Keyword substring match on name/summary/keywords    ← always available
```

---

## How dmcp Uses Embeddings

**Implemented.** `dmcp sync-index` downloads per-server embeddings from
`registry.json` into a local vector index; `dmcp browse --vector '<json
array>'` (or `--vectors` for a batch) ranks by cosine similarity with
`--top-k` (default 5) and `--min-score`. `dmcp embedding-spec` reports the
model the index expects, `dmcp server-count`/`dmcp index-server` support the
consumer flow. The keyword path (`-k`) remains available.

Note: dmcp's `Manifest` struct intentionally has no embeddings field — vectors
flow from `registry.json` inline embeddings into a dedicated local vector
index (`src/vector_index.rs`, populated by `sync_index.rs`).

---

## Generating / Regenerating Embeddings

Pre-computed vectors are generated offline (not at install time) and committed
along with the manifest. Use the script below when:

- A new server is added to the registry.
- A server's `name`, `summary`, `keywords`, or `tools` change.
- The embedding model version changes (requires a full re-run for all servers).

### Generation script

Use `scripts/generate_embeddings.py`:

```
python3 scripts/generate_embeddings.py [--model <name>] [--force]
```

It is incremental — servers whose canonical-text SHA-256 matches the stored
`embeddings[model].hash` are skipped (`--force` re-embeds everything). It
writes both the per-manifest `{"v": ..., "hash": ...}` form and the inline
`registry.json` `embeddings` objects (server + per-tool vectors), and updates
`embedding_spec`.

## Full Example

### `registry.json` (excerpt)

```json
{
  "version": "1.0",
  "updated": "2026-05-08T00:00:00Z",
  "embedding_spec": {
    "model": "nomic-embed-text",
    "dimensions": 768,
    "provider": "ollama",
    "canonical_fields": ["name", "summary", "keywords", "tools"]
  },
  "signing": { "keyringUrl": null, "signatures": [] },
  "servers": {
    "org.modelcontextprotocol.server-filesystem": {
      "id": "org.modelcontextprotocol.server-filesystem",
      "name": "Filesystem MCP",
      "summary": "Secure local filesystem server: read, write, search, diff, patch, and manage files",
      "trustStatus": "community",
      "manifest": "https://raw.githubusercontent.com/.../manifest.json"
    }
  }
}
```

### `servers/org.modelcontextprotocol.server-filesystem/manifest.json` (excerpt)

```json
{
  "id": "org.modelcontextprotocol.server-filesystem",
  "name": "Filesystem MCP",
  "summary": "Secure local filesystem server: read, write, search, diff, patch, and manage files",
  "keywords": ["filesystem", "files", "read", "write", "diff", "patch", "search", "grep"],
  "tools": [
    { "name": "read",  "description": "Read text file contents (supports head/tail/line ranges)." },
    { "name": "write", "description": "Write file contents (overwrites existing content)." },
    { "name": "edit",  "description": "Apply literal string replacements." }
  ],
  "embeddings": {
    "nomic-embed-text": [0.0312, -0.1847, 0.0091, "... 765 more floats ..."]
  }
}
```

---

## Notes and Tradeoffs

### Manifest size

768 floats × 4 bytes ≈ 3 KB per server in raw form; ~5 KB as JSON text.
For a registry with 50 servers that is ~250 KB of extra JSON — significant
but not prohibitive for a periodic fetch with local caching.

If size becomes a concern: store embeddings in a single `embeddings.json` at
the registry root (keyed by server ID) rather than inline per manifest. That
file can be fetched separately and cached independently of the manifest files.
The `embedding_spec` field in `registry.json` serves as the declaration
regardless of storage strategy.

### Model drift

When the model is upgraded (e.g. `nomic-embed-text` v1.5 → v2), all
pre-computed vectors must be regenerated — partial updates produce inconsistent
comparisons. Bump `embedding_spec.model` to the new model identifier and run
a full regeneration pass.

### dmcp models.rs

The current `Manifest` struct in dmcp (see `src/models.rs`) does not yet have
an `embeddings` field — unknown fields are silently ignored by serde. The spec
is defined here first; the dmcp struct will gain `embeddings` when the semantic
browse feature is implemented.

## Changelog — corrected claims

*2026-07-22:* semantic search marked implemented (`dmcp sync-index` + `browse --vector`); per-manifest embeddings shape corrected to `{model: {v, hash}}`; registry inline `embeddings` (server + per-tool vectors) documented as the consumer path; model-mismatch behavior corrected (JARVIS adopts the registry's model); fallback chain corrected; inline script replaced by the real incremental `scripts/generate_embeddings.py`; `vetted` example fixed; load-time length validation claim removed.
