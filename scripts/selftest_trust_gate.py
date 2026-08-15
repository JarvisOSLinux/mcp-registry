#!/usr/bin/env python3
"""selftest_trust_gate.py — prove the trust-boundary checks actually fire.

The checks here are the ones that decide what a client will *run*, and each is
the kind that looks identical whether it is working or silently absent:

  1. `official` requires a full-SHA source pin. Without it dmcp clones the
     branch head, so the maintainer review the tier records binds nothing. A tag
     is not a pin either — dmcp checks it out and skips verification, so it reads
     like one and binds nothing.
  2. The manifest URL must be hosted by this registry. dmcp fetches it on every
     install; an off-registry host is content this repo cannot review or revoke.
  3. Leaving a revoked state (`deprecated`/`removed`) needs the maintainer label,
     the same as a promotion. dmcp refuses a revoked entry, so lifting the
     tombstone re-arms it for every client — and unlike a promotion it needs no
     new field, so it would otherwise pass as a one-word diff.
  4. A changed manifest is annotated for review, naming what it now launches.
     `transports[].command` runs on *every call*; the setup script runs once at
     install. Flagging only the second had the blast radius backwards.

Each case builds a throwaway registry in a temp directory, runs the real
validate entry point against it, and asserts on what it reports — including the
transitions that must stay silent, so revoking and downgrading are not made
harder by a rule aimed at raising trust.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_trust_gate.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

import generate_embeddings
import sync_registry
import validate_registry

SERVER_ID = "com.example.mcp.demo"
REGISTRY_HOST = "https://raw.githubusercontent.com/JarvisOSLinux/mcp-registry/main"
MANIFEST_URL = f"{REGISTRY_HOST}/servers/demo/manifest.json"

EMBEDDING_MODEL = generate_embeddings.DEFAULT_MODEL
EMBEDDING_VECTOR = [0.0, 1.0, -1.0, 0.5]

SETUP_SH = "#!/usr/bin/env bash\nset -euo pipefail\npython3 -m venv .venv\n"

# 40 hex chars: what dmcp's is_full_commit_sha accepts and verify_rev then
# confirms against HEAD.
PINNED_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def manifest(source=None, transports=None):
    return {
        "version": "1.0.0",
        "scope": "user",
        "platforms": ["linux"],
        "name": "Demo Server",
        "summary": "Fixture server for the trust-gate self-test",
        "keywords": ["demo"],
        "transports": transports
        or [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        "source": source if source is not None else {"type": "git", "url": "https://x.invalid/d.git"},
        "setupScript": "setup.sh",
        # threat_level is required on every tool of a live entry (see
        # validate_threat_levels); declaring it keeps these cases about the trust
        # boundary instead of failing on an unrelated gate.
        "tools": [{"name": "ping", "description": "Reply with pong", "threat_level": "safe"}],
    }


@contextlib.contextmanager
def fixture(doc, trust="community", manifest_url=MANIFEST_URL):
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-trustgate-") as tmp:
        root = pathlib.Path(tmp)
        server_dir = root / "servers" / "demo"
        server_dir.mkdir(parents=True)

        chash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
        doc = dict(doc, embeddings={EMBEDDING_MODEL: {"v": EMBEDDING_VECTOR, "hash": chash}})
        # newline="": these bytes are hashed, so a platform rewriting "\n" would
        # desynchronise the fixture from the hashes sync_registry.py computes.
        (server_dir / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n", newline="")
        (server_dir / "setup.sh").write_text(SETUP_SH, newline="")

        (root / "registry.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "updated": "2026-01-01T00:00:00Z",
                    "embedding_spec": {
                        "model": EMBEDDING_MODEL,
                        "dimensions": len(EMBEDDING_VECTOR),
                        "provider": "ollama",
                        "canonical_fields": ["name", "summary", "keywords", "tools"],
                    },
                    "servers": {
                        SERVER_ID: {
                            "id": SERVER_ID,
                            "name": "Demo Server",
                            "summary": "Fixture server for the trust-gate self-test",
                            "version": "1.0.0",
                            "scope": "user",
                            "keywords": ["demo"],
                            "trustStatus": trust,
                            "integrity": {},
                            "manifest": manifest_url,
                            "embeddings": {
                                "model": EMBEDDING_MODEL,
                                "version": chash[:16],
                                "server": EMBEDDING_VECTOR,
                                "tools": {},
                            },
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            newline="",
        )
        os.chdir(root)
        try:
            # Fill integrity hashes and mirror platforms, as a contributor would.
            run(sync_registry.main, ["sync_registry.py"])
            yield root
        finally:
            os.chdir(saved_cwd)


def run(entry_point, argv):
    buffer = io.StringIO()
    saved_argv = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(buffer):
            code = entry_point()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv = saved_argv
    return code or 0, buffer.getvalue()


def validate(*args):
    return run(validate_registry.main, ["validate_registry.py", *args])


def write_base(root, trust=None):
    """Snapshot registry.json as a --base file, optionally with a different tier."""
    base = json.loads((root / "registry.json").read_text())
    if trust is not None:
        base["servers"][SERVER_ID]["trustStatus"] = trust
    path = root / "base_registry.json"
    path.write_text(json.dumps(base, indent=2) + "\n", newline="")
    return str(path)


def set_trust(root, trust):
    registry = json.loads((root / "registry.json").read_text())
    registry["servers"][SERVER_ID]["trustStatus"] = trust
    (root / "registry.json").write_text(json.dumps(registry, indent=2) + "\n", newline="")


CASES = []
FAILURES = []
running = "?"


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


# --- 1. the official-tier source pin ---------------------------------------


@case
def official_requires_a_full_sha_pin():
    with fixture(manifest(), trust="official") as root:
        code, out = validate()
        check(code == 1, "an official entry with no source.rev fails")
        check("has no" in out and "rev" in out, "the error names the missing rev")

    unpinned = manifest(source={"type": "git", "url": "https://x.invalid/d.git", "rev": "v1.2.3"})
    with fixture(unpinned, trust="official") as root:
        code, out = validate()
        check(code == 1, "an official entry pinned to a tag fails")
        check("40-character" in out, "the error explains that only a full SHA is verified")

    short = manifest(source={"type": "git", "url": "https://x.invalid/d.git", "rev": "a1b2c3d"})
    with fixture(short, trust="official") as root:
        code, _ = validate()
        check(code == 1, "an official entry pinned to a short rev fails")

    pinned = manifest(source={"type": "git", "url": "https://x.invalid/d.git", "rev": PINNED_SHA})
    with fixture(pinned, trust="official") as root:
        code, out = validate()
        check(code == 0, "an official entry pinned to a full SHA passes")


@case
def community_may_track_a_branch():
    with fixture(manifest(), trust="community") as root:
        code, out = validate()
        check(code == 0, "a community entry with no pin passes")
        check("rev" not in out, "and is not nagged about one — that tier trusts the submitter")


@case
def an_official_entry_without_a_git_source_is_not_asked_to_pin():
    # npx/uvx/go transports carry their own version pin; there is no clone to fix.
    no_source = manifest(source={})
    no_source.pop("source")
    with fixture(no_source, trust="official") as root:
        code, out = validate()
        check(code == 0, "an official entry with no source block passes")


# --- 2. manifest hosting ----------------------------------------------------


@case
def the_manifest_must_be_hosted_by_this_registry():
    off_host = "https://elsewhere.invalid/servers/demo/manifest.json"
    with fixture(manifest(), manifest_url=off_host) as root:
        code, out = validate()
        check(code == 1, "an off-registry manifest URL fails")
        check("not hosted by this registry" in out, "the error says why")

    with fixture(manifest()) as root:
        code, out = validate()
        check(code == 0, "a registry-hosted manifest URL passes")


# --- 3. leaving a revoked state --------------------------------------------


@case
def lifting_a_revocation_needs_the_maintainer_label():
    with fixture(manifest(), trust="community") as root:
        base = write_base(root, trust="removed")

        code, out = validate("--base", base)
        check(code == 1, "removed->community without the label fails")
        check("revocation lifted" in out, "the error names the revocation, not a promotion")

        code, out = validate("--base", base, "--approval-label-present")
        check(code == 0, "removed->community passes with the label")
        check("revocation lifted with maintainer label" in out, "and is recorded as a notice")


@case
def a_deprecated_entry_is_gated_the_same_way():
    with fixture(manifest(), trust="community") as root:
        base = write_base(root, trust="deprecated")
        code, out = validate("--base", base)
        check(code == 1, "deprecated->community without the label fails")


@case
def reviving_straight_to_official_trips_both_gates():
    pinned = manifest(source={"type": "git", "url": "https://x.invalid/d.git", "rev": PINNED_SHA})
    with fixture(pinned, trust="official") as root:
        base = write_base(root, trust="removed")
        code, out = validate("--base", base)
        check(code == 1, "removed->official without the label fails")
        check("without maintainer approval" in out, "the promotion gate fires")
        check("revocation lifted" in out, "and the revocation gate fires too")


@case
def revoking_and_downgrading_stay_ungated():
    """Raising trust needs ceremony; lowering it must not — revocation is urgent."""
    with fixture(manifest(), trust="removed") as root:
        base = write_base(root, trust="community")
        code, out = validate("--base", base)
        check(code == 0, "community->removed passes with no label")
        check("revocation lifted" not in out, "and is not mistaken for a revival")

    with fixture(manifest(), trust="deprecated") as root:
        base = write_base(root, trust="official")
        code, _ = validate("--base", base)
        check(code == 0, "official->deprecated passes with no label")

    with fixture(manifest(), trust="community") as root:
        base = write_base(root, trust="official")
        code, out = validate("--base", base)
        check(code == 0, "official->community (a downgrade) passes with no label")

    with fixture(manifest(), trust="removed") as root:
        base = write_base(root, trust="deprecated")
        code, out = validate("--base", base)
        check(code == 0, "deprecated->removed passes: still revoked, not lifted")


# --- 4. the manifest-change review signal ----------------------------------


@case
def a_changed_manifest_is_annotated_with_what_it_launches():
    with fixture(manifest()) as root:
        # A base whose recorded manifest hash differs is exactly what a PR that
        # edits the manifest produces.
        base = json.loads((root / "registry.json").read_text())
        base["servers"][SERVER_ID]["integrity"]["manifestSha256"] = "0" * 64
        base_path = root / "base_registry.json"
        base_path.write_text(json.dumps(base, indent=2) + "\n", newline="")

        code, out = validate("--base", str(base_path))
        check(code == 0, "a manifest change is advisory, not fatal")
        check("manifest changed" in out, "the change is annotated for review")
        check("python3 server.py" in out, "the annotation names the command it now launches")


@case
def an_unchanged_manifest_is_not_flagged():
    with fixture(manifest()) as root:
        base = write_base(root)
        code, out = validate("--base", base)
        check(code == 0, "an unchanged entry passes")
        check("manifest changed" not in out, "and is not flagged for review")


@case
def a_new_entry_is_not_flagged_as_a_manifest_change():
    """A first submission has no base entry — the whole thing is under review."""
    with fixture(manifest()) as root:
        base = json.loads((root / "registry.json").read_text())
        base["servers"] = {}
        base_path = root / "base_registry.json"
        base_path.write_text(json.dumps(base, indent=2) + "\n", newline="")

        code, out = validate("--base", str(base_path))
        check(code == 0, "a brand-new community entry passes")
        check("manifest changed" not in out, "and is not annotated as a change")


def main() -> int:
    global running
    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        fn()

    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK: trust-gate self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
