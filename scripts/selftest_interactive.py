#!/usr/bin/env python3
"""selftest_interactive.py — prove execute_interactive answers a live prompt.

execute_interactive runs a command under a PTY and answers the prompts it raises
via MCP elicitation, so a command that must prompt mid-run (pacman without
--noconfirm, fdisk, an installer with no -y) is answered live instead of aborting
on a closed stdin. This self-test has two halves:

  1. Unit-tests the PURE helpers directly, on both servers' modules, and requires
     them to agree byte-for-byte:
       - `_live_prompt_line(tail)` — the live twin of `_detect_unanswered_prompt`,
         detecting a prompt only at the tail of the not-yet-answered output.
       - `_decode_elicit_reply(reply)` — the outcome->PTY mapping: which of
         accept / decline / cancel a dmcp elicitation response means, and that
         every malformed or missing shape collapses to a safe decline.
       - byte-identity of the whole execute_interactive body across both servers.

  2. Drives the server through the REAL dmcp binary end to end. A temp
     MCP_USER_INSTALL_DIR is populated with a manifest + index.json (the shape
     dmcp's own tests/session_broker.rs fixture uses), then
     `dmcp call <id> execute_interactive --interactive` is spawned with piped
     stdio and answered over the tagged JSON stream. The bar is a REAL `read a`
     in a REAL `sh` receiving a REAL `y`:
       (a) accept carries the answer into the command (GOT=y);
       (b) a [Y/n]-style prompt is detected and surfaced;
       (c) decline aborts the command legibly (no answer reaches it);
       (d) a clean command with no prompt returns normally too;
       (f) without --interactive it falls back to the closed-stdin report.
     Both server.py files are exercised on the accept path, since they must be
     identical.

  If the real dmcp binary is absent, the end-to-end half SKIPS with a message
  rather than failing (it is present in this repo's environment, so it runs).

Offline, stdlib only.

Usage:
  python3 scripts/selftest_interactive.py
"""
import importlib.util
import json
import os
import pathlib
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
USER_SERVER = REPO / "servers" / "jarvis-shell" / "server.py"
SYSTEM_SERVER = REPO / "servers" / "jarvis-shell-system" / "server.py"
DMCP_BIN = pathlib.Path("/home/user/dmcp/target/release/dmcp")

FAILURES = []
SKIPS = []


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
# Half 1: the pure helpers
# ---------------------------------------------------------------------------

MIDDLE_OUTPUT = (
    "Do you want to continue? [Y/n]\n"
    "Get:1 http://archive.example.org stable/main amd64 foo 1.2 [4 kB]\n"
    "Unpacking foo (1.2) ...\n"
    "Done.\n"
)
CLEAN_OUTPUT = "Reading state information...\nInstallation complete.\n"


