#!/usr/bin/env python3
"""
JARVIS Shell MCP Server

Minimal stdio MCP server that runs shell commands and returns structured output.
No whitelist — security gating is handled by JARVIS's confirmation layer.
Uses only Python stdlib; no external dependencies.
"""

import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from typing import Any

# Shapes an unanswered interactive prompt leaves at the very end of a command's
# output. Matched against the last line only (see _detect_unanswered_prompt), so
# a "[Y/n]" inside a package list printed mid-output is not read as a live
# prompt.
_CONFIRM_PROMPT_RE = re.compile(r"(\[[A-Za-z](?:/[A-Za-z])+\]|\(yes/no\))\s*$", re.IGNORECASE)
_QUESTION_PROMPT_RE = re.compile(r"\?\s*$")
_PASSWORD_PROMPT_RE = re.compile(r"(?:password|passphrase)[^\n]*:\s*$", re.IGNORECASE)

# execute_interactive is the JSON-RPC CLIENT while it answers a prompt, so its
# outgoing request ids must not collide with the ones dmcp assigns to its
# requests to us; a high base keeps the two id spaces clearly apart.
_INTERACTIVE_REQ_BASE = 8_000_000

# read(2) syscall number by machine. Used to read /proc/<pid>/syscall and tell a
# child parked in a terminal read (a real prompt awaiting an answer) from one
# merely sleeping or computing: a quiet tail alone is not evidence the child is
# blocked on input, and answering when it is not would let the reply land on a
# LATER, unrelated read. x86_64 read is 0; the asm-generic table
# (aarch64/riscv64) is 63; the older/32-bit family is 3. An unknown machine (or a
# host without /proc) leaves this None and _pty_read_state degrades to the
# output-only heuristic rather than misreading "cannot tell" as "not waiting".
_READ_SYSCALL_NR = ({
    "x86_64": 0,
    "aarch64": 63,
    "riscv64": 63,
    "i386": 3, "i486": 3, "i586": 3, "i686": 3,
    "armv6l": 3, "armv7l": 3, "armv8l": 3,
    "ppc": 3, "ppc64": 3, "ppc64le": 3,
    "s390": 3, "s390x": 3,
}.get(os.uname().machine) if hasattr(os, "uname") else None)

