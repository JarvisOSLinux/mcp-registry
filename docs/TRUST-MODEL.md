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
`dmcp serve`. That surface is **confined**, and the confinement is the mitigation
for Threats #1 and #3 (see §6). The agent:

- installs servers **by ID only**, from **already-configured** registries
  — `install_server` calls `fetch_server_from_registry(paths, id)` and has no
  URL parameter (`src/serve.rs:156`), **implemented**;
- has **no tool to add, remove, or reorder sources** — the source list is not
  reachable from the MCP surface, **implemented (by absence)**;
- must, by policy, install **official**-tier servers only unless a human has
  explicitly widened the policy — **proposed** (dmcp does not yet read
  `trustStatus`; see §5).

> **The confinement currently holds by accident, not by contract.** It is true
> today only because nobody has added a `url` field to `install_server` or an
> `add_source` MCP tool. The single most important hardening task is to make this
> an **explicit, tested invariant** so a future convenience change cannot silently
> dissolve the boundary. See issue: *"dmcp: assert serve surface cannot add
> sources or install from URL."*

---

## 3. Two trust tiers

Every server entry in `registry.json` carries a `trustStatus`. There are **two
tiers** plus a revocation state. This supersedes the older three-level scale.

### `community`
**Default for every new submission.** The manifest passed automated validation
(valid JSON, IDs match, hashes correct, no self-promotion of trust) but **no
human has endorsed the source code.** Installing a `community` server means
trusting the submitter, not the registry maintainers.

- Human CLI: installable after an explicit acknowledgment that shows the manifest
  and any `setup.sh`.
- Autonomous agent: **not installed by default.**

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
hard-deleted) to avoid breaking existing installs; dmcp should warn on
`deprecated` and refuse `removed`. **proposed** (dmcp reads neither today).

### Migration from current data
`registry.json` currently holds `unreviewed` (10) and `vetted` (7), and
`trust-levels.md` documents `unreviewed`/`community`/`verified`. Collapse to:

| Old value(s) | New value |
|---|---|
| `unreviewed`, (absent) | `community` |
| `vetted`, `verified`, `community` (old "reviewed" sense) | `official` |
| `deprecated`, `removed` | unchanged |

Tracked in issue: *"mcp-registry: collapse trustStatus to community/official and
backfill all entries."*

---

## 4. The vetting mechanism (`community` → `official`)

This is the "community review earns official status" workflow, encoded so that
trust is **bound to a reviewed git state** rather than a hand-typed label. This
is what makes the mitigation real enough to publish.

```
 developer opens submission PR (adds servers/<id>/manifest.json [+ setup.sh])
        │
        ▼
 PR-gate CI (required, blocking):                              [proposed]
   • JSON + schema validation of registry.json and the manifest
   • entry id == manifest id; manifest URL resolves to the file
   • integrity.manifestSha256 / setupScriptSha256 recomputed and match
   • trustStatus MUST be "community" (or absent) on a submission PR —
     it CANNOT be raised to "official" without a maintainer approval label
        │
        ▼
 trustStatus = community  (installable by humans, invisible to the agent)
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
server `official`. `REGISTRY-AUTOMATION.md` §3.1 already proposes this; it is not
yet built. The existing `sync-registry.yml` runs only on push-to-`main`, not on
PRs, so there is currently **no PR gate at all**.

---

## 5. The integrity chain and dmcp enforcement

The tier label is only meaningful if the client verifies that the bytes it
installs are the bytes that were reviewed. The full chain:

```
reviewed PR  →  merged entry records manifestSha256 + pinned source commit
             →  dmcp verifies manifestSha256 against the RAW fetched bytes
             →  dmcp clones source at the pinned commit (not branch HEAD)
             →  agent is confined to configured official sources
```

Enforcement status in dmcp today:

| Requirement | Where | Status |
|---|---|---|
| Verify `setupScriptSha256` before running `setup.sh` | `install.rs` | **implemented** |
| Verify `integrity.manifestSha256` on install | `install.rs` | **missing** — never read; raw bytes are discarded by `resp.json()` + merge before any check could run |
| Read `trustStatus`; gate agent path to `official` | `install.rs` / `serve.rs` | **missing** — field never read anywhere in `src/` |
| Pin `source.url` to a commit (not `--depth 1` HEAD) | `install.rs` | **missing** — clones branch HEAD |
| Warn on `deprecated`, refuse `removed` | `install.rs` | **missing** |
| Serve surface exposes no URL-install / no add-source | `serve.rs` | **implemented, untested** — make it an invariant test |
| Validate embedding dimension against `embedding_spec` | `sync_index.rs` | **missing** — mismatched vectors silently score 0.0 |

The first four rows are the substance of the Threat #1 mitigation. Until they
land, the registry's trust metadata is **recorded but not enforced**, and the
paper must describe it that way.

---

## 6. Mapping to the threat taxonomy

Honest status of the mitigations this document underpins:

- **Threat #1 — Malicious MCP Servers.** Mitigation = the community-vetted
  registry + client integrity verification + agent source-confinement.
  Status: **partial.** Vetting *venue* and metadata exist; the PR-gate and dmcp's
  integrity/trust enforcement do not yet. Source-confinement of the agent is
  **implemented** (by absence) and needs to become a tested invariant.
- **Threat #3 — Misleading MCP Server Usage.** Mitigation = the `official`-tier
  review step that verifies tool descriptions match behavior, plus the structured
  tool schema. Status: **partial** — the review criterion is defined here; the
  structured-schema half exists in the manifest format.

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
2. **Default agent policy source.** Is "agent installs official-only" a
   hard-coded default in `dmcp serve`, or a config value a deployment sets? A
   config value is more flexible but adds a knob the agent must not be able to
   flip — it must live in human-controlled config, never the MCP surface.
3. **Signing scheme** (if adopted): OpenPGP vs. Ed25519/minisign vs. Sigstore.
4. **Where `community` servers live.** Either as `community`-tier entries inside
   the official registry (this document's assumption), or only as open PRs / in
   separate user-added sources. The former lets a human opt into them by tier;
   the latter keeps the official registry uniformly `official`. This choice
   determines whether dmcp's tier-gate or its source-gate is the primary control.
