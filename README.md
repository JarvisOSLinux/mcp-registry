# mcp-registry

**Server catalog for the JARVIS MCP ecosystem.**

A curated registry of MCP (Model Context Protocol) servers that can be
discovered, browsed, and installed via [dmcp](https://github.com/JarvisOSLinux/dmcp).
Each server entry includes metadata, integrity hashes, and optional embedding
vectors for semantic search.

## Structure

```
registry.json          Main index — all server metadata and embeddings
servers/
  calculator-ts/
    manifest.json      Install/run metadata for this server
    setup.sh           Optional setup script
    setup.ps1          Optional PowerShell setup script for Windows hosts
  calculator-py/
  calculator-rust/
  jarvis-shell/
  hello-sse/
  hello-ws/
  ...
scripts/
  sync_registry.py     Recompute integrity hashes + sync derived fields (name, summary, keywords); --check for CI
  validate_registry.py Validate schema, hashes, and trust tiers (PR gate)
  generate_embeddings.py  Generate semantic embeddings via Ollama
docs/
  EMBEDDING-SPEC.md    Embedding format and generation spec
  REGISTRY-AUTOMATION.md  CI/CD automation strategy
  REGISTRY-AS-SERVICE.md  Hosting patterns
  TRUST-MODEL.md       Trust tiers and revocation
  manifest-reference.md  Full manifest field reference
  trust-levels.md      Trust level definitions
```

## How It Works

1. Add this registry URL to your MCP sources:
   ```bash
   dmcp sources add https://raw.githubusercontent.com/JarvisOSLinux/mcp-registry/main/registry.json
   ```
2. Browse available servers:
   ```bash
   dmcp browse
   ```
3. Install a server:
   ```bash
   dmcp install com.github.yakupatahanov.mcp.calculator-ts
   ```

## Trust Model

Each entry carries a `trustStatus`: `community` (default for new servers) or
`official`; `deprecated`/`removed` mark revocation. dmcp warns on `community`
installs ("you are trusting the submitter") and refuses `removed`; the
autonomous agent path is stricter still. Promotion to `official` requires a
maintainer review and the `trust-approved` PR label — see
`docs/TRUST-MODEL.md`.

## Adding a Server

See [MCP-REGISTRY-GUIDE.md](MCP-REGISTRY-GUIDE.md) for the full specification.

In short:
1. Create `servers/<your-server>/manifest.json` with metadata and transport config
2. Optionally add a `setup.sh` for dependency installation (plus a `setup.ps1` if you vetted it on Windows)
3. Add an entry for your server to `registry.json` (id, scope, trustStatus, manifest URL pointing at `servers/<id>/manifest.json`), then run `python scripts/sync_registry.py` to fill integrity hashes and sync derived fields
4. Submit a PR

**One capability, one server.** A server supported on several operating systems
is one entry listing them all in `platforms`, with one transport per platform
where the launch command differs — never a per-OS family of near-identical
entries. See "One Capability, One Server" in
[MCP-REGISTRY-GUIDE.md](MCP-REGISTRY-GUIDE.md).

## Integrity

Each server entry includes SHA-256 hashes of its manifest and of every setup
script it ships — `setup.sh` and, for Windows hosts, `setup.ps1` — so dmcp can
verify whichever script it is about to run. The `sync-registry.yml` workflow
recomputes hashes automatically when manifests or setup scripts change on `main`
(or on manual dispatch) and opens an automated PR with the updated
`registry.json`. Pull requests are gated by `validate-pr.yml`, which runs
`scripts/validate_registry.py` (schema, trust-tier, and integrity checks).

## Embeddings

Server descriptions are embedded using `nomic-embed-text` (via Ollama) for
semantic search in dmcp. The `generate_embeddings.py` script handles this;
the `generate-embeddings.yml` workflow runs it on demand.

See [docs/EMBEDDING-SPEC.md](docs/EMBEDDING-SPEC.md) for the vector format.

## License

GPL-3.0
