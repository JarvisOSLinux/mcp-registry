#!/usr/bin/env python3
"""selftest_tool_declarations.py — prove the `login` and reminder checks fire.

Two manifest declarations tell the orchestrator how to treat a tool, and a
wrong one fails quietly at runtime, so each is checked here against the real
validator entry point on a throwaway registry, including the cases that must
stay silent.

`login` names the tool a server signs the user in with (Project-JARVIS#229,
design v3). JARVIS finds the sign-in by that name, so one pointing at no tool
leaves the user with nothing to call.

  1. A `login` naming one of the server's tools passes, silently.
  2. A `login` naming no tool ERRORS.
  3. A `login` that is not an object, or has a misspelt key, ERRORS.

`suggestedRemindAfter` is the interval dispatch repeats a blocking tool's
reminder at, and each reminder costs a model turn.

  4. An interval at or above the floor passes.
  5. An interval below the floor ERRORS.
  6. An interval that is not a whole number of seconds ERRORS.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_tool_declarations.py
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
        "summary": "Fixture server for the tool declaration self-test",
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
    with tempfile.TemporaryDirectory(prefix="mcp-registry-tool-declarations-selftest-") as tmp:
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


def _remind_after(value):
    def edit(m):
        m["tools"][1]["blocking"] = True
        m["tools"][1]["suggestedRemindAfter"] = value

    return edit


@case
def a_reminder_at_or_above_the_floor_passes():
    floor = validate_registry.MIN_SUGGESTED_REMIND_AFTER
    for value in (floor, 180):
        with fixture(_remind_after(value)):
            code, out = validate()
            check(code == 0 and "::error::" not in out, f"suggestedRemindAfter {value} passes")


@case
def a_reminder_below_the_floor_is_an_error():
    floor = validate_registry.MIN_SUGGESTED_REMIND_AFTER
    expect_error(
        _remind_after(floor - 1),
        needle=f"suggestedRemindAfter {floor - 1} is below {floor} s",
        label="a reminder just below the floor",
    )
    expect_error(_remind_after(5), needle="suggestedRemindAfter 5 is below", label="a 5 s reminder")


@case
def a_reminder_that_is_not_whole_seconds_is_an_error():
    needle = "suggestedRemindAfter must be a whole number of seconds"
    for value in (30.5, "30", True):
        expect_error(_remind_after(value), needle=needle, label=f"suggestedRemindAfter {value!r}")


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
    print("\nOK: tool declaration self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
