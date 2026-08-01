#!/usr/bin/env python3
"""selftest_embedding_drift.py — prove the embedding checks actually fire.

Issue #72: validate_registry.py had zero embedding checks, which is how four
servers sat silently drifted (jarvis-shell, jarvis-shell-system) or with no
vectors at all (email, caldav) under a run that printed "registry validation
passed". An embedding is a claim about text — this vector is what the model
produced for THIS name, summary, keywords and tool descriptions — and editing
any of them makes the claim quietly false. Nothing else in the gate can see
that: the integrity hashes cover the file's bytes, not the meaning the vectors
encode.

The checks report as WARNINGS, which is the deliberate part and therefore the
part most worth testing. Failing would deadlock the workflow: a vector can only
be produced by Ollama, which lives solely in the manually dispatched Generate
Embeddings workflow, and that workflow embeds what is on `main` — so an error
would block a one-word tool-description fix until someone regenerated vectors
for text that has not merged. `--strict-embeddings` is there for anyone who
wants the harder rule, and it is tested too.

Each case builds a throwaway registry in a temp directory, runs the real
validate entry point against it, and asserts on what it reports:

  1. A coherent entry is silent — no warning, no error, exit 0.
  2. A manifest with no vector at all is reported (email / caldav's shape).
  3. A manifest edited after embedding is reported as STALE (jarvis-shell's).
  4. registry.json's inline copy drifting from the manifest is reported
     separately, since sync-index reads the inline copy.
  5. A vector whose width disagrees with embedding_spec is reported.
  6. Every one of those becomes an error under --strict-embeddings, and the run
     exits non-zero.
  7. A `removed` entry is exempt: dmcp refuses to install it, so what its
     vectors would rank is moot.
  8. The validator uses generate_embeddings' own canonical_text/canonical_hash
     objects, not a second definition that could drift from them.
  9. A run carrying warnings never prints a bare "passed" line.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_embedding_drift.py
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


def manifest(**extra):
    doc = {
        "version": "1.0.0",
        "scope": "user",
        "platforms": ["linux"],
        "name": "Demo Server",
        "summary": "Fixture server for the embedding self-test",
        "keywords": ["demo"],
        "transports": [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        "source": {"type": "git", "url": "https://example.invalid/demo.git"},
        "tools": [{"name": "ping", "description": "Reply with pong"}],
    }
    doc.update(extra)
    return doc


@contextlib.contextmanager
def fixture(doc, *, manifest_hash=None, inline_version=None, inline_vector=None,
            drop_manifest_embedding=False, drop_inline=False, trust="community"):
    """Build a one-server registry whose embedding state the case chooses.

    The default is coherent: the manifest's recorded hash IS its canonical
    hash, and the inline copy agrees. Each keyword breaks exactly one of those
    agreements, so a case names the one thing it is about.
    """
    saved_cwd = os.getcwd()
    real_hash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
    with tempfile.TemporaryDirectory(prefix="mcp-registry-embed-selftest-") as tmp:
        root = pathlib.Path(tmp)
        server_dir = root / "servers" / "demo"
        server_dir.mkdir(parents=True)

        if not drop_manifest_embedding:
            doc = dict(
                doc,
                embeddings={MODEL: {"v": VECTOR, "hash": manifest_hash or real_hash}},
            )
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
        }
        if not drop_inline:
            entry["embeddings"] = {
                "model": MODEL,
                "version": inline_version or real_hash[:16],
                "server": inline_vector or VECTOR,
                "tools": {},
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
            yield root, real_hash
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
def a_coherent_entry_is_silent():
    with fixture(manifest()):
        code, out = validate()
        check(code == 0, "a coherent entry passes")
        check("::warning::" not in out, "a coherent entry raises no warning")
        check("::error::" not in out, "a coherent entry raises no error")
        check(out.strip().endswith("OK: registry validation passed."), "the run reports a clean pass")

        code, out = validate("--strict-embeddings")
        check(code == 0, "a coherent entry passes under --strict-embeddings too")


@case
def a_missing_embedding_is_reported():
    with fixture(manifest(), drop_manifest_embedding=True, drop_inline=True):
        code, out = validate()
        check(code == 0, "a server with no vectors does not fail the gate")
        check("::warning::" in out, "a server with no vectors is reported")
        check(
            "carries no 'nomic-embed-text' embedding" in out,
            "the warning says the manifest has no vector",
        )
        check(
            "no inline embedding" in out,
            "the warning also says registry.json has nothing for sync-index",
        )
        check(SERVER_ID in out, "the warning names the server")


@case
def an_edited_manifest_is_reported_as_stale():
    # A tool-description edit is the everyday case: it changes what the server
    # says without touching anything the integrity hashes would notice.
    edited = manifest(tools=[{"name": "ping", "description": "Reply with pong, but faster"}])
    stale = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(manifest()))
    with fixture(edited, manifest_hash=stale, inline_version=stale[:16]):
        code, out = validate()
        check(code == 0, "an edited manifest does not fail the gate")
        check("STALE" in out, "the drift is reported as stale, unmistakably")
        check(
            "describes an older edition" in out,
            "the warning explains what a stale vector actually means",
        )
        check(
            "Generate Embeddings" in out,
            "the summary names the workflow that regenerates vectors",
        )
        check(
            "cannot be fixed from an ordinary PR checkout" in out,
            "the summary says why this is not something the PR author can just run",
        )
        check(
            out.strip().endswith("with 2 warning(s)."),
            "a run with findings counts them instead of printing a bare 'passed' line",
        )


@case
def an_out_of_step_inline_copy_is_reported():
    # sync-index reads the inline copy, so a registry.json that disagrees with
    # its manifest is the version users actually get.
    with fixture(manifest(), inline_version="0000000000000000"):
        code, out = validate()
        check(code == 0, "an out-of-step inline copy does not fail the gate")
        check("inline embedding version" in out, "the inline mismatch is reported on its own")
        check(
            "registry.json and the manifest disagree" in out,
            "the warning names both sides of the disagreement",
        )
        check("STALE" not in out, "the manifest itself is not blamed for the index's drift")


@case
def a_wrong_width_vector_is_reported():
    with fixture(manifest(), inline_vector=[0.0, 1.0]):
        code, out = validate()
        check(code == 0, "a mis-sized vector does not fail the gate")
        check("2 dimensions" in out and "declares 4" in out, "the warning names both widths")
        check(
            "one index against one query" in out,
            "the warning explains why a mixed-width index is broken",
        )


@case
def strict_mode_promotes_every_finding():
    scenarios = [
        ("no vectors", dict(drop_manifest_embedding=True, drop_inline=True)),
        ("an out-of-step inline copy", dict(inline_version="0000000000000000")),
        ("a mis-sized vector", dict(inline_vector=[0.0, 1.0])),
    ]
    for label, kwargs in scenarios:
        with fixture(manifest(), **kwargs):
            code, out = validate("--strict-embeddings")
            check(code == 1, f"--strict-embeddings fails on {label}")
            check("::error::" in out, f"{label} is annotated as an error under strict")
            check("::warning::" not in out, f"{label} is not ALSO reported as a warning")

            code, out = validate()
            check(code == 0, f"{label} is only a warning by default")


@case
def a_removed_entry_is_exempt():
    with fixture(manifest(), drop_manifest_embedding=True, drop_inline=True, trust="removed"):
        code, out = validate()
        check(code == 0, "a removed entry with no vectors passes")
        check("embedding" not in out, "a removed entry is not asked for vectors it will never serve")

        code, _ = validate("--strict-embeddings")
        check(code == 0, "a removed entry is exempt under --strict-embeddings too")


@case
def the_canonical_definition_is_shared_not_copied():
    # Two definitions of "the text that was embedded" would drift apart, and the
    # drift would surface as a gate that passes stale data while looking checked.
    check(
        validate_registry.canonical_text is generate_embeddings.canonical_text,
        "the validator uses generate_embeddings' canonical_text object itself",
    )
    check(
        validate_registry.canonical_hash is generate_embeddings.canonical_hash,
        "the validator uses generate_embeddings' canonical_hash object itself",
    )
    check(
        validate_registry.DEFAULT_MODEL is generate_embeddings.DEFAULT_MODEL,
        "both scripts assume the same model when embedding_spec is silent",
    )
    doc = manifest()
    check(
        generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
        != generate_embeddings.canonical_hash(
            generate_embeddings.canonical_text(
                manifest(tools=[{"name": "ping", "description": "Something else"}])
            )
        ),
        "a tool-description edit changes the canonical hash the gate compares",
    )


def main() -> int:
    global running

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        fn()

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} embedding self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: embedding-drift self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
