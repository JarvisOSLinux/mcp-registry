# MCP Registry Guide

How to create and host an MCP server registry for KDE Discover.

## Overview

The MCP catalogue in KDE Discover fetches server listings from **registries** -- JSON files hosted at a URL. Users add your registry URL to their `~/.config/mcp/sources.list`, and Discover pulls your server catalogue automatically.

The flow looks like this:

```
Your GitHub repo                     User's machine
  registry.json         -->        KDE Discover
  (hosted via raw URL)              fetches & caches
                                    shows servers in UI
                                    user clicks Install
                                    pkexec installs into /usr/share/mcp/installed/{id}/
                                    manifest.json written to installed/{id}/
                                    mcp.json index updated
```

## Registry File Format

A registry is a single JSON file with this structure:

```json
{
  "version": "1.0",
  "updated": "2025-02-03T00:00:00Z",
  "servers": [
    { ... },
    { ... }
  ]
}
```

| Field       | Type   | Description                              |
|-------------|--------|------------------------------------------|
| `version`   | string | Registry format version (use `"1.0"`)    |
| `updated`   | string | ISO 8601 timestamp of last update        |
| `servers`   | array  | Array of server entry objects (see below) |

## Server Entry Schema

Each object in the `servers` array describes one MCP server.

### Required Fields

```json
{
  "id": "com.yourorg.mcp.servername",
  "name": "My MCP Server",
  "summary": "One-line description shown in the listing",
  "version": "1.0.0",
  "transports": [ ... ],
  "source": { ... }
}
```

| Field       | Type   | Description                                              |
|-------------|--------|----------------------------------------------------------|
| `id`        | string | Unique identifier. Use reverse-domain notation.          |
| `name`      | string | Display name in the catalogue.                           |
| `summary`   | string | Short description shown in listings.                     |
| `version`   | string | Semantic version of the server.                          |
| `transports`| array  | Array of entrypoints (stdio, SSE, or WebSocket).         |
| `source`    | object | Git source for local servers; omit or use empty for remote. |

### Optional Fields

| Field                | Type   | Description                                                     |
|----------------------|--------|-----------------------------------------------------------------|
| `description`        | string | Long description. Supports `\n` for line breaks.                |
| `author`             | string | Author name.                                                    |
| `homepage`           | string | URL to the project homepage.                                    |
| `bugUrl`             | string | URL to the issue tracker.                                       |
| `donationUrl`        | string | URL for donations.                                              |
| `icon`               | string | Freedesktop icon name (e.g. `"folder"`, `"network-server"`).   |
| `categories`         | array  | Category strings for filtering (see Categories below).          |
| `capabilities`       | array  | What the server can do (freeform strings for display).          |
| `permissions`        | array  | Permissions the server requires (freeform strings for display). |
| `tools`              | array  | Tools provided (strings or `{"name": ..., "description": ...}`).|
| `configurableProperties` | array | Configuration properties (required and optional, see below). |
| `license`            | object | `{"name": "MIT", "url": "https://..."}`.                       |
| `releaseDate`        | string | ISO 8601 date string (`"2025-01-15"`).                          |
| `size`               | number | Approximate size in bytes (for display).                        |
| `screenshots`        | array  | Screenshot URLs or `{"thumbnail": ..., "url": ...}` objects.   |
| `changelog`          | string | Changelog text.                                                 |
| `scope`              | string | `"user"` (default) or `"system"`. See Scope below.              |

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

User-provided values are stored in the installed manifest at `<installDir>/manifest.json` in the `config` object. Optional property defaults are applied automatically if the user doesn't override them.

## Categories

Use these category strings so your servers appear under the right filter in Discover:

| Category              | Description              |
|-----------------------|--------------------------|
| `mcp`                 | Generic MCP server (always include this) |
| `mcp-database`        | Database servers         |
| `mcp-filesystem`      | Filesystem operations    |
| `mcp-web`             | Web-related servers      |
| `mcp-search`          | Search capabilities      |
| `mcp-development`     | Development tools        |
| `mcp-ai`              | AI-related services      |
| `mcp-shell`           | Shell execution          |
| `mcp-communication`   | Communication tools      |
| `mcp-media`           | Media handling           |
| `mcp-productivity`    | Productivity tools       |

Always include `"mcp"` in addition to any specific category. A server can belong to multiple categories.

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
      "categories": ["mcp", "mcp-development"]
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
      "categories": ["mcp", "mcp-web"],
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
5. **Display**: Servers appear under Development > MCP Servers, filterable by category and searchable by name/summary/id.

## What Happens on Install

When a user clicks Install on your server:

1. If `configurableProperties` exist and any required ones are unconfigured, a configuration dialog is shown.
2. If `scope` is `"system"`, the user authenticates via polkit (password prompt for pkexec).
3. A dedicated directory is created at `<base>/mcp/installed/<id>/`.
4. For **local servers** (stdio): `git clone` fetches the repo, then the project root (`source.path` or repo root) is extracted into the install dir. The transport's `command` + `args` run from that directory.
5. For **remote servers** (SSE/WebSocket): The endpoint is validated via HTTP HEAD, then the manifest is written. No local clone.
6. A manifest is written to `<installDir>/manifest.json` with all metadata, config values, and install date.
7. For user-scope, `<base>` is `~/.local/share`. For system-scope, `<base>` is `/usr/share`.

### Directory Layout After Install

**User-scope** (`~/.local/share/mcp/installed/`):

```
~/.local/share/mcp/installed/
├── com.example.calculator/                     (local server — Git clone)
│   ├── manifest.json                           (server metadata + config)
│   ├── server.py                               (project root contents)
│   └── ...                                     (other project files)
└── com.example.remote-api/                     (SSE server — no local files)
    └── manifest.json
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
