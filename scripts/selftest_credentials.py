#!/usr/bin/env python3
"""selftest_credentials.py — prove the sign-in declaration checks actually fire.

A server that declares `credentials` is asking dmcp to hand it a signed-in
account's token at spawn (Project-JARVIS#229). Every rule here guards where that
token can go: a provider's endpoints decide where the user signs in and where
the token is exchanged; `inject` decides which environment variable receives it.
A rule that quietly stops checking does not break anything visible — it just
lets the next manifest route a token somewhere nobody reviewed. So each case
builds a throwaway registry, runs the real validator entry point against it and
asserts on what it reports, including the cases that must stay silent.

  1. A coherent provider + credential passes, silently.
  2. A provider file the registry does not mirror ERRORS (run sync_registry.py).
  3. A malformed provider ERRORS: plain-http endpoint, a published client
     secret, a missing scope catalogue, an id that disagrees with its file.
  4. A credential naming an unknown provider, or a scope outside the provider's
     catalogue, ERRORS.
  5. An inject target that is not a declared property ERRORS, and a token
     injected into a property not marked sensitive ERRORS — an `account` name
     may go into a plain one.
  6. An inject field dmcp cannot supply ERRORS; so does a misspelt credential key.
  7. Credentials on a system-scope server, or on a server with no stdio
     transport, ERROR — neither could ever be delivered.
  8. A `login` naming a tool the server does not have ERRORS.
  9. Adding, changing or removing a provider needs the maintainer label; an
     unchanged provider does not.
 10. A new or changed manifest that requests account access is reported naming
     the provider and scopes; an unchanged one is not.
 11. sync_registry.py mirrors providers/ into registry.json and drops the map
     when providers/ is gone.
 12. A hosted server's own sign-in (`auth: "oauth"`) passes on an https http
     transport, and ERRORS on plain http, on stdio, or with an unknown value;
     an unknown transport type ERRORS; a new hosted sign-in is reported.

Offline, stdlib only, writes nothing outside its temp directory.

Usage:
  python3 scripts/selftest_credentials.py
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


def provider():
    return {
        "id": "demo",
        "name": "Demo",
        "oauth": {
            "deviceAuthorizationEndpoint": "https://auth.example.invalid/device",
            "tokenEndpoint": "https://auth.example.invalid/token",
        },
        "identity": {"url": "https://api.example.invalid/me", "field": "login"},
        "scopes": {"read": "Read your things", "write": "Change your things"},
    }


def manifest():
    return {
        "version": "1.0.0",
        "scope": "user",
        "platforms": ["linux"],
        "name": "Demo Server",
        "summary": "Fixture server for the credential self-test",
        "keywords": ["demo"],
        "transports": [{"type": "stdio", "command": "python3", "args": ["server.py"]}],
        "configurableProperties": [
            {"key": "DEMO_TOKEN", "label": "Token", "sensitive": True, "required": True},
            {"key": "DEMO_USER", "label": "User"},
        ],
        "credentials": [
            {"provider": "demo", "scopes": ["read"], "inject": {"DEMO_TOKEN": "access_token"}}
        ],
        "tools": [
            {"name": "ping", "description": "Reply with pong", "threat_level": "safe"},
            {"name": "sign_in", "description": "Sign in", "threat_level": "elevated"},
        ],
    }


@contextlib.contextmanager
def fixture(edit_manifest=None, edit_provider=None, *, scope="user", mirror=True):
    """A one-server, one-provider registry that is coherent unless a case edits it."""
    doc = manifest()
    if edit_manifest:
        edit_manifest(doc)
    prov = provider()
    if edit_provider:
        edit_provider(prov)

    real_hash = generate_embeddings.canonical_hash(generate_embeddings.canonical_text(doc))
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mcp-registry-credential-selftest-") as tmp:
        root = pathlib.Path(tmp)
        server_dir = root / "servers" / "demo"
        server_dir.mkdir(parents=True)
        (root / "providers").mkdir()
        (root / "providers" / "demo.json").write_text(json.dumps(prov, indent=2) + "\n")

        doc = dict(doc, embeddings={MODEL: {"v": VECTOR, "hash": real_hash}})
        (server_dir / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n")

        entry = {
            "id": SERVER_ID,
            "name": doc["name"],
            "summary": doc["summary"],
            "version": "1.0.0",
            "scope": scope,
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
        if mirror:
            registry["providers"] = {"demo": prov}
        (root / "registry.json").write_text(json.dumps(registry, indent=2) + "\n")
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(saved_cwd)


def run(module, *args):
    """Call a script's real main() with argv, capturing exit code and output."""
    buffer = io.StringIO()
    saved_argv = sys.argv
    sys.argv = [module.__name__ + ".py", *args]
    try:
        with contextlib.redirect_stdout(buffer):
            code = module.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv = saved_argv
    return code or 0, buffer.getvalue()


