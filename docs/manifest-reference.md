# Manifest Reference

`manifest.json` is the per-server file that describes how to install and run an MCP server. It lives at `servers/<server-id>/manifest.json`.

Display metadata (name, summary, keywords, icon, categories) lives in `registry.json`, not in the manifest. The manifest contains install and runtime metadata, plus a machine-managed top-level `embeddings` object (`{"<model>": {"v": [...], "hash": "..."}}`) written by `scripts/generate_embeddings.py` — do not edit it by hand.

---

## Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | string | Yes | Semantic version of the server (e.g. `"1.0.0"`). Used for upgrade detection. |
| `scope` | string | Yes | `"user"` or `"system"`. See [Scope](#scope). |
| `transports` | array | Yes | One or more entrypoints. See [Transports](#transports). |
| `tools` | array | Yes | Tools the server exposes. See [Tools](#tools). |
| `source` | object | Conditional | Git source for local stdio servers. Omit for remote SSE/WebSocket. See [Source](#source). |
| `homepage` | string | No | URL to the project homepage or upstream repo. |
| `setupScript` | string | No | For local servers: filename (e.g. `"setup.sh"`). For remote: full HTTPS URL. See [Setup Script](#setup-script). |
| `configurableProperties` | array | No | User-configurable properties (API keys, endpoints). See [Configurable Properties](#configurable-properties). |
| `stateful` | boolean | No | `true` if the server holds state in-process across tool calls (browser, desktop control, REPL, DB connection). See [Stateful](#stateful). |
| `trust` | object | No | Human-readable review metadata. See [Trust Object](#trust-object). |
| `name` | string | No | Display name. Synced into `registry.json` by `sync_registry.py`. |
| `summary` | string | No | One-line description. Synced into `registry.json`. |
| `keywords` | array | No | Search keywords. Synced into `registry.json`. |

---

## Scope

Controls where the server is installed and what privileges are required.

| Value | Install path | Privileges |
|-------|-------------|------------|
| `"user"` | `~/.local/share/mcp/installed/<id>/` | None (runs as current user) |
| `"system"` | `/usr/share/mcp/installed/<id>/` | pkexec (root) — requires polkit authentication |

Use `"user"` unless your server genuinely needs system-wide access (e.g. a daemon that must be visible to all users on the machine).

---

## Transports

The `transports` array lists one or more ways to connect to or launch the server. At least one entry is required.

### stdio (Local Process)

Runs the server as a local subprocess. Requires a `source` block.

```json
{
  "type": "stdio",
  "command": "python3",
  "args": ["server.py"],
  "description": "Main interface"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Must be `"stdio"`. |
| `command` | string | Yes | Executable to run (e.g. `"python3"`, `"node"`, `".venv/bin/python3"`). |
| `args` | array | Yes | Arguments, relative to the project root (the cloned directory). |
| `description` | string | No | Human-readable label for this entrypoint. |

The command and args run from the project root (the `source.path` directory or the repo root if `path` is omitted). Use a venv-relative path like `.venv/bin/python3` if your `setup.sh` creates a virtual environment.

### SSE (Server-Sent Events)

Connects to a remote HTTP endpoint. No local clone occurs.

```json
{
  "type": "sse",
  "url": "https://api.example.com/mcp/sse",
  "description": "Cloud API endpoint"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Must be `"sse"`. |
| `url` | string | Yes | Full HTTPS URL of the SSE endpoint. |
| `description` | string | No | Human-readable label. |

### WebSocket

Connects to a remote WebSocket endpoint.

```json
{
  "type": "websocket",
  "wsUrl": "wss://api.example.com/mcp/ws",
  "description": "WebSocket endpoint"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Must be `"websocket"`. |
| `wsUrl` | string | Yes | Full WSS URL of the WebSocket endpoint. |
| `description` | string | No | Human-readable label. |

---

## Tools

The `tools` array declares the tools the server exposes. This list is used for display in dmcp and for semantic search (embeddings are generated from the manifest's `name`, `summary`, `keywords`, and the names/descriptions of the first 20 `tools` entries — tool descriptions directly affect semantic ranking).

```json
"tools": [
  {
    "name": "summarize_commits",
    "description": "Summarize recent git commits for a repository path"
  },
  {
    "name": "list_branches",
    "description": "List all branches in a local git repository"
  }
]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Tool identifier (snake_case recommended). |
| `description` | string | Yes | What the tool does. Used for semantic search. |

At least one tool is required. Descriptions should be specific enough for an LLM to determine when to use the tool.

---

## Source

Specifies the Git repository to clone for local stdio servers. Omit for remote (SSE/WebSocket) servers.

```json
"source": {
  "type": "git",
  "url": "https://github.com/alice/git-summary-mcp.git",
  "path": "servers/my-server"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Must be `"git"`. |
| `url` | string | Yes | Full HTTPS or SSH Git URL. Must be publicly accessible. |
| `path` | string | No | Subdirectory within the repo to use as the project root. Omit to use the repo root. |
| `rev` | string | No | Full 40-character commit SHA to pin the clone to. Required for `official`-tier servers — dmcp clones with full history, checks out this exact commit, and refuses to install if the resolved `HEAD` doesn't match (`SourceRevMismatch`). Omit for `community`-tier servers, which take a fast shallow (`--depth 1`) clone of the default branch instead. |

After cloning, the transport's `command` + `args` run from the resolved project root.

---

## Setup Script

`setupScript` points to a script that installs dependencies and prepares the server environment.

- **Local server:** value is a filename (e.g. `"setup.sh"`). The script runs in the project root after `git clone`.
- **Remote server:** value is a full HTTPS URL to a script. The script runs locally in the install directory (which contains only `manifest.json`).

The script is executed via `sh <script>` — write it to be POSIX-sh compatible. For system-scope installs it runs with elevated privileges via pkexec. dmcp exports `MCP_INSTALL_DIR` plus `MCP_CONFIG_<KEY>` (uppercased, `-`/`.` → `_`) for each config key.

The setup script runs **by default** during install; pass `--no-setup` to `dmcp install` to skip it. It can be re-run at any time with `dmcp setup <id>` (e.g. after changing config). For registry-listed servers, `setup.sh` lives in the registry repo at `servers/<id>/setup.sh` next to the manifest; its SHA-256 is recorded in the registry entry's `integrity.setupScriptSha256` and verified before running.

**Requirements:**
- Must be a bash script.
- Must be idempotent.
- Must not prompt for user input.
- Must exit non-zero on failure.

---

## Configurable Properties

Declares user-configurable values (API keys, endpoint URLs, options). These are shown in the install/configure dialog and stored in the installed manifest.

```json
"configurableProperties": [
  {
    "key": "API_KEY",
    "label": "API Key",
    "description": "Obtain from https://example.com/settings",
    "sensitive": true,
    "required": true
  },
  {
    "key": "TIMEOUT",
    "label": "Timeout (seconds)",
    "description": "Request timeout. Default is 30.",
    "default": "30",
    "sensitive": false,
    "required": false
  }
]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | Yes | Internal identifier. Used as the key in the installed manifest's `config` object. |
| `label` | string | Yes | Label shown in the UI. |
| `description` | string | Yes | Help text shown below the input field. Include a link to obtain the value if applicable. |
| `default` | string | No | Default value pre-filled in the UI. For optional properties only. |
| `sensitive` | boolean | Yes | If `true`, the field is shown as a password input and masked in UIs. Values are currently stored in **plaintext** in the installed manifest's `config` object; encryption at rest is planned (kernel keyring). |
| `required` | boolean | Yes | If `true`, the install dialog blocks until the user provides a value. |

Never hardcode API keys or tokens directly in the manifest. All secrets must be declared here as `"required": true, "sensitive": true` properties.

---

## Stateful

```json
"stateful": true
```

Optional top-level boolean. Set it to `true` when the server holds state
in-process across tool calls — a browser session, a desktop-control connection,
a language REPL, an open database connection. Absent or `false` means the server
is **stateless**: every tool call is self-contained and nothing is retained
between calls (the default; existing servers need no change).

A `stateful: true` server is eligible for dmcp **session-scoped** calls
(`dmcp call <id> <tool> --session <session_id>`), where a single long-lived
server process is reused across a series of calls so the in-process state
persists. Stateless servers ignore sessions entirely and always run one-shot.
Declaring `stateful` never changes one-shot behavior; it only advertises that
the server *can* be driven session-scoped.

---

## Trust Object

The `trust` object provides human-readable evidence for reviewers and users. It does not confer any automatic status — trust level is set in `registry.json` by maintainers after review.

```json
"trust": {
  "reviewReferences": [
    "https://github.com/alice/git-summary-mcp",
    "https://github.com/alice/git-summary-mcp/blob/main/LICENSE"
  ],
  "checks": [
    "license: MIT",
    "source: https://github.com/alice/git-summary-mcp.git",
    "build: pip install -r requirements.txt",
    "no outbound network calls"
  ],
  "notes": "Reads local git history only. Pure Python, no compiled extensions.",
  "securityNotes": "Requires read access to target git repositories."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `reviewReferences` | array | URLs reviewers should inspect: upstream repo, license, audit reports. |
| `checks` | array | Freeform strings describing what was verified: license, source origin, build process. |
| `notes` | string or null | General notes about the server's behavior. |
| `securityNotes` | string or null | Security considerations users should be aware of: network access, filesystem access, required permissions. |

---

## Complete Example

A Python stdio server with one required API key:

```json
{
  "version": "1.2.0",
  "scope": "user",
  "name": "Git Summary MCP",
  "summary": "Summarize recent git commits and branch activity",
  "keywords": ["git", "commits", "summary", "version-control"],
  "homepage": "https://github.com/alice/git-summary-mcp",
  "transports": [
    {
      "type": "stdio",
      "command": ".venv/bin/python3",
      "args": ["server.py"],
      "description": "Main interface"
    }
  ],
  "source": {
    "type": "git",
    "url": "https://github.com/alice/git-summary-mcp.git"
  },
  "setupScript": "setup.sh",
  "tools": [
    {
      "name": "summarize_commits",
      "description": "Summarize recent git commits for a given repository path"
    },
    {
      "name": "list_branches",
      "description": "List all branches in a local git repository"
    },
    {
      "name": "diff_summary",
      "description": "Summarize the diff between two branches or commits"
    }
  ],
  "configurableProperties": [],
  "trust": {
    "reviewReferences": [
      "https://github.com/alice/git-summary-mcp",
      "https://github.com/alice/git-summary-mcp/blob/main/LICENSE"
    ],
    "checks": [
      "license: MIT",
      "source: https://github.com/alice/git-summary-mcp.git",
      "build: pip install gitpython",
      "no outbound network requests"
    ],
    "notes": "Reads local git history. No external API calls.",
    "securityNotes": "Requires read access to target git repositories on the local filesystem."
  }
}
```

## Changelog — corrected claims

*2026-07-22:* `sensitive` values are stored in plaintext today (masking is UI-only; encryption planned); setup scripts run by default with `sh` (`--no-setup` to skip, `dmcp setup <id>` to re-run) and receive `MCP_INSTALL_DIR`/`MCP_CONFIG_<KEY>`; registry-hosted `setup.sh` location and SHA-256 verification documented; machine-managed `embeddings` field documented; embedding canonical text corrected.
