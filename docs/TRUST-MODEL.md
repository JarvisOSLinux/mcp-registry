# JARVIS MCP Trust Model

**Status: canonical.** This document is the single source of truth for how trust
works in the JARVIS MCP ecosystem. Where it disagrees with
[`trust-levels.md`](trust-levels.md), [`REGISTRY-AUTOMATION.md`](REGISTRY-AUTOMATION.md),
`MCP-REGISTRY-GUIDE.md`, or `CLAUDE.md`, this document wins; those are being
reconciled to match it.

It defines *what* trust means, *who* is allowed to do what, and *which component
enforces each rule*. It is written to be cited directly by the research paper, so
every claim here is marked **implemented**, **partial**, or **proposed** against
the code as it actually stands.

---

## 1. The core principle

> **Trust is a property of the source, and the source list is human-controlled.
> An autonomous agent inherits the trust a human has configured and cannot
> expand it.**

This is the same trust model as Arch Linux's package system, adapted to MCP
servers — which are, after all, installable applications:

| Arch Linux | JARVIS MCP |
|---|---|
| `pacman.conf` repositories | dmcp `sources.list` registries |
| `[core]` / `[extra]` (signed, curated) | **official**-tier registry entries |
| AUR (submitted, you review the PKGBUILD) | **community**-tier entries / user-added sources |
| pacman + an AUR helper | `dmcp` (installs from either, treats them differently) |
| package signature / checksum | `integrity.manifestSha256` + pinned `source` commit |

The registry (`mcp-registry`) is the **catalog and the vetting venue**. dmcp is
the **client that enforces the trust decisions the catalog records**. Neither
invents trust on its own: the registry records what was reviewed, and dmcp
verifies that what it installs matches what was reviewed.

---

## 2. Two actors, two trust paths

Trust is enforced differently depending on *who is driving*, because the human
operator and the autonomous agent have different threat models.

### 2.1 Human operator — `dmcp` CLI

The human is in full control and is trusted to make their own risk decisions.
Via the CLI they **may**:

- add any registry source (`dmcp sources add <url>`),
- install any server by ID or by direct URL,
- install a **community**-tier server after an explicit acknowledgment,
- run anything they installed.

This is deliberate and is **not** a gap. dmcp is a general-purpose MCP manager;
restricting the human here would break that. Installing an unreviewed server from
a source you added yourself is your risk — exactly as `makepkg` on an AUR package
is the user's risk. The registry's job is to make the risk *legible* (show the
manifest and `setup.sh` before running them), not to forbid it.

### 2.2 Autonomous agent — Project-JARVIS via `dmcp serve`

The LLM talks to dmcp only through the MCP tool surface exposed by
`dmcp serve`. That surface is **confined to the human's configured sources**, and
that source-confinement — not a tier allowlist — is the mitigation for Threats #1
and #3 (see §6). The agent:

- installs servers **by ID only**, from **already-configured** registries
  — `install_server` calls `fetch_server_from_registry(paths, id)` and has no
  URL parameter (`src/serve.rs:156`), **implemented**;
- has **no tool to add, remove, or reorder sources** — the source list is not
  reachable from the MCP surface, **implemented (by absence)**;
- may install **both `community` and `official`** entries from those sources —
  every registry entry is PR-vetted, so `community` is *reviewed-but-not-
  maintainer-endorsed*, not *unvetted*. `community` installs surface a
  "not maintainer-reviewed" warning; a deployment that wants official-only can set
  `DMCP_AGENT_ALLOW_COMMUNITY=0` (human-controlled config, never the MCP surface),
  **implemented** (`install.rs::agent_trust_gate`). `deprecated` / `removed`
  entries are never agent-installable.

> **The source-confinement is now an explicit, tested invariant.** It no longer
> holds "by accident": `serve.rs` exposes no `url` parameter on `install_server`
> and no `add_source` tool, and that is locked by an invariant test (dmcp PR #24)
> so a future convenience change cannot silently dissolve the boundary. The trust
> tier is a *within-source* assurance signal layered on top; the source list
> itself remains the primary control and is human-only.

---

## 3. Two trust tiers

Every server entry in `registry.json` carries a `trustStatus`. There are **two
tiers** plus a revocation state. This supersedes the older three-level scale.

### `community`
**Default for every new submission.** The manifest passed automated validation
(valid JSON, IDs match, hashes correct, no self-promotion of trust) but **no
human has endorsed the source code.** Installing a `community` server means
trusting the submitter, not the registry maintainers.

- Human CLI: installable with a printed "not maintainer-reviewed — you are
  trusting the submitter" warning (no interactive acknowledgment, and the
  manifest/setup.sh are not displayed — rendering them pre-install is a
  desired hardening, currently *proposed*).