def unit_tests():
    print("  helper_unit_tests")
    user = load_module(USER_SERVER, "shell_user")
    system = load_module(SYSTEM_SERVER, "shell_system")

    live = user._live_prompt_line
    # A prompt at the tail is detected and returned verbatim (trimmed line).
    prompt_cases = [
        ("Continue? [Y/n] ", "Continue? [Y/n]"),
        ("Overwrite '/etc/config'? [y/N]", "Overwrite '/etc/config'? [y/N]"),
        ("Install these 3 packages? [Y/n/q]", "Install these 3 packages? [Y/n/q]"),
        ("Are you sure? (yes/no) ", "Are you sure? (yes/no)"),
        ("rm: remove regular file 'notes.txt'? ", "rm: remove regular file 'notes.txt'?"),
        ("[sudo] password for user: ", "[sudo] password for user:"),
    ]
    for tail, expected in prompt_cases:
        check(live(tail) == expected, f"_live_prompt_line detects {expected!r}")

    # A prompt shape MID-output, clean output, and empty output are NOT prompts.
    check(live(MIDDLE_OUTPUT) is None, "_live_prompt_line ignores a [Y/n] mid-output")
    check(live(CLEAN_OUTPUT) is None, "_live_prompt_line ignores clean output")
    check(live("") is None, "_live_prompt_line ignores empty output")

    # It reuses _detect_unanswered_prompt exactly — same shapes, same lines.
    for tail, _ in prompt_cases:
        report = user._detect_unanswered_prompt(tail)
        check(
            report is not None and live(tail) == report["prompt"],
            f"_live_prompt_line reuses _detect_unanswered_prompt for {tail.strip()!r}",
        )

    # _decode_elicit_reply: the outcome -> PTY mapping.
    decode = user._decode_elicit_reply
    check(
        decode({"result": {"action": "accept", "content": {"answer": "y"}}}) == ("accept", "y"),
        "_decode_elicit_reply: accept carries the answer string",
    )
    check(
        decode({"result": {"action": "accept", "content": {"answer": ""}}}) == ("accept", ""),
        "_decode_elicit_reply: accept with an empty answer is still accept (bare enter)",
    )
    check(
        decode({"result": {"action": "decline"}}) == ("decline", None),
        "_decode_elicit_reply: decline maps to decline",
    )
    check(
        decode({"result": {"action": "cancel"}}) == ("cancel", None),
        "_decode_elicit_reply: cancel maps to cancel",
    )
    # Every unsafe / malformed shape collapses to a decline.
    for label, reply in [
        ("None (stream closed)", None),
        ("a JSON-RPC error", {"error": {"code": -1, "message": "no"}}),
        ("accept with no content", {"result": {"action": "accept"}}),
        ("accept with a non-string answer", {"result": {"action": "accept", "content": {"answer": 5}}}),
        ("an unrecognized action", {"result": {"action": "explode"}}),
        ("a missing result", {"jsonrpc": "2.0"}),
    ]:
        check(decode(reply) == ("decline", None), f"_decode_elicit_reply: {label} -> decline")

    # Both servers ship identical helpers; require identical behavior.
    for tail, _ in prompt_cases:
        check(
            user._live_prompt_line(tail) == system._live_prompt_line(tail),
            f"both servers' _live_prompt_line agree on {tail.strip()!r}",
        )
    for reply in [
        {"result": {"action": "accept", "content": {"answer": "z"}}},
        {"result": {"action": "cancel"}},
        None,
    ]:
        check(
            user._decode_elicit_reply(reply) == system._decode_elicit_reply(reply),
            "both servers' _decode_elicit_reply agree",
        )


# ---------------------------------------------------------------------------
# Half 1b: the shared bodies stay byte-identical across both servers
# ---------------------------------------------------------------------------

# Every top-level def execute_interactive adds, plus the two functions
# execute_command already shared — all must be byte-identical in both files.
SHARED_DEFS = (
    "def _detect_unanswered_prompt(",
    "def _prompt_message(",
    "def _call_execute_command(",
    "def _call_execute_interactive(",
    "def _execute_interactive_fallback(",
    "def _execute_interactive_pty(",
    "def _interactive_outcome_result(",
    "def _interactive_result(",
    "def _interactive_idle_window(",
    "def _interactive_max_prompts(",
    "def _tail_text(",
    "def _live_prompt_line(",
    "def _blocked_read_prompt(",
    "def _decode_elicit_reply(",
    "def _send_message(",
    "def _read_elicit_reply(",
    "def _elicit(",
    "def _eof_pty(",
    "def _group_pids(",
    "def _pty_read_state(",
    "def _signal_group(",
    "def _pgid_of(",
    "def _kill_group(",
    "def _reap(",
    "def _drain_into(",
    "def _drain_after_eof(",
)


