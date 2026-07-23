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
servers/                   Per-server directories (dir name from the entry's
  <dir>/                   manifest URL — first-party dirs use short names,
    manifest.json          third-party dirs the full reverse-domain id)
    setup.sh               Optional dependency installation script
scripts/
  sync_registry.py         Recompute integrity hashes (SHA-256)
  generate_embeddings.py   Generate embedding vectors via Ollama
docs/
  EMBEDDING-SPEC.md        Embedding format spec
  REGISTRY-AUTOMATION.md   CI/CD automation strategy
  REGISTRY-AS-SERVICE.md   Service hosting patterns
  TRUST-MODEL.md           Trust tiers and promotion/revocation rules
  manifest-reference.md    Full manifest.json field reference
  trust-levels.md          Trust level definitions
MCP-REGISTRY-GUIDE.md      Full registry and manifest specification
```

## Key Files

### registry.json

Main index. Each server entry contains:
- `id`, `name`, `summary`, `version`, `scope`
- `keywords`, `categories`
- `trustStatus` (`community` / `official`; `deprecated` / `removed` for revocation — see `docs/TRUST-MODEL.md`)
- `integrity` (manifestSha256, setupScriptSha256)
- `manifest` URL pointing to the server's manifest.json
- `embeddings` (model, version, `server` vector [768d], `tools` per-tool vector map)

### manifest.json (per server)

- `transports` — how to run: stdio (command + args), SSE (URL), or WebSocket (URL)
- `source` — git repo to clone for local servers (optional `rev` pin — a full 40-char SHA is binding)
- `configurableProperties` — user-configurable fields (API keys, endpoints); each has key/label/description/sensitive/required/default (see `docs/manifest-reference.md`)
- `tools` — list of tools the server exposes

## Automation

### CI Workflows

- `sync-registry.yml` — Triggers on manifest/setup.sh changes on `main`;
  recomputes hashes; opens PRs with updated registry.json
- `generate-embeddings.yml` — Manual dispatch; generates embeddings via Ollama;
  only re-embeds servers with changed canonical text
- `validate-pr.yml` — Blocking PR gate; runs `scripts/validate_registry.py`
  (schema, id/scope/trustStatus enums, integrity hashes) and blocks
  `trustStatus` promotion to `official` without the maintainer
  `trust-approved` label

### Scripts

```bash
python scripts/sync_registry.py        # Update integrity hashes + sync name/summary/keywords (--check for CI)
python scripts/generate_embeddings.py  # Generate embeddings (requires Ollama; incremental via canonical-text hashes)
python scripts/validate_registry.py    # Validate schema, hashes, trust tiers (PR gate)
```

## Adding a Server

1. Create `servers/<id>/manifest.json`
2. Optionally add `servers/<id>/setup.sh`
3. Add an entry for the server to `registry.json` (id as the map key, plus
   id/name/summary/version/scope/trustStatus and the raw-GitHub manifest URL)
4. Run `python scripts/sync_registry.py` (fills integrity hashes, syncs
   name/summary/keywords from the manifest)
5. Submit PR — `validate-pr.yml` gates it

See `MCP-REGISTRY-GUIDE.md` for format details.

## Conventions

- Server IDs use reverse-domain notation: `com.github.<user>.mcp.<name>`
- Integrity hashes are always recomputed via CI, never manually edited
- Embeddings use `nomic-embed-text` model via Ollama
