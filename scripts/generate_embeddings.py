#!/usr/bin/env python3
"""generate_embeddings.py — incremental embedding generation for registry manifests.

For each server manifest, builds canonical text from name/summary/keywords/tools
and computes its SHA-256. If the stored hash under embeddings[model]["hash"]
already matches, the server is skipped — Ollama is only called for new or changed
servers. When the embedding model changes, all servers are missing the new key
and get re-embedded automatically.

Embedding format stored in each manifest.json:
  "embeddings": {
    "nomic-embed-text": {
      "v": [0.031, -0.184, ...],
      "hash": "sha256-of-canonical-text"
    }
  }

Usage:
  python3 scripts/generate_embeddings.py
  python3 scripts/generate_embeddings.py --model nomic-embed-text
  python3 scripts/generate_embeddings.py --force   # re-embed even if hash matches

Requires Ollama running locally (or OLLAMA_URL env var pointing elsewhere).
"""
import json
import hashlib
import os
import pathlib
import sys
import argparse
import urllib.request
import urllib.error

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/embeddings")


def canonical_text(manifest: dict) -> str:
    parts = []
    if manifest.get("name"):
        parts.append(manifest["name"].strip() + ".")
    if manifest.get("summary"):
        parts.append(manifest["summary"].strip() + ".")
    kws = manifest.get("keywords", [])
    if kws:
        parts.append("Keywords: " + ", ".join(kws) + ".")
    tools = manifest.get("tools", [])[:20]
    if tools:
        tool_strs = []
        for t in tools:
            name = t["name"] if isinstance(t, dict) else str(t)
            desc = t.get("description", "").strip() if isinstance(t, dict) else ""
            tool_strs.append(f"{name}: {desc}" if desc else name)
        parts.append("Tools: " + "; ".join(tool_strs) + ".")
    return " ".join(parts)


def canonical_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def embed(text: str, model: str) -> list[float]:
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["embedding"]


def dir_from_url(url: str) -> str | None:
    if "/servers/" in url and url.endswith("/manifest.json"):
        return url.split("/servers/")[1].split("/")[0]
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="nomic-embed-text", help="Ollama model name")
    parser.add_argument(
        "--force", action="store_true", help="Re-embed even if canonical hash matches"
    )
    args = parser.parse_args()
    model = args.model

    registry = json.loads(REGISTRY.read_text())
    updated = 0
    skipped = 0
    failed = 0

    for server_id, entry in registry["servers"].items():
        dir_name = dir_from_url(entry.get("manifest", ""))
        if not dir_name:
            continue

        manifest_path = SERVERS_DIR / dir_name / "manifest.json"
        if not manifest_path.exists():
            continue

        manifest = json.loads(manifest_path.read_text())
        text = canonical_text(manifest)
        if not text.strip():
            print(f"  SKIP {server_id}: no canonical text (missing name/summary)")
            continue

        chash = canonical_hash(text)
        existing = manifest.get("embeddings", {}).get(model)
        if not args.force and isinstance(existing, dict) and existing.get("hash") == chash:
            print(f"  SKIP {server_id} (unchanged)")
            skipped += 1
            continue

        print(f"  EMBED {server_id} ...", end=" ", flush=True)
        try:
            vector = embed(text, model)
        except urllib.error.URLError as e:
            print(f"FAILED: {e}")
            failed += 1
            continue
        except Exception as e:
            print(f"FAILED: {e}")
            failed += 1
            continue

        manifest.setdefault("embeddings", {})[model] = {"v": vector, "hash": chash}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"done ({len(vector)}d)")
        updated += 1

    # Update embedding_spec in registry.json if any vectors were generated
    if updated > 0:
        dims = None
        for entry in registry["servers"].values():
            dir_name = dir_from_url(entry.get("manifest", ""))
            if not dir_name:
                continue
            mp = SERVERS_DIR / dir_name / "manifest.json"
            if mp.exists():
                m = json.loads(mp.read_text())
                emb = m.get("embeddings", {}).get(model)
                if isinstance(emb, dict) and "v" in emb:
                    dims = len(emb["v"])
                    break

        registry["embedding_spec"] = {
            "model": model,
            "dimensions": dims,
            "provider": "ollama",
            "canonical_fields": ["name", "summary", "keywords", "tools"],
        }
        REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")

    print(f"\nDone: {updated} embedded, {skipped} skipped, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
