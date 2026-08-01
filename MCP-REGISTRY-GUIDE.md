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

## One Capability, One Server

**One capability, one server.** A server's identity is its *capability*, not the
platform it happens to run on. A shell server that gains macOS support widens its
[`platforms`](#platforms) list to `["linux", "darwin"]` — it does not become
`jarvis-shell` plus `jarvis-shell-darwin`. `platforms` is **coverage state**: how
far vetting has reached, growing as people verify the server on more operating
systems. It is never a reason to publish per-OS sibling entries for the same
capability.

The manifest format is built to make that possible rather than merely
aspirational. A single entry can carry one stdio transport per platform —
`command: "python3"` on POSIX, `"python"` on Windows; `.venv/bin/…` against
`.venv\Scripts\….exe` — and ship a `setup.ps1` next to its `setup.sh`. "Runs
everywhere" is therefore one entry with per-platform launch details, not three
catalogue entries. See [Per-Transport Platforms](#per-transport-platforms) and
[Setup Script](#setup-script).

**The exception: capabilities that are themselves platform-shaped.** Per-OS
servers are legitimate when the platform is the subject matter rather than an
implementation detail. Linux desktop control built on AT-SPI, ydotool, and KWin
*is* Linux desktop technology: `computer-use-linux` is a genuinely different
capability from a macOS accessibility-API equivalent, not the same capability
wearing a different coat. Splitting there is honest. Splitting a portable Python
server because its launcher is spelled differently on Windows is not.

**The LLM never reasons about platform.** That is the point of keeping the
catalogue this way. An agent searching for a shell server gets one result to
weigh, not three near-identical ones it has to disambiguate by OS — and dmcp
filters entries the host cannot run *out of the results before they reach the
agent* ([`dmcp browse`](#how-dmcp-processes-your-registry) flags them, install
refuses them). Platform is infrastructure's problem, not the model's.

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
  - `setupScriptWindowsSha256`: SHA-256 of the PowerShell setup script `setup.ps1` (if present). dmcp verifies whichever script it is about to run, so a per-platform script is not a hole in integrity verification.
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
      "platforms": ["linux"],
      "categories": ["mcp", "mcp-development"],
      "trustStatus": "community",
      "integrity": {
        "manifestSha256": "<sha256 of manifest.json>",
        "setupScriptSha256": "<sha256 of setup.sh (if present)>",
        "setupScriptWindowsSha256": "<sha256 of setup.ps1 (if present)>"
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
| `platforms` | array  | Mirrored from the manifest by `sync_registry.py`. Operating systems the registry vouches for; absent = unrestricted. See Platforms below. |
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
| `platforms`             | array  | Operating systems the registry vouches for: `"linux"`, `"darwin"`, `"windows"`. Absent = unrestricted. **Required for entries in this registry.** See Platforms below. |
| `setupScript`           | string | For local: filename (e.g. `"setup.sh"`). For remote: URL. See Setup Script below. |
| `setupScriptWindows`    | string | PowerShell setup script used on Windows hosts instead of `setupScript`. For local servers the value must be `"setup.ps1"`. See Setup Script below. |
| `homepage`              | string | URL to the project homepage.                                    |
| `configurableProperties`| array  | Configuration properties (required and optional, see below).   |
| `stateful`              | boolean| `true` if the server holds state in-process across tool calls (browser, desktop control, REPL, DB connection); makes it eligible for dmcp session-scoped calls, which are **user scope only**. Absent/`false` = stateless — every call is a fresh process, see [stdin, stdout, and the Process Lifecycle](#stdin-stdout-and-the-process-lifecycle). |
| `trust`                 | object | Optional. Human-readable review details (no status). Useful for community registries. |

### Platforms

```json
"platforms": ["linux"]
```

`platforms` names the operating systems the registry **vouches for**: the ones an
entry was actually vetted on. Allowed values are `"linux"`, `"darwin"` (macOS),
and `"windows"`. dmcp resolves the machine it is running on to one of those three
strings; a host that matches none of them is treated as unsupported.

- **Absent = unrestricted.** Manifests without the field install anywhere, so
  registries written before `platforms` existed behave exactly as they did. In
  this registry the field is mandatory — `scripts/validate_registry.py` rejects a
  PR whose entry omits it, leaves it empty, or uses a value outside the enum.
- **Mirrored into the index.** `scripts/sync_registry.py` copies `platforms` from
  each manifest into its `registry.json` entry, because dmcp filters by host from
  the index alone: `dmcp browse` marks entries the host cannot run, and
  `dmcp install` refuses them before any clone or setup script executes, without
  fetching every manifest to find out.
- **Trust rides on `trustStatus`.** `platforms` is plain manifest data, no more
  and no less trustworthy than `transports`, `tools`, or `setup.sh` — `official`
  means a maintainer confirmed it, `community` means it is the submitter's word
  pending promotion review.
- **The list grows by vetting.** Verify a server on another OS (dmcp's
  `--ignore-platform` flag is the intended path for that), then open a PR adding
  the platform; the manifest-hash change propagates the wider support to
  installed users via `dmcp update`.

**Coverage, not identity.** `platforms` records how far vetting has reached; it
is never a reason to split one capability into per-OS sibling entries. A server
that runs on more than one OS says so in one entry, with a transport per platform
if the launch details differ — see
[One Capability, One Server](#one-capability-one-server) and
[Per-Transport Platforms](#per-transport-platforms).

### Tools

Define each tool the MCP server provides. Required for tooling and validation.

```json
"tools": [
  { "name": "add", "description": "Add two numbers together" },
  { "name": "subtract", "description": "Subtract the second number from the first" }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | **Required.** Tool identifier. |
| `description` | string | **Required.** What the tool does. Feeds semantic search. |
| `blocking` | boolean | Optional. `true` if this tool can park indefinitely awaiting input. See Blocking Tools below. |
| `suggestedRemindAfter` | integer | Optional. Seconds; the reminder interval the server recommends for this tool. See Blocking Tools below. |

#### Blocking Tools

```json
{
  "name": "run_job",
  "description": "Run a command that may need interactive input …",
  "blocking": true,
  "suggestedRemindAfter": 30
}
```

Some tools cannot answer promptly by design: they run something that stops and
waits for a human decision — an installer with no `-y`, a partitioning wizard, a
REPL — so the tool call stays outstanding for as long as the wait lasts. To an
orchestrator that dispatched it concurrently and scheduled nothing to look at it,
that is indistinguishable from a hang, and the question at the far end goes
unanswered.

`blocking: true` is the tool saying so up front, and `suggestedRemindAfter` is
the server author's recommended reminder interval in seconds — they know how long
the tool's normal quiet stretches run.

- **Optional and opt-in.** A tool without `blocking` behaves exactly as it does
  today. Every manifest written before these fields existed stays valid.
- **`suggestedRemindAfter` alone is inert.** Without `blocking: true` it means
  nothing, but it is not an error.
- **Consumer rule.** When a dispatched task targets a `blocking: true` tool **and
  the caller supplied no reminder interval**, the orchestrator applies the tool's
  `suggestedRemindAfter` (or its own default if the manifest gives none). A
  caller-supplied interval **always** wins, including an explicit opt-out of
  reminders — the manifest supplies a default for callers that did not think
  about it, never an override for callers that did.
- **These are declarations, not enforcement.** Like `tools` and `platforms`, they
  are ordinary manifest data whose trustworthiness rides on `trustStatus`. A
  server cannot make a caller set a reminder; it can only say that it needs one.

Any server author may use both fields — they are a general manifest facility, not
a convention private to any one server.

#### Interactive Tools: Closed stdin

A tool that wraps another program eventually wraps one that stops and asks a
question — a package manager's `[Y/n]`, an installer with no `-y`, a host-key
prompt, `fdisk`. Two shapes handle that, and which one you need depends on
whether the question has to be *answered* or merely *reported*. This section is
the reporting shape; [The Job Pattern](#the-job-pattern) below is the answering
one.

**Spawn every child with its stdin closed.** A stdio server's own stdin is the
JSON-RPC channel (see
[stdin, stdout, and the Process Lifecycle](#stdin-stdout-and-the-process-lifecycle)),
so a child that inherits it is reading the protocol wire. With stdin on
`/dev/null` a command that reads it gets immediate EOF instead: it aborts, takes
its default, or silently does nothing — in milliseconds, rather than hanging
forever on input that will never arrive.

All three outcomes look like an ordinary run from the outside, and that is the
part worth reporting. Inspect the **tail** of the finished command's combined
output; when its last line still holds an unanswered prompt, attach a purely
additive object beside the ordinary result. The first-party shell servers call it
`needs_input`:

```json
{
  "success": false,
  "exit_code": 1,
  "stdout": "",
  "stderr": ":: Proceed with installation? [Y/n] ",
  "needs_input": {
    "prompted": true,
    "prompt": ":: Proceed with installation? [Y/n]",
    "hint": "The command asked for input; stdin is closed, so its default was used (or it aborted). Re-run with the tool's non-interactive flag (e.g. -y / --yes / --noconfirm) to choose explicitly."
  }
}
```

- **Additive, never a verdict.** The report never flips `success` or `exit_code`.
  It says what the output shows; the exit code still says what the command did.
- **Key on the shape of the last line, not on the exit code.** A `[Y/n]`-style
  confirmation, a bare `…?`, a `password:` / `passphrase:` request. Shape catches
  the abort (exit 1), the silent no-op (exit 0) and the took-a-default (exit 0)
  alike, which a status check cannot. Matching only the *last* line is what keeps
  a `[Y/n]` printed mid-output — inside a package list, a changelog — from
  reading as a live prompt.
- **A credential prompt is its own case.** Give it a distinct hint that forbids a
  blind re-run: a password or passphrase is the credential boundary, and a human
  (or a configured non-interactive credential source) has to supply it.
- **This is a report, not a dialogue.** Nothing is held, nothing waits. The
  caller surfaces the prompt and re-runs non-interactively.

#### The Job Pattern

Some work cannot be made non-interactive, because the questions only appear as
the work unfolds: a partitioning wizard, `mysql_secure_installation`, a REPL. No
argument supplied up front answers a sequence nobody has seen yet.

The constraint that shapes the answer is the **stdio lifecycle**: dmcp is
one-shot by default — a fresh server process per tool call, spawned, called once,
killed. So a tool call cannot both wait on a prompt and receive the answer: the
call carrying the answer arrives at a *different process*, and everything the
first process held in memory is gone. In-process state cannot bridge that gap.
**The handle has to be on the filesystem** — a directory the next process finds
by name. That is true of any server wrapping an interactive process, whatever it
wraps; it is not a quirk of shells.

The shape, as the first-party shell servers implement it: four tools over one
named job.

| Tool | Role |
|------|------|
| `run_job` | Starts `command` under a real PTY as the named job and **blocks** until the job exits, streaming the job's output to the server's stderr while it waits. Returns the transcript, exit code, and job name. Declared `blocking: true` with `suggestedRemindAfter: 30`. |
| `send_input` | Writes `text` verbatim to the running job's terminal (the caller supplies any trailing newline) and returns immediately. Called from a *later, separate* tool call, while `run_job` is still parked. |
| `read_output` | The job's output so far plus its running/exited state, with optional `tail` / `offset` windows and `output_offset` / `output_length` / `total_length` so a long log can be walked. |
| `kill_job` | SIGTERM then SIGKILL to the job's whole process group, which also unblocks the pending `run_job`. |

The loop a consumer runs: dispatch `run_job` as a concurrent task **with a
reminder**; when that reminder (or a status tail) shows the command waiting on
input, call `send_input` with exactly what the output asked for.

One directory per job, under a per-user root:

```
$XDG_RUNTIME_DIR/<server>/       0700, refused unless owned by this uid
                                 (fallback: /tmp/<server>-<uid>)
  <job>/                         0700, one directory per job name
    out.log                      every byte the command wrote to the PTY
    in.sock                      0600 socket; the holder relays it into the PTY master
    status                       exit code, written atomically — the "finished" signal
    holder.pid, child.pid        liveness, and the process group to signal
```

Rules worth copying, each one earned:

- **A PTY, not pipes.** Many programs only prompt when they believe a terminal is
  attached, and the prompt is the thing this pattern exists to surface.
- **The holder must not be the server's child.** Detach it — double `fork` plus
  `setsid` — or it dies with the tool call that started it. Put its own stdin,
  stdout and stderr on `/dev/null`: it was forked from a process whose stdout is
  the protocol wire.
- **A job name is a path component, not a path.** Validate against a strict
  character set and reject `.` and `..` explicitly — those are navigation, not
  names. In Python, anchor with `\Z`: `$` also matches before a trailing newline,
  which would admit `evil\n` and slip `..\n` past a dot check.
- **Write the exit code atomically** (temporary file, then rename over the
  target). It doubles as the "job finished" signal, so a reader must never
  observe half of it.
- **Signal the command's own session, not just its leader**, so everything it
  spawned goes with it — while leaving the holder alive to reap the child and
  record the code.
- **Sweep on startup.** A job directory with no recorded exit code whose holder is
  dead is a crashed holder's residue; a finished one past a TTL has had its day.
  Signal the recorded process group *before* removing the directory: that file is
  the last handle to a command that may still be running, and deleting it turns a
  survivor into a runaway nothing can ever address again.
- **Declare it.** The blocking tool carries `blocking: true` and a
  `suggestedRemindAfter` — see [Blocking Tools](#blocking-tools). A job model
  whose blocking call is dispatched with no reminder leaves a human sitting at a
  prompt nobody reads.
- **Say so where the host cannot.** The pattern needs a Unix PTY and Unix
  sockets. On a host with neither, the job tools should return a legible
  "unsupported" error naming the non-interactive tool to use instead, not an
  import failure.

**What consumers get back: sterilized output.** Under a PTY, output is not text —
it is a recording of an animation. A progress bar redraws one line thousands of
times, and every redraw carries its own colour and cursor escapes. Replaying that
spends a model's context on frames nobody watched. So job output is **rendered at
read time, never at write time**: `out.log` keeps every byte the command wrote —
nothing is destroyed, and a human can always go back to the file — while
`run_job`'s transcript and `read_output` return the rendering. A consumer of this
pattern can expect:

- Carriage-return redraws collapsed to each line's final visible state, with
  erase-in-line honoured — the difference between `done` and
  `doneloading big-file.tar`. Inventing text the command never displayed is the
  failure mode being avoided.
- Colour, title-setting and cursor escapes removed; horizontal cursor motion and
  backspace honoured, since those decide what the line says.
- A run of identical lines folded into the line plus a count.
- **The trailing line untouched** — not folded, not trimmed, not reordered. An
  unanswered prompt has no newline after it, so it *is* the trailing line, and a
  renderer able to hide it would defeat the model it serves.

Bound every hop that ends in a context window. The transcript `run_job` returns
is a tool result spent, in full, out of a model's budget, so it is capped at its
**tail** — the exit state, the error and the unanswered question all live at the
end — and opens with a marker naming the exact `read_output` call, offset
included, that fetches the omitted head. The live stderr stream gets the same
renderer incrementally, so an escape sequence split across two reads is held
rather than mangled, and cursor motion into cells nobody wrote stops at a right
margin: `ESC[200000000C` is a dozen bytes a command may choose to write, and
materializing that column as padding would evict every other line from the ring a
reminder reads.

### Setup Script

Developers point to a `setupScript` URL in the registry. dmcp **downloads** that script and runs it in the server’s **install directory** (where `manifest.json` lives), so the script can read the user’s config from the manifest and set up the environment.

- **Local servers (stdio):** The script runs after the Git clone. Use it to install dependencies (e.g. `pip install -r requirements.txt`, `npm install`, `cargo build --release`) and optionally apply config from `manifest.json`.
- **Remote servers (SSE/WebSocket):** There is no clone; the install directory only contains `manifest.json` with the user’s config (API key, endpoint, etc.). The script runs **locally** in that directory, reads the manifest, and prepares the connection (e.g. writes a `.env` or client config file) so the local client can use the user’s config when connecting to the remote server. The script never runs on the remote server—it bridges the user’s local config with the remote endpoint.

- **Default-on**: `dmcp install` downloads and runs the setup script by default (after SHA-256 verification against the registry's `setupScriptSha256`). Pass `--no-setup` to skip it.
- **Re-run**: `dmcp setup <id>` re-runs the setup script for an installed server (e.g. after config changes or dependency upgrades).
- **Execution**: The script runs in the install directory under the interpreter its shebang names (bash for `#!/usr/bin/env bash`, otherwise `sh`) and receives `MCP_INSTALL_DIR` plus `MCP_CONFIG_<KEY>` env vars. For system scope, it runs with elevated privileges.
- **Storage**: The installed manifest stores `setupScript`, `setupScriptPath`, `setupScriptVersion`, and `setupScriptRunAt`.

Example (local server):

```json
{
  "version": "1.0.0",
  "scope": "user",
  "platforms": ["linux"],
  "homepage": "https://github.com/example/mcp-registry",
  "setupScript": "setup.sh",
  "source": { "type": "git", "url": "...", "path": "servers/my-server" },
  "transports": [{ "type": "stdio", "command": "python3", "args": ["server.py"] }],
  "tools": [{ "name": "add", "description": "Add two numbers" }]
}
```

#### Windows: `setupScriptWindows`

`setup.sh` is a bash script — dmcp runs it through bash when its shebang asks
for bash, `sh` otherwise — and stock Windows has neither. A server that is
vetted on Windows therefore ships a PowerShell script
alongside its POSIX one and names it in the optional `setupScriptWindows` field:

```json
{
  "setupScript": "setup.sh",
  "setupScriptWindows": "setup.ps1"
}
```

- **Selection**: dmcp runs `setup.ps1` through PowerShell on Windows hosts and
  `setup.sh` everywhere else. The two scripts are siblings, not alternatives —
  a server with both covers every platform it is vetted on.
- **Integrity**: the registry entry carries
  `integrity.setupScriptWindowsSha256` next to `setupScriptSha256`, and dmcp
  verifies whichever script it is about to execute. A per-platform script is
  not a hash-verification hole.
- **Filename**: the value must be `"setup.ps1"`, stored next to `manifest.json`
  in the server directory. That is the only name `scripts/sync_registry.py`
  hashes, so any other name would ship an unverified script;
  `scripts/validate_registry.py` rejects it, along with a script that has no
  recorded hash and a recorded hash whose script is gone. The same holds for
  `setupScript`: either field may be written as a URL, but only the one that
  resolves to the committed sibling of the manifest — dmcp fetches an
  off-registry `https://` script and runs it with nothing to verify it against.
- **Not required**: a server with no Windows vetting needs neither the field nor
  the file, and existing manifests are unaffected.

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
| `platforms`   | array  | Optional. Hosts this entrypoint is for. See Per-Transport Platforms below. |

#### stdin, stdout, and the Process Lifecycle

A stdio server does not merely *use* stdin and stdout: for the life of the
process they **are** the MCP wire. dmcp writes JSON-RPC requests to the process's
stdin and reads responses from its stdout. Two rules follow, and both bite the
first time a tool spawns a child process:

- **Never let a child inherit stdin.** Python's
  `subprocess.run(..., capture_output=True)` redirects stdout and stderr and
  leaves stdin inherited — so a child that reads stdin is reading the protocol
  channel. It blocks forever on input that channel will never carry, and it can
  swallow a request that arrives mid-run, eating the very status or kill call
  meant to rescue it. Pass `stdin=subprocess.DEVNULL`, or your language's
  equivalent. A command fed a script *on* stdin is already safe: it hits EOF on
  its own. What to do about the prompt that then goes unanswered is
  [Interactive Tools: Closed stdin](#interactive-tools-closed-stdin).
- **Write nothing but JSON-RPC to stdout.** Diagnostics, progress, and any live
  output belong on stderr or in a file; one stray byte on stdout corrupts the
  stream. stderr is the useful channel here — dmcp relays a server's stderr
  onward *while the call is still running*, which is how a caller sees a
  long-running tool's output before it returns.

**Lifecycle: one process per tool call.** dmcp is one-shot by default — it
spawns the server, makes the call, and kills it. Nothing a server holds in memory
survives to the next tool call, and that call will be answered by a different
process. `stateful: true` plus `dmcp call --session <id>` is the exception,
keeping one process alive across a series of calls, and it is **user-scope only**
(a system-scope server is refused a session, so an elevated server cannot become
a standing capability).

Anything that must outlive a single call therefore has to leave its handle on the
filesystem. That is the constraint behind [The Job Pattern](#the-job-pattern),
which is how a tool starts a program that asks questions and lets a *later* call
answer them.

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

### Per-Transport Platforms

Any transport entry may carry its own `platforms` array, using the same three
values as the top-level field: `"linux"`, `"darwin"`, `"windows"`. It says which
hosts *this entrypoint* is for, so one server entry can launch correctly
everywhere it is vetted:

| Rule | Behavior |
|------|----------|
| Absent | The transport matches every host. This is the default and today's behavior, so every existing manifest stays valid. |
| Present | The transport matches only the listed hosts. Must be a non-empty array of allowed values — omit the field to mean "all", never write `[]`. |
| Selection | dmcp picks the **first** transport whose list includes the host, and a transport without the field counts as a match. Order the array most-specific first. |
| Ordering | Because the first match wins, a transport an earlier one already matches can never be selected. Put every transport carrying `platforms` **before** any transport without the field — appending a Windows transport after a bare one leaves the Windows entry dead, and `scripts/validate_registry.py` rejects it. |
| No match | Hard error naming the platforms the manifest does offer — dmcp does not fall back to a transport meant for another OS. |

Ordering matters most when a manifest keeps one platform-less transport and adds
a platform-specific sibling — the specific one goes first:

```json
"transports": [
  { "type": "stdio", "command": ".venv\\Scripts\\python.exe", "args": ["server.py"], "platforms": ["windows"] },
  { "type": "stdio", "command": "python3", "args": ["server.py"] }
]
```

Reverse those two and every host, Windows included, gets `python3`.

What usually differs is small — the interpreter's name (`python3` on POSIX,
`python` on Windows) and the venv layout — and one extra stdio transport covers
both:

```json
{
  "version": "1.4.0",
  "scope": "user",
  "platforms": ["linux", "windows"],
  "homepage": "https://github.com/example/doc-search-mcp",
  "source": { "type": "git", "url": "https://github.com/example/doc-search-mcp.git" },
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
  "tools": [{ "name": "search_docs", "description": "Search indexed documentation" }]
}
```

One capability, one entry, two launch recipes. A Linux host runs the first
transport, a Windows host the second; both are the same server with the same ID,
tools, and embeddings. Note that a Windows path needs its backslashes escaped in
JSON (`.venv\\Scripts\\python.exe`).

The two `platforms` fields answer different questions and do not have to agree.
The top-level one is the **vetting gate** — the OSes this registry vouches for,
enforced by dmcp on install. A per-transport one is a **launch detail**. A
manifest may therefore carry a Windows transport before `"windows"` joins the
vetted list; the transport simply goes unused until vetting catches up, which is
how the format lands ahead of adoption. The reverse is the mistake worth
catching: if a vetted platform has no transport that matches it, a host on that
platform passes the install gate and then finds nothing to launch, so
`scripts/validate_registry.py` warns about it (a warning, not an error — the
transport may legitimately arrive in a later PR than the platform).

Transport *order* is the harder rule, and it is an error rather than a warning:
a transport that an earlier one already matches is dead on every host at every
point in time, so nothing later can rescue it.

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
      "platforms": ["linux"],
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
      "platforms": ["linux", "darwin"],
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
5. **List**: `dmcp browse` lists the servers, searchable by name, summary, id, and keywords. Entries whose `platforms` exclude the host are flagged as unsupported in both the table and `--json`, so agent-driven discovery can skip them.

## What Happens on Install

When a user runs `dmcp install <id>`:

1. If `configurableProperties` exist and any required ones are unset, dmcp prompts for them.
2. If `scope` is `"system"`, the user authenticates via polkit (password prompt for pkexec).
3. dmcp checks the entry's `platforms` against the host OS and refuses — before
   any clone, download, or setup script — when the host is not listed
   (`--ignore-platform` overrides, which is how someone verifies a new OS so the
   list can grow). It then checks the entry's `trustStatus` (refuses `removed`, warns on
   `community`/`deprecated`; the autonomous agent path refuses
   `deprecated`/`removed` outright) and verifies the fetched manifest's raw
   bytes against `integrity.manifestSha256` — a mismatch aborts the install.
   Setup scripts are verified before running: `setupScriptSha256`, or
   `setupScriptWindowsSha256` for the PowerShell script dmcp runs on a Windows
   host.
4. A dedicated directory is created at `<base>/mcp/installed/<id>/`.
5. For **local servers** (stdio): `git clone` fetches the repo, then the project root (`source.path` or repo root) is extracted into the install dir. The transport's `command` + `args` run from that directory — when the manifest lists several transports, dmcp spawns the first one whose `platforms` includes the host (see [Per-Transport Platforms](#per-transport-platforms)).
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

*2026-07-25:* setup scripts run under the interpreter their shebang names (bash for `#!/usr/bin/env bash`, otherwise `sh`), superseding the 2026-07-22 "runs with `sh`" note; transport order documented as load-bearing and enforced (a transport an earlier one already matches is rejected); `setupScript` / `setupScriptWindows` in URL form must resolve to the committed script beside the manifest, since dmcp cannot hash-verify anything else.
