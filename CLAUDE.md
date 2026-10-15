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
providers/                 Sign-in providers (<id>.json: OAuth endpoints, public
                           client id, scope catalogue), mirrored into
                           registry.json `providers` by sync_registry.py
servers/_lib/              Shared sign-in helper (host pinning, device flow, token
                           store, stdio proxy, detached browser opener, hosted-server browser sign-in
                           and stdio-to-HTTP proxy). Canonical source only: dmcp installs
                           just a server's own directory, so each wrapper carries a
                           byte-identical copy at servers/<wrapper>/_lib/
                           (scripts/sync_lib.py; the PR gate fails on drift)
servers/                   Per-server directories (dir name from the entry's
  <dir>/                   manifest URL — first-party dirs use short names,
    manifest.json          third-party dirs the full reverse-domain id)
    setup.sh               Optional dependency installation script
    setup.ps1              Optional PowerShell setup script for Windows hosts
scripts/
  sync_registry.py         Recompute integrity hashes (SHA-256)
  sync_lib.py              Refresh the vendored servers/<wrapper>/_lib/ copies (--check for CI)
  generate_embeddings.py   Generate embedding vectors via Ollama
  validate_registry.py     PR-gate validation (schema, hashes, trust, orphans)
  remove_server.py         Hard-excise a server (entry + dir + embeddings)
  selftest_platform_format.py  Temp-dir self-test for the platform-format checks
  selftest_embedding_drift.py  Temp-dir self-test for the embedding-drift checks
  selftest_trust_gate.py   Temp-dir self-test for the trust-boundary checks
                           (official-tier source pin, manifest hosting,
                           lifting a revocation, manifest-change review signal)
  selftest_needs_input.py  Self-test of the shell servers' needs_input report
  selftest_jobs.py         End-to-end self-test of the jarvis-shell job model
  selftest_threat_level.py Temp-dir self-test for the tool threat_level check
  selftest_categories.py   Temp-dir self-test for the category vocabulary and
                           fixture flag checks
  selftest_system_status.py  Synthetic-/proc and /sys self-test of the
                           system-status server's parsing (btrfs, batteries,
                           /proc/pid/stat, sensors, health thresholds)
  selftest_credentials.py  Temp-dir self-test for the provider, credential and
                           login checks and the provider-change approval gate
  selftest_signin_lib.py   Loopback self-test for the shared sign-in helper and the
                           GitHub wrapper (host pinning, device flow, token store,
                           stdio proxy, vendored-copy drift)
  selftest_microsoft_365.py  Self-test for the Microsoft 365 wrapper against stand-ins
                           (auth-cache protocol, blocking login, withheld tools,
                           browser opener off the MCP wire); honours JOBS_SELFTEST_TREE
  selftest_notion.py       Self-test for the Notion wrapper against a fake hosted server
                           (spec sign-in, forged callbacks, pinning, SSE, renewal, 401
                           retry); honours JOBS_SELFTEST_TREE
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
- `keywords`, `categories` (closed, validator-enforced vocabulary of capability terms; no `mcp`/`mcp-*` — see MCP-REGISTRY-GUIDE.md)
- `fixture` (optional bool; a test fixture, dropped from consumer semantic search by dmcp but still installable and still indexed)
- `platforms` (mirrored from the manifest; the OSes the registry vouches for — dmcp filters by host from this index alone)
- `trustStatus` (`community` / `official`; `deprecated` / `removed` for revocation — see `docs/TRUST-MODEL.md`)
- `integrity` (manifestSha256, setupScriptSha256, setupScriptWindowsSha256)
- `manifest` URL pointing to the server's manifest.json
- `embeddings` (model, version, `server` vector [768d], `tools` per-tool vector map)

Top level, beside `servers`: `providers` — a verbatim mirror of `providers/*.json`,
so dmcp learns how to sign a user in from the same fetch as the catalogue. Any
diff to it needs the `trust-approved` label.

