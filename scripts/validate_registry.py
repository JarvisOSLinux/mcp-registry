#!/usr/bin/env python3
"""validate_registry.py — PR-gate validation for the MCP registry.

Enforces the checks required by docs/TRUST-MODEL.md and
docs/REGISTRY-AUTOMATION.md so a submission PR is validated before merge.

Static checks (always run):
  - registry.json parses and has the expected top-level shape
  - every server entry: map key matches entry.id; required fields present
  - trustStatus is one of the allowed values
  - scope is one of the allowed values
  - the manifest URL resolves to an existing servers/<dir>/manifest.json
  - integrity.manifestSha256 is present and matches the manifest file bytes
  - if a setup.sh exists, integrity.setupScriptSha256 is present and matches
  - no orphan directory: every first-party servers/<dir>/ is referenced by an
    entry (catches a half-done removal that dropped the entry but left the dir)

Trust-promotion gate (when --base is given):
  - compares trustStatus per entry against the base registry.json
  - if any entry is promoted to 'official' (or added directly as 'official'),
    a maintainer approval is REQUIRED: the run fails unless
    --approval-label-present is passed. This is the rule that stops a submitter
    from self-assigning the official tier.
  - a changed or newly added setup.sh is reported as a warning (advisory).

Usage:
  python3 scripts/validate_registry.py
  python3 scripts/validate_registry.py --base base_registry.json
  python3 scripts/validate_registry.py --base base_registry.json --approval-label-present
"""
import argparse
import json
import pathlib
import sys

from sync_registry import dir_from_url, sha256_file

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")

ALLOWED_TRUST = {"community", "official", "deprecated", "removed"}
ALLOWED_SCOPE = {"user", "system"}
REQUIRED_FIELDS = ("id", "name", "summary", "version", "scope", "trustStatus", "manifest")


def annotate(level: str, msg: str) -> None:
    # GitHub Actions annotation; harmless plain text when run locally.
    print(f"::{level}::{msg}" if level in ("error", "warning") else msg)


def validate_static(registry: dict, errors: list) -> None:
    if "servers" not in registry or not isinstance(registry["servers"], dict):
        errors.append("registry.json: missing or malformed 'servers' object")
        return

    for server_id, entry in registry["servers"].items():
        where = f"servers['{server_id}']"

        for field in REQUIRED_FIELDS:
            if field not in entry:
                errors.append(f"{where}: missing required field '{field}'")

        if entry.get("id") not in (None, server_id):
            errors.append(f"{where}: entry.id '{entry.get('id')}' != map key '{server_id}'")

        trust = entry.get("trustStatus")
        if trust is not None and trust not in ALLOWED_TRUST:
            errors.append(
                f"{where}: trustStatus '{trust}' not in {sorted(ALLOWED_TRUST)}"
            )

        scope = entry.get("scope")
        if scope is not None and scope not in ALLOWED_SCOPE:
            errors.append(f"{where}: scope '{scope}' not in {sorted(ALLOWED_SCOPE)}")

        dir_name = dir_from_url(entry.get("manifest", ""))
        if not dir_name:
            errors.append(f"{where}: cannot derive a local dir from manifest URL")
            continue

        manifest_path = SERVERS_DIR / dir_name / "manifest.json"
        setup_path = SERVERS_DIR / dir_name / "setup.sh"
        if not manifest_path.exists():
            errors.append(f"{where}: manifest {manifest_path} not found")
            continue

        integrity = entry.get("integrity", {})
        recorded = integrity.get("manifestSha256", "")
        actual = sha256_file(manifest_path)
        if not recorded:
            errors.append(f"{where}: integrity.manifestSha256 missing")
        elif recorded != actual:
            errors.append(
                f"{where}: integrity.manifestSha256 stale "
                f"(recorded {recorded[:12]}…, actual {actual[:12]}…) — run sync_registry.py"
            )

        if setup_path.exists():
            recorded_s = integrity.get("setupScriptSha256", "")
            actual_s = sha256_file(setup_path)
            if not recorded_s:
                errors.append(f"{where}: setup.sh present but integrity.setupScriptSha256 missing")
            elif recorded_s != actual_s:
                errors.append(
                    f"{where}: integrity.setupScriptSha256 stale — run sync_registry.py"
                )


def validate_no_orphan_dirs(registry: dict, errors: list) -> None:
    """Flag any first-party servers/<dir>/ not referenced by a registry entry.

    Deleting an entry but leaving its directory is the one removal mistake the
    entry-driven checks above cannot see — this closes that gap.
    """
    if not SERVERS_DIR.is_dir():
        return

    referenced = {
        dir_from_url(entry.get("manifest", ""))
        for entry in registry.get("servers", {}).values()
    }
    referenced.discard(None)

    for child in sorted(SERVERS_DIR.iterdir()):
        if child.is_dir() and child.name not in referenced:
            errors.append(
                f"servers/{child.name}/: orphan directory — no registry entry "
                f"references it (remove the directory, or add its entry)"
            )


def validate_promotions(registry: dict, base: dict, approval: bool, errors: list) -> None:
    base_servers = base.get("servers", {}) if isinstance(base, dict) else {}
    promotions = []
    setup_changes = []

    for server_id, entry in registry["servers"].items():
        head_trust = entry.get("trustStatus")
        base_entry = base_servers.get(server_id)
        base_trust = base_entry.get("trustStatus") if base_entry else None

        if head_trust == "official" and base_trust != "official":
            promotions.append(server_id)

        head_setup = entry.get("integrity", {}).get("setupScriptSha256")
        base_setup = (base_entry or {}).get("integrity", {}).get("setupScriptSha256")
        if head_setup and head_setup != base_setup:
            setup_changes.append(server_id)

    for sid in setup_changes:
        annotate("warning", f"{sid}: setup.sh added/changed — review the script before merge")

    if promotions:
        listed = ", ".join(promotions)
        if approval:
            annotate("notice", f"trustStatus→official approved by maintainer label for: {listed}")
        else:
            errors.append(
                "trustStatus raised to 'official' without maintainer approval for: "
                f"{listed}. A maintainer must apply the 'trust-approved' label "
                "(see docs/TRUST-MODEL.md §4)."
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="Path to the base branch's registry.json for diff checks")
    parser.add_argument(
        "--approval-label-present",
        action="store_true",
        help="Set when the PR carries the maintainer 'trust-approved' label",
    )
    args = parser.parse_args()

    errors: list = []

    try:
        registry = json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError) as e:
        annotate("error", f"registry.json failed to parse: {e}")
        return 1

    validate_static(registry, errors)
    validate_no_orphan_dirs(registry, errors)

    if args.base:
        try:
            base = json.loads(pathlib.Path(args.base).read_text())
        except (OSError, json.JSONDecodeError):
            base = {}
        validate_promotions(registry, base, args.approval_label_present, errors)

    for err in errors:
        annotate("error", err)

    if errors:
        print(f"\nFAIL: {len(errors)} validation error(s).")
        return 1
    print("OK: registry validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