- Autonomous agent: **installable, with a "not maintainer-reviewed" warning** —
  because every entry is PR-vetted before it reaches the registry. Deployments
  that want official-only can opt in via `DMCP_AGENT_ALLOW_COMMUNITY=0`.

### `official`
**A maintainer reviewed and endorsed it.** To reach `official` a maintainer has:

1. read the source and found no malicious or deceptive behavior,
2. confirmed the declared license is accurate,
3. confirmed the `tools` descriptions match what the tools actually do
   (this is the Threat #3 check — misleading tool descriptions),
4. **pinned `source.url` to a specific commit or tag** and recorded it,
5. left a review reference (reviewer handle + PR/issue) in the entry.

- Human CLI: installs cleanly.
- Autonomous agent: installable.

### Revocation (`deprecated` / `removed`)
Orthogonal to the two tiers. Set when a security issue is found, the upstream
disappears or is taken over, or the server misbehaves. Entries are kept (not
hard-deleted) to avoid breaking existing installs. dmcp warns on `deprecated` and
refuses `removed` on the human CLI, and refuses **both** on the agent path.
**implemented** (`install.rs::cli_trust_gate` / `agent_trust_gate`).

### Migration from legacy data — complete
All legacy `unreviewed`/`vetted` values have been collapsed; `registry.json`
now holds 19 entries, all `community`. No entry has yet been promoted to
`official`. (`validate_registry.py` rejects any value outside
{community, official, deprecated, removed}.)

---

## 4. The vetting mechanism (`community` → `official`)

This is the "community review earns official status" workflow, encoded so that
trust is **bound to a reviewed git state** rather than a hand-typed label. This
is what makes the mitigation real enough to publish.

```
 developer opens submission PR (adds servers/<id>/manifest.json [+ setup.sh])
        │
        ▼
 PR-gate CI (required, blocking):                              [implemented]
   • registry.json parses; per-entry required fields present;
     trustStatus/scope enums valid; entry id == registry map key
   • manifest URL resolves to an existing servers/<id>/manifest.json file
     (the manifest itself is hash-checked, not schema-validated)
   • integrity.manifestSha256 / setupScriptSha256 / setupScriptWindowsSha256
     recomputed and match, in both directions (a recorded hash needs its script)
   • trustStatus is REQUIRED and CANNOT be (or become) "official" without a
     maintainer applying the `trust-approved` label
        │
        ▼
 trustStatus = community  (installable by humans and the agent; the agent sees
                           a "not maintainer-reviewed" warning)
        │
        ▼
 maintainer review: read source, verify license, verify tool descriptions,
   pin source.url to a commit, record reviewer                 [process]
        │
        ▼
 maintainer merges promotion → trustStatus = official
   with integrity.manifestSha256 + pinned source commit recorded
```

The crucial CI rule — **`trustStatus` cannot be raised except through a
maintainer-gated approval** — is what stops a submitter from marking their own
server `official`. This is now built: `.github/workflows/validate-pr.yml` runs
`scripts/validate_registry.py` on every PR to `main` and fails the check if an
entry is promoted to `official` without a maintainer applying the `trust-approved`
label (schema, id/scope, and integrity hashes are validated in the same gate).

---

## 5. The integrity chain and dmcp enforcement

The tier label is only meaningful if the client verifies that the bytes it
installs are the bytes that were reviewed. The full chain:

```
reviewed PR  →  merged entry records manifestSha256 + pinned source commit
             →  dmcp verifies manifestSha256 against the RAW fetched bytes
             →  dmcp clones source at the pinned commit (not branch HEAD)
             →  agent is confined to the human's configured sources (community + official)
```

Enforcement status in dmcp today:

| Requirement | Where | Status |
|---|---|---|
| Verify `setupScriptSha256` before running `setup.sh` | `install.rs` | **implemented** |
| Verify `integrity.manifestSha256` on install | `install.rs` | **implemented** — hashed against the RAW fetched bytes before parse/merge (`verify_manifest_hash`) |
| Read `trustStatus`; gate the agent path | `install.rs` / `serve.rs` | **implemented** — `agent_trust_gate` allows `official`, warns+allows `community` (opt-out via `DMCP_AGENT_ALLOW_COMMUNITY=0`), refuses `deprecated`/`removed` |
| Pin `source.url` to a commit (not `--depth 1` HEAD) | `install.rs` | **implemented** — clones and checks out the recorded commit, then verifies HEAD matches the pin |
| Warn on `deprecated`, refuse `removed` | `install.rs` | **implemented** — `cli_trust_gate` / `agent_trust_gate` |
| Serve surface exposes no URL-install / no add-source | `serve.rs` | **implemented + tested** — locked by an invariant test (PR #24) |
| Validate embedding dimension against `embedding_spec` | `sync_index.rs` | **missing** — mismatched vectors still silently score 0.0 |

The integrity + trust rows — the substance of the Threat #1 mitigation — have now
landed, so the registry's trust metadata is **recorded *and* enforced by the
client**. One gap remains in this table (embedding-dimension validation), and the
PR-gate above is what keeps the recorded metadata honest at submission time.

---

## 6. Mapping to the threat taxonomy

Honest status of the mitigations this document underpins:

- **Threat #1 — Malicious MCP Servers.** Mitigation = a **PR-gated registry**
  (no anonymous upload; every tier reviewed before inclusion) + **agent
  source-confinement** (installs only from human-configured sources, by id, never
  a URL — a tested invariant) + the **client integrity chain** (`manifestSha256`
  over raw bytes + pinned-commit clones). The trust *tiers* layer higher assurance
  on top (`official` = maintainer-endorsed) but are not the boundary — the boundary
  is "reviewed-before-inclusion, from a confined source, byte-for-byte verified."
  Status: **largely implemented** — PR-gate, integrity verification, commit
  pinning, revocation, and the confinement invariant are all in code; embedding-
  dimension validation (§5) is the one remaining gap.
- **Threat #3 — Misleading MCP Server Usage.** Mitigation = the `official`-tier
  review step that verifies tool descriptions match behavior, plus the structured
  tool schema. Status: **partial** — the review criterion is defined here and the
  structured-schema half exists in the manifest format; the maintainer-review
  process itself is the human step that remains ongoing.

Threats #2 (Prompt Injection), #4/#5 (sudo), and #6 (Bloated Context) are
mitigated elsewhere (dispatch, the OS embodiment, contextor) and are out of scope
for this document.

---

## 7. Non-goals (deliberate)

- **Restricting the human operator.** The CLI can add any source and install any
  tier. This keeps dmcp a general tool; the operator owns that risk.
- **Signing, today.** The `signing` block in `registry.json` is a reserved stub.
  Hashes + pinned commits come first; signatures are a later staged rollout
  (`REGISTRY-AUTOMATION.md` §"Signing"). Decide **implement vs. remove the stub**
  before publication.
- **Policing servers a user runs directly.** Out of scope by design.

---

## 8. Open decisions

1. **Naming.** `community`/`official` is used here. If the paper prefers to keep
   the word `verified`, rename `official` → `verified` consistently everywhere
   (data + docs + dmcp). Pick one and only one.
2. ~~**Default agent policy source.**~~ **Resolved (#28).** The agent is **not**
   official-only: it installs `community` + `official` from configured sources,
   because all tiers are PR-vetted. Official-only is an *opt-in* via the
   `DMCP_AGENT_ALLOW_COMMUNITY` environment variable — human-controlled config,
   never reachable from the MCP surface.
3. **Signing scheme** (if adopted): OpenPGP vs. Ed25519/minisign vs. Sigstore.
4. ~~**Where `community` servers live.**~~ **Resolved (#28).** `community` entries
   live in the registry alongside `official` ones and are installable by both the
   human and the agent. The **source-gate** (confinement to human-configured
   registries) is the primary control; the tier is a within-source assurance
   signal, not the boundary.

## Changelog — corrected claims

*2026-07-22:* migration marked complete (19 entries, all `community`); community-tier install described as a printed warning (no interactive acknowledgment or manifest display — that hardening is proposed); PR-gate bullets matched to `validate_registry.py` (manifest is hash-checked, not schema-validated; `trustStatus` is required).