### manifest.json (per server)

- `platforms` — OSes vetted on (`linux` / `darwin` / `windows`); required in this registry, absent = unrestricted
- `transports` — how to run: stdio (command + args), SSE (URL), or WebSocket (URL); each entry may carry its own `platforms` (same enum, absent = every host, first match wins, so order most-specific first)
- `setupScriptWindows` — PowerShell script (`setup.ps1`) run instead of `setupScript` on Windows hosts; like `setupScript`, it must name a committed script in the server directory, never an off-registry URL
- `source` — git repo to clone for local servers (optional `rev` pin — a full 40-char SHA is binding)
- `configurableProperties` — user-configurable fields (API keys, endpoints); each has key/label/description/sensitive/required/default (see `docs/manifest-reference.md`)
- `credentials` — signed-in accounts the server needs: `provider` (a `providers/` id), `scopes` (from that provider's catalogue), `inject` (property key → `access_token`/`refresh_token`/`client_id`/`account`; tokens only into `sensitive` properties). User scope + a stdio transport only. dmcp delivers them at spawn to servers the user granted the account to (Project-JARVIS#229)
- `login` — `{"tool": "<name>"}`, a server's own sign-in tool; must be one of its `tools`
- `tools` — list of tools the server exposes; a tool that can park awaiting input
  declares `blocking: true` plus an optional `suggestedRemindAfter` (seconds),
  the reminder interval an orchestrator applies when the caller set none

### servers/github/ and servers/_lib/ — the sign-in wrapper pattern

Design v3 of Project-JARVIS#229: a server owns its sign-in. Where an upstream
server has none, a per-server wrapper adds it. `servers/github/server.py` is the
reference: it starts the upstream GitHub server (fetched at a pinned commit by
`setup.sh`) through `_lib.proxy`, adds `login` / `status` / `logout`, and runs
the upstream with the stored token. `login` is one blocking call
(`blocking: true`): it prints the link and device code to stderr (dmcp relays it
live), opens the browser, and polls. The token is stored in the keyring
(`secret-tool`) or an owner-only file, and is never returned in a tool result.
A token set by hand in `GITHUB_PERSONAL_ACCESS_TOKEN` still wins.

Every link and endpoint goes through `_lib.hostpin`: a wrapper refuses hosts it
does not name, including ones the provider advertises or redirects to. The
GitHub wrapper signs in through the JarvisOS OAuth App, whose public client id
is `DEFAULT_CLIENT_ID`; `GITHUB_OAUTH_CLIENT_ID` overrides it.

Edit `servers/_lib/`, then run `python scripts/sync_lib.py`. Gated by
`scripts/selftest_signin_lib.py`.

### servers/microsoft-365/ — reusing an upstream's own sign-in

The other shape of the wrapper pattern: the upstream (@softeria/ms-365-mcp-server)
already signs in, so the wrapper keeps its sign-in and changes only what dmcp's
one-process-per-call lifecycle breaks. The upstream's `login` tool returns the
device code and finishes in the background, which dmcp ends; the wrapper's
`login` runs the upstream's blocking `--login` instead, relays the link and code
to stderr and opens the browser through `_lib.browser` (detached, off the MCP
wire, display only). `cache_store.py` is Softeria's `MS365_MCP_AUTH_CACHE_COMMAND`,
so its MSAL cache lives in the keyring through `_lib.tokenstore`. `HIDDEN_TOOLS`
withholds what the forbidden tier covers or that bypasses the gate (`graph-batch`,
container deletes, webhook subscriptions) via `proxy.run_proxy(hidden_tools=…)`.
The upstream installs with `npm ci` from the committed `package-lock.json`
(`--ignore-scripts`, optional packages omitted). Defaults to the mail and calendar
presets (`MS365_PRESET`). Gated by `scripts/selftest_microsoft_365.py`.

### servers/notion/ — a hosted server, signed in by its wrapper

Notion's MCP server is hosted (mcp.notion.com) and signs its caller in under the
MCP authorization spec, which puts the sign-in on the client. The wrapper is that
client, a local stdio server built from two helper modules:
`_lib.oauthbrowser.sign_in` (RFC 9728/8414 discovery, RFC 7591 registration as a
public client, PKCE S256, state, RFC 8707 `resource`, a one-shot 127.0.0.1
listener that ignores requests without the sign-in's state) and
`_lib.httpproxy.run_http_proxy` (answers `initialize` itself, opens the upstream
session lazily, reads JSON and SSE answers, renews and retries once after a 401).
Everything is pinned to mcp.notion.com; tokens live in `_lib.tokenstore` and are
renewed before expiry. No setup script: stdlib only. Gated by
`scripts/selftest_notion.py`.

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
- `validate-pr.yml` — PR gate; runs every `scripts/selftest_*.py` it finds
  (discovered, not listed, so a new self-test needs no workflow edit — #100), then
  `scripts/validate_registry.py` (schema,
  id/scope/trustStatus/platforms
  enums incl. per-transport, transport order, integrity hashes for both setup
  scripts, setup-script locations, manifest hosted by this registry, a full-SHA
  `source.rev` on `official` entries, orphan directories, vendored `_lib` copies
  identical to `servers/_lib`, and a `threat_level` —
  `safe`/`elevated`/`dangerous`/`forbidden`, or the legacy
  `confirmation_required: true` — on every tool of a live entry, well-formed
  https-only `providers/` mirrored into registry.json, and `credentials`/`login`
  declarations that resolve against them) and blocks `trustStatus` promotion to
  `official`, lifting a `deprecated`/`removed` revocation, and adding, changing
  or removing a sign-in provider without the maintainer `trust-approved` label.
  Enforced by the `main protection` ruleset: `validate` required, one approving
  review, stale approvals dismissed on push, force-push and deletion blocked.
  **Repository admins hold a pull-request-scoped bypass** — no direct pushes to
  `main`, but they can merge their own PR without the second approval. So every
  rule binds mechanically for an outside submission (no write access, no
  bypass), while a maintainer-authored promotion rests on process. The job
  executes the validator and all self-tests from a checkout of the **base
  branch** and judges the PR's tree as data (`selftest_jobs.py` targets the
  PR's shell servers via `JOBS_SELFTEST_TREE`, as does
  `selftest_system_status.py` for that server), so a PR editing the gate and
  its tests together does not change how that PR is judged; the workflow file
  itself still runs from the PR's merge ref, which is what the required review
  must read — see `docs/TRUST-MODEL.md` §4 for the full statement of what is
  and is not mechanical.
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
python scripts/selftest_trust_gate.py   # Offline self-test: official-tier pin, manifest hosting, revocation lifting
python scripts/selftest_needs_input.py  # Offline self-test: unanswered-prompt detection in the shell servers
python scripts/selftest_jobs.py         # Offline self-test: jarvis-shell interactive job model (PTY jobs, real JSON-RPC)
python scripts/selftest_threat_level.py # Offline self-test: per-tool threat_level enforcement (missing/invalid/exempt)
python scripts/selftest_categories.py   # Offline self-test: closed category vocabulary + fixture flag rules
python scripts/selftest_system_status.py  # Offline self-test: system-status parsing against synthetic machines
python scripts/selftest_credentials.py  # Offline self-test: providers, credentials, login, provider-change approval
python scripts/selftest_signin_lib.py   # Offline self-test: sign-in helper + GitHub wrapper (loopback provider)
python scripts/selftest_microsoft_365.py  # Offline self-test: Microsoft 365 wrapper (stand-in upstream)
python scripts/selftest_notion.py       # Offline self-test: Notion wrapper (fake hosted server)
python scripts/sync_lib.py              # Refresh vendored servers/<wrapper>/_lib copies (--check to verify)
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
