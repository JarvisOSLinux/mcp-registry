# Registry Automation (GitHub Actions) — Suggestions

This document sketches a practical automation plan for a community-vetted, client-agnostic MCP registry where `registry.json` is the canonical entrypoint.

## Goals

- Keep `registry.json` and `servers/*/manifest.json` **valid, consistent, and easy to review** in PRs.
- Make “derived fields” (hashes, embeddings, signatures) **deterministic** and not hand-maintained.
- Enable consumers (individuals/companies) to apply policy like **“install only vetted”** and **“only run setup for vetted + verified”**.

## Data model recap (current direction)

- `registry.json`:
  - **`trustStatus`** per server entry for fast filtering.
  - **`integrity`** per server entry (`manifestSha256`, `setupScriptSha256`, `setupScriptWindowsSha256`) to bind trust decisions to exact content.
  - Reserved top-level **`signing`** fields (keyring + signatures).
  - **`embedding_spec`** (optional) — declares the model used to generate pre-computed vectors stored in manifests. See [EMBEDDING-SPEC.md](EMBEDDING-SPEC.md).
- `servers/*/manifest.json`:
  - **`trust`** object (details only; no status) for review references, checks performed, notes.
  - **`embeddings`** (optional) — pre-computed vectors keyed by model name. See [EMBEDDING-SPEC.md](EMBEDDING-SPEC.md).

## Upstream sourcing policy (no submodules by default)

This registry should not mirror upstream code by default. Instead, each server entry should reference upstream projects and pin them over time.

- **Default**: reference upstream via `manifest.json` → `source.url` (and later a pinned tag/commit).
- **Pinning**: prefer pinning to a reviewed upstream release/tag/commit so “what was vetted” is well-defined.
  - If you can’t encode a pin in the schema yet, record it in `manifest.json` → `trust.reviewReferences` / `trust.notes`.
- **Exceptions (when to vendor/submodule)**: only when you must patch upstream, preserve an upstream that might disappear, or need a strict “snapshot for audit” in-repo.

## Proposed GitHub Actions workflows

### 1) PR gate: validate + policy checks (required)

Run on every PR and push to default branch.

Suggested checks:
- JSON parse + schema-ish checks for `registry.json` and all manifests.
- `registry.json` server IDs match their entry `id`.
- `registry.json` `manifest` URLs correspond to existing `servers/<name>/manifest.json` (when applicable).
- `integrity.manifestSha256` matches the actual manifest content.
- If a manifest has a `setupScript` / `setupScriptWindows`, the matching `integrity.setupScriptSha256` / `integrity.setupScriptWindowsSha256` exists and matches the referenced script content — and the reverse, since a recorded hash whose script was deleted verifies nothing. Both directions are derived state, so the sync step below is what clears a hash after its script is removed.
- Disallow or flag “unsafe” changes (optional policy):
  - `trustStatus` cannot be changed to `"vetted"` without maintainer label/approval.
  - `setupScript` changes require additional review label.

### 2) Maintenance: regenerate derived fields (manual dispatch)

Run via “Run workflow” (workflow_dispatch) and/or on merge to main.

Actions:
- Recompute and update (dropping a setup-script hash whose script is gone):
  - `integrity.manifestSha256`
  - `integrity.setupScriptSha256`
  - `integrity.setupScriptWindowsSha256`
- Normalize manifests to ensure the `trust` details object exists (if you want strict uniformity).

Two safe modes:
- **Check-only** (fails CI if mismatched).
- **Auto-fix** (opens a PR with regenerated values).

### 3) Embeddings generation (manual dispatch or on merge)

Pre-compute embedding vectors for semantic server discovery. See **[EMBEDDING-SPEC.md](EMBEDDING-SPEC.md)** for the full schema, canonical text construction recipe, and the reference Python script.

Summary of what this workflow does:
- Reads each `servers/*/manifest.json`.
- Builds canonical text from `name`, `summary`, `keywords`, and `tools` (first 20).
- Calls the configured embedding model (default: `nomic-embed-text` via Ollama).
- Writes the vector into `manifest.embeddings["<model-name>"]`.
- Updates `registry.json` → `embedding_spec` to declare the model and dimensions used.
- Opens a PR with the updated manifests (never pushes directly to `main`).

This workflow is **optional** — consumers that don’t need semantic search ignore `embeddings` entirely and fall back to keyword filtering.

When to run:
- A new server is added.
- An existing server’s `name`, `summary`, `keywords`, or `tools` change.
- The embedding model version is upgraded (requires a full re-run for all servers).

## Signing & keyrings (recommended staged rollout)

### What “signing” means

Signing lets consumers verify:
- the registry metadata wasn’t tampered with, and
- it was approved by a trusted key (curator/maintainer).

### Suggested staged rollout

1. **Start with hashes only** (`integrity.*Sha256`), unsigned.
2. Add **signature verification support** in clients (optional).
3. Introduce **signatures** for curated/vetted releases.

### Key management caution

Avoid keeping the only signing key in CI long-term.

Safer options:
- Offline maintainer signing of release artifacts.
- Multiple signatures (e.g., 2-of-N maintainers) recorded in `registry.json` (or alongside it).
- If CI signing is used, restrict it to protected branches + hardened secrets policy.

### Algorithms (pick one later)

- **OpenPGP (GPG)**: similar mental model to pacman keyrings.
- **Ed25519 (minisign/signify-style)**: simple, modern, easy to verify.
- **Sigstore**: strong provenance, more ecosystem complexity.

This repo can reserve fields now and decide the scheme later without breaking consumers.

## Suggested conventions

- Treat `registry.json` as the **single entry point**.
- Keep `trustStatus` lightweight and reviewable; put detailed review notes in `manifest.json` `trust`.
- Prefer “fail CI if derived fields are wrong” over “silently mutate in CI”.
- If auto-fixing, do it by **opening a PR** rather than pushing directly to main.