def validate(*args):
    return run(validate_registry, *args)


def write_base(root, edit=None):
    """A base registry.json to diff against: the head one, optionally edited."""
    base = json.loads((root / "registry.json").read_text())
    if edit:
        edit(base)
    path = root / "base_registry.json"
    path.write_text(json.dumps(base))
    return str(path)


def expect_error(edit_manifest=None, edit_provider=None, *, needle, label, **kw):
    with fixture(edit_manifest, edit_provider, **kw):
        code, out = validate()
        check(code != 0, f"{label} fails the gate")
        check(needle in out, f"{label}: the finding says {needle!r}")
        check("Traceback" not in out, f"{label}: reported, not crashed")


@case
def a_coherent_declaration_passes_silently():
    with fixture():
        code, out = validate()
        check(code == 0, "a known provider, catalogued scope and sensitive target pass")
        check("::error::" not in out, "no error at all")
        check("::warning::" not in out, "no warning either")


@case
def an_unmirrored_provider_is_an_error():
    expect_error(needle="run sync_registry.py", label="a provider missing from registry.json", mirror=False)
    with fixture(edit_provider=lambda p: None):
        reg = json.loads(pathlib.Path("registry.json").read_text())
        reg["providers"]["demo"]["oauth"]["tokenEndpoint"] = "https://elsewhere.example.invalid/token"
        pathlib.Path("registry.json").write_text(json.dumps(reg))
        code, out = validate()
        check(code != 0, "a mirror repointed away from its file fails the gate")
        check("does not match providers/" in out, "the finding names the drift")


@case
def a_malformed_provider_is_an_error():
    def plain_http(p):
        p["oauth"]["tokenEndpoint"] = "http://auth.example.invalid/token"

    def secret(p):
        p["oauth"]["clientSecret"] = "shh"

    def no_scopes(p):
        del p["scopes"]

    def wrong_id(p):
        p["id"] = "other"

    def bad_identity(p):
        p["identity"] = {"url": "http://api.example.invalid/me"}

    expect_error(edit_provider=plain_http, needle="tokenEndpoint must be an https:// URL", label="a plain-http token endpoint")
    expect_error(edit_provider=secret, needle="clientSecret must not be published", label="a published client secret")
    expect_error(edit_provider=no_scopes, needle="missing required field 'scopes'", label="no scope catalogue")
    expect_error(edit_provider=wrong_id, needle="!= file name", label="an id that disagrees with its file")
    expect_error(edit_provider=bad_identity, needle="identity.url must be an https:// URL", label="a plain-http identity URL")


@case
def unknown_providers_and_scopes_are_errors():
    def unknown_provider(m):
        m["credentials"][0]["provider"] = "nosuch"

    def uncatalogued_scope(m):
        m["credentials"][0]["scopes"] = ["read", "admin"]

    expect_error(unknown_provider, needle="'nosuch' is not a provider", label="an unknown provider")
    expect_error(uncatalogued_scope, needle="scope 'admin' is not in the demo provider's catalogue", label="an uncatalogued scope")


@case
def inject_targets_are_declared_and_tokens_stay_sensitive():
    def undeclared(m):
        m["credentials"][0]["inject"] = {"SOMEWHERE_ELSE": "access_token"}

    def plain_target(m):
        m["credentials"][0]["inject"] = {"DEMO_USER": "access_token"}

    def account_in_plain(m):
        m["credentials"][0]["inject"] = {"DEMO_TOKEN": "access_token", "DEMO_USER": "account"}

    expect_error(undeclared, needle="'SOMEWHERE_ELSE' is not a declared configurableProperties key", label="an undeclared target")
    expect_error(plain_target, needle="receives the access_token but is not marked sensitive", label="a token into a plain property")
    with fixture(account_in_plain):
        code, out = validate()
        check(code == 0, "an account name may go into a plain property")


@case
def unknown_fields_and_keys_are_errors():
    def bad_field(m):
        m["credentials"][0]["inject"] = {"DEMO_TOKEN": "password"}

    def misspelt_key(m):
        m["credentials"][0]["scope"] = ["read"]

    def empty_inject(m):
        m["credentials"][0]["inject"] = {}

    expect_error(bad_field, needle="'password' is not one of", label="an inject field dmcp cannot supply")
    expect_error(misspelt_key, needle="unknown field(s) ['scope']", label="a misspelt credential key")
    expect_error(empty_inject, needle="'inject' must map at least one", label="an empty inject map")


@case
def undeliverable_credentials_are_errors():
    def system_manifest(m):
        m["scope"] = "system"

    def hosted_only(m):
        m["transports"] = [{"type": "sse", "url": "https://mcp.example.invalid/sse"}]

    expect_error(system_manifest, needle="only supported on user-scope servers", label="credentials on a system-scope server", scope="system")
    expect_error(hosted_only, needle="need a stdio transport", label="credentials on a hosted-only server")


