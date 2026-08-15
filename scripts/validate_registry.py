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
  - per-transport platforms (manifest transports[].platforms) use the same enum,
    and no transport is shadowed by an earlier one that already matches its hosts
  - the manifest URL is hosted by this registry and resolves to an existing
    servers/<dir>/manifest.json
  - every tool of a live entry declares a threat_level (safe/elevated/dangerous/
    forbidden) or the legacy confirmation_required: true — a tool that classifies
    what it can do to the host, so the daemon's confirmation gate is not blind to
    a destructive tool hiding under an unfamiliar name (only `removed` is exempt;
    a `deprecated` server is still installable via the human CLI)
  - integrity.manifestSha256 is present and matches the manifest file bytes
  - an 'official' entry whose manifest has a git source pins it to a full
    40-character commit SHA — the only pin dmcp verifies
  - setup scripts and their hashes agree in both directions: a setup.sh /
    setup.ps1 in the server directory needs a recorded hash that matches, and a
    recorded hash needs the script it claims to verify
  - setupScript / setupScriptWindows resolve to a committed, hashed script —
    never to an off-registry URL dmcp would fetch and execute unverified
  - no orphan directory: every first-party servers/<dir>/ is referenced by an
    entry (catches a half-done removal that dropped the entry but left the dir)

Warnings (reported, never fatal):
  - a vetted top-level platform that no transport can serve
  - embeddings that are missing, or stale against the manifest they claim to
    describe (see validate_embeddings / report_embeddings for why these are
    warnings, and --strict-embeddings to make them errors)

Trust-promotion gate (when --base is given):
  - compares trustStatus per entry against the base registry.json
  - if any entry is promoted to 'official' (or added directly as 'official'),
    a maintainer approval is REQUIRED: the run fails unless
    --approval-label-present is passed. This is the rule that stops a submitter
    from self-assigning the official tier.
  - the same approval is REQUIRED to leave a revoked state (deprecated/removed
    -> community/official). Revocation is the registry's only kill switch, so
    turning it back off is a trust-raising act, not routine editing.
  - a changed or newly added setup script (setup.sh / setup.ps1) is reported as
    a warning (advisory) — both run on users' machines, so both want eyes.
  - a changed manifest is reported the same way, naming the transport commands
    it now carries — the manifest is what runs on every call, not just install.

Usage:
  python3 scripts/validate_registry.py
  python3 scripts/validate_registry.py --strict-embeddings
  python3 scripts/validate_registry.py --base base_registry.json
  python3 scripts/validate_registry.py --base base_registry.json --approval-label-present
