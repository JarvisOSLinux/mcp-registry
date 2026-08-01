#!/usr/bin/env python3
"""
JARVIS Shell MCP Server

Minimal stdio MCP server that runs shell commands and returns structured output.
No whitelist — security gating is handled by JARVIS's confirmation layer.
Uses only Python stdlib; no external dependencies.

Interactive commands run as named jobs: run_job double-forks a detached PTY
holder whose handle (out.log / in.sock / status / pids) lives on the
filesystem under $XDG_RUNTIME_DIR/jarvis-shell/, because every tool call is a
fresh, short-lived server process and only the filesystem survives between
calls.
"""

import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

try:
    import pty
except ImportError:
    # Windows has no PTY; the job tools then report "unsupported" instead of
    # the whole server failing at import.
    pty = None

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
    {
        "name": "open_app",
        "description": (
            "Open a file, URL, or application with the desktop's default handler "
            "(xdg-open / open / start). Use to launch a GUI app or show something "
            "to the user — not to run a command and read its output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "File path, URL, or application name to open",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "run_job",
        "description": (
            "Run a command that may need interactive input. The command runs "
            "under a real PTY as a named job that survives across tool calls, "
            "and this call BLOCKS until the job exits — so dispatch run_job "
            "as a concurrent task, not inline, and ALWAYS give that task a "
            "remind_after (30s is the suggested interval). The call parks for "
            "as long as the command waits on input, so with no reminder "
            "nothing ever reports that a prompt is up. While blocked, "
            "everything the command prints is streamed live to this task's "
            "stderr. The "
            "intended loop: dispatch run_job as a task; when a REMIND or "
            "status tail shows the command waiting on input (a prompt, a "
            "menu, a [Y/n]), call send_input with exactly what the output "
            "asked for; never guess input the output did not ask for. "
            "Returns the full transcript, exit code, and job name. Prefer "
            "execute_command for anything non-interactive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run under /bin/sh -c in the job's PTY",
                },
                "job": {
                    "type": "string",
                    "description": (
                        "Job name (1-64 chars: letters, digits, '.', '_', '-'). "
                        "Addresses the job from send_input / read_output / kill_job."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional. Seconds before the job is killed. Omit to run with no timeout — long-running work is tracked and controlled via dispatch's REMIND/wait/kill, not killed here.",
                },
            },
            "required": ["command", "job"],
        },
    },
    {
        "name": "send_input",
        "description": (
            "Deliver input to a running job started by run_job (call this "
            "from a separate task — the run_job call itself stays blocked). "
            "The text is written verbatim to the job's terminal: include the "
            "trailing newline yourself when the command expects Enter. "
            "Returns immediately with a delivery confirmation; the effect "
            "shows up in the job's output (read_output, or the blocked "
            "run_job's stderr stream). Send only what the job's output "
            "actually asked for — never guess input it did not ask for."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {
                    "type": "string",
                    "description": "Name of the running job to send input to",
                },
                "text": {
                    "type": "string",
                    "description": "Text to write verbatim to the job's terminal (caller supplies any trailing newline)",
                },
            },
            "required": ["job", "text"],
        },
    },
    {
        "name": "read_output",
        "description": (
            "Read a job's output so far, with its running/exited state. Works "
            "mid-run and after exit: while the job runs it returns everything "
            "printed so far (use it to see the exact prompt before "
            "send_input); once the job has exited it also carries the exit "
            "code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {
                    "type": "string",
                    "description": "Name of the job to read",
                },
            },
            "required": ["job"],
        },
    },
    {
        "name": "kill_job",
        "description": (
            "Terminate a running job: SIGTERM to the job's whole process "
            "group, escalating to SIGKILL after a short grace, then a report "
            "of what happened. Use for a job that is stuck, looping, or no "
            "longer needed — killing it unblocks the pending run_job call, "
            "which returns the transcript so far."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {
                    "type": "string",
                    "description": "Name of the job to terminate",
                },
            },
            "required": ["job"],
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


def _display_env() -> dict:
    """Env with display vars set, detected from XDG_RUNTIME_DIR when missing.

    A server started by dmcp does not always inherit the desktop session's
    WAYLAND_DISPLAY, and without it the opener has no display to target.
    """
    env = os.environ.copy()
    if "WAYLAND_DISPLAY" not in env:
        xdg_runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        for candidate in ("wayland-1", "wayland-0"):
            if os.path.exists(os.path.join(xdg_runtime, candidate)):
                env["WAYLAND_DISPLAY"] = candidate
                env.setdefault("XDG_RUNTIME_DIR", xdg_runtime)
                break
    env.setdefault("DISPLAY", ":0")
    return env


def _open_command(target: str) -> str:
    """Per-OS command to open a file/URL/app (never elevated)."""
    if sys.platform == "darwin":
        return f"open {shlex.quote(target)}"
    if sys.platform == "win32":
        # `start` needs an explicit empty title argument or it misreads a
        # quoted target as the window title.
        return f'cmd /c start "" {shlex.quote(target)}'
    return f"xdg-open {shlex.quote(target)}"


def _call_open_app(arguments: dict) -> dict:
    target = arguments.get("target")
    if not isinstance(target, str) or not target.strip():
        return _run_result(False, -1, "", "open_app requires a non-empty 'target'")

    env = (
        os.environ.copy()
        if sys.platform in ("darwin", "win32")
        else _display_env()
    )

    try:
        proc = subprocess.run(
            _open_command(target),
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # The opener handed off to a GUI app that kept the pipes open; the
        # launch itself succeeded, so this is not a failure.
        return _run_result(True, 0, f"Launched: {target}", "")
    except Exception as exc:
        return _run_result(False, -1, "", str(exc))

    if proc.returncode != 0:
        return _run_result(False, proc.returncode, proc.stdout, proc.stderr)
    return _run_result(True, 0, f"Opened: {target}", proc.stderr)


# \Z, not $: $ also matches before a trailing newline, which would admit
# 'evil\n' as a job name — and '..\n' past the explicit dot-name check.
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_JOB_LOG = "out.log"
_JOB_SOCK = "in.sock"
_JOB_STATUS = "status"
_JOB_HOLDER_PID = "holder.pid"
_JOB_CHILD_PID = "child.pid"
_JOB_FINISHED_TTL_SECS = 24 * 60 * 60
# Shields a job dir a concurrent run_job created moments ago, before its holder
# wrote holder.pid — without this the sweep would read "no pid, no status" as a
# crashed holder and delete a job that is still being born.
_JOB_SWEEP_GRACE_SECS = 60
_JOB_KILL_GRACE_SECS = 2.0
_JOB_HOLDER_READY_TIMEOUT_SECS = 10


def _jobs_unsupported() -> str | None:
    if pty is not None and sys.platform in ("linux", "darwin"):
        return None
    return (
        "Job tools need a Unix PTY and Unix sockets, which this host "
        f"({sys.platform}) does not provide; only Linux and macOS are "
        "supported. Use execute_command for non-interactive commands."
    )


def _jobs_root() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return os.path.join(xdg, "jarvis-shell")
    return os.path.join(tempfile.gettempdir(), f"jarvis-shell-{os.getuid()}")


def _ensure_jobs_root() -> str:
    """Create (0700) and trust-check the per-user jobs directory.

    The /tmp fallback is a shared namespace, so a pre-planted directory owned
    by another uid would let a co-resident user read job output and feed input
    into jobs. Same caution as dmcp's broker socket dir: refuse anything not
    owned by us, refuse a non-directory (lstat also catches a symlink), and
    tighten a pre-existing looser mode.
    """
    root = _jobs_root()
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"refusing to use {root}: not a directory")
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"refusing to use {root}: owned by uid {st.st_uid}, not {os.getuid()}"
        )
    os.chmod(root, 0o700)
    return root


