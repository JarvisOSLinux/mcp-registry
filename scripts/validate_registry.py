#!/usr/bin/env python3
"""validate_registry.py — PR-gate validation for the MCP registry.

Enforces the checks required by docs/TRUST-MODEL.md and
docs/REGISTRY-AUTOMATION.md so a submission PR is validated before merge.

Static checks (always run):
  - registry.json parses and has the expected top-level shape
  - every server entry: map key matches entry.id; required fields present
  - trustStatus is one of the allowed values
  - scope is one of the allowed values
  - platforms is a non-empty array of allowed values, present on every entry,
    and agrees with the manifest it was mirrored from
  - per-transport platforms (manifest transports[].platforms) use the same enum
  - the manifest URL resolves to an existing servers/<dir>/manifest.json
  - integrity.manifestSha256 is present and matches the manifest file bytes
  - setup scripts and their hashes agree in both directions: a setup.sh /
    setup.ps1 in the server directory needs a recorded hash that matches, and a
    recorded hash needs the script it claims to verify
  - no orphan directory: every first-party servers/<dir>/ is referenced by an
    entry (catches a half-done removal that dropped the entry but left the dir)

Warnings (reported, never fatal):
  - a vetted top-level platform that no transport can serve

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

from sync_registry import SETUP_SCRIPTS, WINDOWS_SETUP_SCRIPT, dir_from_url, sha256_file

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")

ALLOWED_TRUST = {"community", "official", "deprecated", "removed"}
ALLOWED_SCOPE = {"user", "system"}
ALLOWED_PLATFORMS = {"linux", "darwin", "windows"}
REQUIRED_FIELDS = ("id", "name", "summary", "version", "scope", "trustStatus", "manifest")


def annotate(level: str, msg: str) -> None:
    # GitHub Actions annotation; harmless plain text when run locally.
    print(f"::{level}::{msg}" if level in ("error", "warning") else msg)


def validate_platforms(where: str, entry: dict, errors: list) -> None:
    """Check the entry's mirrored `platforms` list.

    An absent list means 'unrestricted' to dmcp, so a silent omission would
    offer a server to hosts nobody ever vetted it on. Entries in this registry
    must therefore say what they were vetted on, explicitly.
    """
    platforms = entry.get("platforms")
    if platforms is None:
        errors.append(
            f"{where}: missing 'platforms' — every entry in this registry must "
            f"declare the platforms it was vetted on (add the field to the "
            f"manifest and run sync_registry.py)"
        )
        return

    if not isinstance(platforms, list) or not platforms:
        errors.append(f"{where}: 'platforms' must be a non-empty array")
        return

    for value in platforms:
        if value not in ALLOWED_PLATFORMS:
            errors.append(
                f"{where}: platform '{value}' not in {sorted(ALLOWED_PLATFORMS)}"
            )


def validate_transports(
    where: str, manifest: dict, entry: dict, errors: list, warnings: list
) -> None:
    """Check per-transport `platforms` and that the vetted platforms are servable.

    A transport may narrow itself to the hosts it can launch on, so one entry can
    spell its command `python3` on POSIX and `python` on Windows. dmcp picks the
    first transport whose list includes the host and a transport without the
    field matches every host, so an entry whose transports collectively miss a
    vetted platform leaves that host with nothing to launch. That is a warning,
    not an error: the transport may legitimately land in a later PR than the
    platform it serves.
    """
    transports = manifest.get("transports")
    if not isinstance(transports, list):
        return

    matches_any_host = False
    covered = set()

    for position, transport in enumerate(transports):
        if not isinstance(transport, dict):
            continue
        at = f"{where}: transports[{position}]"

        if "platforms" not in transport:
            matches_any_host = True
            continue

        platforms = transport["platforms"]
        if not isinstance(platforms, list) or not platforms:
            errors.append(
                f"{at}: 'platforms' must be a non-empty array — omit the field "
                f"for a transport that runs on every host"
            )
            continue

        for value in platforms:
            if value not in ALLOWED_PLATFORMS:
                errors.append(f"{at}: platform '{value}' not in {sorted(ALLOWED_PLATFORMS)}")
            else:
                covered.add(value)

    if matches_any_host:
        return

    vetted = entry.get("platforms")
    if not isinstance(vetted, list):
        return

    unservable = [p for p in vetted if p not in covered]
    if unservable:
        warnings.append(
            f"{where}: vetted platform(s) {unservable} have no matching transport — "
            f"dmcp has nothing to launch there (add a transport carrying that "
            f"platform, or drop it from 'platforms')"
        )


def validate_setup_scripts(
    where: str, dir_name: str, manifest: dict, integrity: dict, errors: list
) -> None:
    """Check that every setup script and its recorded hash imply each other.

    dmcp verifies a setup script against the registry hash before executing it,
    so a script with no hash cannot run and a hash with no script verifies
    nothing — the second is what a half-done script removal leaves behind.
    """
    for filename, integrity_key in SETUP_SCRIPTS:
        script_path = SERVERS_DIR / dir_name / filename
        recorded = integrity.get(integrity_key, "")

        if script_path.exists():
            actual = sha256_file(script_path)
            if not recorded:
                errors.append(
                    f"{where}: {filename} present but integrity.{integrity_key} "
                    f"missing — run sync_registry.py"
                )
            elif recorded != actual:
                errors.append(f"{where}: integrity.{integrity_key} stale — run sync_registry.py")
        elif recorded:
            errors.append(
                f"{where}: integrity.{integrity_key} recorded but "
                f"servers/{dir_name}/{filename} does not exist"
            )

    # A local Windows script is hashed by filename, so any other name silently
    # ships an unverified script.
    declared = manifest.get("setupScriptWindows")
    if isinstance(declared, str) and "://" not in declared:
        if declared != WINDOWS_SETUP_SCRIPT:
            errors.append(
                f"{where}: setupScriptWindows '{declared}' — a local Windows setup "
                f"script must be named '{WINDOWS_SETUP_SCRIPT}', the only name "
                f"sync_registry.py hashes"
            )
        elif not (SERVERS_DIR / dir_name / WINDOWS_SETUP_SCRIPT).exists():
            errors.append(
                f"{where}: manifest declares setupScriptWindows '{declared}' but "
                f"servers/{dir_name}/{declared} does not exist"
            )


def validate_static(registry: dict, errors: list, warnings: list) -> None:
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

        validate_platforms(where, entry, errors)

        dir_name = dir_from_url(entry.get("manifest", ""))
        if not dir_name:
            errors.append(f"{where}: cannot derive a local dir from manifest URL")
            continue

        manifest_path = SERVERS_DIR / dir_name / "manifest.json"
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

        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"{where}: manifest {manifest_path} failed to parse: {e}")
            continue

        validate_setup_scripts(where, dir_name, manifest, integrity, errors)
        validate_transports(where, manifest, entry, errors, warnings)

        # The entry's platforms are a mirror, so a hand-edited entry could claim
        # coverage the vetted manifest never did.
        declared = manifest.get("platforms")
        if declared is None:
            errors.append(
                f"{where}: manifest {manifest_path} declares no 'platforms'"
            )
        elif declared != entry.get("platforms"):
            errors.append(
                f"{where}: 'platforms' {entry.get('platforms')} does not match "
                f"manifest {declared} — run sync_registry.py"
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
    warnings: list = []

    try:
        registry = json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError) as e:
        annotate("error", f"registry.json failed to parse: {e}")
        return 1

    validate_static(registry, errors, warnings)
    validate_no_orphan_dirs(registry, errors)

    if args.base:
        try:
            base = json.loads(pathlib.Path(args.base).read_text())
        except (OSError, json.JSONDecodeError):
            base = {}
        validate_promotions(registry, base, args.approval_label_present, errors)

    for warn in warnings:
        annotate("warning", warn)

    for err in errors:
        annotate("error", err)

    if errors:
        print(f"\nFAIL: {len(errors)} validation error(s).")
        return 1
    print("OK: registry validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