# Whether the connected client declared elicitation support during initialize
# (dmcp advertises it only under `--interactive`). execute_interactive consults
# this to decide whether it may answer prompts live or must fall back to the
# non-interactive report. Captured once per connection in `_handle`.
_CLIENT = {"elicitation": False}

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
        "name": "execute_interactive",
        "description": (
            "Execute a program under a PTY and answer its prompts live via MCP "
            "elicitation. Use for a command that must prompt mid-run and cannot "
            "be made non-interactive: pacman without --noconfirm, fdisk, an "
            "installer with no -y. When the client declared elicitation (dmcp "
            "call --interactive), each detected prompt is relayed for an answer "
            "and the reply is typed back to the command; the prompt text is the "
            "command's own output and is untrusted. Without that capability it "
            "falls back to execute_command's safe behavior — closed stdin plus a "
            "'needs_input' report — and never hangs. The result carries 'mode' "
            "(interactive / non-interactive), 'outcome', and 'prompts_answered'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Executable name or path (e.g. 'pacman', '/usr/bin/fdisk')",
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
                    "description": "Optional. Seconds before the command's process group is killed. Omit to run with no timeout.",
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


def _call_execute_interactive(arguments: dict) -> dict:
    """execute_interactive: run a command under a PTY and answer its prompts live.

    Distinct from execute_command, which closes stdin and can only *report* a
    prompt. Here the command keeps a real terminal, so a program that checks
    isatty() still prompts; a detected prompt is relayed to the client via MCP
    elicitation and the answer is typed back. Only attempted when the client
    declared elicitation (dmcp under --interactive); otherwise it falls back to
    the same closed-stdin report execute_command produces, so it never hangs.
    """
    command = arguments["command"]
    args = [str(a) for a in arguments.get("args") or []]
    cwd = arguments.get("cwd") or None
    timeout = _timeout_seconds(arguments)
    extra_env = arguments.get("env") or {}

    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.update(extra_env)

    # A prompt can only be answered if the client promised it can answer — dmcp
    # advertises elicitation solely under --interactive. Without that promise
    # there is nobody to ask, so degrade to the safe non-interactive report
    # rather than block a PTY on an answer that will never arrive.
    if not _CLIENT.get("elicitation"):
        return _execute_interactive_fallback(command, args, cwd, timeout, env)
    return _execute_interactive_pty(command, args, cwd, timeout, env)


def _execute_interactive_fallback(command, args, cwd, timeout, env):
    """No elicitation channel: run with a closed stdin and report exactly like
    execute_command (output plus a needs_input object), tagged mode
    'non-interactive' so the caller can see which path was taken."""
    try:
        proc = subprocess.run(
            [command] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        needs_input = _detect_unanswered_prompt(
            ((proc.stdout or "") + (proc.stderr or ""))[-200:]
        )
        return _interactive_result(
            proc.returncode == 0,
            proc.returncode,
            proc.stdout,
            proc.stderr,
            mode="non-interactive",
            prompts_answered=0,
            outcome="no-elicitation-capability",
            needs_input=needs_input,
        )
    except FileNotFoundError:
        return _interactive_result(
            False, 127, "", f"Command not found: {command}",
            mode="non-interactive", prompts_answered=0, outcome="error",
        )
    except subprocess.TimeoutExpired:
        return _interactive_result(
            False, -1, "", f"Command timed out after {timeout}s",
            mode="non-interactive", prompts_answered=0, outcome="timeout",
        )
    except Exception as exc:
        return _interactive_result(
            False, -1, "", str(exc),
            mode="non-interactive", prompts_answered=0, outcome="error",
        )


def _execute_interactive_pty(command, args, cwd, timeout, env):
    """Run the command under a pseudo-terminal, detecting quiet-tail prompts and
    answering them via elicitation. try/finally guarantees the child's process
    group and both pty fds are torn down — no orphan, no leaked descriptor."""
    idle = _interactive_idle_window()
    max_prompts = _interactive_max_prompts()

    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        return _interactive_result(
            False, -1, "", f"Failed to allocate a pty: {exc}",
            mode="interactive", prompts_answered=0, outcome="error",
        )

    proc = None
    output = bytearray()
    unconsumed = bytearray()
    prompts_answered = 0
    outcome = "completed"
    # After an accept, the terminal echoes the answer we typed back onto the
    # master; that echo is not a new prompt (a prompt-shaped answer would
    # otherwise be re-detected). just_answered marks the window that must ignore
    # it. pgid + pts_path let _pty_read_state confirm the child is actually
    # blocked reading before we treat a quiet tail as a prompt.
    just_answered = False
    answered_text = ""
    pgid = None
    try:
        pts_path = os.ttyname(slave_fd)
    except OSError:
        pts_path = None
    state = {"req_id": _INTERACTIVE_REQ_BASE}
    deadline = (time.monotonic() + timeout) if timeout else None

    try:
        try:
            proc = subprocess.Popen(
                [command] + args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError:
            return _interactive_result(
                False, 127, "", f"Command not found: {command}",
                mode="interactive", prompts_answered=0, outcome="error",
            )
        except Exception as exc:
            return _interactive_result(
                False, -1, "", str(exc),
                mode="interactive", prompts_answered=0, outcome="error",
            )

        # The child owns the slave now; the parent reads and writes the master.
        os.close(slave_fd)
        slave_fd = -1
        pgid = _pgid_of(proc)

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                _kill_group(proc)
                outcome = "timeout"
                break

            window = idle
            if deadline is not None:
                window = min(idle, max(0.0, deadline - time.monotonic()))
            try:
                readable, _, _ = select.select([master_fd], [], [], window)
            except (OSError, ValueError):
                break

            if readable:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    # EIO on Linux once the child's side is gone — treat as EOF.
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
                unconsumed.extend(chunk)
                continue

            # A quiet PTY: the child has either exited or is parked waiting.
            if proc.poll() is not None:
                _drain_into(master_fd, output, unconsumed)
                break

            # Drop the terminal's echo of the answer we just typed. If the only
            # new output is that echo, it is not a fresh prompt — without this a
            # prompt-shaped answer (one ending in '?', or a '[y/N]' choice) would
            # be re-read as the prompt for the NEXT, promptless read and surfaced
            # as if it were a live question.
            if just_answered:
                seen = bytes(unconsumed).decode("utf-8", "replace").strip()
                if seen == "" or seen == answered_text.strip():
                    unconsumed = bytearray()
                just_answered = False

            # Only treat a quiet tail as a prompt when the child is ACTUALLY
            # blocked reading the pty. Detection is output-only and cannot prove
            # an injected answer would land on the triggering read; a quiet
            # computation whose last line merely looks like a prompt must not be
            # answered (the reply would be buffered for a later, unrelated read)
            # nor declined (that would EOF and kill a healthy long-running
            # command). None = the host cannot be introspected (no /proc, unknown
            # arch): fall back to the output-only heuristic there.
            reading = _pty_read_state(pgid, pts_path)
            if reading is False:
                continue

            tail = _tail_text(unconsumed)
            prompt_line = _live_prompt_line(tail)
            if prompt_line is None and reading is True:
                # Provably blocked in read(2): the child is waiting for input
                # whether or not its last line matches a known [Y/n] shape, so
                # relay whatever prompt it printed instead of aborting the
                # operation. A bare read with nothing printed still falls through
                # to the EOF path below.
                prompt_line = _blocked_read_prompt(tail)
            if prompt_line is None:
                if reading is True:
                    # Confirmed blocked on a read we cannot characterize as a
                    # prompt (a bare `read`, fdisk's "Command (m for help): ", a
                    # numbered menu, a REPL banner). There is no prompt shape to
                    # relay and nothing to answer, so do what execute_command's
                    # closed stdin does — EOF the read so it returns 0 and the
                    # command aborts or takes its default — instead of hanging
                    # here forever when no timeout was set.
                    _eof_pty(master_fd)
                    outcome = "no-prompt"
                    _drain_after_eof(master_fd, output, idle, proc)
                    break
                continue

            if prompts_answered >= max_prompts:
                _eof_pty(master_fd)
                outcome = "max-prompts"
                _drain_after_eof(master_fd, output, idle, proc)
                break

            action, answer = _decode_elicit_reply(_elicit(prompt_line, state))
            prompts_answered += 1

            if action == "accept":
                try:
                    os.write(master_fd, answer.encode("utf-8", "replace") + b"\n")
                except OSError:
                    outcome = "write-failed"
                    break
                # This prompt is answered; only later output can be the next one,
                # and the terminal's echo of the answer is not it.
                unconsumed = bytearray()
                just_answered = True
                answered_text = answer
                continue
            if action == "cancel":
                _kill_group(proc)
                outcome = "cancelled"
                _drain_into(master_fd, output, None)
                break
            # decline / unrecognized: EOF the pty so the command aborts as it
            # would under a closed stdin, then capture its exit output. Stop
            # asking — the operation was declined.
            _eof_pty(master_fd)
            outcome = "declined"
            _drain_after_eof(master_fd, output, idle, proc)
            break

        exit_code = _reap(proc)
        transcript = output.decode("utf-8", "replace")
        return _interactive_outcome_result(
            outcome, exit_code, transcript, prompts_answered, timeout, max_prompts
        )
    finally:
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
        _reap(proc)
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def _interactive_outcome_result(outcome, exit_code, transcript, prompts_answered, timeout, max_prompts):
    """Turn a loop outcome + collected transcript into the tool result."""
    code = exit_code if exit_code is not None else -1
    if outcome == "timeout":
        return _interactive_result(
            False, code, transcript, f"Command timed out after {timeout}s",
            mode="interactive", prompts_answered=prompts_answered, outcome="timeout",
        )
    if outcome == "cancelled":
        return _interactive_result(
            False, code, transcript, "Operation cancelled by the client mid-prompt.",
            mode="interactive", prompts_answered=prompts_answered, outcome="cancelled",
        )
    if outcome == "write-failed":
        return _interactive_result(
            False, code, transcript, "Failed to write the answer to the command's pty.",
            mode="interactive", prompts_answered=prompts_answered, outcome="write-failed",
        )
    if outcome == "no-prompt":
        # The child was blocked on a read we could not characterize as a prompt;
        # it got EOF and aborted or took its default, exactly as
        # execute_command's closed stdin would leave it. Carry the same
        # needs_input report so this is never worse than execute_command here.
        needs_input = _detect_unanswered_prompt(transcript[-200:])
        return _interactive_result(
            exit_code == 0, code, transcript, "",
            mode="interactive", prompts_answered=prompts_answered,
            outcome="no-prompt", needs_input=needs_input,
        )
    stderr = ""
    if outcome == "max-prompts":
        stderr = (
            f"Interactive prompt budget ({max_prompts}) exhausted; the command's "
            f"input was closed and it was left to abort."
        )
    return _interactive_result(
        exit_code == 0, code, transcript, stderr,
        mode="interactive", prompts_answered=prompts_answered, outcome=outcome,
    )


def _interactive_result(
    success: bool,
    exit_code: int,
    stdout: str,
    stderr: str,
    mode: str,
    prompts_answered: int,
    outcome: str,
    needs_input: dict | None = None,
) -> dict:
    """execute_interactive's result envelope: execute_command's fields plus the
    mode taken, the number of prompts answered, and a terminal outcome."""
    payload = {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "mode": mode,
        "prompts_answered": prompts_answered,
        "outcome": outcome,
    }
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


def _interactive_idle_window() -> float:
    """Seconds of PTY quiet before the tail is inspected for a prompt. There is
    no signal that a program is 'waiting for input', so a short idle window is
    the heuristic; env-overridable via JARVIS_INTERACTIVE_IDLE_MS."""
    raw = os.environ.get("JARVIS_INTERACTIVE_IDLE_MS")
    try:
        ms = float(raw) if raw else 300.0
    except ValueError:
        ms = 300.0
    return (ms if ms > 0 else 300.0) / 1000.0


def _interactive_max_prompts() -> int:
    """Cap on elicitations for one call, so a program looping on a prompt cannot
    loop forever; env-overridable via JARVIS_INTERACTIVE_MAX_PROMPTS."""
    raw = os.environ.get("JARVIS_INTERACTIVE_MAX_PROMPTS")
    try:
        n = int(raw) if raw else 24
    except ValueError:
        n = 24
    return n if n > 0 else 24


def _tail_text(unconsumed) -> str:
    """Decode the tail of the not-yet-answered output for prompt inspection."""
    return bytes(unconsumed[-200:]).decode("utf-8", "replace")


def _live_prompt_line(tail_text: str):
    """The prompt line the command is blocked on, or None. The live twin of
    `_detect_unanswered_prompt`: it reuses that helper's exact regexes and
    last-line/tail logic, so the shapes answered here are the shapes
    execute_command reports — only inspected while the process is still alive."""
    report = _detect_unanswered_prompt(tail_text)
    return report["prompt"] if report else None


def _blocked_read_prompt(tail_text):
    """The last visible line to relay when the child is provably blocked in
    read(2) but its line matches no known [Y/n] prompt shape.

    A process parked in read(2) on our pty is waiting for input by definition,
    so its last non-empty line is the question — fdisk's "Command (m for help):
    ", a REPL banner, "Select an option:". Relaying it lets the operation be
    answered instead of aborted, which is the difference between working on a
    command and merely not hanging on it. Returns None when there is nothing to
    show — a bare `read` that printed no prompt — so the caller EOFs rather than
    raising an empty question. Only ever consulted once _pty_read_state has
    confirmed the child is actually blocked reading, so a quiet computation is
    never mistaken for a prompt."""
    if not tail_text:
        return None
    stripped = tail_text.rstrip("\r\n")
    if not stripped:
        return None
    last = stripped.rsplit("\n", 1)[-1].strip()
    return last or None


def _decode_elicit_reply(reply):
    """Map dmcp's elicitation response to (action, answer).

    `reply` is the full JSON-RPC object dmcp sent, or None if the stream closed.
    Only a well-formed accept carrying a string 'answer' becomes an accept; an
    explicit cancel is honored; every other shape — decline, an error, a dropped
    stream, a malformed payload — collapses to a decline, the one safe default,
    the same result a closed stdin already produces."""
    if not isinstance(reply, dict) or "error" in reply:
        return ("decline", None)
    result = reply.get("result")
    if not isinstance(result, dict):
        return ("decline", None)
    action = result.get("action")
    if action == "accept":
        content = result.get("content")
        if isinstance(content, dict) and isinstance(content.get("answer"), str):
            return ("accept", content["answer"])
        return ("decline", None)
    if action == "cancel":
        return ("cancel", None)
    return ("decline", None)


def _send_message(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _read_elicit_reply(want_id):
    """Read stdin until the reply to `want_id` arrives; return it, or None on EOF.

    execute_interactive is the JSON-RPC CLIENT for the duration of an
    elicitation, so stdin now carries two things: the response to our question
    (matching id, has result/error) and any request dmcp still sends (has method
    + id). A request must be answered — with a busy error — never dropped, or
    dmcp would block on a request we ate and re-create the hang this tool
    removes. Mirrors fake_eliciting_server.py's `_read_reply`."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == want_id and ("result" in msg or "error" in msg):
            return msg
        if msg.get("method") is not None and msg.get("id") is not None:
            _send_message({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32603, "message": "busy answering an interactive prompt"},
            })
    return None


def _elicit(prompt_line: str, state: dict):
    """Ask the client to answer `prompt_line`; return dmcp's raw reply (or None).

    Emits an `elicitation/create` request carrying the command's own prompt as
    `message` verbatim — it is the command's untrusted output, not ours, so it
    is passed through for dmcp and the daemon to attribute and gate."""
    state["req_id"] += 1
    want_id = state["req_id"]
    _send_message({
        "jsonrpc": "2.0",
        "id": want_id,
        "method": "elicitation/create",
        "params": {
            "message": prompt_line,
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The line of input to type at the command's prompt.",
                    }
                },
                "required": ["answer"],
            },
        },
    })
    return _read_elicit_reply(want_id)


def _eof_pty(master_fd):
    """Deliver EOF to the command reading the pty — the terminal analog of
    closing a pipe's write end. In canonical mode an EOT (^D) on an empty input
    line makes the child's read() return 0, so a command waiting on input aborts
    as it does under a closed stdin, without tearing down the fd we still read
    the abort output from."""
    try:
        os.write(master_fd, b"\x04")
    except OSError:
        pass


def _group_pids(pgid):
    """PIDs in the command's process group — its whole subtree, since
    start_new_session made the child the group leader — read from /proc. Empty on
    any host without /proc, so callers degrade rather than fail."""
    pids = []
    if pgid is None:
        return pids
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/" + name + "/stat") as fh:
                data = fh.read()
        except OSError:
            continue
        # comm (field 2) can hold spaces and parens, so split AFTER the last
        # ')': the fields that follow are state, ppid, pgrp, ...
        cut = data.rfind(")")
        if cut < 0:
            continue
        fields = data[cut + 1:].split()
        if len(fields) < 3:
            continue
        try:
            if int(fields[2]) == pgid:
                pids.append(int(name))
        except ValueError:
            continue
    return pids


def _pty_read_state(pgid, pts_path):
    """Whether a process in the command's group is blocked reading our pty.

    True when some group member is parked in read(2) on a descriptor that
    resolves to `pts_path` — the evidence that a quiet pty is a prompt awaiting
    an answer rather than a busy or sleeping command. False when the group was
    inspected and none is, so a quiet-but-working command is left to run exactly
    as execute_command's closed stdin leaves it. None when the host cannot be
    introspected at all — no /proc, an architecture whose read syscall number we
    do not know, or every /proc/<pid>/syscall unreadable — so the caller falls
    back to the output-only heuristic instead of misreading 'cannot tell' as
    'not waiting'."""
    if _READ_SYSCALL_NR is None or not pts_path:
        return None
    want = str(_READ_SYSCALL_NR)
    saw_any = False
    for pid in _group_pids(pgid):
        try:
            with open("/proc/" + str(pid) + "/syscall") as fh:
                fields = fh.read().split()
        except OSError:
            continue
        saw_any = True
        if not fields or fields[0] != want:
            continue
        try:
            fd = int(fields[1], 16)
        except (IndexError, ValueError):
            continue
        try:
            target = os.readlink("/proc/" + str(pid) + "/fd/" + str(fd))
        except OSError:
            continue
        if target == pts_path:
            return True
    return False if saw_any else None


def _signal_group(proc, pgid, sig):
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except OSError:
        pass


def _pgid_of(proc):
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return None


def _kill_group(proc):
    """SIGTERM then, if needed, SIGKILL the command's process group.
    start_new_session made the child a group leader, so this tears down the
    whole subtree instead of leaving a grandchild behind."""
    if proc is None or proc.poll() is not None:
        return
    pgid = _pgid_of(proc)
    _signal_group(proc, pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    _signal_group(proc, pgid, signal.SIGKILL)


def _reap(proc):
    """Wait for the child and return its exit code (or None if uncollectable),
    so no zombie survives the tool call."""
    if proc is None:
        return None
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_group(proc, _pgid_of(proc), signal.SIGKILL)
        try:
            return proc.wait(timeout=2)
        except Exception:
            return None
    except Exception:
        return proc.returncode


def _drain_into(master_fd, output, unconsumed):
    """Pull already-buffered PTY output without blocking (the child has exited or
    is being torn down)."""
    while True:
        try:
            readable, _, _ = select.select([master_fd], [], [], 0)
        except (OSError, ValueError):
            return
        if not readable:
            return
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        output.extend(chunk)
        if unconsumed is not None:
            unconsumed.extend(chunk)


def _drain_after_eof(master_fd, output, idle, proc):
    """Read what the command prints as it aborts after EOF, bounded so a command
    that ignores EOF cannot hold the call open."""
    stop = time.monotonic() + max(idle, 2.0)
    while True:
        if proc.poll() is not None:
            _drain_into(master_fd, output, None)
            return
        remaining = stop - time.monotonic()
        if remaining <= 0:
            return
        try:
            readable, _, _ = select.select([master_fd], [], [], min(idle, remaining))
        except (OSError, ValueError):
            return
        if not readable:
            continue
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        output.extend(chunk)


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
        # Remember whether the client can answer a live prompt. dmcp declares
        # elicitation only on the `--interactive` path, so this is what tells
        # execute_interactive to answer prompts live versus fall back to the
        # closed-stdin report.
        caps = params.get("capabilities") or {}
        _CLIENT["elicitation"] = isinstance(caps, dict) and "elicitation" in caps
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
        if name == "execute_interactive":
            return ok(_call_execute_interactive(arguments))
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
