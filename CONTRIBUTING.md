# Contributing to mcp-registry

This guide explains how to submit a new MCP server to the JARVIS MCP registry.

The registry works like Homebrew or the AUR: you fork, add your server directory, and open a pull request. CI validates the manifest automatically, and maintainers review the code before merging.

---

## Table of Contents

1. [Before You Start](#before-you-start)
2. [Step 1 — Fork and Clone](#step-1--fork-and-clone)
3. [Step 2 — Create Your Server Directory](#step-2--create-your-server-directory)
4. [Step 3 — Write manifest.json](#step-3--write-manifestjson)
5. [Step 4 — Write setup.sh (Optional)](#step-4--write-setupsh-optional)
6. [Step 5 — Sync registry.json](#step-5--sync-registryjson)
7. [Step 6 — Open a Pull Request](#step-6--open-a-pull-request)
8. [Trust Levels](#trust-levels)
9. [Manifest Validation Requirements](#manifest-validation-requirements)
10. [Worked Example: git-summary-mcp](#worked-example-git-summary-mcp)
11. [Frequently Asked Questions](#frequently-asked-questions)

---

## Before You Start

- Your MCP server must already exist in a public repository (GitHub, GitLab, Sourcehut, etc.).
- The server must speak the [Model Context Protocol](https://modelcontextprotocol.io/) over at least one transport: stdio, SSE, or WebSocket.
- You must have a GitHub account to submit a PR.

---

## Step 1 — Fork and Clone

```bash
# Fork via GitHub UI, then:
git clone https://github.com/<your-username>/mcp-registry.git
cd mcp-registry
git checkout -b add/<your-server-id>
```

Use your reverse-domain server ID as the branch name, for example `add/com.github.alice.mcp.git-summary`.

---

## Step 2 — Create Your Server Directory

Server directories live under `servers/`. The directory name must be the server's unique ID in reverse-domain notation:

```
servers/
  com.github.alice.mcp.git-summary/
    manifest.json       ← required
    setup.sh            ← optional but recommended
```

**ID format:** `com.github.<github-username>.mcp.<server-name>`

Examples:
- `com.github.alice.mcp.git-summary`
- `io.github.bobcorp.mcp-weather`
- `org.modelcontextprotocol.server-filesystem`

The ID must be unique across the registry. Check `registry.json` before choosing one.

---

## Step 3 — Write manifest.json

The manifest describes how to install and run your server. Full field reference: [`docs/manifest-reference.md`](docs/manifest-reference.md).

### Minimal manifest (stdio server)

```json
{
  "version": "1.0.0",
  "scope": "user",
  "homepage": "https://github.com/alice/git-summary-mcp",
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
    "url": "https://github.com/alice/git-summary-mcp.git"
  },
  "setupScript": "setup.sh",
  "tools": [
    {
      "name": "summarize_commits",
      "description": "Summarize recent git commits for a repository path"
    }
  ],
  "configurableProperties": []
}
```

### Manifest for a remote SSE server

```json
{
  "version": "2.1.0",
  "scope": "user",
  "homepage": "https://github.com/alice/cloud-search-mcp",
  "transports": [
    {
      "type": "sse",
      "url": "https://api.alice.dev/mcp/sse",
      "description": "Cloud search endpoint"
    }
  ],
  "setupScript": "setup.sh",
  "tools": [
    {
      "name": "search",
      "description": "Search across indexed documents"
    }
  ],
  "configurableProperties": [
    {
      "key": "API_KEY",
      "label": "API Key",
      "description": "Obtain from https://alice.dev/settings",
      "sensitive": true,
      "required": true
    }
  ]
}
```

### Trust block (fill in honestly)

All submissions start at `community`. The `trust` block is informational — fill it in to help reviewers and users understand what your server does:

```json
"trust": {
  "reviewReferences": [
    "https://github.com/alice/git-summary-mcp"
  ],
  "checks": [
    "license: MIT",
    "source: https://github.com/alice/git-summary-mcp.git",
    "build: pip install -r requirements.txt"
  ],
  "notes": "Reads local git repos. Does not make network requests.",
  "securityNotes": "Requires filesystem read access to the target repo."
}
```

For the full trust level system, see [Trust Levels](#trust-levels) and [`docs/trust-levels.md`](docs/trust-levels.md).

---

## Step 4 — Write setup.sh (Optional)

`setup.sh` installs runtime dependencies and prepares the server for use. It runs in the server's install directory after `git clone` (for stdio servers) or locally (for SSE/WebSocket servers).

```bash
#!/usr/bin/env bash
set -euo pipefail

# Create and activate a Python virtual environment
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
echo "Setup complete."
```

Requirements:
- Must be a `bash` script (shebang: `#!/usr/bin/env bash`).
- Must be idempotent — safe to run more than once.
- Must not prompt for user input (it may run in an automated context).
- Should exit non-zero on failure.

---

## Step 5 — Sync registry.json

After creating your manifest, run the sync script to add your server to `registry.json` and compute integrity hashes:

```bash
# From the repo root
python3 scripts/sync_registry.py
```

This updates `registry.json` with your server's metadata and SHA-256 hashes. Commit the result.

> **Note:** The `embeddings` field is populated by a separate CI job (`generate-embeddings.yml`) after merge. You do not need to generate embeddings locally.

---

## Step 6 — Open a Pull Request

```bash
git add servers/com.github.alice.mcp.git-summary/ registry.json
git commit -m "feat(registry): add com.github.alice.mcp.git-summary"
git push origin add/com.github.alice.mcp.git-summary
```

Then open a PR against `main` on `JarvisOSLinux/mcp-registry`. Use the **Server Submission** PR template — it appears automatically when your branch name starts with `add/`.

Fill out the PR checklist completely. Incomplete submissions will be asked to revise before review begins.

---

## Trust Levels

All new servers start at `community`. The `official` tier is earned through maintainer review.

| Level | `trustStatus` value | Meaning |
|-------|---------------------|---------|
| Community | `"community"` | Newly submitted; passed automated validation but no human has audited the source. Installing means trusting the submitter, not the registry maintainers. Default for every submission. |
| Official | `"official"` | A maintainer reviewed the source and tool descriptions, confirmed the license, and pinned `source.url` to a specific commit. Suitable for security-sensitive and autonomous-agent use. |

`trustStatus` cannot be raised to `official` in a submission PR — a maintainer applies the `trust-approved` label, which the PR gate enforces. `"deprecated"` / `"removed"` are revocation states. For the full model and vetting flow, see [`docs/TRUST-MODEL.md`](docs/TRUST-MODEL.md).

---

## Manifest Validation Requirements

CI runs `python3 scripts/validate_registry.py` on every PR (the `validate-pr.yml` workflow), and `scripts/sync_registry.py --check` keeps derived fields honest. The gate fails if:

- `registry.json` integrity hashes do not match the submitted manifest files.
- A required field is missing, or `scope`/`trustStatus` uses a value outside the allowed set.
- An entry's `id` does not match its map key, or its `manifest` URL does not resolve locally.
- `trustStatus` is raised to `official` without a maintainer `trust-approved` label.

Beyond the automated check, reviewers verify:

| Requirement | Detail |
|-------------|--------|
| Required fields present | `version`, `scope`, `transports`, `tools` must be non-empty. |
| Valid JSON | The manifest must parse without errors. |
| Unique server ID | The directory name must not conflict with an existing entry in `registry.json`. |
| Stable server ID | The ID (directory name) must not change after submission — it is how dmcp tracks installations. |
| Valid `scope` | Must be `"user"` or `"system"`. |
| Transport fields | stdio requires `command`; SSE requires `url`; WebSocket requires `wsUrl`. |
| Tools list non-empty | At least one tool must be declared. |
| `setup.sh` is bash | If present, must start with `#!/usr/bin/env bash` or `#!/bin/bash`. |
| No credentials in manifest | API keys must use `configurableProperties`, never hardcoded values. |

---

## Worked Example: git-summary-mcp

This example walks through submitting a Python stdio server from scratch.

### 1. Fork and branch

```bash
git clone https://github.com/<you>/mcp-registry.git
cd mcp-registry
git checkout -b add/com.github.alice.mcp.git-summary
```

### 2. Create the server directory

```bash
mkdir -p servers/com.github.alice.mcp.git-summary
```

### 3. Write manifest.json

Create `servers/com.github.alice.mcp.git-summary/manifest.json`:

```json
{
  "version": "1.0.0",
  "scope": "user",
  "name": "Git Summary MCP",
  "summary": "Summarize recent git commits for a repository",
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
      "description": "List all branches in a git repository"
    }
  ],
  "configurableProperties": [],
  "trust": {
    "reviewReferences": [
      "https://github.com/alice/git-summary-mcp"
    ],
    "checks": [
      "license: MIT",
      "source: https://github.com/alice/git-summary-mcp.git",
      "build: pip install -r requirements.txt"
    ],
    "notes": "Reads local git history. No network calls.",
    "securityNotes": "Requires read access to target git repositories."
  }
}
```

### 4. Write setup.sh

Create `servers/com.github.alice.mcp.git-summary/setup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
.venv/bin/pip install --quiet gitpython
echo "git-summary-mcp: setup complete."
```

Make it executable:

```bash
chmod +x servers/com.github.alice.mcp.git-summary/setup.sh
```

### 5. Sync registry.json

```bash
python3 scripts/sync_registry.py
```

Expected output:
```
  com.github.alice.mcp.git-summary: manifestSha256 updated
  com.github.alice.mcp.git-summary: setupScriptSha256 updated
  com.github.alice.mcp.git-summary: 'name' synced from manifest
  com.github.alice.mcp.git-summary: 'summary' synced from manifest
  com.github.alice.mcp.git-summary: 'keywords' synced from manifest
registry.json updated.
```

### 6. Commit and push

```bash
git add servers/com.github.alice.mcp.git-summary/ registry.json
git commit -m "feat(registry): add com.github.alice.mcp.git-summary"
git push origin add/com.github.alice.mcp.git-summary
```

### 7. Open PR

Open a PR to `JarvisOSLinux/mcp-registry`. The server submission PR template appears automatically. Fill in the checklist and submit.

---

## Frequently Asked Questions

**Q: My server isn't on GitHub. Can I still submit?**
Yes. Any public Git URL works in `source.url`. The setup script URL in `setupScript` can also be a raw HTTPS URL pointing to any public host.

**Q: Can I submit a server I don't own?**
Yes, as long as the upstream license allows redistribution and you clearly note the upstream source in `trust.reviewReferences`. You are responsible for keeping the submission current.

**Q: Do I need to generate embeddings before submitting?**
No. The `generate-embeddings.yml` CI job runs after merge and populates the `embeddings` field automatically.

**Q: What if the sync script fails?**
Ensure you are running from the repo root and that your `manifest.json` is valid JSON. Use `python3 -c "import json; json.load(open('servers/<id>/manifest.json'))"` to check.

**Q: How long does review take?**
Community submissions are typically reviewed within a few days. Verified status requires a deeper review and may take longer.

**Q: Can I update an existing server entry?**
Yes. Edit the manifest, re-run `python3 scripts/sync_registry.py`, and open a PR. CI will recompute hashes.
