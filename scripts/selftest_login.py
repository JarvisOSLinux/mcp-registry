#!/usr/bin/env python3
"""selftest_login.py — prove the manifest `login` check actually fires.

A server that works in the user's account signs in through its own tool
(Project-JARVIS#229, design v3), and its manifest names that tool in `login`.
JARVIS finds the sign-in by that name, so a `login` pointing at no tool would
leave the user with nothing to call. Each case builds a throwaway registry,
runs the real validator entry point against it and asserts on what it reports,
including the case that must stay silent.

  1. A `login` naming one of the server's tools passes, silently.
  2. A `login` naming no tool ERRORS.
  3. A `login` that is not an object ERRORS.
  4. A `login` with a misspelt key ERRORS.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_login.py
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
MANIFEST_URL = (
    "https://raw.githubusercontent.com/JarvisOSLinux/mcp-registry/main/servers/demo/manifest.json"
)
MODEL = generate_embeddings.DEFAULT_MODEL
VECTOR = [0.0, 1.0, -1.0, 0.5]

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


def manifest():
    return {
        "version": "1.0.0",
        "scope": "user",
        "platforms": ["linux"],
        "name": "Demo Server",
        "summary": "Fixture server for the login self-test",
        "keywords": ["demo"],
        "transports": [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        "tools": [
            {"name": "ping", "description": "Reply with pong", "threat_level": "safe"},
            {"name": "login", "description": "Sign in", "threat_level": "elevated"},
        ],
    }


@contextlib.contextmanager
def fixture(edit_manifest=None):
    """A one-server registry that is coherent unless a case edits it."""
    doc = manifest()
    if edit_manifest:
        edit_manifest(doc)

    real_hash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-login-selftest-") as tmp:
        root = pathlib.Path(tmp)
        server_dir = root / "servers" / "demo"
        server_dir.mkdir(parents=True)

        doc = dict(doc, embeddings={MODEL: {"v": VECTOR, "hash": real_hash}})
        (server_dir / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n")

        entry = {
            "id": SERVER_ID,
            "name": doc["name"],
            "summary": doc["summary"],
            "version": "1.0.0",
            "scope": "user",
            "keywords": doc["keywords"],
            "categories": ["productivity"],
            "platforms": doc["platforms"],
            "trustStatus": "community",
            "integrity": {"manifestSha256": sync_registry.sha256_file(server_dir / "manifest.json")},
            "manifest": MANIFEST_URL,
            "embeddings": {"model": MODEL, "version": real_hash[:16], "server": VECTOR, "tools": {}},
        }
        registry = {
            "version": "1.0",
            "updated": "2026-01-01T00:00:00Z",
            "embedding_spec": {
                "model": MODEL,
                "dimensions": len(VECTOR),
                "provider": "ollama",
                "canonical_fields": ["name", "summary", "keywords", "tools"],
            },
            "servers": {SERVER_ID: entry},
        }
        (root / "registry.json").write_text(json.dumps(registry, indent=2) + "\n")
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(saved_cwd)


def validate(*args):
    """Call the validator's real main() with argv, capturing exit code and output."""
    buffer = io.StringIO()
    saved_argv = sys.argv
    sys.argv = ["validate_registry.py", *args]
    try:
        with contextlib.redirect_stdout(buffer):
            code = validate_registry.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv = saved_argv
    return code or 0, buffer.getvalue()


def expect_error(edit_manifest, *, needle, label):
    with fixture(edit_manifest):
        code, out = validate()
        check(code != 0, f"{label} fails the gate")
        check(needle in out, f"{label}: the finding says {needle!r}")
        check("Traceback" not in out, f"{label}: reported, not crashed")


@case
def a_login_naming_a_tool_passes_silently():
    def good(m):
        m["login"] = {"tool": "login"}

    with fixture(good):
        code, out = validate()
        check(code == 0, "a login naming one of the server's tools passes")
        check("::error::" not in out, "no error at all")
        check("::warning::" not in out, "no warning either")


@case
def a_login_naming_no_tool_is_an_error():
    def missing(m):
        m["login"] = {"tool": "log_in"}

    expect_error(missing, needle="login.tool 'log_in' is not one of the server's tools", label="a login naming no tool")


@case
def a_malformed_login_is_an_error():
    def not_an_object(m):
        m["login"] = "login"

    def misspelt(m):
        m["login"] = {"tool": "login", "tol": "login"}

    expect_error(not_an_object, needle="'login' must be an object", label="a login that is a string")
    expect_error(misspelt, needle="login has unknown field(s) ['tol']", label="a login with a misspelt key")


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
    print("\nOK: login self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
