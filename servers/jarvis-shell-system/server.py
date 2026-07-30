#!/usr/bin/env python3
"""
JARVIS Shell MCP Server

Minimal stdio MCP server that runs shell commands and returns structured output.
No whitelist — security gating is handled by JARVIS's confirmation layer.
Uses only Python stdlib; no external dependencies.
"""

import json
import os
import re
import subprocess
import sys
from typing import Any

# Shapes an unanswered interactive prompt leaves at the very end of a command's
# output. Matched against the last line only (see _detect_unanswered_prompt), so
# a "[Y/n]" inside a package list printed mid-output is not read as a live
# prompt.
_CONFIRM_PROMPT_RE = re.compile(r"(\[[A-Za-z](?:/[A-Za-z])+\]|\(yes/no\))\s*$", re.IGNORECASE)
_QUESTION_PROMPT_RE = re.compile(r"\?\s*$")
_PASSWORD_PROMPT_RE = re.compile(r"(?:password|passphrase)[^\n]*:\s*$", re.IGNORECASE)

TOOLS = [
    {
        "name": "execute_command",
        "description": (
            "Execute a program and return its stdout, stderr, and exit code. "
            "Use for single commands: 'python3', 'git', 'ls', etc. stdin is "
            "closed, so an interactive command cannot be answered here; when the "
            "output ends in an unanswered prompt the result carries a "
            "'needs_input' object (prompted / prompt / hint) — surface it to the "
            "user and re-run non-interactively, or for a password prompt let a "
            "human act."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Executable name or path (e.g. 'python3', '/usr/bin/git')",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments to pass to the command",
                    "default": [],
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (defaults to current directory)",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional. Seconds before the command is killed. Omit to run with no timeout — long-running work is tracked and controlled via dispatch's REMIND/wait/kill, not killed here.",
                },
                "env": {
                    "type": "object",
                    "description": "Extra environment variables to merge with the current environment",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "execute_script",
        "description": (
            "Execute a multi-line shell script via /bin/sh and return its output. "
            "Use for pipelines, conditionals, or multi-step shell logic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Shell script content to execute",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional. Seconds before the command is killed. Omit to run with no timeout — long-running work is tracked and controlled via dispatch's REMIND/wait/kill, not killed here.",
                },
            },
            "required": ["script"],
        },
    },
]


def _run_result(
    success: bool,
    exit_code: int,
    stdout: str,
    stderr: str,
    needs_input: dict | None = None,
) -> dict:
    payload = {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    # Purely additive: a detected prompt never flips success or exit_code.
    if needs_input is not None:
        payload["needs_input"] = needs_input
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2),
            }
        ],
        "isError": not success,
    }


def _detect_unanswered_prompt(tail_text: str) -> dict | None:
    """Report an interactive prompt the command was left blocked on, or None.

    `tail_text` is the tail (~last 200 bytes) of the finished command's combined
    stdout+stderr. Detection keys on the SHAPE of the last line — a [Y/n]-style
    confirmation, a bare '...?' question, or a password/passphrase request — and
    is independent of the exit code, so it catches a tool that aborted (exit 1),
    one that silently did nothing (exit 0), and one that took a default and
    proceeded (exit 0) alike. It only ever adds a report; it never decides
    success.
    """
    if not tail_text:
        return None
    stripped = tail_text.rstrip("\r\n")
    if not stripped:
        return None
    last_line = stripped.rsplit("\n", 1)[-1].strip()
    if not last_line:
        return None

    if _PASSWORD_PROMPT_RE.search(last_line):
        return {
            "prompted": True,
            "prompt": last_line,
            "hint": (
                "The command asked for a password or passphrase — the credential "
                "boundary. stdin is closed, so it received none. Do NOT re-run "
                "blindly: a human must supply the secret (or a non-interactive "
                "credential source must be configured) before this can succeed."
            ),
        }

    if _CONFIRM_PROMPT_RE.search(last_line) or _QUESTION_PROMPT_RE.search(last_line):
        return {
            "prompted": True,
            "prompt": last_line,
            "hint": (
                "The command asked for input; stdin is closed, so its default was "
                "used (or it aborted). Re-run with the tool's non-interactive "
                "flag (e.g. -y / --yes / --noconfirm) to choose explicitly."
            ),
        }

    return None


def _timeout_seconds(arguments: dict):
    """Seconds before the command is killed, or None for no timeout.

    Long-running commands are not killed here on a default timer: dispatch's
    REMIND/wait/kill keeps JARVIS informed and in control instead. A timeout is
    applied only when the caller explicitly sets a positive one.
    """
    t = arguments.get("timeout")
    return float(t) if t else None


def _call_execute_command(arguments: dict) -> dict:
    command = arguments["command"]
    args = [str(a) for a in arguments.get("args") or []]
    cwd = arguments.get("cwd") or None
    timeout = _timeout_seconds(arguments)
    extra_env = arguments.get("env") or {}

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.update(extra_env)

    try:
        proc = subprocess.run(
            [command] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=env,
            # The server's own stdin IS the JSON-RPC channel from dmcp. Without
            # this, a command that reads stdin (e.g. `pacman -Syu` prompting
            # [Y/n]) inherits that pipe: it blocks forever waiting on input the
            # channel will never carry, and can even swallow a later request off
            # the wire. DEVNULL turns an interactive prompt into an immediate
            # EOF, so such a command aborts with a legible error instead of
            # hanging. Non-interactive invocation (`--noconfirm`, `-y`) is the
            # caller's job — TLA already confirmed the command upstream.
            stdin=subprocess.DEVNULL,
        )
        # Inspect only the tail so a "[Y/n]" mentioned mid-output (a package
        # list, a changelog) does not read as a live prompt.
        needs_input = _detect_unanswered_prompt(
            ((proc.stdout or "") + (proc.stderr or ""))[-200:]
        )
        return _run_result(
            proc.returncode == 0,
            proc.returncode,
            proc.stdout,
            proc.stderr,
            needs_input,
        )
    except FileNotFoundError:
        return _run_result(False, 127, "", f"Command not found: {command}")
    except subprocess.TimeoutExpired:
        return _run_result(False, -1, "", f"Command timed out after {timeout}s")
    except Exception as exc:
        return _run_result(False, -1, "", str(exc))


def _call_execute_script(arguments: dict) -> dict:
    script = arguments["script"]
    cwd = arguments.get("cwd") or None
    timeout = _timeout_seconds(arguments)

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

    try:
        proc = subprocess.run(
            ["/bin/sh"],
            input=script,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=env,
        )
        return _run_result(
            proc.returncode == 0,
            proc.returncode,
            proc.stdout,
            proc.stderr,
        )
    except subprocess.TimeoutExpired:
        return _run_result(False, -1, "", f"Script timed out after {timeout}s")
    except Exception as exc:
        return _run_result(False, -1, "", str(exc))


def _handle(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    # Notifications have no id and require no response.
    if req_id is None:
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jarvis-shell-system-mcp", "version": "1.0.0"},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "execute_command":
            return ok(_call_execute_command(arguments))
        if name == "execute_script":
            return ok(_call_execute_script(arguments))
        return err(-32601, f"Unknown tool: {name}")

    return err(-32601, f"Method not found: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
