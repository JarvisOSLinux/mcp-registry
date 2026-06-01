# CLAUDE.md — mcp-registry

## What This Is

The server catalog for the JARVIS MCP ecosystem. Contains metadata, manifests,
and embedding vectors for MCP servers that can be discovered and installed via
dmcp.

## Role in the JARVIS Ecosystem

mcp-registry is the "app store." dmcp fetches registry.json from configured
registry URLs, uses it to browse and install servers, and uses the embedded
vectors for semantic search when the LLM needs to discover tools by capability.

## Structure

```
registry.json              Main index (server metadata + embeddings)
servers/                   Per-server directories
  <server-id>/
    manifest.json          Server metadata, transports, config, tools
    setup.sh               Optional dependency installation script
scripts/
  sync_registry.py         Recompute integrity hashes (SHA-256)
  generate_embeddings.py   Generate embedding vectors via Ollama
docs/
  EMBEDDING-SPEC.md        Embedding format spec
  REGISTRY-AUTOMATION.md   CI/CD automation strategy
  REGISTRY-AS-SERVICE.md   Service hosting patterns
MCP-REGISTRY-GUIDE.md      Full registry and manifest specification
```

## Key Files

### registry.json

Main index. Each server entry contains:
- `id`, `name`, `summary`, `version`, `scope`
- `keywords`, `categories`
- `trustStatus` (unreviewed / community / verified)
- `integrity` (manifestSha256, setupScriptSha256)
- `manifest` URL pointing to the server's manifest.json
- `embeddings` (model, version, vector array)

### manifest.json (per server)

- `transports` — how to run: stdio (command + args), SSE (URL), or WebSocket (URL)
- `source` — git repo to clone for local servers
- `config` — configurable properties (API keys, endpoints)
- `tools` — list of tools the server exposes

## Automation

### CI Workflows

- `sync-registry.yml` — Triggers on manifest/setup.sh changes; recomputes
  hashes; opens PRs with updated registry.json
- `generate-embeddings.yml` — Manual dispatch; generates embeddings via Ollama;
  only re-embeds servers with changed canonical text

### Scripts

```bash
python scripts/sync_registry.py        # Update integrity hashes
python scripts/generate_embeddings.py  # Generate embeddings (requires Ollama)
```

## Adding a Server

1. Create `servers/<id>/manifest.json`
2. Optionally add `servers/<id>/setup.sh`
3. Run `python scripts/sync_registry.py`
4. Submit PR

See `MCP-REGISTRY-GUIDE.md` for format details.

## Conventions

- Server IDs use reverse-domain notation: `com.github.<user>.mcp.<name>`
- Integrity hashes are always recomputed via CI, never manually edited
- Embeddings use `nomic-embed-text` model via Ollama