def extract_block(text, header):
    """Return the source of the top-level def whose signature starts with header."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(header))
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end] == ""):
        end += 1
    while end > start + 1 and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])


def identity_tests():
    print("  byte_identity")
    user_src = USER_SERVER.read_text()
    system_src = SYSTEM_SERVER.read_text()
    for header in SHARED_DEFS:
        u = extract_block(user_src, header)
        s = extract_block(system_src, header)
        check(u == s and u.strip() != "", f"{header[:-1]!r} body is byte-identical in both servers")


# ---------------------------------------------------------------------------
# Half 1c: _pty_read_state distinguishes a child blocked on a terminal read from
# one that is merely sleeping — the gate that stops a quiet, non-reading command
# being answered or killed, and lets an unanswerable read be EOF'd rather than
# hang forever with no timeout.
# ---------------------------------------------------------------------------

def _spawn_under_pty(script):
    master, slave = pty.openpty()
    pts = os.ttyname(slave)
    proc = subprocess.Popen(
        ["sh", "-c", script],
        stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, close_fds=True,
    )
    os.close(slave)
    return proc, master, pts


def _teardown(proc, master):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    try:
        os.close(master)
    except OSError:
        pass


def read_state_tests():
    print("  read_state")
    user = load_module(USER_SERVER, "shell_user_rs")
    rs = user._pty_read_state

    # Cannot introspect -> None (never a false 'not waiting').
    check(rs(None, None) is None, "_pty_read_state(None, None) is None")
    check(rs(123, "") is None, "_pty_read_state with no pts_path is None")

    if user._READ_SYSCALL_NR is None or not os.path.isdir("/proc"):
        msg = "no /proc or unknown arch — SKIPPING the blocked-on-read integration checks"
        print(f"    SKIP  {msg}")
        SKIPS.append(msg)
        return

    # A child actually parked in a terminal read is detected as blocked.
    proc, master, pts = _spawn_under_pty('printf "prompt: "; read a; echo "GOT=$a"')
    try:
        time.sleep(0.6)
        check(rs(os.getpgid(proc.pid), pts) is True, "a child blocked on read(pty) -> True")
    finally:
        _teardown(proc, master)

    # A child merely sleeping (not reading) is NOT blocked-on-read, so a quiet
    # tail on such a command is never answered or killed.
    proc, master, pts = _spawn_under_pty('printf "Have we converged yet?\\n"; sleep 5')
    try:
        time.sleep(0.6)
        check(rs(os.getpgid(proc.pid), pts) is False, "a sleeping (non-reading) child -> False")
    finally:
        _teardown(proc, master)


# ---------------------------------------------------------------------------
# Half 2: the real server through the REAL dmcp binary, end to end
# ---------------------------------------------------------------------------

def install_server(root: pathlib.Path, server_id: str, server_py: pathlib.Path) -> dict:
    """Populate a temp MCP_USER_INSTALL_DIR with a manifest + index.json pointing
    python3 at `server_py`, mirroring tests/session_broker.rs's install fixture,
    and return the dmcp env for this tree."""
    user_installed = root / "user" / "installed"
    server_dir = user_installed / server_id
    server_dir.mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir(parents=True, exist_ok=True)

    manifest = {
        "id": server_id,
        "name": server_id,
        "version": "0.1.0",
        "transports": [
            {"type": "stdio", "command": sys.executable, "args": [str(server_py)]}
        ],
        "installDir": str(server_dir),
    }
    (server_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    index = {"servers": {server_id: {"location": str(server_dir / "manifest.json"), "keywords": []}}}
    (user_installed / "index.json").write_text(json.dumps(index, indent=2))

    env = os.environ.copy()
    env.update(
        MCP_USER_INSTALL_DIR=str(user_installed),
        MCP_SYSTEM_INSTALL_DIR=str(root / "system" / "installed"),
        MCP_USER_SOURCES_PATH=str(root / "user" / "sources.list"),
        MCP_SYSTEM_SOURCES_PATH=str(root / "system" / "sources.list"),
        MCP_VECTOR_INDEX_DIR=str(root / "vector"),
        XDG_RUNTIME_DIR=str(root / "run"),
    )
    return env


def drive_interactive(env, server_id, args_obj, answer, deadline_s=30):
    """Spawn `dmcp call <id> execute_interactive --interactive` with piped stdio,
    answer each prompt with `answer`, and return (prompts, payload, is_error).

    Bounded by a wall-clock deadline so a hang fails loudly instead of wedging
    the whole self-test."""
    proc = subprocess.Popen(
        [str(DMCP_BIN), "call", server_id, "execute_interactive", "--interactive",
         "--args", json.dumps(args_obj)],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    prompts = []
    result = None
    deadline = time.monotonic() + deadline_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("dmcp did not return within the deadline")
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") == "prompt":
                prompts.append(msg)
                proc.stdin.write(json.dumps(answer) + "\n")
                proc.stdin.flush()
            elif msg.get("type") == "result":
                result = msg
                break
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if result is None:
        raise AssertionError(
            f"no result from dmcp; stderr: {proc.stderr.read() if proc.stderr else ''}"
        )
    payload = json.loads(result["content"])
    return prompts, payload, result.get("isError")


def drive_noninteractive(env, server_id, args_obj, deadline_s=30):
    """Run `dmcp call <id> execute_interactive` WITHOUT --interactive. dmcp prints
    the raw (pretty, multi-line) payload string, so read it whole and parse it."""
    proc = subprocess.Popen(
        [str(DMCP_BIN), "call", server_id, "execute_interactive",
         "--args", json.dumps(args_obj)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    out, _err = proc.communicate(timeout=deadline_s)
    return json.loads(out)


def e2e_tests():
    print("  e2e[real dmcp]")
    if not (DMCP_BIN.exists() and os.access(DMCP_BIN, os.X_OK)):
        msg = f"real dmcp binary not at {DMCP_BIN} — SKIPPING end-to-end half"
        print(f"    SKIP  {msg}")
        SKIPS.append(msg)
        return

    root = pathlib.Path(tempfile.mkdtemp(prefix="dmcp-interactive-it-"))
    try:
        env = install_server(root, "com.test.shell", USER_SERVER)

        # (a) accept carries the answer into a real `read a` in a real `sh`.
        prompts, payload, is_error = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'printf "Continue? [y/N] "; read a; echo "GOT=$a"']},
            {"action": "accept", "content": {"answer": "y"}},
        )
        check(len(prompts) == 1, "(a) exactly one prompt was raised")
        check(payload.get("stdout", "").find("GOT=y") != -1,
              "(a) the command RECEIVED the answer: GOT=y in stdout")
        check(payload.get("mode") == "interactive", "(a) mode is interactive")
        check(payload.get("prompts_answered") == 1, "(a) prompts_answered is 1")
        check(payload.get("success") is True and is_error is False, "(a) the call succeeded")

        # (b) a [Y/n]-style prompt is detected and surfaced verbatim.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'printf "Proceed with install? [Y/n] "; read a; echo "PICK=$a"']},
            {"action": "accept", "content": {"answer": "Y"}},
        )
        check(len(prompts) == 1 and "[Y/n]" in prompts[0].get("message", ""),
              "(b) the [Y/n] prompt was detected and relayed verbatim")
        check(prompts and prompts[0].get("server") == "com.test.shell",
              "(b) the prompt is attributed to the server that asked")
        check("PICK=Y" in payload.get("stdout", ""), "(b) the answer reached the command: PICK=Y")

        # (c) decline aborts the command legibly — no answer reaches it.
        prompts, payload, is_error = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c",
             'printf "Answer? [y/N] "; if read a; then echo "GOT=$a"; else echo NO_ANSWER; exit 5; fi']},
            {"action": "decline"},
        )
        check(len(prompts) == 1, "(c) the prompt was raised before the decline")
        check("NO_ANSWER" in payload.get("stdout", ""), "(c) the command saw EOF and aborted (NO_ANSWER)")
        check("GOT=" not in payload.get("stdout", ""), "(c) NO answer was injected into the command")
        check(payload.get("outcome") == "declined", "(c) outcome is 'declined'")
        check(payload.get("success") is False and is_error is True, "(c) the aborted command reports failure")

        # (d) a clean command with NO prompt returns normally through the tool.
        prompts, payload, is_error = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'echo hello-from-interactive; exit 0']},
            {"action": "accept", "content": {"answer": "unused"}},
        )
        check(prompts == [], "(d) a clean command raises no prompt")
        check("hello-from-interactive" in payload.get("stdout", ""), "(d) clean output is returned")
        check(payload.get("success") is True and is_error is False, "(d) the clean command succeeds")
        check(payload.get("prompts_answered") == 0, "(d) prompts_answered is 0")

        # (f) without --interactive, execute_interactive falls back safely: closed
        # stdin, a needs_input report, mode non-interactive — never a hang.
        payload = drive_noninteractive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'printf "Continue? [y/N] "; read a || exit 1']},
        )
        check(payload.get("mode") == "non-interactive", "(f) no elicitation -> mode non-interactive")
        check(isinstance(payload.get("needs_input"), dict), "(f) fallback carries a needs_input report")
        check(payload.get("prompts_answered") == 0, "(f) fallback answers no prompts")

        # (g) A visible prompt with no [Y/n] shape — fdisk's "Command (m for
        # help): ", a REPL banner, a numbered menu — is ELICITED, not aborted:
        # once the child is provably blocked in read(2), its printed line IS the
        # question, so the operation can be answered instead of merely not
        # hanging. The answer reaches the command.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c",
             'printf "Command (m for help): "; read a; echo "GOT=$a"']},
            {"action": "accept", "content": {"answer": "p"}},
            deadline_s=20,
        )
        check(len(prompts) == 1, "(g) a colon-menu prompt is elicited once")
        check(
            any("Command (m for help)" in (pr.get("message") or "") for pr in prompts),
            "(g) the elicitation relays the command's own prompt line",
        )
        check("GOT=p" in payload.get("stdout", ""), "(g) the answer reached the command")
        check(payload.get("outcome") == "completed", "(g) outcome is 'completed'")

        # (g2) A bare read that prints NO prompt has nothing to show, so it still
        # EOFs (no-prompt) rather than raising an empty question — the remaining
        # safe-abort path when the child is blocked in read with no visible line.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c",
             'if read a; then echo "GOT=$a"; else echo NO_INPUT; fi']},
            {"action": "accept", "content": {"answer": "x"}},
            deadline_s=20,
        )
        check(prompts == [], "(g2) a bare read with no printed prompt raises no elicitation")
        check("NO_INPUT" in payload.get("stdout", ""), "(g2) the bare read saw EOF (never hung)")
        check(payload.get("outcome") == "no-prompt", "(g2) outcome is 'no-prompt'")

        # (h) Finding: a quiet computation whose last line merely looks like a
        # prompt, while the command is NOT reading, must run to completion — not
        # be answered, and not be killed by a decline.
        prompts, payload, is_error = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c",
             'echo "Have we converged yet?"; sleep 2; echo RESULT_OK']},
            {"action": "accept", "content": {"answer": "y"}},
            deadline_s=20,
        )
        check(prompts == [], "(h) a non-reading command raises no elicitation")
        check("RESULT_OK" in payload.get("stdout", ""), "(h) the command completed (output not lost)")
        check(payload.get("success") is True and is_error is False, "(h) it was not killed")

        # (i) Finding: the injected answer must land on the prompt that triggered
        # it. A '?'-ending status line printed while the command is NOT reading
        # must not be answered — only the later, real prompt is.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c",
             'echo "Reticulating splines 50%?"; sleep 1; '
             'printf "Delete ALL files? [y/N] "; read a; echo "DECISION=$a"']},
            {"action": "accept", "content": {"answer": "n"}},
            deadline_s=20,
        )
        check(len(prompts) == 1, "(i) exactly one prompt — the status line was not answered")
        check(prompts and "Delete ALL files?" in prompts[0].get("message", ""),
              "(i) the prompt raised is the real one, not the status line")
        check("DECISION=n" in payload.get("stdout", ""), "(i) the answer reached the triggering prompt")

        # (j) Finding: after an accepted answer whose TEXT is prompt-shaped, the
        # terminal echo of it must not be re-detected as a fresh prompt.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'printf "Go? [y/N] "; read a; sleep 1; echo "final=$a"']},
            {"action": "accept", "content": {"answer": "huh?"}},
            deadline_s=20,
        )
        check(len(prompts) == 1, "(j) the echoed prompt-shaped answer raised no second prompt")
        check("final=huh?" in payload.get("stdout", ""), "(j) the answer reached the command")
        check(payload.get("prompts_answered") == 1, "(j) prompts_answered is 1, not inflated by the echo")

        # (k) Finding: a silent second read after a prompt-shaped answer must not
        # be surfaced with the prior echoed answer as its message. There is no
        # prompt to show, so it is EOF'd (no-prompt), not elicited.
        prompts, payload, _ = drive_interactive(
            env, "com.test.shell",
            {"command": "sh", "args": ["-c", 'printf "One? [y/N] "; read a; read b; echo "a=$a b=$b"']},
            {"action": "accept", "content": {"answer": "pick [y/N]"}},
            deadline_s=20,
        )
        check(len(prompts) == 1, "(k) the silent second read raised no spurious prompt")
        check(prompts and "One?" in prompts[0].get("message", ""),
              "(k) the one prompt is the real first prompt, not the echo")
        check("a=pick [y/N] b=" in payload.get("stdout", ""),
              "(k) the first answer landed on read a; read b saw EOF")
        check(payload.get("outcome") == "no-prompt", "(k) the promptless second read ended as no-prompt")

        # Both server.py files must actually work through dmcp (they are
        # byte-identical, so the system one answers the same way).
        env2 = install_server(root, "com.test.shell.system", SYSTEM_SERVER)
        _, payload, is_error = drive_interactive(
            env2, "com.test.shell.system",
            {"command": "sh", "args": ["-c", 'printf "Value? [y/N] "; read a; echo "SYS_GOT=$a"']},
            {"action": "accept", "content": {"answer": "42"}},
        )
        check("SYS_GOT=42" in payload.get("stdout", ""),
              "(a') the jarvis-shell-system server also carries the answer through dmcp")
        check(is_error is False, "(a') the system server call succeeds")
    finally:
        shutil.rmtree(root, ignore_errors=True)



def context_tests():
    """The question carries the block it belongs to, bounded by characters."""
    print("  prompt_context")
    user = load_module(USER_SERVER, "shell_user_ctx")
    system = load_module(SYSTEM_SERVER, "shell_system_ctx")
    msg = user._prompt_message

    agreement = (
        b"=== LICENSE ===\r\n"
        b"1. You grant access to /home.\r\n"
        b"2. Data is retained for 7 years.\r\n"
        b"Do you accept? [yes/no] "
    )
    out = msg(bytearray(agreement))
    check("1. You grant access to /home." in out, "the terms travel with the question")
    check("2. Data is retained for 7 years." in out, "every term travels, not just the last")
    check(out.splitlines()[-1] == "Do you accept? [yes/no]", "the prompt line stays last")
    check("\r" not in out, "pty carriage returns are normalized away")

    # Control sequences are stripped: this text is rendered into a human's UI and
    # is the command's own untrusted output, so cursor/colour codes are a
    # spoofing surface rather than information.
    ansi = bytearray(b"\x1b[2J\x1b[31mDANGER\x1b[0m\r\nProceed? [y/N] ")
    out = msg(ansi)
    check("\x1b" not in out, "escape sequences are stripped from the question")
    check("DANGER" in out, "the words survive the stripping")

    # Characters govern, not lines: the cap is what the reader must read.
    big = bytearray(("x" * 100 + "\n").encode() * 200 + b"Accept? [y/N] ")
    out = msg(big)
    check(len(out) <= 4200, "the default window is bounded by characters")
    check("truncated" in out, "a truncated window says so")
    wide = msg(big, 20000)
    check(len(wide) > len(out), "an explicit budget widens the window")

    check(msg(bytearray()) == "", "an empty buffer yields no question")
    for fixture in (agreement, ansi, b"Accept? [y/N] "):
        check(
            user._prompt_message(bytearray(fixture))
            == system._prompt_message(bytearray(fixture)),
            "both servers build the same question",
        )


def escalation_tests():
    """A reader that cannot decide can ask for more instead of guessing."""
    print("  need_more_context")
    user = load_module(USER_SERVER, "shell_user_esc")
    decode = user._decode_elicit_reply

    reply = {"result": {"action": "accept", "content": {"need_more_context": 8000}}}
    check(decode(reply) == ("more", 8000), "a request for more context is its own outcome")

    # A reader that fills both must not be read as having answered: it said it
    # could not decide yet.
    both = {"result": {"action": "accept", "content": {"answer": "yes", "need_more_context": 500}}}
    check(decode(both)[0] == "more", "asking for more wins over a half-formed answer")

    # Only a positive integer means it; a bool is not a count.
    for bad in (0, -5, True, "20", None):
        r = {"result": {"action": "accept", "content": {"answer": "y", "need_more_context": bad}}}
        check(decode(r) == ("accept", "y"), f"need_more_context={bad!r} is not a request")
    check(
        decode({"result": {"action": "accept", "content": {"answer": "y"}}}) == ("accept", "y"),
        "an ordinary answer is unaffected",
    )


def main():
    unit_tests()
    context_tests()
    escalation_tests()
    identity_tests()
    read_state_tests()
    e2e_tests()
    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} execute_interactive self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    suffix = f" ({len(SKIPS)} skip)" if SKIPS else ""
    print(f"\nOK: execute_interactive self-test passed{suffix}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
