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
  calculator-py/
  calculator-rust/
  jarvis-shell/
  hello-sse/
  hello-ws/
  ...
scripts/
  sync_registry.py     Recompute integrity hashes
  generate_embeddings.py  Generate semantic embeddings via Ollama
docs/
  EMBEDDING-SPEC.md    Embedding format and generation spec
  REGISTRY-AUTOMATION.md  CI/CD automation strategy
  REGISTRY-AS-SERVICE.md  Hosting patterns
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

## Adding a Server

See [MCP-REGISTRY-GUIDE.md](MCP-REGISTRY-GUIDE.md) for the full specification.

In short:
1. Create `servers/<your-server>/manifest.json` with metadata and transport config
2. Optionally add a `setup.sh` for dependency installation
3. Run `python scripts/sync_registry.py` to update `registry.json`
4. Submit a PR

## Integrity

Each server entry includes SHA-256 hashes of its manifest and setup script.
The `sync_registry.py` script recomputes these on every change. The CI
workflow (`sync-registry.yml`) runs this automatically on PRs.

## Embeddings

Server descriptions are embedded using `nomic-embed-text` (via Ollama) for
semantic search in dmcp. The `generate_embeddings.py` script handles this;
the `generate-embeddings.yml` workflow runs it on demand.

See [docs/EMBEDDING-SPEC.md](docs/EMBEDDING-SPEC.md) for the vector format.

## License

GPL-3.0
