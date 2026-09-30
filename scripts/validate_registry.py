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
  - a 'windows' platform claim on an entry that HAS a POSIX setupScript carries a
    Windows setup path too (a setupScriptWindows field or a committed setup.ps1),
    mirroring dmcp's SetupError::NoWindowsScript so the gate catches at PR time
    what dmcp otherwise only fails at install time (an entry with no setupScript
    at all is exempt — fetch-at-launch runs nothing to need a Windows counterpart)
  - no orphan directory: every first-party servers/<dir>/ is referenced by an
    entry (catches a half-done removal that dropped the entry but left the dir)
  - every providers/<id>.json is a well-formed sign-in provider (https-only
    endpoints, a scope catalogue) and registry.json's `providers` mirrors them
  - a manifest's `credentials` name a known provider and catalogued scopes,
    inject only into declared sensitive configurableProperties, stay on user
    scope and a stdio transport; a `login` names one of the server's tools
  - every transport `type` is one dmcp can run, and `auth` appears only as
    "oauth" on an https Streamable HTTP transport

Warnings (reported, never fatal):
  - a vetted top-level platform that no transport can serve
  - a 'windows' entry whose selected transport launches a POSIX-only command
    (python3, or a .venv/bin/ path) that likely will not resolve on Windows
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
  - a new or changed manifest that requests account access is reported naming
    the provider and scopes it asks for, or the host whose own sign-in it uses.
  - adding, changing or removing a sign-in provider REQUIRES the same approval:
    a provider decides where a user's sign-in is sent and where the resulting
    token goes, for every server that declares it.

Usage:
  python3 scripts/validate_registry.py
  python3 scripts/validate_registry.py --strict-embeddings
  python3 scripts/validate_registry.py --base base_registry.json
  python3 scripts/validate_registry.py --base base_registry.json --approval-label-present
