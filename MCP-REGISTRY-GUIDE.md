# MCP Registry Guide

How to create and host an MCP server registry for dmcp.

## Overview

dmcp fetches server listings from **registries** -- JSON files hosted at a URL. Users add your registry URL to their `~/.config/mcp/sources.list` (`dmcp sources add <url>`), and dmcp browses, searches, and installs from your catalogue. dmcp is a command-line and programmatic client; the JARVIS agent drives it through the same interface (see `dmcp serve`).

The flow looks like this:

```
Your GitHub repo                     User's machine
  registry.json         -->        dmcp browse / search
  (index: id -> manifest URL)       fetches the index
  servers/*/manifest.json  -->     fetches a manifest on install
                                    dmcp install <id>
                                    manifest.json written to installed/{id}/
                                    index.json updated (id -> manifest location)
```

Registry sources are read from (in priority order):
- `~/.config/mcp/sources.list` (user)
- `/etc/mcp/sources.list` (system)

### Automatic vs Manual Setup

dmcp creates most files automatically; only the system sources list needs manual setup:

| File or directory | Created by | Notes |
|-------------------|------------|-------|
| `~/.config/mcp/sources.list` | dmcp | Created by `dmcp sources add <url>` on first use. If absent, dmcp treats the source list as empty — there is no built-in default registry. |
| `~/.local/share/mcp/installed/index.json` | dmcp | Created when the first server is installed. Updated on each install/remove. |
| `~/.local/share/mcp/installed/<id>/manifest.json` | dmcp | Written per server on install; updated when the user saves configuration. |
| `~/.local/share/mcp/vector_index/index.json` | dmcp | Semantic-search index built from registry embeddings. Rebuildable via `dmcp sync-index`. |
| `/etc/mcp/sources.list` | Admin/distro | **Manual setup.** System-wide registry sources. Create this file if you want all users on the machine to see the same registries by default. |

## Registry File Format

A registry has two parts:

1. **Index** (`registry.json`) – Display metadata (name, summary, icon, keywords, categories) and a pointer to each manifest. The server ID is the key.
2. **Manifests** – Per-server JSON files (`servers/<name>/manifest.json`) with install/run metadata only (transports, source, setupScript, tools).

### Optional: Trust, Integrity, and Signing (Community Registry)

If you want a community-vetted registry (AUR-like), you can add optional fields to support **trust status**, **immutability**, and **signatures**.

- **`trustStatus` (index entry)**: A lightweight status flag used for filtering without fetching manifests.
  - Values: `"community"` (submitted, automated checks only) and `"official"` (maintainer-reviewed, source pinned), plus `"deprecated"` / `"removed"` for revocation. See [`docs/TRUST-MODEL.md`](docs/TRUST-MODEL.md) for the canonical model and the vetting flow.
- **`integrity` (index entry)**: Content fingerprints that bind review status to an exact manifest and setup script.
  - `manifestSha256`: SHA-256 of the referenced `manifest.json` content.
  - `setupScriptSha256`: SHA-256 of the referenced setup script content (if present).
- **`signing` (registry top-level)**: Reserved fields for cryptographic signing. This guide does not mandate an algorithm; clients can optionally verify signatures using a keyring.
  - `keyringUrl`: URL to a published set of trusted public keys (or `null`).
  - `signatures`: Array of signature objects (empty if unsigned).

### Index Format

The index is a single JSON file with this structure:

```json
{
  "version": "1.0",
  "updated": "2025-02-03T00:00:00Z",
  "signing": {
    "keyringUrl": null,
    "signatures": []
  },
  "servers": {
    "com.example.mcp.my-server": {
      "id": "com.example.mcp.my-server",
      "name": "My MCP Server",
      "summary": "One-line description shown in the listing",
      "version": "1.0.0",
      "scope": "user",
      "homepage": "https://github.com/example/mcp-registry",
      "icon": "https://...",
      "keywords": ["keyword1", "keyword2"],
      "categories": ["mcp", "mcp-development"],
      "trustStatus": "community",
      "integrity": {
        "manifestSha256": "<sha256 of manifest.json>",
        "setupScriptSha256": "<sha256 of setup.sh (if present)>"
      },
      "manifest": "https://raw.githubusercontent.com/example/mcp-registry/main/servers/my-server/manifest.json"
    }
  }
}
```

