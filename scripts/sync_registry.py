#!/usr/bin/env python3
"""sync_registry.py — sync registry.json derived fields from server manifests.

For each server entry in registry.json:
  - Recomputes integrity.manifestSha256 from the local manifest file
  - Recomputes integrity.setupScriptSha256 if setup.sh exists
  - Syncs name, summary, keywords, platforms from the manifest into the entry
  - Updates the top-level updated timestamp

Usage:
  python3 scripts/sync_registry.py          # apply changes
  python3 scripts/sync_registry.py --check  # exit 1 if any field would change
"""
import json
import hashlib
import pathlib
import sys
import datetime
import argparse

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")

# Fields copied verbatim from the manifest into the index entry. 'platforms' is
# mirrored because dmcp filters by host from registry.json alone — browse and
# the install gate must not have to fetch every manifest to learn which OSes an
# entry was vetted on.
SYNCED_FIELDS = ("name", "summary", "keywords", "platforms")

# Where a newly mirrored field is inserted, so it lands next to the metadata it
# belongs with instead of after the multi-hundred-float embeddings blob that
# ends every entry — a registry.json diff is read by a human reviewer.
FIELD_ANCHOR = {"platforms": "keywords"}


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def move_after(entry: dict, field: str, anchor: str) -> None:
    """Reorder `entry` so `field` sits directly after `anchor`."""
    if field not in entry or anchor not in entry:
        return
    value = entry.pop(field)
    reordered = {}
    for key, existing in entry.items():
        reordered[key] = existing
        if key == anchor:
            reordered[field] = value
    entry.clear()
    entry.update(reordered)


def dir_from_url(url: str) -> str | None:
    """Extract the server directory name from a manifest URL."""
    if "/servers/" in url and url.endswith("/manifest.json"):
        return url.split("/servers/")[1].split("/")[0]
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any field would change (use as CI gate)",
    )
    args = parser.parse_args()

    registry = json.loads(REGISTRY.read_text())
    changed = False

    for server_id, entry in registry["servers"].items():
        dir_name = dir_from_url(entry.get("manifest", ""))
        if not dir_name:
            print(f"  SKIP {server_id}: cannot derive local dir from manifest URL")
            continue

        local_dir = SERVERS_DIR / dir_name
        manifest_path = local_dir / "manifest.json"
        setup_path = local_dir / "setup.sh"

        if not manifest_path.exists():
            print(f"  SKIP {server_id}: {manifest_path} not found locally")
            continue

        # Recompute manifestSha256
        new_sha = sha256_file(manifest_path)
        old_sha = entry.get("integrity", {}).get("manifestSha256", "")
        if new_sha != old_sha:
            print(f"  {server_id}: manifestSha256 updated")
            entry.setdefault("integrity", {})["manifestSha256"] = new_sha
            changed = True

        # Recompute setupScriptSha256 when setup.sh exists
        if setup_path.exists():
            new_setup_sha = sha256_file(setup_path)
            old_setup_sha = entry.get("integrity", {}).get("setupScriptSha256", "")
            if new_setup_sha != old_setup_sha:
                print(f"  {server_id}: setupScriptSha256 updated")
                entry.setdefault("integrity", {})["setupScriptSha256"] = new_setup_sha
                changed = True

        # Sync descriptive and vetting fields from manifest into registry entry
        manifest = json.loads(manifest_path.read_text())
        for field in SYNCED_FIELDS:
            if field in manifest and manifest[field] != entry.get(field):
                print(f"  {server_id}: '{field}' synced from manifest")
                is_new = field not in entry
                entry[field] = manifest[field]
                if is_new and field in FIELD_ANCHOR:
                    move_after(entry, field, FIELD_ANCHOR[field])
                changed = True

    if changed:
        if args.check:
            print("\nFAIL: registry.json is out of sync. Run scripts/sync_registry.py to fix.")
            sys.exit(1)
        registry["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
        print("\nregistry.json updated.")
    else:
        print("All derived fields are already up to date.")


if __name__ == "__main__":
    main()
