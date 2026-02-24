# MCP Registry Guide

How to create and host an MCP server registry for KDE Discover.

## Overview

The MCP catalogue in KDE Discover fetches server listings from **registries** -- JSON files hosted at a URL. Users add your registry URL to their `~/.config/mcp/sources.list`, and Discover pulls your server catalogue automatically.

The flow looks like this:

```
Your GitHub repo                     User's machine
  registry.json         -->        KDE Discover
  (index: id -> manifest URL)       fetches index & caches
  servers/*/manifest.json  -->     fetches manifests as needed
                                    shows servers in UI
                                    user clicks Install
                                    manifest.json written to installed/{id}/
                                    index.json updated (id -> manifest location)
```

Registry sources are read from (in priority order):
- `~/.config/mcp/sources.list` (user)
- `/etc/mcp/sources.list` (system)

### Automatic vs Manual Setup

Discover creates most files automatically; only the system sources list needs manual setup:

| File or directory | Created by | Notes |
|-------------------|------------|-------|
| `~/.config/mcp/sources.list` | Discover | Auto-created on first run from the installed default, or a fallback if none exists. Users can add registry URLs here. |
| `~/.local/share/mcp/installed/index.json` | Discover | Created when the first server is installed. Updated on each install/remove. |
| `~/.local/share/mcp/installed/<id>/manifest.json` | Discover | Written per server on install; updated when the user saves configuration. |
| `~/.cache/discover/mcp-registries/` | Discover | Cache for fetched registries. Created when registries are first fetched. |
| `/etc/mcp/sources.list` | Admin/distro | **Manual setup.** System-wide registry sources. Create this file if you want all users on the machine to see the same registries by default. |

## Registry File Format

A registry has two parts:

1. **Index** (`registry.json`) – Display metadata (name, summary, icon, keywords, categories) and a pointer to each manifest. The server ID is the key.
2. **Manifests** – Per-server JSON files (`servers/<name>/manifest.json`) with install/run metadata only (transports, source, setupScript, tools).

### Index Format

The index is a single JSON file with this structure:

