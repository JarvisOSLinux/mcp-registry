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
    "nomic-embed-text": [0.0312, -0.1847, 0.0091, ...]
  }
}
```

The array length must equal `embedding_spec.dimensions`. Consumers validate
this at load time and skip malformed entries.

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
| `EMBED_MODEL` != `embedding_spec.model` (or null) | Re-embed canonical text locally using `EMBED_MODEL`.          |
| `embedding_spec` missing                          | Re-embed canonical text locally.                              |

Re-embedding happens once at startup and is cached in memory for the session.
Results are never written back to the manifest files — the registry is the
canonical source of pre-computed vectors.

### Fallback chain

```
1. Pre-computed vectors (fast, no model call)          ← preferred
2. Local re-embedding of canonical text (one-time cost)
3. Keyword substring match on name/summary/keywords    ← always available
```

Fallback 3 is always tried for any server that fails steps 1 or 2 (e.g. the
manifest is missing the `embeddings` key because it was added before this spec
was adopted).

---

## How dmcp Uses Embeddings

`dmcp browse` currently filters by keyword (`-k`). When `embedding_spec` is
present in a fetched registry and the local model matches, dmcp can optionally
rank results by cosine similarity instead of substring match.

This is **not yet implemented** — the spec is defined here so the schema is
stable before implementation begins. The existing keyword path is unchanged.

Future `dmcp browse` flags (planned):

```
--semantic <query>   rank by cosine similarity (requires local Ollama)
--model <name>       override the local model used for comparison
```

---

## Generating / Regenerating Embeddings

Pre-computed vectors are generated offline (not at install time) and committed
along with the manifest. Use the script below when:

- A new server is added to the registry.
- A server's `name`, `summary`, `keywords`, or `tools` change.
- The embedding model version changes (requires a full re-run for all servers).

### Quick script (Python, Ollama)

```python
#!/usr/bin/env python3
"""generate_embeddings.py — regenerate embeddings for all registry manifests."""
import json, pathlib, urllib.request

MODEL = "nomic-embed-text"
OLLAMA_URL = "http://localhost:11434/api/embeddings"
SERVERS_DIR = pathlib.Path("servers")

def canonical_text(m: dict) -> str:
    parts = []
    if m.get("name"):
        parts.append(m["name"] + ".")
    if m.get("summary"):
        parts.append(m["summary"] + ".")
    if m.get("keywords"):
        parts.append("Keywords: " + ", " .join(m["keywords"]) + ".")
    tools = m.get("tools", [])[:20]
    if tools:
        tool_strs = []
        for t in tools:
            name = t["name"] if isinstance(t, dict) else str(t)
            desc = t.get("description", "") if isinstance(t, dict) else ""
            tool_strs.append(f"{name}: {desc}" if desc else name)
        parts.append("Tools: " + "; ".join(tool_strs) + ".")
    return " ".join(parts)

def embed(text: str) -> list[float]:
    body = json.dumps({"model": MODEL, "prompt": text}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["embedding"]

for manifest_path in sorted(SERVERS_DIR.glob("*/manifest.json")):
    manifest = json.loads(manifest_path.read_text())
    text = canonical_text(manifest)
    print(f"Embedding {manifest_path.parent.name} ...")
    vector = embed(text)
    manifest.setdefault("embeddings", {})[MODEL] = vector
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"  done ({len(vector)}d)")

print("All done. Commit the updated manifests.")
```

Run it:

```bash
# Ensure Ollama is running with the target model pulled
ollama pull nomic-embed-text
python3 scripts/generate_embeddings.py
git add servers/*/manifest.json
git commit -m "chore: regenerate embeddings (nomic-embed-text)"
```

### GitHub Actions (automated)

See [REGISTRY-AUTOMATION.md](REGISTRY-AUTOMATION.md) section 3 for the
workflow_dispatch approach. The same script above can be invoked there, with
the Ollama server started as a service step.

Recommendation: regenerate via PR rather than pushing directly to `main`, so
the large diff (many floats) can be reviewed and the hash in `integrity` blocks
can be updated in the same commit.

---

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
      "trustStatus": "vetted",
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
