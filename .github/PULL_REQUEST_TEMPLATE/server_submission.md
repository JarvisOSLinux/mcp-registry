## Server Submission

**Server ID:** `com.github.<username>.mcp.<server-name>`
**Upstream repository:** <!-- link to the server's source repo -->
**Short description:** <!-- one sentence, shown in the registry listing -->

---

### Submission Checklist

#### Files

- [ ] Added `servers/<server-id>/manifest.json`
- [ ] Added `servers/<server-id>/setup.sh` (or left absent with justification below)
- [ ] Ran `python3 scripts/sync_registry.py` and committed the updated `registry.json`

#### manifest.json

- [ ] `version` field is a valid semver string (e.g. `"1.0.0"`)
- [ ] `scope` is `"user"` or `"system"`
- [ ] `platforms` is non-empty and lists only OSes you actually ran the server on (`"linux"`, `"darwin"`, `"windows"`)
- [ ] `transports` array is non-empty and each entry has the correct required fields:
  - stdio: `type`, `command`, `args`
  - SSE: `type`, `url`
  - WebSocket: `type`, `wsUrl`
- [ ] `tools` array is non-empty; every tool has `name` and `description`
- [ ] `source.url` points to a public, reachable Git repository (stdio servers only)
- [ ] No API keys, tokens, or credentials are hardcoded — secrets use `configurableProperties`
- [ ] `trust.reviewReferences` includes the upstream source URL
- [ ] `trust.checks` lists license, source, and build steps

#### Server ID

- [ ] Directory name matches the reverse-domain convention: `com.github.<username>.mcp.<server-name>`
- [ ] The ID does not conflict with an existing entry in `registry.json`

#### setup.sh (if present)

- [ ] Starts with `#!/usr/bin/env bash` or `#!/bin/bash`
- [ ] Idempotent (safe to run multiple times)
- [ ] Does not prompt for interactive input
- [ ] Exits non-zero on error (`set -e` or explicit checks)
- [ ] Is executable (`chmod +x`)

#### Content

- [ ] I have tested this server locally and it works as described
- [ ] The `tools` descriptions accurately reflect what the server does
- [ ] The upstream license permits inclusion in a public registry

---

### Notes for Reviewers

<!-- Anything that helps the reviewer: unusual build steps, known limitations,
     why system scope is needed, security considerations, etc. -->

---

### Trust Level Requested

- [ ] `unreviewed` (default — no action needed)
- [ ] `community` — I believe this is ready for community trust. Evidence: <!-- link to audit, test results, etc. -->
- [ ] `verified` — Requesting deep review for verified status.

See [`docs/trust-levels.md`](../docs/trust-levels.md) for promotion criteria.
