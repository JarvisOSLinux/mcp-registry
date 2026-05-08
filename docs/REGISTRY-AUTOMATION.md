# Registry Automation (GitHub Actions) — Suggestions

This document sketches a practical automation plan for a community-vetted, client-agnostic MCP registry where `registry.json` is the canonical entrypoint.

## Goals

- Keep `registry.json` and `servers/*/manifest.json` **valid, consistent, and easy to review** in PRs.
- Make “derived fields” (hashes, embeddings, signatures) **deterministic** and not hand-maintained.
- Enable consumers (individuals/companies) to apply policy like **“install only vetted”** and **“only run setup for vetted + verified”**.

## Data model recap (current direction)

- `registry.json`:
  - **`trustStatus`** per server entry for fast filtering.
  - **`integrity`** per server entry (`manifestSha256`, `setupScriptSha256`) to bind trust decisions to exact content.
  - Reserved top-level **`signing`** fields (keyring + signatures).
- `servers/*/manifest.json`:
  - **`trust`** object (details only; no status) for review references, checks performed, notes.

## Proposed GitHub Actions workflows

### 1) PR gate: validate + policy checks (required)

Run on every PR and push to default branch.

Suggested checks:
- JSON parse + schema-ish checks for `registry.json` and all manifests.
- `registry.json` server IDs match their entry `id`.
- `registry.json` `manifest` URLs correspond to existing `servers/<name>/manifest.json` (when applicable).
- `integrity.manifestSha256` matches the actual manifest content.
- If a manifest has a `setupScript`, `integrity.setupScriptSha256` exists and matches the referenced script content.
- Disallow or flag “unsafe” changes (optional policy):
  - `trustStatus` cannot be changed to `"vetted"` without maintainer label/approval.
  - `setupScript` changes require additional review label.

### 2) Maintenance: regenerate derived fields (manual dispatch)

Run via “Run workflow” (workflow_dispatch) and/or on merge to main.

Actions:
- Recompute and update:
  - `integrity.manifestSha256`
  - `integrity.setupScriptSha256`
- Normalize manifests to ensure the `trust` details object exists (if you want strict uniformity).

Two safe modes:
- **Check-only** (fails CI if mismatched).
- **Auto-fix** (opens a PR with regenerated values).

### 3) Optional: embeddings generation (manual dispatch or on merge)

If you want vector search (server discovery, semantic matching), generate embeddings from canonical text:
- `name`, `summary`, `keywords`
- `tools[].name`, `tools[].description`
- (optional) `categories`

Recommendation:
- Store output in a separate artifact file, e.g. `embeddings.json` keyed by server ID.
- Keep it optional so consumers who don’t need embeddings can ignore it.

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

