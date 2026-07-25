#!/usr/bin/env python3
"""selftest_platform_format.py — prove the platform-format checks actually fire.

This repo has no test suite, and the per-transport `platforms` / Windows setup
script checks are exactly the kind that rot unnoticed: a validator that never
fires looks identical to a registry with nothing wrong. Each case below builds a
throwaway registry in a temp directory, runs the real sync and validate entry
points against it, and asserts on what they report — including the cases that
must stay silent, so backward compatibility is checked too.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_platform_format.py
"""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile

import sync_registry
import validate_registry

SERVER_ID = "com.example.mcp.demo"
MANIFEST_URL = "https://example.invalid/servers/demo/manifest.json"

SETUP_SH = "#!/usr/bin/env bash\nset -euo pipefail\npython3 -m venv .venv\n"
SETUP_PS1 = "$ErrorActionPreference = 'Stop'\npython -m venv .venv\n"

WINDOWS_TRANSPORT = {
    "type": "stdio",
    "command": ".venv\\Scripts\\python.exe",
    "args": ["server.py"],
    "platforms": ["windows"],
}


def posix_transport(platforms):
    return {
        "type": "stdio",
        "command": ".venv/bin/python3",
        "args": ["server.py"],
        "platforms": list(platforms),
    }


def manifest(transports, platforms, **extra):
    doc = {
        "version": "1.0.0",
        "scope": "user",
        "platforms": list(platforms),
        "name": "Demo Server",
        "summary": "Fixture server for the registry self-test",
        "keywords": ["demo"],
        "transports": transports,
        "source": {"type": "git", "url": "https://example.invalid/demo.git"},
        "setupScript": "setup.sh",
        "tools": [{"name": "ping", "description": "Reply with pong"}],
    }
    doc.update(extra)
    return doc


@contextlib.contextmanager
def fixture(doc, scripts):
    """Build a one-server registry in a temp dir and run the scripts inside it."""
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-selftest-") as tmp:
        root = pathlib.Path(tmp)
        server_dir = root / "servers" / "demo"
        server_dir.mkdir(parents=True)
        (server_dir / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n")
        for filename, body in scripts.items():
            (server_dir / filename).write_text(body)

        # No integrity hashes and no mirrored platforms: sync_registry.py fills
        # both, the same way a contributor's first run does.
        (root / "registry.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "updated": "2026-01-01T00:00:00Z",
                    "servers": {
                        SERVER_ID: {
                            "id": SERVER_ID,
                            "name": "Demo Server",
                            "summary": "Fixture server for the registry self-test",
                            "version": "1.0.0",
                            "scope": "user",
                            "keywords": ["demo"],
                            "trustStatus": "community",
                            "integrity": {},
                            "manifest": MANIFEST_URL,
                        }
                    },
                },
                indent=2,
            )
            + "\n"
        )
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(saved_cwd)


def run(entry_point, argv):
    """Call a script's main() with argv, capturing its exit code and output."""
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


def sync(*args):
    return run(sync_registry.main, ["sync_registry.py", *args])


def validate():
    return run(validate_registry.main, ["validate_registry.py"])


def read_entry(root):
    return json.loads((root / "registry.json").read_text())["servers"][SERVER_ID]


def write_entry(root, entry):
    registry = json.loads((root / "registry.json").read_text())
    registry["servers"][SERVER_ID] = entry
    (root / "registry.json").write_text(json.dumps(registry, indent=2) + "\n")


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


@case
def sync_hashes_the_windows_setup_script():
    doc = manifest(
        [posix_transport(["linux", "darwin"]), WINDOWS_TRANSPORT],
        ["linux", "windows"],
        setupScriptWindows="setup.ps1",
    )
    with fixture(doc, {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}) as root:
        code, out = sync("--check")
        check(code == 1, "--check fails while the hashes are missing")

        code, out = sync()
        check(code == 0 and "setupScriptWindowsSha256 updated" in out, "sync reports the new hash")

        integrity = read_entry(root)["integrity"]
        expected = hashlib.sha256(SETUP_PS1.encode()).hexdigest()
        check(
            integrity.get("setupScriptWindowsSha256") == expected,
            "setupScriptWindowsSha256 matches setup.ps1",
        )
        check(
            integrity.get("setupScriptSha256") == hashlib.sha256(SETUP_SH.encode()).hexdigest(),
            "setupScriptSha256 is still computed from setup.sh",
        )

        code, _ = sync("--check")
        check(code == 0, "--check passes once synced")


