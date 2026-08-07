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
  selftest_embedding_drift.py  Temp-dir self-test for the embedding-drift checks
  selftest_needs_input.py  Self-test of the shell servers' needs_input report
  selftest_jobs.py         End-to-end self-test of the jarvis-shell job model
  selftest_threat_level.py Temp-dir self-test for the tool threat_level check
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
- `tools` — list of tools the server exposes; a tool that can park awaiting input
  declares `blocking: true` plus an optional `suggestedRemindAfter` (seconds),
  the reminder interval an orchestrator applies when the caller set none

### servers/jarvis-shell{,-system}/server.py — the interactive-command pattern

Reference implementation of the two shapes a server uses when it wraps a command
that can prompt. Both are documented for third-party authors in
`MCP-REGISTRY-GUIDE.md` ("Interactive Tools: Closed stdin", "The Job Pattern",
"stdin, stdout, and the Process Lifecycle"); this is where they actually live.

- **Closed stdin + `needs_input`** — `execute_command` spawns with
  `stdin=DEVNULL`, because a stdio server's own stdin IS the JSON-RPC channel. An
  interactive command hits EOF and aborts legibly instead of hanging; the tool
  then inspects the output tail and attaches a purely additive
  `needs_input` (prompted / prompt / hint) when the last line is still a prompt.
  A report, never a dialogue. Gated by `scripts/selftest_needs_input.py`.
- **The job model** — `run_job` / `send_input` / `read_output` / `kill_job`. Every
  tool call is a fresh server process (one-shot dmcp lifecycle), so in-process
  state cannot survive to the call that answers a prompt: the job's handle lives
  on the **filesystem** (`$XDG_RUNTIME_DIR/jarvis-shell/<job>/`, uid-checked,
  `/tmp` fallback) behind a detached PTY holder. `run_job` blocks and streams to
  stderr (stdout is the wire), and carries `blocking: true` +
  `suggestedRemindAfter: 30`. Output is sterilized at **read** time — `out.log`
  keeps every raw byte — and the transcript is capped at its tail. Gated by
  `scripts/selftest_jobs.py`, which also enforces that both servers' shared
  blocks stay byte-identical.

## Automation

### CI Workflows

- `sync-registry.yml` — Triggers on manifest/setup-script changes on `main`;
  recomputes hashes; opens PRs with updated registry.json
- `generate-embeddings.yml` — Manual dispatch; generates embeddings via Ollama;
  only re-embeds servers with changed canonical text
- `validate-pr.yml` — Blocking PR gate; runs `scripts/selftest_platform_format.py`,
  `scripts/selftest_embedding_drift.py`, `scripts/selftest_threat_level.py` and
  `scripts/selftest_jobs.py`, then
  `scripts/validate_registry.py` (schema,
  id/scope/trustStatus/platforms
  enums incl. per-transport, transport order, integrity hashes for both setup
  scripts, setup-script locations, orphan directories, and a `threat_level` —
  `safe`/`elevated`/`dangerous`/`forbidden`, or the legacy
  `confirmation_required: true` — on every tool of a live entry) and blocks
  `trustStatus` promotion to `official` without the maintainer `trust-approved`
  label.
  Embedding drift is checked on every entry but reported as a **warning**:
  vectors need Ollama, which only the manual `generate-embeddings.yml` has, so
  failing would block a manifest edit until vectors were regenerated for text
  that has not merged. `--strict-embeddings` promotes them to errors
- `remove-server.yml` — Manual dispatch (`server_id` + optional `force`);
  runs `scripts/remove_server.py` and opens a **non-auto-merged** removal PR
  for a maintainer to review

### Scripts

```bash
python scripts/sync_registry.py         # Update/prune integrity hashes + sync name/summary/keywords/platforms (--check for CI)
python scripts/generate_embeddings.py   # Generate embeddings (requires Ollama; incremental via canonical-text hashes)
python scripts/validate_registry.py     # Validate schema, hashes, trust tiers, orphan dirs, per-tool threat_level, embedding drift (PR gate)
python scripts/validate_registry.py --strict-embeddings  # ...failing on stale/missing embeddings instead of warning
python scripts/remove_server.py <id>    # Hard-excise a server (--force for live entries, --check for dry run)
python scripts/selftest_platform_format.py  # Offline self-test: per-transport platforms + setup.ps1 hashing
python scripts/selftest_embedding_drift.py  # Offline self-test: stale/missing embedding detection + severity
python scripts/selftest_needs_input.py  # Offline self-test: unanswered-prompt detection in the shell servers
python scripts/selftest_jobs.py         # Offline self-test: jarvis-shell interactive job model (PTY jobs, real JSON-RPC)
python scripts/selftest_threat_level.py # Offline self-test: per-tool threat_level enforcement (missing/invalid/exempt)
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

If the server wraps commands that can prompt, both shapes are available patterns
rather than things to reinvent: close the child's stdin and report the prompt
(`needs_input`), and where the prompt must actually be *answered*, use the job
model — a detached PTY holder with the job handle on the filesystem, since a
stdio server gets a fresh process per tool call. The blocking tool then declares
`blocking: true` + `suggestedRemindAfter`. `servers/jarvis-shell/server.py` is
the reference implementation; `MCP-REGISTRY-GUIDE.md` documents both for
third-party authors.

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