"""
import argparse
import json
import pathlib
import sys

# Imported, never re-derived: canonical_text is the definition of "the text that
# was embedded", and a second copy of it here would drift from the generator's
# — leaving a gate that passes stale vectors while looking like it checked them.
from generate_embeddings import DEFAULT_MODEL, canonical_hash, canonical_text
from sync_registry import (
    POSIX_SETUP_SCRIPT,
    SETUP_SCRIPTS,
    WINDOWS_SETUP_SCRIPT,
    dir_from_url,
    sha256_file,
)

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")
MANIFEST_FILE = "manifest.json"

ALLOWED_TRUST = {"community", "official", "deprecated", "removed"}
ALLOWED_SCOPE = {"user", "system"}

# Revocation is the registry's only kill switch — dmcp refuses `removed` on both
# the human and the agent path. Leaving one of these states re-arms a server the
# maintainers disarmed, so it needs the same signal as a promotion rather than
# passing as an ordinary one-word edit.
REVOKED_TRUST = {"deprecated", "removed"}

# dmcp fetches this URL on every install. The recorded hash makes a substituted
# body fail closed, so an off-registry host is not a code-execution path — but it
# points installs at content this repo cannot review, update, or revoke, which is
# the whole job of the catalogue.
MANIFEST_URL_PREFIX = "https://raw.githubusercontent.com/JarvisOSLinux/mcp-registry/"
ALLOWED_PLATFORMS = {"linux", "darwin", "windows"}
ALLOWED_THREAT_LEVELS = {"safe", "elevated", "dangerous", "forbidden"}
REQUIRED_FIELDS = ("id", "name", "summary", "version", "scope", "trustStatus", "manifest")

# Manifest field naming a setup script, paired with the only filename that field
# may resolve to in this registry. Both are executed on the user's machine, so
# both must land on a committed file that sync_registry.py hashes.
SETUP_SCRIPT_FIELDS = (
    ("setupScript", POSIX_SETUP_SCRIPT),
    ("setupScriptWindows", WINDOWS_SETUP_SCRIPT),
)
INTEGRITY_KEY = dict(SETUP_SCRIPTS)

# A revoked entry is a tombstone: dmcp refuses to install it, so what its
# vectors would rank is moot. Everything else in the catalogue is discoverable
# and must therefore be discoverable from what it actually says today.
EMBEDDING_EXEMPT_TRUST = {"removed"}

# Only a `removed` tombstone is exempt: dmcp refuses to install it on both the
# human CLI and the agent path, so a tool it will never run need not classify
# itself. A `deprecated` entry is NOT exempt — cli_trust_gate warns and the
# install PROCEEDS (dmcp install.rs), so a human can still install and run it,
# and an unclassified tool reaches the daemon's confirmation gate and runs
# unconfirmed. This is the same "still installable" line EMBEDDING_EXEMPT_TRUST
# draws (removed only) — not remove_server.py's removability line, which counts
# deprecated as excisable for a different reason (it is on its way out).
THREAT_LEVEL_EXEMPT_TRUST = {"removed"}

EMBEDDING_SUMMARY = (
    "{count} embedding problem(s) above: semantic search ranks these servers "
    "from text they no longer carry, or cannot rank them at all. Regenerate "
    "with the 'Generate Embeddings' workflow (Actions -> Generate Embeddings) "
    "and merge the PR it opens. Vectors need Ollama, so this cannot be fixed "
    "from an ordinary PR checkout."
)


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

    # isinstance first: a nested array or object is unhashable, so a bare set
    # lookup would abort the whole gate with a traceback instead of reporting
    # this entry and carrying on to the rest of the registry.
    for value in platforms:
        if not isinstance(value, str) or value not in ALLOWED_PLATFORMS:
            errors.append(
                f"{where}: platform {value!r} not in {sorted(ALLOWED_PLATFORMS)}"
            )


def validate_transports(
    where: str, manifest: dict, entry: dict, errors: list, warnings: list
) -> None:
    """Check per-transport `platforms`, transport order, and servable platforms.

    A transport may narrow itself to the hosts it can launch on, so one entry can
    spell its command `python3` on POSIX and `python` on Windows. dmcp picks the
    first transport whose list includes the host and a transport without the
    field matches every host, which makes order load-bearing: a transport that an
    earlier one already matches can never be selected on any host, at any point
    in time. That is dead configuration and an error.

    An entry whose transports collectively miss a vetted platform leaves that
    host with nothing to launch. That one is a warning, not an error: the
    transport may legitimately land in a later PR than the platform it serves.
    """
    transports = manifest.get("transports")
    if transports is None:
        # An absent array is the most complete case of "nothing to launch", so
        # fall through to the servability check rather than passing in silence.
        transports = []
    elif not isinstance(transports, list):
        errors.append(
            f"{where}: 'transports' must be an array — dmcp cannot deserialize a "
            f"{type(transports).__name__} here, so the manifest declares no "
            f"launchable entrypoint"
        )
        return

    matches_any_host = False
    covered: set = set()
    catch_all_at = None

    for position, transport in enumerate(transports):
        if not isinstance(transport, dict):
            continue
        at = f"{where}: transports[{position}]"

        if "platforms" not in transport:
            matches_any_host = True
            if catch_all_at is None:
                catch_all_at = position
            continue

        platforms = transport["platforms"]
        if not isinstance(platforms, list) or not platforms:
            errors.append(
                f"{at}: 'platforms' must be a non-empty array — omit the field "
                f"for a transport that runs on every host"
            )
            continue

        declared = set()
        for value in platforms:
            if not isinstance(value, str) or value not in ALLOWED_PLATFORMS:
                errors.append(f"{at}: platform {value!r} not in {sorted(ALLOWED_PLATFORMS)}")
            else:
                declared.add(value)

        if catch_all_at is not None:
            errors.append(
                f"{at}: unreachable — transports[{catch_all_at}] declares no "
                f"'platforms', so it matches every host and dmcp selects it "
                f"first. Move this transport ahead of it (order most-specific "
                f"first)."
            )
        elif declared and declared <= covered:
            errors.append(
                f"{at}: unreachable — platform(s) {sorted(declared)} are already "
                f"claimed by an earlier transport, which dmcp selects first."
            )

        # After the shadow test, so a transport is never compared with itself.
        covered |= declared

    if matches_any_host:
        return

    vetted = entry.get("platforms")
    if not isinstance(vetted, list):
        return

    unservable = [p for p in vetted if isinstance(p, str) and p not in covered]
    if unservable:
        warnings.append(
            f"{where}: vetted platform(s) {unservable} have no matching transport — "
            f"dmcp has nothing to launch there (add a transport carrying that "
            f"platform, or drop it from 'platforms')"
        )


def validate_setup_scripts(
    where: str,
    dir_name: str,
    manifest_url: str,
    manifest: dict,
    integrity: dict,
    errors: list,
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
                f"servers/{dir_name}/{filename} does not exist — run sync_registry.py"
            )

    # A setup script is hashed by filename, so a value naming anything else has
    # no hash behind it. A URL is the dangerous spelling: dmcp fetches an
    # https:// setup script straight from the network and runs it, and only a
    # recorded hash makes it verify first — so the sole URL this registry
    # accepts is the one pointing back at the committed sibling of the manifest.
    for field, filename in SETUP_SCRIPT_FIELDS:
        declared = manifest.get(field)
        if not isinstance(declared, str) or not declared:
            continue

        if "://" in declared:
            hosted = manifest_url[: -len(MANIFEST_FILE)] + filename
            if declared != hosted:
                errors.append(
                    f"{where}: {field} '{declared}' is not hosted by this registry, "
                    f"so no integrity.{INTEGRITY_KEY[filename]} covers it and dmcp "
                    f"would fetch and run it unverified — commit "
                    f"servers/{dir_name}/{filename} and name it '{filename}' "
                    f"(or point at '{hosted}')"
                )
                continue
        elif declared != filename:
            errors.append(
                f"{where}: {field} '{declared}' — a setup script in this registry "
                f"must be named '{filename}', the only name sync_registry.py hashes"
            )
            continue

        if not (SERVERS_DIR / dir_name / filename).exists():
            errors.append(
                f"{where}: manifest declares {field} '{declared}' but "
                f"servers/{dir_name}/{filename} does not exist"
            )


def validate_threat_levels(where: str, manifest: dict, errors: list) -> None:
    """Every tool a live server exposes must classify what it can do to the host.

    JARVIS decides whether a tool call needs the user's confirmation from the
    strictest of three sources: a host floor keyed on well-known tool names, this
    manifest field, and a scan of the call's actual arguments. The host floor is
    a list of names the daemon already knows, so a genuinely destructive tool
    under a name it does not recognise (`apply`, `sync`, `type_text`) is invisible
    to it — absent this field, such a tool classifies `safe` and runs unconfirmed.

    So a tool that declares neither `threat_level` nor the legacy
    `confirmation_required: true` is a hole in the confirmation gate, not a
    stylistic omission. The catalogue's tools are fully classified today; making
    the omission an ERROR is what stops the next merged server from silently
    reopening the gap. An unknown threat_level string is an error for the same
    reason a malformed platform is: a value the daemon cannot map is not a
    classification.
    """
    tools = manifest.get("tools")
    if not isinstance(tools, list):
        # The shape of `tools` is the Tools contract's own concern; a non-list
        # is a manifest problem this check is not the right place to report.
        return

    for position, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or f"tools[{position}]"
        level = tool.get("threat_level")

        if level is None:
            # The legacy shorthand: confirmation_required: true is an older
            # spelling of `elevated`, still accepted so pre-threat_level
            # manifests are not forced to migrate in the same PR.
            if tool.get("confirmation_required") is True:
                continue
            errors.append(
                f"{where}: tool '{name}' declares neither 'threat_level' "
                f"({'|'.join(sorted(ALLOWED_THREAT_LEVELS))}) nor the legacy "
                f"'confirmation_required: true' — every tool a live server exposes "
                f"must classify what it can do to the host, or the confirmation "
                f"gate treats it as 'safe' and runs it unconfirmed"
            )
        elif level not in ALLOWED_THREAT_LEVELS:
            errors.append(
                f"{where}: tool '{name}' threat_level {level!r} not in "
                f"{sorted(ALLOWED_THREAT_LEVELS)}"
            )
def is_full_commit_sha(rev: str) -> bool:
    """Mirror of dmcp's `install.rs::is_full_commit_sha` — the only pin it verifies.

    dmcp checks out whatever `rev` names, but re-reads HEAD and compares it back
    only when the rev is a full SHA. A tag or short rev is checked out and never
    verified, so a moved tag silently substitutes different code: it reads like a
    pin and binds nothing. Keep this predicate identical to dmcp's — a pin this
    file accepts but dmcp does not verify is worse than no pin, because it looks
    like one.
    """
    return len(rev) == 40 and all(c in "0123456789abcdefABCDEF" for c in rev)


def validate_manifest_url(where: str, url: str, errors: list) -> None:
    """Require the manifest to be served from this registry."""
    if not url.startswith(MANIFEST_URL_PREFIX):
        errors.append(
            f"{where}: manifest URL '{url}' is not hosted by this registry — dmcp "
            f"fetches it on every install, so it must be a "
            f"'{MANIFEST_URL_PREFIX}...' URL whose bytes this repo controls and "
            f"can revoke"
        )


def validate_source_pin(where: str, entry: dict, manifest: dict, errors: list) -> None:
    """An `official` entry must pin the commit its review actually covered.

    docs/TRUST-MODEL.md §3 makes pinning a condition of the tier, and the reason
    is mechanical: with no `rev`, dmcp clones `--depth 1` and installs whatever
    the branch head is on the day of the install, so the source review the tier
    records binds none of the code the user runs. `community` is deliberately
    exempt — that tier says "you are trusting the submitter", and tracking a
    branch is a coherent thing for it to mean.
    """
    if entry.get("trustStatus") != "official":
        return

    source = manifest.get("source")
    if not isinstance(source, dict) or not source.get("url"):
        return

    rev = source.get("rev")
    rev = rev.strip() if isinstance(rev, str) else ""

    if not rev:
        errors.append(
            f"{where}: trustStatus 'official' but the manifest's source has no "
            f"'rev' — dmcp would clone the branch head, so the maintainer review "
            f"this tier records would not bind the installed code "
            f"(docs/TRUST-MODEL.md §3)"
        )
    elif not is_full_commit_sha(rev):
        errors.append(
            f"{where}: trustStatus 'official' but source.rev '{rev}' is not a full "
            f"40-character commit SHA — dmcp verifies HEAD only against a full "
            f"SHA, so a tag or short rev is checked out unverified and can move "
            f"under the review"
        )


def validate_embeddings(where: str, entry: dict, manifest: dict, spec: dict, notes: list) -> None:
    """Check that the stored vectors were computed from the manifest as it is now.

    An embedding is a claim about text: this vector is what the model produced
    for THIS name, summary, keywords and tool descriptions. Edit any of them and
    the claim goes quietly false — dmcp keeps ranking the server, just from a
    description it no longer has, and nothing in the rest of this gate can see
    it. The integrity hashes cover the file's bytes; they say nothing about
    whether the meaning those bytes carry is the meaning the vectors encode.

    Three separate ways it breaks, so three separate findings:
      - the manifest has no vector for the model at all (nothing to rank with);
      - the manifest's recorded canonical hash no longer matches its own text
        (the vector describes a previous edition of this server);
      - registry.json's inline copy is out of step with the manifest (dmcp
        sync-index reads the inline copy, so this is the one users get).
    """
    model = spec.get("model") or DEFAULT_MODEL
    text = canonical_text(manifest)
    if not text.strip():
        # Nothing embeddable; generate_embeddings.py skips these too, so
        # demanding a vector here would demand one nothing can produce.
        return
    expected = canonical_hash(text)

    manifest_block = (manifest.get("embeddings") or {}).get(model)
    if not isinstance(manifest_block, dict) or not manifest_block.get("v"):
        notes.append(
            f"{where}: manifest carries no '{model}' embedding — the server "
            f"cannot be found by semantic search at all"
        )
    elif manifest_block.get("hash") != expected:
        recorded = str(manifest_block.get("hash"))
        notes.append(
            f"{where}: manifest embedding is STALE — recorded for canonical text "
            f"{recorded[:12]}…, but the manifest's text now hashes to "
            f"{expected[:12]}…, so the stored vector describes an older edition "
            f"of this server"
        )

    inline = entry.get("embeddings")
    if not isinstance(inline, dict) or not inline.get("server"):
        notes.append(
            f"{where}: registry entry has no inline embedding — 'dmcp sync-index' "
            f"loads vectors from this index, so there is nothing for it to load"
        )
        return

    if inline.get("model") != model:
        notes.append(
            f"{where}: inline embedding model {inline.get('model')!r} is not the "
            f"registry's {model!r}"
        )
    if inline.get("version") != expected[:16]:
        notes.append(
            f"{where}: inline embedding version {inline.get('version')!r} does not "
            f"match the manifest's canonical hash {expected[:16]!r} — registry.json "
            f"and the manifest disagree about what was embedded"
        )

    dimensions = spec.get("dimensions")
    vector = inline.get("server")
    if isinstance(dimensions, int) and isinstance(vector, list) and len(vector) != dimensions:
        notes.append(
            f"{where}: inline embedding has {len(vector)} dimensions, but "
            f"embedding_spec declares {dimensions} — dmcp scores every vector in "
            f"one index against one query"
        )


def report_embeddings(notes: list, strict: bool, errors: list, warnings: list) -> None:
    """Fold the embedding findings into the run at the chosen severity.

    Warnings by default, and that is a judgement, not timidity.

    Failing would deadlock the workflow. A vector can only be produced by
    Ollama, which exists in this repo solely inside the manually dispatched
    Generate Embeddings workflow — and that workflow embeds what is on `main`.
    So a PR that fixes a typo in a tool description would be unmergeable until
    someone regenerated vectors for text that has not merged yet. The gate would
    block the very change it is asking for.

    It is also the wrong severity. Every error in this file guards something a
    client executes or trusts: an unverified setup script, a hash that covers
    nothing, an entry offered to a host nobody vetted. A stale vector degrades
    *ranking* — the server is still described, still installed from a
    hash-verified manifest, still gated by trustStatus. The unservable-platform
    warning already draws this line for the same reason: real, worth saying,
    fixable in a later PR.

    What the gate must not do is what it did before this check existed: print
    "registry validation passed" over four drifted servers and say nothing. Each
    finding is now annotated against its server on every run, with a summary
    naming the count and the one workflow that clears it. --strict-embeddings
    promotes the lot to errors for anyone who wants the harder rule — a
    maintainer sweeping the catalogue, or a scheduled job that should fail loudly.
    """
    if not notes:
        return
    (errors if strict else warnings).extend(notes)
    annotate("error" if strict else "warning", EMBEDDING_SUMMARY.format(count=len(notes)))


def validate_static(registry: dict, errors: list, warnings: list, embeddings: list) -> None:
    if "servers" not in registry or not isinstance(registry["servers"], dict):
        errors.append("registry.json: missing or malformed 'servers' object")
        return

    spec = registry.get("embedding_spec")
    if not isinstance(spec, dict):
        spec = {}

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

        manifest_url = entry.get("manifest", "")
        validate_manifest_url(where, manifest_url, errors)
        dir_name = dir_from_url(manifest_url)
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

        validate_setup_scripts(where, dir_name, manifest_url, manifest, integrity, errors)
        validate_source_pin(where, entry, manifest, errors)
        validate_transports(where, manifest, entry, errors, warnings)
        if entry.get("trustStatus") not in THREAT_LEVEL_EXEMPT_TRUST:
            validate_threat_levels(where, manifest, errors)
        if entry.get("trustStatus") not in EMBEDDING_EXEMPT_TRUST:
            validate_embeddings(where, entry, manifest, spec, embeddings)

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


def transport_commands(server_id: str, entry: dict) -> list:
    """The command lines a manifest would launch, for the review annotation.

    Read from the head manifest rather than diffed against the base: the base
    file is registry.json alone, which carries the manifest's hash and not its
    body. Naming what the manifest says *now* is what a reviewer needs anyway.
    """
    dir_name = dir_from_url(entry.get("manifest", ""))
    if not dir_name:
        return []
    try:
        manifest = json.loads((SERVERS_DIR / dir_name / MANIFEST_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return []

    lines = []
    for transport in manifest.get("transports", []) or []:
        if not isinstance(transport, dict):
            continue
        command = transport.get("command")
        if not command:
            continue
        args = " ".join(str(a) for a in transport.get("args", []) or [])
        lines.append(f"{command} {args}".strip())
    return lines


def validate_promotions(registry: dict, base: dict, approval: bool, errors: list) -> None:
    base_servers = base.get("servers", {}) if isinstance(base, dict) else {}
    promotions = []
    revivals = []
    setup_changes = []
    manifest_changes = []

    for server_id, entry in registry["servers"].items():
        head_trust = entry.get("trustStatus")
        base_entry = base_servers.get(server_id)
        base_trust = base_entry.get("trustStatus") if base_entry else None

        if head_trust == "official" and base_trust != "official":
            promotions.append(server_id)

        # A tombstone is the one state dmcp refuses outright. Lifting it hands
        # the entry back to every client, so it is a promotion in everything but
        # name — and unlike a promotion it needs no new field, which is exactly
        # why it would otherwise slip through as a one-word diff.
        if base_trust in REVOKED_TRUST and head_trust not in REVOKED_TRUST:
            revivals.append(f"{server_id} ({base_trust}->{head_trust})")

        integrity = entry.get("integrity", {})
        base_integrity = (base_entry or {}).get("integrity", {})

        # Both setup scripts execute on the user's machine during install, so
        # both need the "a human read this" signal, not just the POSIX one.
        for filename, integrity_key in SETUP_SCRIPTS:
            head_setup = integrity.get(integrity_key)
            if head_setup and head_setup != base_integrity.get(integrity_key):
                setup_changes.append((server_id, filename))

        # The manifest earns the same signal for a stronger reason: a setup
        # script runs once at install, while transports[].command is what
        # launches on every single call. Flagging the install-time code and not
        # the run-time code had the blast radius backwards.
        head_manifest = integrity.get("manifestSha256")
        if base_entry and head_manifest and head_manifest != base_integrity.get("manifestSha256"):
            manifest_changes.append(server_id)

    for sid, filename in setup_changes:
        annotate("warning", f"{sid}: {filename} added/changed — review the script before merge")

    for sid in manifest_changes:
        commands = transport_commands(sid, registry["servers"][sid])
        launches = "; ".join(commands) if commands else "(no stdio transport)"
        annotate(
            "warning",
            f"{sid}: manifest changed — review it before merge; it now launches: {launches}",
        )

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

    if revivals:
        listed = ", ".join(revivals)
        if approval:
            annotate("notice", f"revocation lifted with maintainer label for: {listed}")
        else:
            errors.append(
                "revocation lifted without maintainer approval for: "
                f"{listed}. A maintainer must apply the 'trust-approved' label "
                "(see docs/TRUST-MODEL.md §4) — dmcp refuses a revoked entry, so "
                "restoring one re-arms it for every client."
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="Path to the base branch's registry.json for diff checks")
    parser.add_argument(
        "--approval-label-present",
        action="store_true",
        help="Set when the PR carries the maintainer 'trust-approved' label",
    )
    parser.add_argument(
        "--strict-embeddings",
        action="store_true",
        help="Fail on stale or missing embeddings instead of warning about them",
    )
    args = parser.parse_args()

    errors: list = []
    warnings: list = []
    embeddings: list = []

    try:
        registry = json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError) as e:
        annotate("error", f"registry.json failed to parse: {e}")
        return 1

    validate_static(registry, errors, warnings, embeddings)
    validate_no_orphan_dirs(registry, errors)

    if args.base:
        try:
            base = json.loads(pathlib.Path(args.base).read_text())
        except (OSError, json.JSONDecodeError):
            base = {}
        validate_promotions(registry, base, args.approval_label_present, errors)

    report_embeddings(embeddings, args.strict_embeddings, errors, warnings)

    for warn in warnings:
        annotate("warning", warn)

    for err in errors:
        annotate("error", err)

    if errors:
        print(f"\nFAIL: {len(errors)} validation error(s).")
        return 1
    if warnings:
        # Never "passed" full stop while something is outstanding: a clean line
        # over a known-drifted registry is how the drift stayed invisible.
        print(f"\nOK: registry validation passed, with {len(warnings)} warning(s).")
        return 0
    print("OK: registry validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