```json
{
  "version": "1.0",
  "updated": "2025-02-03T00:00:00Z",
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

Discover loads the index for display; manifests are fetched for install. Display metadata comes from the index; install metadata (transports, setupScript, tools) comes from the manifest.

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

### Tools

Define each tool the MCP server provides. Required for tooling and validation.

```json
"tools": [
  { "name": "add", "description": "Add two numbers together" },
  { "name": "subtract", "description": "Subtract the second number from the first" }
]
```

### Setup Script

Developers point to a `setupScript` URL in the registry. Discover **downloads** that script and runs it in the server’s **install directory** (where `manifest.json` lives), so the script can read the user’s config from the manifest and set up the environment.

- **Local servers (stdio):** The script runs after the Git clone. Use it to install dependencies (e.g. `pip install -r requirements.txt`, `npm install`, `cargo build --release`) and optionally apply config from `manifest.json`.
- **Remote servers (SSE/WebSocket):** There is no clone; the install directory only contains `manifest.json` with the user’s config (API key, endpoint, etc.). The script runs **locally** in that directory, reads the manifest, and prepares the connection (e.g. writes a `.env` or client config file) so the local client can use the user’s config when connecting to the remote server. The script never runs on the remote server—it bridges the user’s local config with the remote endpoint.

- **User opt-in**: Users must explicitly enable "Run setup script" during install (checkbox off by default).
- **Re-run**: Installed servers can run the setup script from the Configure dialog (e.g. "Repair" or after upgrading dependencies).
- **Execution**: The script runs with `bash` in the install directory. For system scope, it runs with elevated privileges.
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

If omitted, Discover falls back to `"application-x-executable"`. Prefer Freedesktop names when a suitable one exists; use URLs for custom branding.

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

### Legacy Format

For backward compatibility, a single transport can be specified with top-level `type` and `transport`:

```json
{
  "type": "stdio",
  "transport": {
    "command": "python3",
    "args": ["server.py"]
  }
}
```

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

Discover clones the repo, extracts the project root (`path` or repo root), and runs the transport's `command` + `args` from that directory. The registry author specifies the exact launcher (e.g. `python3 server.py`, `node index.js`) — any language works.

For **remote servers** (SSE/WebSocket), omit `source` or use an empty object. Discover validates the endpoint and stores the connection details. Shows "Connect" / "Disconnect" instead of "Install" / "Remove".

## Configuration Properties

Servers can declare configurable properties in a single `configurableProperties` array. Each property has a `required` flag to indicate whether it must be filled before installation.

Discover shows a configuration dialog before installation if any required properties are empty. Optional properties are pre-filled with their `default` value and can be edited post-install.

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
| `label`       | string  | Label shown in the configuration dialog.                   |
| `description` | string  | Help text shown below the input field.                     |
| `default`     | string  | Default value. Pre-filled in the UI (mainly for optional). |
| `sensitive`   | boolean | If `true`, field is shown as a password input.             |
| `required`    | boolean | If `true`, must be filled before installation.             |

User-provided values are stored in the per-server manifest at `<installDir>/manifest.json` in the `config` object. MCP servers read their configuration from this manifest file. Optional property defaults are applied automatically if the user doesn't override them.

All MCP servers appear under **Development > MCP Servers** in Discover. Use `keywords` for searchability.


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

Host `registry.json` on any web server. Discover sends a standard HTTP GET with the User-Agent `KDE Discover MCP Backend/1.0`. Ensure HTTPS is used and redirects are followed.

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

## How Discover Processes Your Registry

1. **Fetch**: On startup (and on manual refresh), Discover fetches each URL from `sources.list`.
2. **Cache**: The response is cached locally at `~/.cache/discover/mcp-registries/`.
3. **Parse**: Each server entry in the `servers` array becomes a resource in the MCP Servers catalogue.
4. **Merge**: If a server from the registry is already installed (matched by `id`), Discover compares versions and marks it as upgradeable if the registry version is newer.
5. **Display**: Servers appear under Development > MCP Servers, searchable by name, summary, id, and keywords.

## What Happens on Install

When a user clicks Install on your server:

1. If `configurableProperties` exist and any required ones are unconfigured, a configuration dialog is shown.
2. If `scope` is `"system"`, the user authenticates via polkit (password prompt for pkexec).
3. A dedicated directory is created at `<base>/mcp/installed/<id>/`.
4. For **local servers** (stdio): `git clone` fetches the repo, then the project root (`source.path` or repo root) is extracted into the install dir. The transport's `command` + `args` run from that directory.
5. For **remote servers** (SSE/WebSocket): The endpoint is validated via HTTP HEAD, then the manifest is written. No local clone.
6. A manifest is written to `<installDir>/manifest.json` with full metadata and config. MCP servers read their configuration from this file.
7. The index at `<base>/mcp/installed/index.json` is updated with `{ "<id>": { "location": "<path>/manifest.json", "keywords": ["..."] } }`. The index stores pointers plus keywords for search; full metadata lives in each manifest.
8. For user-scope, `<base>` is `~/.local/share`. For system-scope, `<base>` is `/usr/share`.

### Directory Layout After Install

**User-scope** (`~/.local/share/mcp/installed/`):

```
~/.local/share/mcp/installed/
├── index.json                                 (id -> location + keywords)
├── com.example.calculator/                     (local server — Git clone)
│   ├── manifest.json                           (full metadata + config; MCP servers read this)
│   ├── server.py                               (project root contents)
│   └── ...                                     (other project files)
└── com.example.remote-api/                     (SSE server)
    └── manifest.json                           (full metadata + config)
```

**System-scope** (`/usr/share/mcp/installed/`) has the same structure but is owned by root and managed via pkexec.

### Uninstall

Removal is a simple `rm -rf <installDir>`. All files are self-contained. For system-scope, `pkexec rm -rf` is used.

## Tips

- **Keep IDs stable.** The `id` field is how Discover tracks a server across registry updates. Changing it creates a "new" server.
- **Use semantic versioning.** Discover compares `installedVersion` against your registry's `version` to detect upgrades.
- **Test your JSON.** A malformed registry file is silently skipped. Validate your JSON before publishing.
- **Update the `updated` timestamp** when you publish changes, so users know the registry is maintained.
- **Provide a `bugUrl`.** It shows a "Report Bug" link on the server's detail page in Discover.
