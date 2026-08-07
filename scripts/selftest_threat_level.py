#!/usr/bin/env python3
"""selftest_threat_level.py — prove the threat-level check actually fires.

Every tool in the registry now declares a `threat_level` (the 215/215 audit),
but an audit that nothing enforces is one merged server away from a hole again:
JARVIS's confirmation gate treats a tool the host floor does not recognise as
`safe` and runs it unconfirmed, so a destructive tool under an unfamiliar name
that forgot to declare its level slips straight through. validate_registry.py
now makes the omission an ERROR — this self-test is what proves that error still
fires, since a validator that has quietly stopped checking looks identical to a
registry with nothing wrong.

Each case builds a throwaway registry in a temp directory, runs the real
validate entry point against it, and asserts on what it reports:

  1. A manifest whose every tool declares a valid threat_level passes, silently.
  2. A tool that declares neither threat_level nor confirmation_required ERRORS,
     and the run exits non-zero.
  3. A tool with a threat_level outside the enum ERRORS.
  4. The legacy `confirmation_required: true` shorthand is accepted in place of
     threat_level.
  5. A `removed` entry is exempt — dmcp refuses to install it, so classifying a
     tool it will never run buys the gate nothing.
  6. A `deprecated` entry is exempt too (the same "not a live entry" line).
  7. Each of the four enum values is accepted.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_threat_level.py
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
MANIFEST_URL = "https://example.invalid/servers/demo/manifest.json"
MODEL = generate_embeddings.DEFAULT_MODEL
# threat_level is not part of the canonical embedding text, so a coherent vector
# stays coherent however a case fiddles the field — the embedding check adds no
# noise to what these cases assert on.
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


def manifest(tools):
    return {
        "version": "1.0.0",
        "scope": "user",
        "platforms": ["linux"],
        "name": "Demo Server",
        "summary": "Fixture server for the threat-level self-test",
        "keywords": ["demo"],
        "transports": [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        "source": {"type": "git", "url": "https://example.invalid/demo.git"},
        "tools": tools,
    }


@contextlib.contextmanager
def fixture(tools, *, trust="community"):
    """Build a one-server registry whose tools the case chooses.

    Everything but the tool list is coherent — a real manifest hash, a matching
    inline embedding — so the only thing a case can trip is the threat-level
    check it is about.
    """
    doc = manifest(tools)
    real_hash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-threat-selftest-") as tmp:
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
            "platforms": doc["platforms"],
            "trustStatus": trust,
            "integrity": {"manifestSha256": sync_registry.sha256_file(server_dir / "manifest.json")},
            "manifest": MANIFEST_URL,
            "embeddings": {
                "model": MODEL,
                "version": real_hash[:16],
                "server": VECTOR,
                "tools": {},
            },
        }
        (root / "registry.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "updated": "2026-01-01T00:00:00Z",
                    "embedding_spec": {
                        "model": MODEL,
                        "dimensions": len(VECTOR),
                        "provider": "ollama",
                        "canonical_fields": ["name", "summary", "keywords", "tools"],
                    },
                    "servers": {SERVER_ID: entry},
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


def validate(*args):
    """Call the real validate main() with argv, capturing exit code and output."""
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


@case
def a_fully_declared_manifest_passes():
    tools = [
        {"name": "list_windows", "description": "List windows", "threat_level": "safe"},
        {"name": "type_text", "description": "Type text", "threat_level": "dangerous"},
    ]
    with fixture(tools):
        code, out = validate()
        check(code == 0, "a fully-declared manifest passes")
        check("threat_level" not in out, "a fully-declared manifest raises no threat_level finding")
        check("::error::" not in out, "a fully-declared manifest raises no error")


@case
def a_tool_missing_threat_level_errors():
    tools = [
        {"name": "list_windows", "description": "List windows", "threat_level": "safe"},
        {"name": "sync", "description": "Sync something"},
    ]
    with fixture(tools):
        code, out = validate()
        check(code == 1, "a tool missing threat_level fails the gate")
        check("::error::" in out, "the missing threat_level is annotated as an error")
        check("'sync'" in out, "the error names the offending tool")
        check(
            "declares neither 'threat_level'" in out,
            "the error explains both accepted ways to classify the tool",
        )
        check(
            "runs it unconfirmed" in out,
            "the error explains the consequence — the gate treats it as safe",
        )


@case
def an_invalid_threat_level_errors():
    tools = [{"name": "apply", "description": "Apply changes", "threat_level": "critical"}]
    with fixture(tools):
        code, out = validate()
        check(code == 1, "an out-of-enum threat_level fails the gate")
        check("::error::" in out, "the invalid value is annotated as an error")
        check("'critical'" in out, "the error names the bad value")
        check("'apply'" in out, "the error names the offending tool")


@case
def the_legacy_confirmation_required_is_accepted():
    # A pre-threat_level manifest declaring confirmation_required: true is the
    # older spelling of `elevated` — accepted so it need not migrate in the same
    # PR that this gate lands in.
    tools = [{"name": "send_message", "description": "Send a message", "confirmation_required": True}]
    with fixture(tools):
        code, out = validate()
        check(code == 0, "confirmation_required: true stands in for threat_level")
        check("::error::" not in out, "the legacy shorthand raises no error")

    # confirmation_required: false is NOT a classification — it is the absence of
    # one, so the tool is still unclassified and must error.
    tools = [{"name": "send_message", "description": "Send a message", "confirmation_required": False}]
    with fixture(tools):
        code, out = validate()
        check(code == 1, "confirmation_required: false does not classify the tool")
        check("'send_message'" in out, "the still-unclassified tool is named")


@case
def a_removed_entry_is_exempt():
    tools = [{"name": "sync", "description": "Sync something"}]  # no threat_level
    with fixture(tools, trust="removed"):
        code, out = validate()
        check(code == 0, "a removed entry with an unclassified tool passes")
        check(
            "declares neither 'threat_level'" not in out,
            "a removed entry is not asked to classify a tool it will never run",
        )


@case
def a_deprecated_entry_is_exempt():
    tools = [{"name": "sync", "description": "Sync something"}]  # no threat_level
    with fixture(tools, trust="deprecated"):
        code, out = validate()
        check(code == 0, "a deprecated entry with an unclassified tool passes")
        check(
            "declares neither 'threat_level'" not in out,
            "a deprecated entry is on its way out and is not asked to classify",
        )


@case
def every_enum_value_is_accepted():
    for level in sorted(validate_registry.ALLOWED_THREAT_LEVELS):
        tools = [{"name": "do_thing", "description": "Do a thing", "threat_level": level}]
        with fixture(tools):
            code, out = validate()
            check(code == 0, f"threat_level {level!r} is accepted")
            check("::error::" not in out, f"threat_level {level!r} raises no error")


def main() -> int:
    global running

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        fn()

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} threat-level self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: threat-level self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
