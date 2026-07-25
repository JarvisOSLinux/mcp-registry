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
    setup.ps1              Optional PowerShell setup script for Windows hosts
scripts/
  sync_registry.py         Recompute integrity hashes (SHA-256)
  generate_embeddings.py   Generate embedding vectors via Ollama
  validate_registry.py     PR-gate validation (schema, hashes, trust, orphans)
  remove_server.py         Hard-excise a server (entry + dir + embeddings)
  selftest_platform_format.py  Temp-dir self-test for the platform-format checks
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
- `platforms` (mirrored from the manifest; the OSes the registry vouches for — dmcp filters by host from this index alone)
- `trustStatus` (`community` / `official`; `deprecated` / `removed` for revocation — see `docs/TRUST-MODEL.md`)
- `integrity` (manifestSha256, setupScriptSha256, setupScriptWindowsSha256)
- `manifest` URL pointing to the server's manifest.json
- `embeddings` (model, version, `server` vector [768d], `tools` per-tool vector map)

### manifest.json (per server)

- `platforms` — OSes vetted on (`linux` / `darwin` / `windows`); required in this registry, absent = unrestricted
- `transports` — how to run: stdio (command + args), SSE (URL), or WebSocket (URL); each entry may carry its own `platforms` (same enum, absent = every host, first match wins, so order most-specific first)
- `setupScriptWindows` — PowerShell script (`setup.ps1`) run instead of `setupScript` on Windows hosts; like `setupScript`, it must name a committed script in the server directory, never an off-registry URL
- `source` — git repo to clone for local servers (optional `rev` pin — a full 40-char SHA is binding)
- `configurableProperties` — user-configurable fields (API keys, endpoints); each has key/label/description/sensitive/required/default (see `docs/manifest-reference.md`)
- `tools` — list of tools the server exposes

## Automation

### CI Workflows

- `sync-registry.yml` — Triggers on manifest/setup-script changes on `main`;
  recomputes hashes; opens PRs with updated registry.json
- `generate-embeddings.yml` — Manual dispatch; generates embeddings via Ollama;
  only re-embeds servers with changed canonical text
- `validate-pr.yml` — Blocking PR gate; runs `scripts/selftest_platform_format.py`
  then `scripts/validate_registry.py` (schema, id/scope/trustStatus/platforms
  enums incl. per-transport, transport order, integrity hashes for both setup
  scripts, setup-script locations, orphan directories) and blocks `trustStatus`
  promotion to `official` without the maintainer `trust-approved` label
- `remove-server.yml` — Manual dispatch (`server_id` + optional `force`);
  runs `scripts/remove_server.py` and opens a **non-auto-merged** removal PR
  for a maintainer to review

### Scripts

```bash
python scripts/sync_registry.py         # Update/prune integrity hashes + sync name/summary/keywords/platforms (--check for CI)
python scripts/generate_embeddings.py   # Generate embeddings (requires Ollama; incremental via canonical-text hashes)
python scripts/validate_registry.py     # Validate schema, hashes, trust tiers, orphan dirs (PR gate)
python scripts/remove_server.py <id>    # Hard-excise a server (--force for live entries, --check for dry run)
python scripts/selftest_platform_format.py  # Offline self-test: per-transport platforms + setup.ps1 hashing
```

## Adding a Server

1. Create `servers/<id>/manifest.json` (one entry per capability — per-OS launch
   differences go in per-transport `platforms`, never in a second server)
2. Optionally add `servers/<id>/setup.sh` (plus `setup.ps1` if vetted on Windows)
3. Add an entry for the server to `registry.json` (id as the map key, plus
   id/name/summary/version/scope/trustStatus and the raw-GitHub manifest URL)
4. Run `python scripts/sync_registry.py` (fills integrity hashes, syncs
   name/summary/keywords/platforms from the manifest)
5. Submit PR — `validate-pr.yml` gates it

See `MCP-REGISTRY-GUIDE.md` for format details.

## Removing a Server

Two stages, safest first:

1. **Soft revoke** (recommended first): set the entry's `trustStatus` to
   `removed` in `registry.json`. dmcp then refuses new installs while existing
   installs keep working, and the entry/provenance stay intact.
2. **Hard excise** (destructive): after a grace period, run the removal —
   locally with `python scripts/remove_server.py <id>`, or via the
   **Remove Server** workflow (Actions → Run workflow → `server_id`). It drops
   the registry entry (including inline embeddings + integrity hashes), deletes
   the `servers/<dir>/` directory, and opens a review PR. `remove_server.py`
   refuses a still-live entry (`trustStatus` other than `removed`/`deprecated`)
   unless `--force` is passed. `validate-pr.yml` blocks any half-done removal
   (dangling entry or orphan directory).

## Conventions

- Server IDs use reverse-domain notation: `com.github.<user>.mcp.<name>`
- Integrity hashes are always recomputed via CI, never manually edited
- Embeddings use `nomic-embed-text` model via Ollama
