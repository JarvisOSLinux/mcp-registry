# Manifest Reference

`manifest.json` is the per-server file that describes how to install and run an MCP server. It lives at `servers/<server-id>/manifest.json`.

Display metadata (name, summary, keywords, icon, categories) lives in `registry.json`, not in the manifest. The manifest contains install and runtime metadata, plus a machine-managed top-level `embeddings` object (`{"<model>": {"v": [...], "hash": "..."}}`) written by `scripts/generate_embeddings.py` — do not edit it by hand.

---

## Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | string | Yes | Semantic version of the server (e.g. `"1.0.0"`). Used for upgrade detection. |
| `scope` | string | Yes | `"user"` or `"system"`. See [Scope](#scope). |
| `platforms` | array | Yes in this registry | Operating systems the registry vouches for: `"linux"`, `"darwin"`, `"windows"`. See [Platforms](#platforms). |
| `transports` | array | Yes | One or more entrypoints. See [Transports](#transports). |
| `tools` | array | Yes | Tools the server exposes. See [Tools](#tools). |
| `source` | object | Conditional | Git source for local stdio servers. Omit for remote SSE/WebSocket. See [Source](#source). |
| `homepage` | string | No | URL to the project homepage or upstream repo. |
| `setupScript` | string | No | For local servers: filename (e.g. `"setup.sh"`). For remote: full HTTPS URL — in this registry, only the one naming the committed `servers/<id>/setup.sh`. See [Setup Script](#setup-script). |
| `setupScriptWindows` | string | No | PowerShell script run instead of `setupScript` on Windows hosts. The value must be `"setup.ps1"` (or the registry-hosted URL for it). See [Windows Setup Script](#windows-setup-script). |
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

## Platforms

```json
"platforms": ["linux"]
```

The operating systems **this registry vouches for** — the platforms the entry was
actually vetted on through the normal review process. Allowed values: `"linux"`,
`"darwin"` (macOS), `"windows"`.

**Absent means unrestricted.** A manifest with no `platforms` is installable on
any host, so third-party registries written before the field keep working
unchanged. Entries in *this* registry must declare it:
`scripts/validate_registry.py` fails a PR that omits it, is empty, or uses a
value outside the enum. That is the point of the field — an unvetted host should
never be silently offered a server.

**It is ordinary manifest data.** There is no claim/verified split and no special
epistemics: `platforms` is exactly as trustworthy as the entry's `trustStatus`
tier, the same as `transports`, `tools`, and `setup.sh` already are. `official`
means a maintainer confirmed it; `community` means it is the submitter's word
until promotion review.

**`sync_registry.py` mirrors it into `registry.json`.** dmcp filters by host from
the index alone: `dmcp browse` marks entries that cannot run on the current host,
and `dmcp install` refuses them *before* any clone or setup script runs. Neither
path fetches every manifest to work that out, so the mirrored copy is the one the
client actually reads.

**The list grows by PR.** Every entry here currently reads `["linux"]`, because
everything in this registry was vetted on Arch. To widen it, verify the server on
another OS — `dmcp install --ignore-platform` exists for exactly that — then open
a PR adding the platform. The resulting manifest-hash change propagates the wider
support to already-installed users through `dmcp update`.

**Platform support is coverage, not identity.** One capability, one server: a
server that gains macOS support extends its `platforms` list; it does not become
a second `-darwin` entry. Per-OS entries are legitimate only when the capability
itself is platform-shaped (Linux desktop automation built on AT-SPI, say). When
the launch details differ between hosts, that is what
[per-transport `platforms`](#per-transport-platforms) and
[`setupScriptWindows`](#windows-setup-script) are for — one entry, one recipe per
OS. See "One Capability, One Server" in
[`../MCP-REGISTRY-GUIDE.md`](../MCP-REGISTRY-GUIDE.md) for the full convention.

`setup.sh` environment checks stay as defense in depth. `platforms` records which
OS was vetted; the setup script still verifies that the dependencies it needs —
a node version, a python interpreter — are actually present.

---

## Transports

The `transports` array lists one or more ways to connect to or launch the server. At least one entry is required. Every transport type also accepts an optional `platforms` array — see [Per-Transport Platforms](#per-transport-platforms).

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
| `platforms` | array | No | Hosts this entrypoint is for. Absent = every host. See [Per-Transport Platforms](#per-transport-platforms). |

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
| `platforms` | array | No | Hosts this endpoint is for. Absent = every host. See [Per-Transport Platforms](#per-transport-platforms). |

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
| `platforms` | array | No | Hosts this endpoint is for. Absent = every host. See [Per-Transport Platforms](#per-transport-platforms). |

### Per-Transport Platforms

```json
"platforms": ["linux", "darwin"]
```

A transport entry may narrow itself to the hosts it can actually launch on,
using the same three values as the top-level field: `"linux"`, `"darwin"`,
`"windows"`. This is what lets **one** server entry run correctly on every OS it
is vetted for, instead of splitting into per-OS siblings.

| Rule | Behavior |
|------|----------|
| Absent | Matches every host — the default, and the behavior of every manifest written before the field existed. |
| Present | Matches only the listed hosts. Must be a non-empty array of allowed values; omit the field to mean "all", never write `[]`. |
| Selection | dmcp uses the **first** transport whose list includes the host; a transport without the field counts as a match. Order most-specific first. |
| Ordering | The first match wins, so a transport an earlier one already matches is never selected. Every transport carrying `platforms` must come **before** any transport without the field; `scripts/validate_registry.py` rejects the shadowed one. |
| No match | Hard error naming the platforms the manifest does offer. dmcp never falls back to a transport meant for another OS. |

The ordering rule bites when a manifest keeps its platform-less transport and
adds a specific sibling — the specific one goes first:

```json
"transports": [
  { "type": "stdio", "command": ".venv\\Scripts\\python.exe", "args": ["server.py"], "platforms": ["windows"] },
  { "type": "stdio", "command": "python3", "args": ["server.py"] }
]
```

Written the other way round, the bare transport matches Windows too and the
Windows entry below it is unreachable configuration.

The differences that matter in practice are the interpreter's name (`python3` on
POSIX, `python` on Windows) and the venv layout (`.venv/bin/…` against
`.venv\Scripts\….exe`) — one extra stdio transport covers both:

```json
{
  "version": "1.4.0",
  "scope": "user",
  "platforms": ["linux", "windows"],
  "name": "Doc Search MCP",
  "summary": "Search indexed documentation",
  "homepage": "https://github.com/example/doc-search-mcp",
  "source": {
    "type": "git",
    "url": "https://github.com/example/doc-search-mcp.git"
  },
  "setupScript": "setup.sh",
  "setupScriptWindows": "setup.ps1",
  "transports": [
    {
      "type": "stdio",
      "command": ".venv/bin/python3",
      "args": ["server.py"],
      "platforms": ["linux", "darwin"],
      "description": "POSIX entrypoint"
    },
    {
      "type": "stdio",
      "command": ".venv\\Scripts\\python.exe",
      "args": ["server.py"],
      "platforms": ["windows"],
      "description": "Windows entrypoint"
    }
  ],
  "tools": [
    {
      "name": "search_docs",
      "description": "Search indexed documentation for a phrase"
    }
  ]
}
```

A Linux host spawns the first transport, a Windows host the second. Same ID,
same tools, same embeddings — one capability, one server. (JSON needs Windows
backslashes escaped: `.venv\\Scripts\\python.exe`.)

**The two `platforms` fields answer different questions.** The top-level one is
the vetting gate dmcp enforces on install; a per-transport one is a launch
detail. They need not agree, and a manifest may carry a Windows transport before
`"windows"` is vetted — the transport simply goes unused until the vetting
catches up. The reverse is worth catching: a vetted platform with no matching
transport passes the install gate and then has nothing to launch, so
`scripts/validate_registry.py` warns about it. It is a warning rather than an
error because the transport may legitimately land in a later PR than the
platform.

Transport order is the stricter rule and is an error: a transport an earlier one
already matches is dead on every host at every point in time, so no later PR can
bring it to life.

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
| `blocking` | boolean | No | `true` if this tool can park indefinitely waiting for input. See [Blocking Tools](#blocking-tools). |
| `suggestedRemindAfter` | integer | No | Seconds. The reminder interval the server recommends for this tool. See [Blocking Tools](#blocking-tools). |
| `threat_level` | string | No | `safe` \| `elevated` \| `dangerous` \| `forbidden`. What this tool can do to the host. See [Threat Level](#threat-level). |
| `confirmation_required` | boolean | No | Legacy shorthand for `threat_level: elevated`. Prefer `threat_level`. |

At least one tool is required. Descriptions should be specific enough for an LLM to determine when to use the tool.

### Threat Level

A consuming daemon decides whether a tool call needs the user's confirmation.
JARVIS computes that as the **strictest** of three independent sources: a host
floor keyed on well-known tool names (`bash`, `exec`, `run_job`, …), this
manifest field, and a scan of the call's actual parameters for destructive
payloads. **A manifest can raise a tool's level but never lower it below the
host floor** — so under-declaring buys a server nothing, while over-declaring
is honoured.

Declare it whenever a tool's *name* would not tell a reader what it can do.
The host floor is a list of names the daemon already knows; a genuinely
destructive tool under an unfamiliar name (`apply`, `sync`, `type_text`) is
invisible to it, and without this field such a tool classifies `safe` and runs
unconfirmed.

| Value | Meaning | Examples |
|-------|---------|----------|
| `safe` | Reads or reports; cannot alter the host or reveal arbitrary content | list windows, get status, read a public feed |
| `elevated` | Reads arbitrary user content, or makes bounded changes — an exfiltration or nuisance surface | screenshot, read the accessibility tree, send a message |
| `dangerous` | Can cause arbitrary effects: executes code, injects input, deletes or overwrites data | run a command, type keystrokes, click arbitrary UI, delete files |
| `forbidden` | Must never run unattended | reserved; nothing in this registry declares it |

Be proportionate rather than maximal. Marking everything `dangerous` trains a
user to approve without reading, which costs more safety than it buys.

```json
{
  "name": "type_text",
  "description": "Type a string into the focused window.",
  "threat_level": "dangerous"
}
```

`servers/computer-use-linux/manifest.json` is the worked example: input
injection is `dangerous`, arbitrary screen reads are `elevated`, and window
enumeration stays `safe`.

### Blocking Tools

```json
{
  "name": "run_job",
  "description": "Run a command that may need interactive input …",
  "blocking": true,
  "suggestedRemindAfter": 30
}
```

Most tools answer and return. A few cannot: they run something that stops and
waits for a human decision — an installer with no `-y`, a partitioning wizard, a
REPL, an `ssh` that hits a host-key prompt — and the tool call stays outstanding
for exactly as long as the wait lasts. That is not a hang; the call is doing its
job. But it is indistinguishable from a hang to a caller with nothing scheduled
to look at it.

An orchestrator that dispatches such a tool as a concurrent task and sets **no
reminder** never finds out the tool is waiting. The task simply never completes,
nothing is pushed, and the question at the other end goes unanswered until
something times out. `blocking` is the tool telling the caller, up front, that
this is a real possibility for it.

| Field | Meaning |
|-------|---------|
| `blocking: true` | This tool can park indefinitely awaiting input. A caller that dispatches it with no reminder will never learn it is waiting. |
| `suggestedRemindAfter: <int>` | Seconds. The reminder interval the server recommends for this tool — chosen by the server author, who knows how long its normal quiet stretches are. |

**Both keys are optional and opt-in.** A tool without `blocking` behaves exactly
as it does today; nothing about existing manifests changes. A
`suggestedRemindAfter` with no `blocking: true` is meaningless, but it is not an
error — it is inert metadata, not a contradiction to reject.

**The consumer rule.** When a task targets a tool whose manifest declares
`blocking: true` **and the caller supplied no reminder interval of its own**, the
orchestrator applies the tool's `suggestedRemindAfter` (or its own built-in
default when the manifest names none). An interval the caller supplied
explicitly **always** wins — including an explicit opt-out of reminders. The
manifest supplies a default for callers that did not think about it; it never
overrides a caller that did. See dispatch's `remind_after`.

**Choosing a value.** Two failure modes bracket it. Too short and every ordinary
run wakes the orchestrator repeatedly for nothing, which costs a round trip each
time. Too long and a human sits in front of an unanswered prompt while nothing
reports it. Pick from the tool's own behavior: how long does it normally go quiet
mid-run, and how quickly does a stall need noticing? A tool whose whole purpose is
interactive work wants a shorter interval than one that merely *might* prompt
after a long silent phase.

**Where a blocking tool comes from.** The usual reason a tool parks is that it is
holding an interactive process open so a *later* tool call can answer it — the
job pattern, since a stdio server gets a fresh process per call and cannot keep
the process in memory. `MCP-REGISTRY-GUIDE.md` ("The Job Pattern", and
"Interactive Tools: Closed stdin" for the non-blocking half) is the worked
version; these two manifest keys are what it declares.

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

In **this** registry the URL form is accepted only when it points back at the
committed sibling of the manifest — `.../servers/<id>/setup.sh`. dmcp fetches an
`https://` setup script straight from the network and runs it, and only a
recorded hash makes it verify first, so a URL nothing in `servers/<id>/` backs
would execute unverified. `scripts/validate_registry.py` rejects it.

The script is executed under the interpreter its shebang names — bash for `#!/usr/bin/env bash`, `sh` otherwise — so a `#!/usr/bin/env bash` script may use bash features such as `set -o pipefail`. For system-scope installs it runs with elevated privileges via pkexec. dmcp exports `MCP_INSTALL_DIR` plus `MCP_CONFIG_<KEY>` (uppercased, `-`/`.` → `_`) for each config key.

The setup script runs **by default** during install; pass `--no-setup` to `dmcp install` to skip it. It can be re-run at any time with `dmcp setup <id>` (e.g. after changing config). For registry-listed servers, `setup.sh` lives in the registry repo at `servers/<id>/setup.sh` next to the manifest; its SHA-256 is recorded in the registry entry's `integrity.setupScriptSha256` and verified before running.

**Requirements:**
- Must be a bash script.
- Must be idempotent.
- Must not prompt for user input.
- Must exit non-zero on failure.

### Windows Setup Script

```json
"setupScript": "setup.sh",
"setupScriptWindows": "setup.ps1"
```

`setup.sh` is bash — dmcp runs it through bash when its shebang asks for bash,
`sh` otherwise — and stock Windows has neither. A
server vetted on Windows ships a PowerShell script beside its POSIX one and
names it in the optional `setupScriptWindows` field. dmcp runs `setup.ps1`
through PowerShell on Windows hosts and `setup.sh` on every other host — the two
are siblings, not alternatives.

- **Filename.** The value must be `"setup.ps1"`, stored next to `manifest.json`
  in the server directory. That is the only name `scripts/sync_registry.py`
  hashes, so any other name would ship an unverified script. As with
  `setupScript`, the URL spelling is accepted only when it resolves to that same
  committed file — `.../servers/<id>/setup.ps1`.
- **Integrity.** The registry entry gains
  `integrity.setupScriptWindowsSha256` beside `setupScriptSha256`, recomputed by
  `sync_registry.py` and verified by dmcp before the script runs. A per-platform
  script is not a hash-verification hole.
- **Validation.** `scripts/validate_registry.py` rejects a `setup.ps1` with no
  recorded hash, a recorded hash whose `setup.ps1` is gone, a stale hash, a
  script under any other name, an off-registry URL, and a declared
  `setupScriptWindows` that was never committed.
- **Optional.** A server with no Windows vetting needs neither the field nor the
  file. Existing manifests are unaffected.

Same requirements as `setup.sh`: idempotent, non-interactive, non-zero exit on
failure. Set `$ErrorActionPreference = 'Stop'` so a failing command actually
fails the script.

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

Sessions are **user scope only** — dmcp refuses `--session` for a system-scope
server, so an elevated server never becomes a standing capability. A server that
must keep something alive across calls without a session (or at system scope)
cannot hold it in memory at all: the handle has to live on the filesystem, which
is the job pattern in `MCP-REGISTRY-GUIDE.md`.

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
  "platforms": ["linux"],
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

*2026-07-25:* setup scripts run under the interpreter their shebang names (bash for `#!/usr/bin/env bash`, otherwise `sh`), superseding the 2026-07-22 "runs with `sh`" note; transport order documented as load-bearing and enforced (a transport an earlier one already matches is rejected); `setupScript` / `setupScriptWindows` in URL form must resolve to the committed script beside the manifest, since dmcp cannot hash-verify anything else.