"""
import argparse
import json
import pathlib
import re
import sys

# Imported, never re-derived: canonical_text is the definition of "the text that
# was embedded", and a second copy of it here would drift from the generator's
# — leaving a gate that passes stale vectors while looking like it checked them.
from generate_embeddings import DEFAULT_MODEL, canonical_hash, canonical_text
from sync_registry import (
    PROVIDERS_DIR,
    POSIX_SETUP_SCRIPT,
    SETUP_SCRIPTS,
    WINDOWS_SETUP_SCRIPT,
    dir_from_url,
    load_providers,
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

# Categories are a closed vocabulary because they are a filter, not prose: a
# typo ("prodcutivity") does not fail anything today, it just quietly drops the
# entry out of every view that selects on that term.
#
# Every term here names a capability — what the server does FOR SOMEONE. There
# is deliberately no `mcp` and no `mcp-*` term. `mcp` said only "this is an MCP
# server", which is true of all 31 entries in an MCP registry and separates
# nothing; `mcp-development`, `mcp-utilities` and `mcp-web` were never defined
# anywhere in this repo and had drifted into a junk drawer — `mcp-development`
# sat on ten test fixtures and on Brave Search alike, which is neither a server
# under development nor tooling for building servers. A category that does not
# divide the catalogue is not a category.
ALLOWED_CATEGORIES = {
    "automation",
    "browser",
    "calendar",
    "computer-use",
    "creative",
    "data-analysis",
    "database",
    "desktop",
    "developer-tools",
    "email",
    "finance",
    "home-automation",
    "image-generation",
    "iot",
    "knowledge-management",
    "media",
    "messaging",
    "office-docs",
    "productivity",
    "search",
    "security",
    "social",
    "storage",
    "system",
    "team-collaboration",
    "travel",
    "weather",
}
# Sign-in providers (#229). A provider id is what dmcp keys a stored account by
# and what a credential names, so it is held to a plain slug.
PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROVIDER_FIELDS = ("id", "name", "oauth", "identity", "scopes")
PROVIDER_ENDPOINTS = ("deviceAuthorizationEndpoint", "tokenEndpoint")

# What dmcp can hand a server from a signed-in account. Closed, so a manifest
# asking for a field dmcp does not have fails here instead of launching the
# server with that variable silently unset.
CREDENTIAL_FIELDS = {"access_token", "refresh_token", "client_id", "account"}
# Fields that are secrets: they may only land in a property the manifest marks
# `sensitive`, which is what keeps every config UI from displaying them.
SECRET_CREDENTIAL_FIELDS = {"access_token", "refresh_token"}
CREDENTIAL_KEYS = {"provider", "scopes", "inject"}

# Transport types dmcp deserializes. `http` and its spellings are Streamable
# HTTP; dmcp speaks the same protocol to `sse`. Anything else would make the
# manifest unloadable on every client.
STDIO_TRANSPORT = "stdio"
HTTP_TRANSPORTS = {"http", "streamable-http", "streamable_http", "sse"}
ALLOWED_TRANSPORT_TYPES = {STDIO_TRANSPORT, "websocket"} | HTTP_TRANSPORTS
# A hosted server's own sign-in (MCP authorization spec). The only value.
ALLOWED_TRANSPORT_AUTH = {"oauth"}
LOGIN_KEYS = {"tool"}

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


def validate_categories(where: str, entry: dict, errors: list) -> None:
    """Check the entry's `categories` against the closed vocabulary.

    Categories never reach the embedding text (EMBEDDING-SPEC.md), so they do
    not move a similarity score — they are what a catalogue view selects on.
    That makes an unrecognised term invisible rather than loud: the entry simply
    stops appearing under the facet its author meant, and no check fails. The
    closed set is what turns that into a build error.
    """
    categories = entry.get("categories")
    if categories is None:
        errors.append(
            f"{where}: missing 'categories' — every entry must say what it is, "
            f"so a consumer view can select on it"
        )
        return

    if not isinstance(categories, list) or not categories:
        errors.append(f"{where}: 'categories' must be a non-empty array")
        return

    # isinstance first, for the reason validate_platforms gives: an unhashable
    # nested value would abort the whole gate instead of reporting this entry.
    for value in categories:
        if not isinstance(value, str) or value not in ALLOWED_CATEGORIES:
            errors.append(
                f"{where}: category {value!r} not in {sorted(ALLOWED_CATEGORIES)}"
            )



def validate_fixture(where: str, entry: dict, errors: list) -> None:
    """Check the entry's `fixture` flag.

    A fixture exists to be run by this repo's own tests and by dmcp's, so it
    stays installable and stays in the index; what it must not do is answer a
    user's question. dmcp drops flagged entries from vector search, which is the
    only reason the flag has to be exact — an entry that is a fixture and does
    not say so competes with real servers for the top-k slots a consumer query
    returns.
    """
    if "fixture" not in entry:
        return

    fixture = entry["fixture"]
    if not isinstance(fixture, bool):
        errors.append(
            f"{where}: 'fixture' must be true or false, not {fixture!r} — dmcp "
            f"reads it as a flag, and a non-boolean reads as absent"
        )
        return

    # `official` is the one tier that lifts the TLA floor for a manifest-declared
    # `safe` tool (Project-JARVIS #223). A fixture is written to exercise the
    # gate — poison-mcp is an adversarial payload by construction — so promoting
    # one to `official` would let exactly the tools built to be caught run
    # unconfirmed. Nothing in the registry does this today; the check keeps it
    # that way.
    if fixture and entry.get("trustStatus") == "official":
        errors.append(
            f"{where}: a fixture must not be trustStatus 'official' — that tier "
            f"lifts the threat floor for declared-safe tools, and a test payload "
            f"must never be the thing that lifts it"
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


def windows_transport(manifest: dict):
    """The transport dmcp would select on a Windows host, or None.

    Mirrors src/transport.rs::select: the first transport the host is in, where a
    transport with no `platforms` matches every host. Order is load-bearing, so
    the first match is the one that would launch.
    """
    for transport in manifest.get("transports", []) or []:
        if not isinstance(transport, dict):
            continue
        platforms = transport.get("platforms")
        if platforms is None or (isinstance(platforms, list) and "windows" in platforms):
            return transport
    return None


def validate_windows_setup(
    where: str, dir_name: str, entry: dict, manifest: dict, errors: list, warnings: list
) -> None:
    """A 'windows' platform claim must carry a runnable Windows install path.

    Mirrors dmcp's runtime SetupError::NoWindowsScript (src/setup.rs): asked to
    install on a Windows host, dmcp refuses a manifest that declares only the
    POSIX setupScript — PowerShell's -File runs nothing but a .ps1, and Windows
    has no shell to hand setup.sh to, so the install aborts. That is caught today
    only at install time; gating it here catches the same claim at PR time.

    An entry with no setupScript at all is exempt from the error: nothing runs at
    install, so there is no POSIX script needing a Windows counterpart — the
    fetch-at-launch case, which dmcp's host branch never reaches. The command
    warning below is independent of setup and fires either way.
    """
    platforms = entry.get("platforms")
    if not isinstance(platforms, list) or "windows" not in platforms:
        return

    setup_script = manifest.get("setupScript")
    if isinstance(setup_script, str) and setup_script:
        setup_windows = manifest.get("setupScriptWindows")
        has_windows_field = isinstance(setup_windows, str) and bool(setup_windows)
        has_ps1 = (SERVERS_DIR / dir_name / WINDOWS_SETUP_SCRIPT).exists()

        if not has_windows_field and not has_ps1:
            errors.append(
                f"{where}: platforms include 'windows' and the manifest declares a "
                f"setupScript ('{setup_script}') but neither a 'setupScriptWindows' "
                f"field nor a committed servers/{dir_name}/{WINDOWS_SETUP_SCRIPT} — dmcp "
                f"raises SetupError::NoWindowsScript on a Windows host (add "
                f"{WINDOWS_SETUP_SCRIPT} + setupScriptWindows, or drop 'windows' from "
                f"'platforms')"
            )

    # A launch command that only resolves on POSIX is a quieter form of the same
    # claim: the host is vetted, but nothing here can start the server there. dmcp
    # cannot know an interpreter name is wrong, so this stays a heuristic warning —
    # 'python3' is the POSIX interpreter (Windows ships 'python'), and '.venv/bin/'
    # is the POSIX venv layout (Windows uses '.venv\Scripts\').
    transport = windows_transport(manifest)
    if isinstance(transport, dict):
        command = transport.get("command")
        if isinstance(command, str) and (command == "python3" or ".venv/bin/" in command):
            warnings.append(
                f"{where}: the transport dmcp selects on a Windows host launches "
                f"'{command}', which likely will not resolve there — 'python3' and "
                f"the POSIX '.venv/bin/' layout are Windows-absent (it ships "
                f"'python' and '.venv\\Scripts\\'); give Windows its own transport"
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


def https_url(value) -> bool:
    return isinstance(value, str) and value.startswith("https://") and len(value) > len("https://")


def validate_providers(registry: dict, errors: list) -> dict:
    """Check providers/<id>.json and the registry's mirror of them.

    A provider is where dmcp sends a user to sign in and where it exchanges the
    code for a token, and the identity URL is sent that token. Every endpoint is
    therefore https: over plain http the token itself crosses the wire in clear.

    Returns the providers that passed, for the credential checks to resolve
    against — a manifest naming a malformed provider is reported once, here,
    and then as unknown.
    """
    valid = {}
    if PROVIDERS_DIR.is_dir():
        for path in sorted(PROVIDERS_DIR.iterdir()):
            where = f"providers/{path.name}"
            if path.suffix != ".json" or not path.is_file():
                errors.append(f"{where}: only <id>.json provider files belong in providers/")
                continue
            try:
                provider = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                errors.append(f"{where}: failed to parse: {e}")
                continue
            if not isinstance(provider, dict):
                errors.append(f"{where}: must be a JSON object")
                continue

            before = len(errors)
            for field in PROVIDER_FIELDS:
                if field not in provider:
                    errors.append(f"{where}: missing required field '{field}'")

            pid = provider.get("id")
            if pid is not None and pid != path.stem:
                errors.append(f"{where}: id {pid!r} != file name {path.stem!r}")
            if not PROVIDER_ID.match(path.stem):
                errors.append(f"{where}: provider id must be a lowercase slug ([a-z0-9-])")
            if "name" in provider and not (isinstance(provider["name"], str) and provider["name"]):
                errors.append(f"{where}: 'name' must be a non-empty string")

            oauth = provider.get("oauth")
            if oauth is not None and not isinstance(oauth, dict):
                errors.append(f"{where}: 'oauth' must be an object")
            elif isinstance(oauth, dict):
                for endpoint in PROVIDER_ENDPOINTS:
                    if not https_url(oauth.get(endpoint)):
                        errors.append(f"{where}: oauth.{endpoint} must be an https:// URL")
                # Public clients only: the client id ships to every machine, so
                # a secret beside it would be a secret nobody can keep.
                if "clientSecret" in oauth:
                    errors.append(f"{where}: oauth.clientSecret must not be published in a registry")
                client_id = oauth.get("clientId")
                if client_id is not None and not (isinstance(client_id, str) and client_id):
                    errors.append(f"{where}: oauth.clientId must be a non-empty string when present")

            identity = provider.get("identity")
            if identity is not None and not isinstance(identity, dict):
                errors.append(f"{where}: 'identity' must be an object")
            elif isinstance(identity, dict):
                if not https_url(identity.get("url")):
                    errors.append(f"{where}: identity.url must be an https:// URL")
                if not (isinstance(identity.get("field"), str) and identity.get("field")):
                    errors.append(f"{where}: identity.field must name the account field")

            scopes = provider.get("scopes")
            if scopes is not None and not (
                isinstance(scopes, dict)
                and all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in scopes.items())
            ):
                errors.append(
                    f"{where}: 'scopes' must map each scope to the description a user "
                    f"is shown when a server asks for it"
                )

            if len(errors) == before:
                valid[path.stem] = provider

    mirrored = registry.get("providers", {})
    if not isinstance(mirrored, dict):
        errors.append("registry.json: 'providers' must be an object")
    elif mirrored != load_providers():
        errors.append(
            "registry.json: 'providers' does not match providers/ — run sync_registry.py"
        )
    return valid


def has_stdio_transport(manifest: dict) -> bool:
    transports = manifest.get("transports")
    return isinstance(transports, list) and any(
        isinstance(t, dict) and t.get("type") == "stdio" for t in transports
    )


def validate_credentials(
    where: str, entry: dict, manifest: dict, providers: dict, errors: list
) -> None:
    """A server's request for a signed-in account, and where dmcp puts it.

    dmcp injects a granted account's token into the server's environment at
    spawn, under the names `inject` maps. Everything here keeps that narrow:
    only a declared property can receive a value (so `dmcp config set` still
    overrides it and every config UI already knows it), a token only ever lands
    in a `sensitive` one, and the request is for a provider and scopes this
    registry has reviewed.
    """
    credentials = manifest.get("credentials")
    if credentials is None:
        return
    if not isinstance(credentials, list) or not credentials:
        errors.append(f"{where}: 'credentials' must be a non-empty array when present")
        return

    # A system-scope server runs as root, which cannot read the signed-in
    # user's keyring — the account would be requested and never delivered.
    if entry.get("scope") != "user" or manifest.get("scope", "user") != "user":
        errors.append(f"{where}: 'credentials' are only supported on user-scope servers")
    # Hosted servers take the token as a bearer header on the connection, which
    # dmcp does not do yet (#229); an env mapping would reach no process.
    if not has_stdio_transport(manifest):
        errors.append(
            f"{where}: 'credentials' need a stdio transport — hosted-server sign-in is not supported yet"
        )

    properties = {
        p.get("key"): p
        for p in manifest.get("configurableProperties") or []
        if isinstance(p, dict) and isinstance(p.get("key"), str)
    }
    seen_providers = set()
    injected = set()

    for position, credential in enumerate(credentials):
        at = f"{where}: credentials[{position}]"
        if not isinstance(credential, dict):
            errors.append(f"{at}: must be an object")
            continue
        unknown = set(credential) - CREDENTIAL_KEYS
        if unknown:
            errors.append(f"{at}: unknown field(s) {sorted(unknown)} (allowed: {sorted(CREDENTIAL_KEYS)})")

        provider_id = credential.get("provider")
        provider = providers.get(provider_id) if isinstance(provider_id, str) else None
        if provider is None:
            errors.append(f"{at}: provider {provider_id!r} is not a provider in providers/")
        elif provider_id in seen_providers:
            errors.append(f"{at}: provider {provider_id!r} is requested twice")
        seen_providers.add(provider_id if isinstance(provider_id, str) else None)

        scopes = credential.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
            errors.append(f"{at}: 'scopes' must be an array of scope names")
        elif provider is not None:
            catalogue = provider.get("scopes", {})
            for scope in scopes:
                if scope not in catalogue:
                    errors.append(
                        f"{at}: scope {scope!r} is not in the {provider_id} provider's catalogue"
                    )

        inject = credential.get("inject")
        if not isinstance(inject, dict) or not inject:
            errors.append(f"{at}: 'inject' must map at least one property key to a credential field")
            continue
        for key, field in inject.items():
            if field not in CREDENTIAL_FIELDS:
                errors.append(
                    f"{at}: inject[{key!r}] = {field!r} is not one of {sorted(CREDENTIAL_FIELDS)}"
                )
            prop = properties.get(key)
            if prop is None:
                errors.append(
                    f"{at}: inject target {key!r} is not a declared configurableProperties key"
                )
            elif field in SECRET_CREDENTIAL_FIELDS and prop.get("sensitive") is not True:
                errors.append(
                    f"{at}: inject target {key!r} receives the {field} but is not marked sensitive"
                )
            if key in injected:
                errors.append(f"{at}: property {key!r} is injected by two credentials")
            injected.add(key)


def validate_transport_types(where: str, manifest: dict, errors: list) -> None:
    """Every transport names a type dmcp runs; `auth` only where it means something.

    `auth: "oauth"` makes dmcp run a browser sign-in against whatever
    authorization server the endpoint names, and then send that server a
    bearer token on every call. Over plain http the token crosses the network
    in the clear, so the endpoint must be https. On stdio or WebSocket the
    field has no meaning, and a value dmcp does not know reads as absent there:
    both are review mistakes worth failing on.
    """
    transports = manifest.get("transports")
    if not isinstance(transports, list):
        return
    for position, transport in enumerate(transports):
        if not isinstance(transport, dict):
            continue
        at = f"{where}: transports[{position}]"
        kind = transport.get("type")
        if kind not in ALLOWED_TRANSPORT_TYPES:
            errors.append(f"{at}: type {kind!r} not in {sorted(ALLOWED_TRANSPORT_TYPES)}")
            continue
        if "auth" not in transport:
            continue
        auth = transport["auth"]
        if auth not in ALLOWED_TRANSPORT_AUTH:
            errors.append(f"{at}: auth {auth!r} not in {sorted(ALLOWED_TRANSPORT_AUTH)}")
        elif kind not in HTTP_TRANSPORTS:
            errors.append(f"{at}: auth is only meaningful on an http transport, not {kind!r}")
        elif not https_url(transport.get("url")):
            errors.append(
                f"{at}: a transport that signs the user in must be https — the token "
                f"is sent to it on every call"
            )


def hosted_sign_ins(manifest: dict) -> list:
    """Hosts of the transports that run their own sign-in."""
    hosts = []
    for transport in manifest.get("transports") or []:
        if isinstance(transport, dict) and transport.get("auth") == "oauth":
            url = str(transport.get("url", ""))
            hosts.append(url.split("://", 1)[-1].split("/", 1)[0] or url)
    return hosts


def validate_login(where: str, manifest: dict, errors: list) -> None:
    """A server that signs in its own way names the tool that does it."""
    login = manifest.get("login")
    if login is None:
        return
    if not isinstance(login, dict):
        errors.append(f"{where}: 'login' must be an object")
        return
    unknown = set(login) - LOGIN_KEYS
    if unknown:
        errors.append(f"{where}: login has unknown field(s) {sorted(unknown)}")
    tool_names = {
        t.get("name") for t in manifest.get("tools") or [] if isinstance(t, dict)
    }
    if login.get("tool") not in tool_names:
        errors.append(f"{where}: login.tool {login.get('tool')!r} is not one of the server's tools")


def validate_static(registry: dict, errors: list, warnings: list, embeddings: list) -> None:
    if "servers" not in registry or not isinstance(registry["servers"], dict):
        errors.append("registry.json: missing or malformed 'servers' object")
        return

    spec = registry.get("embedding_spec")
    if not isinstance(spec, dict):
        spec = {}

    providers = validate_providers(registry, errors)

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
        validate_categories(where, entry, errors)
        validate_fixture(where, entry, errors)

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
        validate_windows_setup(where, dir_name, entry, manifest, errors, warnings)
        validate_transport_types(where, manifest, errors)
        validate_credentials(where, entry, manifest, providers, errors)
        validate_login(where, manifest, errors)
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


def credential_requests(entry: dict) -> list:
    """What account access a manifest asks for, phrased for the review note."""
    dir_name = dir_from_url(entry.get("manifest", ""))
    if not dir_name:
        return []
    try:
        manifest = json.loads((SERVERS_DIR / dir_name / MANIFEST_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    requests = []
    for credential in manifest.get("credentials") or []:
        if isinstance(credential, dict):
            scopes = ", ".join(str(x) for x in credential.get("scopes") or []) or "no scopes"
            requests.append(
                f"requests sign-in access to {credential.get('provider')} ({scopes}) — "
                f"check the scopes are the least its tools need"
            )
    for host in hosted_sign_ins(manifest):
        requests.append(
            f"signs users in itself at {host} and receives their token — check the "
            f"host is the vendor's own"
        )
    return requests


def validate_promotions(registry: dict, base: dict, approval: bool, errors: list) -> None:
    base_servers = base.get("servers", {}) if isinstance(base, dict) else {}
    promotions = []
    revivals = []
    setup_changes = []
    manifest_changes = []
    account_requests = []

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

        # A grant is the user's call, but it is made on the strength of this
        # review: the prompt names the server and the scopes, and nothing else.
        if head_manifest != base_integrity.get("manifestSha256"):
            for request in credential_requests(entry):
                account_requests.append((server_id, request))

    for sid, filename in setup_changes:
        annotate("warning", f"{sid}: {filename} added/changed — review the script before merge")

    for sid, request in account_requests:
        annotate("warning", f"{sid}: {request}")

    # A provider decides where a user is sent to sign in and where the token it
    # yields goes — for every server that names it, installed or not. Adding
    # one, repointing one, or dropping one out from under its servers all move
    # that trust, so each needs the same maintainer label a promotion does.
    base_providers = base.get("providers", {}) if isinstance(base, dict) else {}
    head_providers = registry.get("providers", {})
    if not isinstance(base_providers, dict):
        base_providers = {}
    if not isinstance(head_providers, dict):
        head_providers = {}
    provider_changes = sorted(
        pid
        for pid in set(base_providers) | set(head_providers)
        if base_providers.get(pid) != head_providers.get(pid)
    )
    if provider_changes:
        listed = ", ".join(provider_changes)
        if approval:
            annotate("notice", f"sign-in provider change approved by maintainer label: {listed}")
        else:
            errors.append(
                f"sign-in provider added, changed or removed without maintainer approval: "
                f"{listed}. A maintainer must apply the 'trust-approved' label "
                "(see docs/TRUST-MODEL.md §4) — a provider decides where users sign in "
                "and where their tokens go."
            )

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