| Field       | Type   | Description                                              |
|-------------|--------|----------------------------------------------------------|
| `version`   | string | Registry format version (use `"1.0"`)                    |
| `updated`   | string | ISO 8601 timestamp of last update                        |
| `servers`   | object | Map of server ID to index entry (see below)              |

**Index entry fields** (per server in `servers`): The server ID is the object key. Include `id` explicitly in each entry (same value as the key).

| Field       | Type   | Description                                              |
|-------------|--------|----------------------------------------------------------|
| `id`        | string | **Required.** Unique identifier (same as the servers object key). |
| `name`      | string | Display name in the catalogue.                           |
| `summary`   | string | Short description shown in listings.                     |
| `version`   | string | Semantic version (for upgrade detection).                 |
| `scope`     | string | **Required.** `"user"` or `"system"`. See Scope below.   |
| `homepage`  | string | Optional. URL to the project homepage.                   |
| `icon`      | string | Icon for display (Freedesktop name or URL).              |
| `keywords`  | array  | Search keywords for discovery.                           |
| `categories`| array  | Categories for filtering (e.g. `["mcp", "mcp-development"]`). |
| `manifest`  | string | URL to the server's manifest JSON (install/run metadata).|
| `trustStatus` | string | Optional. Review tier: `"community"` or `"official"` (see `docs/TRUST-MODEL.md`). |
| `integrity` | object | Optional. Content hashes that bind vetting to specific manifest/script content. |
| `embeddings` | object | Optional, machine-managed. Pre-computed vectors `{model, version, server, tools}` consumed by `dmcp sync-index` for semantic search — see `docs/EMBEDDING-SPEC.md`. |

dmcp loads the index for display; manifests are fetched for install. Display metadata comes from the index; install metadata (transports, setupScript, tools) comes from the manifest.

### Manifest Format (Server Entry Schema)

Each server folder contains a `manifest.json` with **install and run metadata only**. Display metadata (name, summary, keywords, icon, categories) lives in the index. The manifest has no `id`—the ID is the key in the index.

**Required fields:**

```json
{
  "version": "1.0.0",
  "scope": "user",
  "transports": [ ... ],
  "source": { ... },
  "tools": [ ... ]
}
```

| Field       | Type   | Description                                              |
|-------------|--------|----------------------------------------------------------|
| `version`   | string | Semantic version of the server.                          |
| `scope`     | string | **Required.** `"user"` or `"system"`. See Scope below.   |
| `transports`| array  | Array of entrypoints (stdio, SSE, or WebSocket).         |
| `source`    | object | Git source for local servers; omit for remote (SSE/WebSocket). |
| `tools`     | array  | Tools the server provides. See Tools below.              |

### Optional Fields (manifest)

| Field                   | Type   | Description                                                     |
|-------------------------|--------|-----------------------------------------------------------------|
| `setupScript`           | string | For local: filename (e.g. `"setup.sh"`). For remote: URL. See Setup Script below. |
| `homepage`              | string | URL to the project homepage.                                    |
| `configurableProperties`| array  | Configuration properties (required and optional, see below).   |
| `stateful`              | boolean| `true` if the server holds state in-process across tool calls (browser, desktop control, REPL, DB connection); makes it eligible for dmcp session-scoped calls. Absent/`false` = stateless. |
| `trust`                 | object | Optional. Human-readable review details (no status). Useful for community registries. |

### Tools

Define each tool the MCP server provides. Required for tooling and validation.

```json
"tools": [
  { "name": "add", "description": "Add two numbers together" },
  { "name": "subtract", "description": "Subtract the second number from the first" }
]
```

### Setup Script

Developers point to a `setupScript` URL in the registry. dmcp **downloads** that script and runs it in the server’s **install directory** (where `manifest.json` lives), so the script can read the user’s config from the manifest and set up the environment.

- **Local servers (stdio):** The script runs after the Git clone. Use it to install dependencies (e.g. `pip install -r requirements.txt`, `npm install`, `cargo build --release`) and optionally apply config from `manifest.json`.
- **Remote servers (SSE/WebSocket):** There is no clone; the install directory only contains `manifest.json` with the user’s config (API key, endpoint, etc.). The script runs **locally** in that directory, reads the manifest, and prepares the connection (e.g. writes a `.env` or client config file) so the local client can use the user’s config when connecting to the remote server. The script never runs on the remote server—it bridges the user’s local config with the remote endpoint.