def _job_file(job_dir: str, name: str) -> str:
    return os.path.join(job_dir, name)


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_file(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM still means the pid exists.
        return True
    return True


def _job_state(job_dir: str) -> tuple:
    """One of ('exited', code), ('running', None), ('crashed', None).

    'crashed' means the holder died without recording an exit code — nothing
    will ever finish this job.
    """
    code = _read_int(_job_file(job_dir, _JOB_STATUS))
    if code is not None:
        return "exited", code
    holder = _read_int(_job_file(job_dir, _JOB_HOLDER_PID))
    if holder is not None and _pid_alive(holder):
        return "running", None
    return "crashed", None


def _read_job_log(job_dir: str) -> str:
    try:
        with open(_job_file(job_dir, _JOB_LOG), "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _holder_relay(master_fd: int, listener, log_fd: int, child_pid: int) -> int:
    """Pump PTY output into out.log and socket input into the PTY until exit.

    The master is non-blocking and pending input is written only when select
    says the PTY is writable, so a full PTY buffer — or a reader that is gone —
    can never wedge the holder. After the child is reaped the loop keeps
    draining only while output keeps arriving: orphaned grandchildren may hold
    the slave open forever, and status is about the command actually run.
    """
    os.set_blocking(master_fd, False)
    conns = []
    pending = b""
    exit_code = None
    drain_deadline = None
    while True:
        rlist = [master_fd, listener] + conns
        wlist = [master_fd] if pending else []
        ready_r, ready_w, _ = select.select(rlist, wlist, [], 0.2)
        if master_fd in ready_r:
            try:
                chunk = os.read(master_fd, 65536)
            except BlockingIOError:
                chunk = None
            except OSError:
                # Linux reports a fully-closed slave as EIO, not EOF.
                chunk = b""
            if chunk:
                os.write(log_fd, chunk)
                if drain_deadline is not None:
                    drain_deadline = time.monotonic() + 0.5
            elif chunk == b"":
                break
        if listener in ready_r:
            try:
                conn, _addr = listener.accept()
                conn.setblocking(False)
                conns.append(conn)
            except OSError:
                pass
        for conn in list(conns):
            if conn not in ready_r:
                continue
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if data:
                pending += data
            else:
                conns.remove(conn)
                conn.close()
        if pending and master_fd in ready_w:
            try:
                pending = pending[os.write(master_fd, pending):]
            except BlockingIOError:
                pass
            except OSError:
                pending = b""
        if exit_code is None:
            done_pid, wstatus = os.waitpid(child_pid, os.WNOHANG)
            if done_pid == child_pid:
                exit_code = os.waitstatus_to_exitcode(wstatus)
                drain_deadline = time.monotonic() + 0.5
        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            break
    for conn in conns:
        conn.close()
    if exit_code is None:
        # EOF on the master with the child still unreaped: it is exiting (or
        # detached after closing every slave fd, in which case the job truly
        # has not finished yet and this holder must keep waiting for it).
        exit_code = os.waitstatus_to_exitcode(os.waitpid(child_pid, 0)[1])
    return exit_code


def _holder_main(job_dir: str, command: str, ready_fd: int) -> None:
    """Body of the detached holder process; never returns.

    Runs with stdin/stdout/stderr already on /dev/null: this process was
    forked from the server, whose stdout is the JSON-RPC wire — a single stray
    byte there would corrupt the protocol stream.
    """
    log_fd = os.open(
        _job_file(job_dir, _JOB_LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    sock_path = _job_file(job_dir, _JOB_SOCK)
    exit_code = -1
    try:
        _write_file(_job_file(job_dir, _JOB_HOLDER_PID), str(os.getpid()))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        os.chmod(sock_path, 0o600)
        listener.listen(4)
        listener.setblocking(False)

        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            env = os.environ.copy()
            env.setdefault(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            )
            try:
                os.execve("/bin/sh", ["/bin/sh", "-c", command], env)
            finally:
                os._exit(127)

        _write_file(_job_file(job_dir, _JOB_CHILD_PID), str(child_pid))
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        exit_code = _holder_relay(master_fd, listener, log_fd, child_pid)
    except Exception as exc:
        try:
            os.write(log_fd, f"\n[jarvis-shell] job holder error: {exc!r}\n".encode())
        except OSError:
            pass
    finally:
        # status doubles as the "job finished" signal; write-then-rename so a
        # reader can never observe a half-written exit code.
        try:
            tmp_path = _job_file(job_dir, _JOB_STATUS + ".tmp")
            _write_file(tmp_path, str(exit_code))
            os.replace(tmp_path, _job_file(job_dir, _JOB_STATUS))
        except OSError:
            pass
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        os._exit(0)


def _spawn_holder(job_dir: str, command: str) -> None:
    """Detach a holder for the job via double fork + setsid.

    Every tool call is a fresh, short-lived server process (one-shot dmcp
    lifecycle), so the holder must not stay our child — only the filesystem
    handle survives between calls. The pipe makes startup synchronous: by the
    time the ready byte arrives, holder.pid, in.sock and child.pid all exist.
    A holder that dies during setup closes the pipe instead, and the caller's
    wait loop reports that as crashed (or exited -1, when the holder got far
    enough to record status). The one failure decided here is a holder that
    stays alive but never signals ready: it is killed and startup fails,
    because falling into the blocking read would hang run_job forever.
    """
    read_fd, write_fd = os.pipe()
    first = os.fork()
    if first == 0:
        try:
            os.close(read_fd)
            os.setsid()
            if os.fork() == 0:
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 0)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                if devnull > 2:
                    os.close(devnull)
                _holder_main(job_dir, command, write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    os.waitpid(first, 0)
    ready, _, _ = select.select([read_fd], [], [], _JOB_HOLDER_READY_TIMEOUT_SECS)
    if not ready:
        # A dead holder closes the pipe (readable EOF); only a wedged-but-alive
        # one times out, and the blocking read below would then wait on it
        # forever. Best-effort kill so it cannot wedge indefinitely off-record.
        os.close(read_fd)
        holder = _read_int(_job_file(job_dir, _JOB_HOLDER_PID))
        if holder is not None:
            try:
                os.kill(holder, signal.SIGKILL)
            except OSError:
                pass
        raise RuntimeError(
            "job holder did not become ready within "
            f"{_JOB_HOLDER_READY_TIMEOUT_SECS}s and was killed"
        )
    try:
        os.read(read_fd, 1)
    except OSError:
        pass
    os.close(read_fd)


def _stream_job_output(log_path: str, offset: int) -> int:
    """Copy new out.log bytes to OUR stderr — never stdout, the JSON-RPC wire.

    dispatch surfaces a running task's stderr tail in REMIND/status signals,
    so this live copy is what lets the model see a prompt while run_job is
    still blocked. Raw bytes, not decoded text: a partial UTF-8 sequence at a
    chunk boundary must not be mangled.
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return offset
    if chunk:
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()
    return offset + len(chunk)


def _job_name_error(job) -> str | None:
    """A job name is a path component under the jobs root, nothing more.

    The regex admits '.' and '..', which are path navigation rather than
    names — the explicit check is what keeps a job inside the root.
    """
    if isinstance(job, str) and _JOB_NAME_RE.match(job) and job not in (".", ".."):
        return None
    return (
        f"Invalid job name {job!r}: use 1-64 characters from letters, digits, "
        "'.', '_', '-' (it is used as a directory name; no slashes, no '.' or '..')"
    )


def _job_response(success: bool, payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": not success,
    }


def _job_error(message: str, job=None) -> dict:
    payload = {"success": False, "error": message}
    if job is not None:
        payload["job"] = job
    return _job_response(False, payload)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _terminate_job(job_dir: str) -> str:
    """SIGTERM the job's process group, escalating to SIGKILL after a grace.

    The group is the command child's own session (pty.fork calls setsid), so
    everything the command spawned goes with it — but not the holder, which
    must survive to reap the child and record the exit code.
    """
    pgid = _read_int(_job_file(job_dir, _JOB_CHILD_PID))
    if pgid is None:
        return "no recorded process group; nothing to signal"
    sent = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except OSError as exc:
            return f"{sig.name} to process group {pgid} failed: {exc}"
        sent.append(sig.name)
        deadline = time.monotonic() + _JOB_KILL_GRACE_SECS
        while time.monotonic() < deadline and _group_alive(pgid):
            time.sleep(0.05)
        if not _group_alive(pgid):
            break
    if _group_alive(pgid):
        return f"sent {' then '.join(sent)}, but the process group is still alive"
    if sent:
        return f"process group terminated by {' then '.join(sent)}"
    return "process group was already gone"


def _release_crashed_job(job_dir: str) -> None:
    """Best-effort SIGTERM to a crashed job's process group, before removal.

    child.pid in this dir is the ONLY handle to the command's session — the
    holder that would have reaped and signalled it is dead. Removing the dir
    without signalling first would leave a still-running command as a runaway
    nothing can ever address again. Alive is checked first, and ESRCH/EPERM
    are swallowed: the group is already gone, or was never ours to signal —
    either way removal may proceed.
    """
    pgid = _read_int(_job_file(job_dir, _JOB_CHILD_PID))
    if pgid is None or not _group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass


def _call_run_job(arguments: dict) -> dict:
    unsupported = _jobs_unsupported()
    if unsupported is not None:
        return _job_error(unsupported)
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return _job_error("run_job requires a non-empty 'command' string")
    job = arguments.get("job")
    invalid = _job_name_error(job)
    if invalid is not None:
        return _job_error(invalid)
    timeout = _timeout_seconds(arguments)

    try:
        root = _ensure_jobs_root()
        job_dir = os.path.join(root, job)
        if os.path.isdir(job_dir):
            state, _code = _job_state(job_dir)
            if state == "running":
                return _job_error(
                    f"Job '{job}' already exists and is still running. Pick "
                    "another name, or use send_input / read_output / kill_job "
                    "to interact with the live job.",
                    job,
                )
            if state == "crashed":
                _release_crashed_job(job_dir)
            shutil.rmtree(job_dir, ignore_errors=True)
        os.mkdir(job_dir, 0o700)
        _spawn_holder(job_dir, command)
    except (OSError, RuntimeError) as exc:
        return _job_error(f"Could not start job '{job}': {exc}", job)

    log_path = _job_file(job_dir, _JOB_LOG)
    sys.stderr.write(f"[job {job}] started: {command}\n")
    sys.stderr.flush()
    offset = 0
    started = time.monotonic()
    while True:
        offset = _stream_job_output(log_path, offset)
        state, code = _job_state(job_dir)
        if state != "running":
            break
        if timeout is not None and time.monotonic() - started > timeout:
            _terminate_job(job_dir)
            state, code = "timeout", None
            break
        time.sleep(0.1)
    _stream_job_output(log_path, offset)
    transcript = _read_job_log(job_dir)

    if state == "exited":
        return _job_response(
            code == 0,
            {"success": code == 0, "exit_code": code, "job": job, "transcript": transcript},
        )
    if state == "timeout":
        return _job_response(
            False,
            {
                "success": False,
                "exit_code": -1,
                "job": job,
                "transcript": transcript,
                "error": f"Job timed out after {timeout}s and was killed",
            },
        )
    return _job_response(
        False,
        {
            "success": False,
            "exit_code": -1,
            "job": job,
            "transcript": transcript,
            "error": "Job holder died without recording an exit code",
        },
    )


def _call_send_input(arguments: dict) -> dict:
    unsupported = _jobs_unsupported()
    if unsupported is not None:
        return _job_error(unsupported)
    job = arguments.get("job")
    invalid = _job_name_error(job)
    if invalid is not None:
        return _job_error(invalid)
    text = arguments.get("text")
    if not isinstance(text, str) or not text:
        return _job_error(
            "send_input requires a non-empty 'text' string (include the "
            "trailing newline yourself when the command expects Enter)",
            job,
        )
    try:
        root = _ensure_jobs_root()
    except (OSError, RuntimeError) as exc:
        return _job_error(str(exc), job)
    job_dir = os.path.join(root, job)
    if not os.path.isdir(job_dir):
        return _job_error(
            f"Unknown job '{job}' — no such job exists (run_job starts one)", job
        )
    state, code = _job_state(job_dir)
    if state == "exited":
        return _job_error(
            f"Job '{job}' already exited with code {code}; input not delivered", job
        )
    if state == "crashed":
        return _job_error(
            f"Job '{job}' holder died without recording an exit code; "
            "input not delivered",
            job,
        )
    payload = text.encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(_job_file(job_dir, _JOB_SOCK))
        client.sendall(payload)
    except OSError as exc:
        return _job_error(
            f"Could not deliver input to job '{job}' ({exc}); it may have just exited",
            job,
        )
    finally:
        client.close()
    return _job_response(
        True,
        {
            "success": True,
            "job": job,
            "delivered_bytes": len(payload),
            "result": (
                "Input delivered verbatim to the job's terminal; the effect "
                "shows up in the job's output (read_output, or the blocked "
                "run_job's stderr stream)"
            ),
        },
    )


def _call_read_output(arguments: dict) -> dict:
    unsupported = _jobs_unsupported()
    if unsupported is not None:
        return _job_error(unsupported)
    job = arguments.get("job")
    invalid = _job_name_error(job)
    if invalid is not None:
        return _job_error(invalid)
    try:
        root = _ensure_jobs_root()
    except (OSError, RuntimeError) as exc:
        return _job_error(str(exc), job)
    job_dir = os.path.join(root, job)
    if not os.path.isdir(job_dir):
        return _job_error(
            f"Unknown job '{job}' — no such job exists (run_job starts one)", job
        )
    state, code = _job_state(job_dir)
    payload = {
        "success": True,
        "job": job,
        "state": state,
        "output": _read_job_log(job_dir),
    }
    if state == "exited":
        payload["exit_code"] = code
    elif state == "crashed":
        payload["note"] = "The job's holder died without recording an exit code"
    return _job_response(True, payload)


def _call_kill_job(arguments: dict) -> dict:
    unsupported = _jobs_unsupported()
    if unsupported is not None:
        return _job_error(unsupported)
    job = arguments.get("job")
    invalid = _job_name_error(job)
    if invalid is not None:
        return _job_error(invalid)
    try:
        root = _ensure_jobs_root()
    except (OSError, RuntimeError) as exc:
        return _job_error(str(exc), job)
    job_dir = os.path.join(root, job)
    if not os.path.isdir(job_dir):
        return _job_error(
            f"Unknown job '{job}' — no such job exists (run_job starts one)", job
        )
    state, code = _job_state(job_dir)
    if state == "exited":
        return _job_response(
            True,
            {
                "success": True,
                "job": job,
                "state": "exited",
                "exit_code": code,
                "result": f"Job already exited with code {code}; nothing to kill",
            },
        )
    result = _terminate_job(job_dir)
    # Give the holder a beat to reap the child and record the exit code the
    # kill provoked, so the caller sees "exited" rather than a stale "running".
    deadline = time.monotonic() + _JOB_KILL_GRACE_SECS
    while time.monotonic() < deadline:
        state, code = _job_state(job_dir)
        if state == "exited":
            break
        time.sleep(0.05)
    payload = {"success": True, "job": job, "state": state, "result": result}
    if state == "exited":
        payload["exit_code"] = code
    elif state == "crashed":
        payload["note"] = (
            "The job's holder was already dead, so no exit code was recorded; "
            "the process group was signalled directly"
        )
    return _job_response(True, payload)


def _sweep_stale_jobs() -> None:
    """Startup sweep of job dirs nothing will ever come back for.

    Two rules: a dir with no status whose holder is dead is a crashed holder's
    residue (nothing will ever finish it), and a finished dir older than 24h
    has had its day. _JOB_SWEEP_GRACE_SECS shields a dir a concurrent run_job
    created moments ago, before its holder wrote holder.pid. Passive on the
    root itself: a foreign-owned or replaced root is left alone, never created
    or repaired here.
    """
    if _jobs_unsupported() is not None:
        return
    root = _jobs_root()
    try:
        st = os.lstat(root)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return
    now = time.time()
    for name in os.listdir(root):
        job_dir = os.path.join(root, name)
        if not os.path.isdir(job_dir):
            continue
        try:
            status_age = now - os.stat(_job_file(job_dir, _JOB_STATUS)).st_mtime
        except OSError:
            status_age = None
        if status_age is not None:
            if status_age > _JOB_FINISHED_TTL_SECS:
                shutil.rmtree(job_dir, ignore_errors=True)
            continue
        try:
            dir_age = now - os.stat(job_dir).st_mtime
        except OSError:
            continue
        if dir_age < _JOB_SWEEP_GRACE_SECS:
            continue
        holder = _read_int(_job_file(job_dir, _JOB_HOLDER_PID))
        if holder is not None and _pid_alive(holder):
            continue
        _release_crashed_job(job_dir)
        shutil.rmtree(job_dir, ignore_errors=True)


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
            "serverInfo": {"name": "jarvis-shell-mcp", "version": "1.1.0"},
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
        if name == "open_app":
            return ok(_call_open_app(arguments))
        if name == "run_job":
            return ok(_call_run_job(arguments))
        if name == "send_input":
            return ok(_call_send_input(arguments))
        if name == "read_output":
            return ok(_call_read_output(arguments))
        if name == "kill_job":
            return ok(_call_kill_job(arguments))
        return err(-32601, f"Unknown tool: {name}")

    return err(-32601, f"Method not found: {method}")


def main() -> None:
    try:
        _sweep_stale_jobs()
    except Exception:
        # A cleanup failure must never take down the JSON-RPC loop.
        pass
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
