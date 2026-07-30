#!/usr/bin/env python3
"""selftest_needs_input.py — prove the shell servers' needs_input report fires.

Issue #63: after #62 gave shell commands a closed stdin, a command that expects
interactive input no longer hangs — it aborts, silently does nothing, or takes a
default. Any of those looks like an ordinary run to the LLM. The shell servers
now add a `needs_input` object to `execute_command`'s result when the tail of the
combined output still holds an unanswered prompt, so the model can surface it and
re-run non-interactively. This report is never a dialogue: no send_input, no PTY,
no held process.

This self-test has two halves:

  1. Unit-tests the pure helper `_detect_unanswered_prompt(tail)` against each
     prompt shape, a password prompt (which must get the distinct credential
     hint), a prompt shape buried MID-output (must NOT match), and clean output
     (must NOT match). Both servers' helpers are exercised and required to agree,
     since they are identical by design.

  2. Drives BOTH real servers over JSON-RPC — spawns `python3 server.py`, sends
     `initialize` then a `tools/call` for `execute_command` running each of the
     three real tool styles via `sh -c ...` — and asserts `needs_input` is
     present with the right shape while `success`/`exit_code` are untouched. A
     clean run whose output merely mentions "[Y/n]" mid-stream must carry no
     `needs_input`.

Offline, stdlib only.

Usage:
  python3 scripts/selftest_needs_input.py
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
USER_SERVER = REPO / "servers" / "jarvis-shell" / "server.py"
SYSTEM_SERVER = REPO / "servers" / "jarvis-shell-system" / "server.py"

FAILURES = []


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(description)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Half 1: the pure helper
# ---------------------------------------------------------------------------

MIDDLE_OUTPUT = (
    "Do you want to continue? [Y/n]\n"
    "Get:1 http://archive.example.org stable/main amd64 foo 1.2 [4 kB]\n"
    "Selecting previously unselected package foo:amd64 (yes/no) placeholder\n"
    "Unpacking foo (1.2) ...\n"
    "Setting up foo (1.2) ...\n"
    "Processing triggers ...\n"
    "Done.\n"
)

CLEAN_OUTPUT = "Reading state information...\nInstallation complete.\nTotal: 5 files.\n"


def unit_tests():
    print("  helper_unit_tests")
    user = load_module(USER_SERVER, "shell_user")
    system = load_module(SYSTEM_SERVER, "shell_system")
    detect = user._detect_unanswered_prompt

    confirm_cases = [
        ("[Y/n]", "Do you want to continue? [Y/n]"),
        ("[y/N]", "Overwrite '/etc/config'? [y/N]"),
        ("[Y/n/q]", "Install these 3 packages? [Y/n/q]"),
        ("(yes/no)", "Are you sure you want to proceed? (yes/no)"),
        ("trailing ?", "rm: remove regular file 'notes.txt'?"),
        ("trailing '? '", "Continue? "),
    ]
    for label, text in confirm_cases:
        result = detect(text)
        check(result is not None, f"{label!r} is detected as a prompt")
        if result is not None:
            check(result.get("prompted") is True, f"{label!r} sets prompted:true")
            check(result.get("prompt") == text.strip(), f"{label!r} prompt is the trimmed line")
            hint = result.get("hint", "")
            check("non-interactive" in hint, f"{label!r} gets the non-interactive hint")
            check("password" not in hint.lower(), f"{label!r} does NOT get the credential hint")

    # Password / passphrase prompts get a DISTINCT hint that forbids a blind
    # re-run — the credential boundary needs a human.
    password_cases = [
        "[sudo] password for user:",
        "Password:",
        "Enter passphrase for key '/home/u/.ssh/id_ed25519':",
    ]
    normal_hint = detect("Continue? [Y/n]")["hint"]
    for text in password_cases:
        result = detect(text)
        check(result is not None, f"password prompt {text!r} is detected")
        if result is not None:
            check(result.get("prompted") is True, f"password {text!r} sets prompted:true")
            check(result.get("prompt") == text.strip(), f"password {text!r} prompt is the trimmed line")
            hint = result.get("hint", "")
            check(hint != normal_hint, f"password {text!r} gets a DISTINCT hint")
            check("Do NOT re-run" in hint, f"password {text!r} hint forbids a blind re-run")
            check("credential" in hint, f"password {text!r} hint names the credential boundary")

    # Must NOT match.
    check(detect(MIDDLE_OUTPUT) is None, "a prompt shape MID-output does not match")
    check(detect(CLEAN_OUTPUT) is None, "clean output does not match")
    check(detect("") is None, "empty output does not match")
    check(detect("\n\n  \n") is None, "whitespace-only output does not match")

    # Both servers ship the same helper — require identical behavior on every
    # fixture above, so the two files cannot silently drift.
    for text in [t for _, t in confirm_cases] + password_cases + [MIDDLE_OUTPUT, CLEAN_OUTPUT, ""]:
        check(
            user._detect_unanswered_prompt(text) == system._detect_unanswered_prompt(text),
            f"both servers' helpers agree on {text[:32]!r}",
        )


# ---------------------------------------------------------------------------
# Half 2: the real servers over JSON-RPC
# ---------------------------------------------------------------------------

def rpc(server_path, calls):
    """Spawn `python3 server.py`, send initialize + the calls, collect results."""
    requests = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}]
    for i, (name, arguments) in enumerate(calls, start=1):
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    proc = subprocess.Popen(
        [sys.executable, str(server_path)],
        cwd=str(pathlib.Path(server_path).parent),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdin_blob = "".join(json.dumps(r) + "\n" for r in requests)
    out, _err = proc.communicate(stdin_blob, timeout=30)
    responses = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        responses[msg.get("id")] = msg
    # Map each call id -> the inner execute_command payload dict.
    payloads = []
    for i in range(1, len(calls) + 1):
        result = responses[i]["result"]
        payloads.append(json.loads(result["content"][0]["text"]))
    return payloads


def sh(script):
    return ("execute_command", {"command": "sh", "args": ["-c", script]})


# The three real tool styles, plus a password prompt and a clean run whose
# output merely mentions "[Y/n]" mid-stream.
JSONRPC_CASES = [
    # label, sh script, expected exit_code, expected success, needs_input?, prompt substr, credential hint?
    (
        "read-abort (exit 1, prompt in stderr)",
        "printf 'Overwrite /etc/config? [y/N] ' >&2; read ans || exit 1; echo changed",
        1, False, True, "[y/N]", False,
    ),
    (
        "rm -i (exit 0, did nothing, prompt in stderr)",
        "printf \"rm: remove regular file 'notes.txt'? \" >&2; read ans || true",
        0, True, True, "?", False,
    ),
    (
        "[Y/n] default-yes (exit 0, proceeded, prompt in stdout)",
        "echo 'Reading package lists... Done'; printf 'Do you want to continue? [Y/n] '",
        0, True, True, "[Y/n]", False,
    ),
    (
        "password prompt (exit 1, credential boundary)",
        "printf '[sudo] password for user: ' >&2; read ans || exit 1; echo authenticated",
        1, False, True, "password", True,
    ),
    (
        "clean run mentioning [Y/n] mid-output (no prompt)",
        "echo 'Selecting package foo [Y/n]-dev'; echo 'Unpacking foo ...'; echo 'Done.'",
        0, True, False, None, False,
    ),
]


def jsonrpc_tests():
    for server_path, tag in ((USER_SERVER, "jarvis-shell"), (SYSTEM_SERVER, "jarvis-shell-system")):
        print(f"  jsonrpc[{tag}]")
        payloads = rpc(server_path, [sh(script) for _, script, *_ in JSONRPC_CASES])
        for (label, _script, exit_code, success, wants_ni, prompt_substr, credential), payload in zip(
            JSONRPC_CASES, payloads
        ):
            check(payload["exit_code"] == exit_code, f"{label}: exit_code is {exit_code} (unchanged by detection)")
            check(payload["success"] is success, f"{label}: success is {success} (unchanged by detection)")
            if wants_ni:
                ni = payload.get("needs_input")
                check(isinstance(ni, dict), f"{label}: needs_input is present")
                if isinstance(ni, dict):
                    check(ni.get("prompted") is True, f"{label}: needs_input.prompted is true")
                    check(prompt_substr in ni.get("prompt", ""), f"{label}: prompt holds {prompt_substr!r}")
                    check(bool(ni.get("hint")), f"{label}: needs_input.hint is non-empty")
                    is_credential = "credential" in ni.get("hint", "")
                    check(is_credential == credential, f"{label}: credential hint == {credential}")
            else:
                check("needs_input" not in payload, f"{label}: needs_input is absent")


# ---------------------------------------------------------------------------
# Half 3: the two files keep the shared bodies byte-identical
# ---------------------------------------------------------------------------

def extract_block(text, header):
    """Return the source of the top-level def whose signature starts with header."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(header))
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end] == ""):
        end += 1
    # Trim trailing blank lines that belong to the separator, not the block.
    while end > start + 1 and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])


def identity_tests():
    print("  byte_identity")
    user_src = USER_SERVER.read_text()
    system_src = SYSTEM_SERVER.read_text()
    for header in ("def _detect_unanswered_prompt(", "def _call_execute_command("):
        u = extract_block(user_src, header)
        s = extract_block(system_src, header)
        check(u == s, f"{header[:-1]!r} body is byte-identical in both servers")


def main():
    unit_tests()
    jsonrpc_tests()
    identity_tests()
    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} needs_input self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: needs_input self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