- **Default-on**: `dmcp install` downloads and runs the setup script by default (after SHA-256 verification against the registry's `setupScriptSha256`). Pass `--no-setup` to skip it.
- **Re-run**: `dmcp setup <id>` re-runs the setup script for an installed server (e.g. after config changes or dependency upgrades).
- **Execution**: The script runs with `sh` in the install directory (write POSIX-compatible scripts) and receives `MCP_INSTALL_DIR` plus `MCP_CONFIG_<KEY>` env vars. For system scope, it runs with elevated privileges.
- **Storage**: The installed manifest stores `setupScript`, `setupScriptPath`, `setupScriptVersion`, and `setupScriptRunAt`.

Example (local server):

```json
{
  "version": "1.0.0",
  "scope": "user",
  "homepage": "https://github.com/example/mcp-registry",
  "setupScript": "setup.sh",
  "source": { "type": "git", "url": "...", "path": "servers/my-server" },
  "transports": [{ "type": "stdio", "command": "python3", "args": ["server.py"] }],
  "tools": [{ "name": "add", "description": "Add two numbers" }]
}
```

### Icons

Registry owners define each server's icon in the `icon` field. Two formats are supported:

1. **Freedesktop icon name** – Use a standard icon from the user's icon theme (e.g. Breeze, Adwaita):
   - `"network-server"` – good for remote SSE/WebSocket servers
   - `"utilities-terminal"` – for CLI/dev tools
   - `"accessories-calculator"` – for calculator-style tools
   - `"applications-development"` – generic development

2. **URL to an image** – Use a custom logo hosted anywhere:
   - GitHub raw URL: `"https://raw.githubusercontent.com/yourorg/mcp-registry/main/logos/my-server.png"`
   - Any public image URL (PNG, SVG, etc.)

`icon` is stored as metadata for GUI frontends (e.g. the JARVIS desktop app); the dmcp CLI ignores it. Prefer Freedesktop names when a suitable one exists; use URLs for custom branding.

## Transports (Entrypoints)

The `transports` array lists one or more entrypoints. Each entrypoint can be stdio (local process), SSE, or WebSocket.

### stdio (Local Process)

Runs as a local process. The `command` and `args` are executed from the project root (install dir).

```json
{
  "type": "stdio",
  "command": "python3",
  "args": ["server.py"],
  "description": "Main calculator interface"
}
```

| Field         | Type   | Description                                    |
|---------------|--------|------------------------------------------------|
| `command`     | string | Executable (e.g. `python3`, `node`).          |
| `args`        | array  | Arguments, relative to project root.          |
| `description` | string | Optional description of this entrypoint.       |

### sse (Server-Sent Events)

Remote endpoint. No local installation.

```json
{
  "type": "sse",
  "url": "https://api.example.com/mcp/sse",
  "description": "Cloud API endpoint"
}
```

### websocket

```json
{
  "type": "websocket",
  "wsUrl": "wss://api.example.com/mcp/ws"
}
```

### Legacy Format (unsupported)

The pre-1.0 single-transport form (top-level `type` + `transport`) is **not**
accepted by dmcp; always use the `transports` array.

## Scope

The `scope` field controls where the server is installed:

| Scope    | Base path                         | Privileges        |
|----------|-----------------------------------|--------------------|
| `user`   | `~/.local/share/mcp/installed/`   | None (user-local) |
| `system` | `/usr/share/mcp/installed/`       | pkexec (root)     |

Default is `"user"`. System-scope installs are visible to all users on the machine and require password authentication via polkit.

SSE/WebSocket servers also support scope. A system-scope SSE entry puts its manifest in `/usr/share/mcp/installed/<id>/manifest.json` so all users see the configured endpoint.

```json
{
  "id": "com.example.shared-tool",
  "scope": "system",
  ...
}
```

## Source Configuration

For **local servers** (stdio), the `source` object specifies a Git repository to clone:

```json
"source": {
  "type": "git",
  "url": "https://github.com/yourorg/mcp-registry.git",
  "path": "servers/calculator-py"
}
```

| Field  | Type   | Description                                                      |
|--------|--------|------------------------------------------------------------------|
| `url`  | string | Git repository URL.                                              |
| `path` | string | Project root within the repo (optional). Empty = repo root.      |
| `rev`  | string | Optional git ref to check out after clone. A full 40-character commit SHA is a binding pin — dmcp verifies the checked-out HEAD matches it and aborts the install on mismatch. |

dmcp clones the repo, extracts the project root (`path` or repo root), and runs the transport's `command` + `args` from that directory. The registry author specifies the exact launcher (e.g. `python3 server.py`, `node index.js`) — any language works.

For **remote servers** (SSE/WebSocket), omit `source` or use an empty object. dmcp validates the endpoint and stores the connection details rather than cloning a repo.

## Configuration Properties

Servers can declare configurable properties in a single `configurableProperties` array. Each property has a `required` flag to indicate whether it must be filled before installation.

dmcp does not prompt or validate `required` properties itself — it stores config values and injects them into the server process as environment variables at spawn. Wrapper UIs (e.g. the JARVIS daemon) prompt for required fields; set values with `dmcp config <id> set <key> <value>`. Defaults are not auto-applied.

```json
"configurableProperties": [
  {
    "key": "api_key",
    "label": "API Key",
    "description": "Your API key from https://example.com/settings",
    "sensitive": true,
    "required": true
  },
  {
    "key": "timeout",
    "label": "Timeout (seconds)",
    "description": "Request timeout in seconds",
    "default": "30",
    "sensitive": false,
    "required": false
  },
  {
    "key": "endpoint",
    "label": "Endpoint URL",
    "description": "API endpoint (defaults to production)",
    "default": "https://api.example.com/v1",
    "sensitive": false,
    "required": false
  }
]
```

### Property Fields

| Field         | Type    | Description                                                |
|---------------|---------|------------------------------------------------------------|
| `key`         | string  | Internal identifier. Used as the key in config storage.    |
| `label`       | string  | Human-readable label for the property.                     |
| `description` | string  | Help text shown below the input field.                     |
| `default`     | string  | Default value. Pre-filled in the UI (mainly for optional). |
| `sensitive`   | boolean | If `true`, field is shown as a password input.             |
| `required`    | boolean | If `true`, must be filled before installation.             |

User-provided values are stored in the per-server manifest at `<installDir>/manifest.json` in the `config` object and injected into the server process as **environment variables** — the `key` IS the env var name (e.g. `BRAVE_API_KEY`). Defaults are not auto-applied.

Use `keywords` to make your server discoverable via `dmcp browse -k <keyword>` and semantic search.


## Hosting Your Registry

### Option 1: GitHub Raw URL (Simplest)

1. Create a `registry.json` in your repo.
2. Use the raw GitHub URL as your registry source:

```
https://raw.githubusercontent.com/yourorg/mcp-registry/main/registry.json
```

Users add this URL to their sources:

```bash
echo "https://raw.githubusercontent.com/yourorg/mcp-registry/main/registry.json" \
  >> ~/.config/mcp/sources.list
```

### Option 2: GitHub Pages

If you want a cleaner URL, serve `registry.json` via GitHub Pages:

```
https://yourorg.github.io/mcp-registry/registry.json
```

### Option 3: Your Own Server

Host `registry.json` on any web server. dmcp sends a standard HTTP GET with the User-Agent `dmcp/1.0`. Ensure HTTPS is used and redirects are followed.

## Minimal Working Example

Here is a complete minimal registry with one local server (Git) and one remote SSE server:

```json
{
  "version": "1.0",
  "updated": "2025-02-09T00:00:00Z",
  "servers": [
    {
      "id": "com.yourorg.mcp.calculator",
      "name": "Calculator MCP",
      "summary": "A simple calculator MCP server",
      "version": "1.0.0",
      "transports": [
        {
          "type": "stdio",
          "command": "python3",
          "args": ["server.py"],
          "description": "Main interface"
        }
      ],
      "source": {
        "type": "git",
        "url": "https://github.com/yourorg/mcp-registry.git",
        "path": "servers/calculator-py"
      },
      "keywords": ["calculator", "math"]
    },
    {
      "id": "com.yourorg.mcp.cloud-api",
      "name": "Cloud API",
      "summary": "Remote SSE server for cloud API access",
      "version": "1.0.0",
      "transports": [
        {
          "type": "sse",
          "url": "https://api.yourorg.com/mcp/sse"
        }
      ],
      "keywords": ["cloud", "api", "sse"],
      "configurableProperties": [
        {
          "key": "api_key",
          "label": "API Key",
          "description": "Get your key at https://yourorg.com/settings",
          "sensitive": true,
          "required": true
        }
      ]
    }
  ]
}
```

## How dmcp Processes Your Registry

1. **Fetch**: On startup (and on manual refresh), dmcp fetches each URL from `sources.list`.
2. **Embeddings**: `dmcp sync-index` separately downloads registry embeddings into the local vector index for semantic search; registry JSON itself is never cached.
3. **Parse**: Each server entry in the `servers` object becomes an installable server in the catalogue.
4. **Merge**: servers already installed (matched by `id`) are flagged as installed in `dmcp browse` output. There is no automatic upgrade detection; rerun `dmcp install <id>` to update in place.
5. **List**: `dmcp browse` lists the servers, searchable by name, summary, id, and keywords.

## What Happens on Install

When a user runs `dmcp install <id>`:

1. If `configurableProperties` exist and any required ones are unset, dmcp prompts for them.
2. If `scope` is `"system"`, the user authenticates via polkit (password prompt for pkexec).
3. dmcp checks the entry's `trustStatus` (refuses `removed`, warns on
   `community`/`deprecated`; the autonomous agent path refuses
   `deprecated`/`removed` outright), then verifies the fetched manifest's raw
   bytes against `integrity.manifestSha256` — a mismatch aborts the install.
   Setup scripts are verified against `setupScriptSha256` before running.
