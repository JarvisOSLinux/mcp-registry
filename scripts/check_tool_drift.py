#!/usr/bin/env python3
"""check_tool_drift.py — compare each wrapper's upstream tools with its manifest.

A wrapper passes on only the tools its manifest declares (servers/_lib/reviewed.py).
A tool the upstream adds is withheld until a registry PR reviews it, and a tool
the upstream drops leaves a declaration with nothing behind it; neither shows
anywhere at runtime. This asks each wrapper what its upstream offers, through
its `--list-upstream-tools` flag, and reports:

  * upstream tools that are neither declared in `tools` nor listed, with a
    reason, in `withheldTools`: new, and waiting for a review;
  * declared tools the upstream no longer offers.

A withheld tool the upstream no longer offers is only noted: nothing reaches
the model either way, and a hosted server can list a tool on some days only.

It runs the copy dmcp installed, which has the built upstream and the sign-in
(GitHub's and Notion's upstreams list nothing without one), and compares with
this checkout's manifest, so a PR's manifest can be checked before it merges.
Pinned upstreams (GitHub, Microsoft 365) change only when the pin does; Notion's
hosted server changes whenever Notion changes it, so check it now and then.

Usage:
  python3 scripts/check_tool_drift.py              # every manifest with withheldTools
  python3 scripts/check_tool_drift.py notion ...   # these server directories

Exit 0: no drift. 1: drift. 2: a wrapper could not be checked.
"""
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVERS_DIR = ROOT / "servers"
LIST_FLAG = "--list-upstream-tools"
TIMEOUT_SECONDS = 180


def install_dir() -> pathlib.Path:
    """dmcp's user-scope install directory, resolved the way dmcp resolves it."""
    if os.environ.get("MCP_USER_INSTALL_DIR"):
        return pathlib.Path(os.environ["MCP_USER_INSTALL_DIR"]).expanduser()
    data = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(data) / "mcp" / "installed"


def server_ids() -> dict:
    """Server directory name -> registry id."""
    registry = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    out = {}
    for server_id, entry in registry.get("servers", {}).items():
        url = entry.get("manifest") or ""
        if "/servers/" in url:
            out[url.split("/servers/", 1)[1].split("/", 1)[0]] = server_id
    return out


def upstream_of(installed: pathlib.Path) -> dict:
    """{"upstream": [...], "local": [...]} from the installed wrapper."""
    manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
    declared = {p.get("key") for p in manifest.get("configurableProperties") or [] if isinstance(p, dict)}
    env = dict(os.environ)
    # The settings dmcp would start it with: declared keys only (dmcp#77).
    env.update({k: str(v) for k, v in (manifest.get("config") or {}).items() if k in declared})
    result = subprocess.run(
        [sys.executable, str(installed / "server.py"), LIST_FLAG],
        cwd=installed,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    try:
        listing = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise OSError(
            f"no tool list ({detail[0][:200]}); if this copy predates {LIST_FLAG}, run dmcp update"
        ) from None
    if not isinstance(listing, dict) or not isinstance(listing.get("upstream"), list):
        raise OSError("the wrapper printed something other than a tool list")
    return listing


def check(directory: str, server_id: str) -> int:
    manifest = json.loads((SERVERS_DIR / directory / "manifest.json").read_text(encoding="utf-8"))
    declared = {t["name"] for t in manifest.get("tools") or [] if isinstance(t, dict) and t.get("name")}
    withheld = {n for names in (manifest.get("withheldTools") or {}).values() for n in names}
    installed = install_dir() / server_id
    print(f"{directory} ({server_id})")
    if not (installed / "server.py").is_file():
        print(f"  cannot check: not installed at {installed}")
        return 2
    try:
        listing = upstream_of(installed)
    except (OSError, subprocess.TimeoutExpired) as err:
        print(f"  cannot check: {err}")
        return 2
    upstream = set(listing["upstream"])
    local = set(listing.get("local") or [])
    new = sorted(upstream - declared - withheld)
    gone = sorted(declared - upstream - local)
    gone_withheld = sorted(withheld - upstream)
    print(f"  the upstream offers {len(upstream)}: {len(upstream & declared)} declared, {len(upstream & withheld)} withheld")
    if new:
        print(f"  NEW, neither declared nor withheld ({len(new)}): {', '.join(new)}")
    if gone:
        print(f"  declared, but no longer offered upstream ({len(gone)}): {', '.join(gone)}")
    if gone_withheld:
        # Harmless, and Notion lists some tools on some days only: a note, not drift.
        print(f"  note: withheld, not offered upstream today ({len(gone_withheld)}): {', '.join(gone_withheld)}")
    return 1 if new or gone else 0


def main(argv: list) -> int:
    ids = server_ids()
    wanted = argv or sorted(
        p.parent.name
        for p in SERVERS_DIR.glob("*/manifest.json")
        if "withheldTools" in json.loads(p.read_text(encoding="utf-8"))
    )
    worst = 0
    for directory in wanted:
        if directory not in ids:
            print(f"{directory}: not a server directory in registry.json")
            worst = 2
            continue
        worst = max(worst, check(directory, ids[directory]))
    print({0: "\nOK: no drift.", 1: "\nDRIFT: review the tools above.", 2: "\nSome wrappers could not be checked."}[worst])
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
