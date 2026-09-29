#!/usr/bin/env python3
"""sync_registry.py — sync registry.json derived fields from server manifests.

For each server entry in registry.json:
  - Recomputes integrity.manifestSha256 from the local manifest file
  - Recomputes integrity.setupScriptSha256 if setup.sh exists, and drops the
    recorded hash when the script is gone
  - Recomputes integrity.setupScriptWindowsSha256 if setup.ps1 exists, and drops
    the recorded hash when the script is gone
  - Syncs name, summary, keywords, platforms from the manifest into the entry

And registry-wide:
  - Mirrors providers/<id>.json into the top-level `providers` map
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
PROVIDERS_DIR = pathlib.Path("providers")

# Fields copied verbatim from the manifest into the index entry. 'platforms' is
# mirrored because dmcp filters by host from registry.json alone — browse and
# the install gate must not have to fetch every manifest to learn which OSes an
# entry was vetted on.
SYNCED_FIELDS = ("name", "summary", "keywords", "platforms")

# Where a newly mirrored field is inserted, so it lands next to the metadata it
# belongs with instead of after the multi-hundred-float embeddings blob that
# ends every entry — a registry.json diff is read by a human reviewer.
FIELD_ANCHOR = {"platforms": "keywords"}

POSIX_SETUP_SCRIPT = "setup.sh"
WINDOWS_SETUP_SCRIPT = "setup.ps1"

# Setup scripts hashed into the entry's integrity block, as (filename, key).
# The Windows script is a sibling of setup.sh, not a replacement: dmcp runs
# setup.ps1 on Windows hosts and setup.sh everywhere else, and verifies the hash
# of whichever one it runs — a per-platform script must not become a hole in
# integrity verification. These are the only two filenames hashed, so a server
# directory has exactly one place to put each script.
SETUP_SCRIPTS = (
    (POSIX_SETUP_SCRIPT, "setupScriptSha256"),
    (WINDOWS_SETUP_SCRIPT, "setupScriptWindowsSha256"),
)


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


def load_providers() -> dict:
    """providers/<id>.json, keyed by file stem, in a stable order.

    A provider is mirrored into registry.json for the same reason `platforms`
    is: dmcp fetches that one file, and signing someone in must not need a
    second fetch per provider. The file stays the source of truth — it is what a
    reviewer reads — and a file that does not parse is left for the validator
    to report rather than silently dropped from the mirror.
    """
    if not PROVIDERS_DIR.is_dir():
        return {}
    providers = {}
    for path in sorted(PROVIDERS_DIR.glob("*.json")):
        try:
            providers[path.stem] = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"  SKIP provider {path.name}: {e}")
    return providers


def place_providers(registry: dict, providers: dict) -> None:
    """Set the top-level `providers` map just ahead of `servers`.

    Ahead, not after: `servers` is thousands of lines of vectors, and a provider
    change is exactly the diff a reviewer must not have to scroll to find.
    """
    reordered = {}
    for key, value in registry.items():
        if key == "providers":
            continue
        if key == "servers" and providers:
            reordered["providers"] = providers
        reordered[key] = value
    registry.clear()
    registry.update(reordered)


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

        # Recompute a hash for each setup script the server directory ships
        for filename, integrity_key in SETUP_SCRIPTS:
            script_path = local_dir / filename
            if not script_path.exists():
                # A hash whose script is gone verifies nothing and fails
                # validation, and this is the only tool allowed to touch the
                # integrity block — so it has to prune in that direction too,
                # or deleting a setup script leaves an error nothing can clear.
                if entry.get("integrity", {}).pop(integrity_key, None) is not None:
                    print(f"  {server_id}: {integrity_key} dropped ({filename} no longer present)")
                    changed = True
                continue
            new_script_sha = sha256_file(script_path)
            old_script_sha = entry.get("integrity", {}).get(integrity_key, "")
            if new_script_sha != old_script_sha:
                print(f"  {server_id}: {integrity_key} updated")
                entry.setdefault("integrity", {})[integrity_key] = new_script_sha
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

    providers = load_providers()
    if providers != registry.get("providers", {}) or (
        not providers and "providers" in registry
    ):
        print("  providers: mirrored from providers/")
        place_providers(registry, providers)
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
