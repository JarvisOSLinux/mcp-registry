#!/usr/bin/env python3
"""selftest_categories.py — prove the category and fixture checks actually fire.

Categories are the one piece of entry metadata that never reaches the embedding
text (EMBEDDING-SPEC.md), so nothing about a wrong one is loud: a typo does not
move a similarity score, it just drops the entry out of every catalogue view
that selects on that term, silently and forever. The same is true in reverse for
`fixture` — dmcp drops flagged entries from vector search, so a fixture that
does not declare itself keeps competing with real servers for the top-k slots a
consumer query returns, and the only symptom is a worse answer.

Both checks are therefore exactly the kind that rot unnoticed, and a validator
that has quietly stopped checking looks identical to a registry with nothing
wrong. Each case below builds a throwaway registry in a temp directory, runs the
real validate entry point against it, and asserts on what it reports —
including the cases that must stay silent, so a check that fires too eagerly is
caught too.

  1. An entry with a capability category passes, silently.
  2. A missing `categories` ERRORS.
  3. An empty or non-list `categories` ERRORS.
  4. A term outside the closed vocabulary ERRORS — the typo case.
  5. A non-string term ERRORS without aborting the run on an unhashable value.
  6. Registry-internal terms alone ERROR: nothing consumer-facing can list it.
  7. A flagged fixture is exempt from 6 — being unlistable is the point.
  8. A non-boolean `fixture` ERRORS; dmcp reads it as a flag, and anything else
     reads as absent.
  9. A fixture at trustStatus `official` ERRORS — that tier lifts the threat
     floor for declared-safe tools (Project-JARVIS #223), and a test payload
     must never be the thing that lifts it.
 10. `fixture: false` and an absent flag behave identically.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_categories.py
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
# `categories` and `fixture` are both outside the canonical embedding text, so a
# coherent vector stays coherent however a case fiddles them — the embedding
# check adds no noise to what these cases assert on.
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
        "summary": "Fixture server for the category self-test",
        "keywords": ["demo"],
        "transports": [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        # Pinned, so a case may set trustStatus `official` without tripping the
        # separate rule that tier carries (TRUST-MODEL.md §3): an unpinned
        # source would clone the branch head, so the review would bind nothing.
        "source": {
            "type": "git",
            "url": "https://example.invalid/demo.git",
            "rev": "0" * 40,
        },
        "tools": [{"name": "ping", "description": "Reply with pong", "threat_level": "safe"}],
    }


# Sentinel distinct from None, so a case can ask for "no categories key at all"
# as well as "categories set to None".
OMIT = object()


@contextlib.contextmanager
def fixture(categories=("productivity",), *, trust="community", flag=OMIT):
    """Build a one-server registry whose categories/fixture the case chooses.

    Everything else is coherent — a real manifest hash, a matching inline
    embedding, a declared threat level — so the only thing a case can trip is
    the check it is about.
    """
    doc = manifest()
    real_hash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-category-selftest-") as tmp:
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
        if categories is not OMIT:
            entry["categories"] = list(categories) if isinstance(categories, tuple) else categories
        if flag is not OMIT:
            entry["fixture"] = flag

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
def a_capability_category_passes_silently():
    with fixture(("productivity",)):
        code, out = validate()
        check(code == 0, "an entry with a capability category passes")
        check("categor" not in out, "it raises no category finding")
        check("::error::" not in out, "it raises no error at all")


@case
def missing_categories_is_an_error():
    with fixture(OMIT):
        code, out = validate()
        check(code != 0, "a missing 'categories' fails the gate")
        check("missing 'categories'" in out, "the finding names the missing field")


@case
def malformed_categories_is_an_error():
    for value, label in (([], "empty list"), ("productivity", "bare string"), ({}, "object")):
        with fixture(value):
            code, out = validate()
            check(code != 0, f"categories as a {label} fails the gate")
            check(
                "must be a non-empty array" in out,
                f"the {label} finding says what the field must be",
            )


@case
def an_unknown_term_is_an_error():
    # The typo case: one character off a real term, which no other check sees.
    with fixture(("productivty",)):
        code, out = validate()
        check(code != 0, "a term outside the vocabulary fails the gate")
        check("'productivty' not in" in out, "the finding quotes the offending term")


@case
def a_non_string_term_does_not_abort_the_run():
    # An unhashable value would take the whole gate down with a TypeError before
    # it reported anything, so every other entry would go unchecked. Same
    # isinstance-first rule validate_platforms follows.
    with fixture((["nested"],)):
        code, out = validate()
        check(code != 0, "a non-string category fails the gate")
        check("not in" in out, "it is reported as a category finding, not a traceback")
        check("Traceback" not in out, "the run reports rather than crashing")


@case
def registry_internal_terms_alone_are_an_error():
    with fixture(("mcp", "mcp-development")):
        code, out = validate()
        check(code != 0, "an entry with only registry-internal terms fails the gate")
        check("no capability category" in out, "the finding explains it cannot be listed")


@case
def a_flagged_fixture_may_be_registry_internal_only():
    with fixture(("mcp", "mcp-testing"), flag=True):
        code, out = validate()
        check(code == 0, "a flagged fixture needs no capability category")
        check("no capability category" not in out, "the capability check skips it")


@case
def a_non_boolean_fixture_flag_is_an_error():
    for value, label in (("true", "string"), (1, "int"), (None, "null")):
        with fixture(flag=value):
            code, out = validate()
            check(code != 0, f"fixture as a {label} fails the gate")
            check("must be true or false" in out, f"the {label} finding says what it must be")


@case
def an_official_fixture_is_an_error():
    with fixture(("mcp", "mcp-testing"), trust="official", flag=True):
        code, out = validate()
        check(code != 0, "a fixture at trustStatus 'official' fails the gate")
        check(
            "must not be trustStatus 'official'" in out,
            "the finding names the tier that lifts the threat floor",
        )
    # The same tier is fine for anything that is not a fixture.
    with fixture(("productivity",), trust="official"):
        code, out = validate()
        check(code == 0, "an ordinary entry may still be 'official'")


@case
def an_explicit_false_flag_reads_as_absent():
    with fixture(("productivity",), flag=False):
        code, out = validate()
        check(code == 0, "fixture: false passes like an absent flag")
        check("fixture" not in out, "it raises no fixture finding")
    # ...and does not buy an exemption the absent flag would not buy either.
    with fixture(("mcp", "mcp-development"), flag=False):
        code, out = validate()
        check(code != 0, "fixture: false does not exempt the capability check")


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
    print("\nOK: category self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