4. A dedicated directory is created at `<base>/mcp/installed/<id>/`.
5. For **local servers** (stdio): `git clone` fetches the repo, then the project root (`source.path` or repo root) is extracted into the install dir. The transport's `command` + `args` run from that directory.
6. For **remote servers** (SSE/WebSocket): the manifest with the connection details is written; the endpoint is not probed — connection errors surface on first `dmcp run`/`dmcp call`.
7. A manifest is written to `<installDir>/manifest.json` with full metadata and config; the `config` map is injected as environment variables when the server is spawned.
8. The index at `<base>/mcp/installed/index.json` is updated with `{ "<id>": { "location": "<path>/manifest.json", "keywords": ["..."] } }`. The index stores pointers plus keywords for search; full metadata lives in each manifest.
9. For user-scope, `<base>` is `~/.local/share`. For system-scope, `<base>` is `/usr/share`.

### Directory Layout After Install

**User-scope** (`~/.local/share/mcp/installed/`):

```
~/.local/share/mcp/installed/
├── index.json                                 (id -> location + keywords)
├── com.example.calculator/                     (local server — Git clone)
│   ├── manifest.json                           (full metadata + config; injected as env vars at spawn)
│   ├── server.py                               (project root contents)
│   └── ...                                     (other project files)
└── com.example.remote-api/                     (SSE server)
    └── manifest.json                           (full metadata + config)
```