@case
def posix_only_server_is_untouched():
    doc = manifest([{"type": "stdio", "command": "python3", "args": ["server.py"]}], ["linux"])
    with fixture(doc, {"setup.sh": SETUP_SH}) as root:
        sync()
        check(
            "setupScriptWindowsSha256" not in read_entry(root)["integrity"],
            "no Windows hash is invented for a server without setup.ps1",
        )
        code, out = validate()
        check(code == 0, "a manifest in today's shape still validates")
        check("::warning::" not in out, "a transport without 'platforms' serves every vetted host")


@case
def cross_platform_entry_validates_clean():
    doc = manifest(
        [posix_transport(["linux", "darwin"]), WINDOWS_TRANSPORT],
        ["linux", "windows"],
        setupScriptWindows="setup.ps1",
    )
    with fixture(doc, {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}):
        sync()
        code, out = validate()
        check(code == 0, "one entry, one transport per platform, validates")
        check("::warning::" not in out and "::error::" not in out, "nothing is reported")


@case
def unknown_transport_platform_is_an_error():
    doc = manifest(
        [posix_transport(["linux"]), {**WINDOWS_TRANSPORT, "platforms": ["win32"]}],
        ["linux", "windows"],
    )
    with fixture(doc, {"setup.sh": SETUP_SH}):
        sync()
        code, out = validate()
        check(code == 1, "an out-of-enum transport platform fails the gate")
        check("transports[1]" in out and "win32" in out, "the error names the offending transport")


@case
def empty_transport_platform_list_is_an_error():
    doc = manifest([{**WINDOWS_TRANSPORT, "platforms": []}], ["windows"])
    with fixture(doc, {"setup.sh": SETUP_SH}):
        sync()
        code, out = validate()
        check(code == 1, "an empty transport 'platforms' fails the gate")
        check(
            "non-empty" in out,
            "the error explains that omitting the field is the way to say 'all'",
        )


@case
def windows_script_without_a_hash_is_an_error():
    doc = manifest([posix_transport(["linux"])], ["linux"], setupScriptWindows="setup.ps1")
    with fixture(doc, {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}) as root:
        sync()
        entry = read_entry(root)
        del entry["integrity"]["setupScriptWindowsSha256"]
        write_entry(root, entry)

        code, out = validate()
        check(code == 1, "a shipped setup.ps1 with no recorded hash fails the gate")
        check("setupScriptWindowsSha256 missing" in out, "the error says which hash is missing")


@case
def windows_hash_without_a_script_is_an_error():
    doc = manifest([posix_transport(["linux"])], ["linux"])
    with fixture(doc, {"setup.sh": SETUP_SH, "setup.ps1": SETUP_PS1}) as root:
        sync()
        (root / "servers" / "demo" / "setup.ps1").unlink()

        code, out = validate()
        check(code == 1, "a hash left behind by a deleted setup.ps1 fails the gate")
        check("recorded but" in out, "the error says the hash verifies nothing")


@case
def misnamed_or_missing_windows_script_is_an_error():
    doc = manifest([posix_transport(["linux"])], ["linux"], setupScriptWindows="install.ps1")
    with fixture(doc, {"setup.sh": SETUP_SH, "install.ps1": SETUP_PS1}):
        sync()
        code, out = validate()
        check(code == 1, "a Windows script under another name fails the gate")
        check(
            "must be named 'setup.ps1'" in out,
            "the error names the only filename that is hashed",
        )

    doc = manifest([posix_transport(["linux"])], ["linux"], setupScriptWindows="setup.ps1")
    with fixture(doc, {"setup.sh": SETUP_SH}):
        sync()
        code, out = validate()
        check(code == 1, "declaring setupScriptWindows without shipping it fails the gate")
        check("does not exist" in out, "the error says the declared script is absent")


@case
def unservable_vetted_platform_is_a_warning():
    doc = manifest(
        [posix_transport(["linux"]), WINDOWS_TRANSPORT],
        ["linux", "darwin", "windows"],
    )
    with fixture(doc, {"setup.sh": SETUP_SH}):
        sync()
        code, out = validate()
        check(code == 0, "a vetted platform with no transport does not fail the gate")
        check(
            "::warning::" in out and "'darwin'" in out,
            "the warning names the unservable platform",
        )
        check("::error::" not in out, "nothing else is reported as an error")


def main() -> int:
    global running

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        fn()

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: platform-format self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