@case
def a_login_tool_must_exist():
    def good(m):
        m["login"] = {"tool": "sign_in"}

    def missing(m):
        m["login"] = {"tool": "log_in"}

    with fixture(good):
        code, out = validate()
        check(code == 0, "a login naming one of the server's tools passes")
    expect_error(missing, needle="login.tool 'log_in' is not one of the server's tools", label="a login naming no tool")


@case
def provider_changes_need_the_maintainer_label():
    with fixture() as root:
        base = write_base(root, lambda b: b.pop("providers"))
        code, out = validate("--base", base)
        check(code != 0, "a new provider without the label fails the gate")
        check("without maintainer approval: demo" in out, "the finding names the provider")
        code, out = validate("--base", base, "--approval-label-present")
        check(code == 0, "the label approves it")

        def repoint(b):
            b["providers"]["demo"]["oauth"]["tokenEndpoint"] = "https://old.example.invalid/token"

        code, out = validate("--base", write_base(root, repoint))
        check(code != 0, "a repointed provider without the label fails the gate")

        code, out = validate("--base", write_base(root))
        check(code == 0, "an unchanged provider needs no label")
        check("provider" not in out, "and raises nothing about providers")

        def extra(b):
            b["providers"]["gone"] = provider()

        code, out = validate("--base", write_base(root, extra))
        check(code != 0, "removing a provider without the label fails the gate")
        check("approval: gone" in out, "the finding names the removed provider")


@case
def account_requests_are_reported_when_the_manifest_changes():
    with fixture() as root:
        def old_hash(b):
            b["servers"][SERVER_ID]["integrity"]["manifestSha256"] = "0" * 64

        code, out = validate("--base", write_base(root, old_hash))
        check(code == 0, "a changed manifest with credentials still passes")
        check("requests sign-in access to demo (read)" in out, "the request names provider and scopes")

        code, out = validate("--base", write_base(root, lambda b: b["servers"].clear()))
        check("requests sign-in access to demo (read)" in out, "a new server's request is reported too")

        code, out = validate("--base", write_base(root))
        check("requests sign-in access" not in out, "an unchanged manifest is not re-reported")


@case
def sync_mirrors_providers():
    with fixture(mirror=False) as root:
        code, out = run(sync_registry)
        reg = json.loads((root / "registry.json").read_text())
        check(reg.get("providers") == {"demo": provider()}, "sync mirrors providers/ into registry.json")
        check(list(reg).index("providers") < list(reg).index("servers"), "the map sits ahead of servers")
        code, out = run(sync_registry, "--check")
        check(code == 0, "and is then in sync")

        for path in (root / "providers").iterdir():
            path.unlink()
        (root / "providers").rmdir()
        code, out = run(sync_registry, "--check")
        check(code != 0, "--check notices a provider removed from disk")
        run(sync_registry)
        reg = json.loads((root / "registry.json").read_text())
        check("providers" not in reg, "sync drops the map when providers/ is gone")


def hosted(transport):
    """A remote-only manifest: a hosted server with no local credentials."""

    def edit(m):
        m["transports"] = [transport]
        del m["credentials"]
        m["configurableProperties"] = []

    return edit


@case
def a_hosted_sign_in_must_be_https_http():
    https = {"type": "http", "url": "https://mcp.example.invalid/mcp", "auth": "oauth"}
    with fixture(hosted(https)):
        code, out = validate()
        check(code == 0, "an https http transport with auth: oauth passes")
        check("::error::" not in out, "and raises nothing")

    expect_error(
        hosted(dict(https, url="http://mcp.example.invalid/mcp")),
        needle="must be https",
        label="a plain-http hosted sign-in",
    )
    expect_error(
        hosted({"type": "stdio", "command": "python3", "args": ["s.py"], "auth": "oauth"}),
        needle="only meaningful on an http transport",
        label="auth on stdio",
    )
    expect_error(
        hosted(dict(https, auth="basic")),
        needle="auth 'basic' not in",
        label="an unknown auth value",
    )
    expect_error(
        hosted({"type": "grpc", "url": "https://x.invalid"}),
        needle="type 'grpc' not in",
        label="an unknown transport type",
    )


@case
def a_new_hosted_sign_in_is_reported():
    https = {"type": "http", "url": "https://mcp.example.invalid/mcp", "auth": "oauth"}
    with fixture(hosted(https)) as root:
        code, out = validate("--base", write_base(root, lambda b: b["servers"].clear()))
        check(
            "signs users in itself at mcp.example.invalid" in out,
            "a new server's own sign-in is named with its host",
        )


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
    print("\nOK: credential self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