**System-scope** (`/usr/share/mcp/installed/`) has the same structure but is owned by root and managed via pkexec.

### Uninstall

Removal is a simple `rm -rf <installDir>`. All files are self-contained. For system-scope, `pkexec rm -rf` is used.

## Tips

- **Keep IDs stable.** The `id` field is how dmcp tracks a server across registry updates. Changing it creates a "new" server.
- **Use semantic versioning.** Versions are informational metadata today — dmcp does not compare versions to detect upgrades.
- **Test your JSON.** A registry that fails to fetch or parse is skipped with a warning on stderr; a valid file with a missing/malformed `servers` key yields an empty listing. Validate your JSON before publishing.
- **Update the `updated` timestamp** when you publish changes, so users know the registry is maintained.
- **Provide a `homepage`.** dmcp stores it and wrapper UIs can surface it. (`bugUrl` is not read by dmcp.)

## Changelog — corrected claims

*2026-07-22:* setup script is default-on (`--no-setup` to skip), runs with `sh`, and receives `MCP_INSTALL_DIR`/`MCP_CONFIG_<KEY>`; config is injected as env vars (no prompting, no auto-defaults, no Configure dialog); trust/integrity gating documented in the install flow; `source.rev` pinning and the index-entry `embeddings` field documented; no registry cache, endpoint probe, upgrade detection, icon fallback, or `bugUrl` handling; legacy single-transport form marked unsupported; sources.list is user-created.
