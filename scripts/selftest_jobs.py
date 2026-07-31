#!/usr/bin/env python3
"""selftest_jobs.py — prove the jarvis-shell job model end to end.

Issue #66: execute_command runs one synchronous child with a closed stdin, so a
genuinely interactive command (a wizard, a REPL, an installer with no -y) can
never be answered there. run_job gives such commands a second path: a named job
under a real PTY whose detached holder outlives any single tool call — every
tool call spawns a fresh server process (one-shot dmcp lifecycle), so the job's
handle lives on the filesystem — with send_input / read_output / kill_job
addressing the job from later calls.

This self-test drives servers/jarvis-shell/server.py over REAL stdio JSON-RPC
(initialize, tools/list, tools/call). No mocks: a real PTY, a real prompting
command, real Unix sockets, real signals.

  1. run_job of a genuinely prompting command BLOCKS; a SECOND, separate
     server process delivers send_input; run_job then returns a transcript
     carrying both the prompt and the post-answer output.
  2. While run_job is blocked, the first server's stderr already carries the
     prompt text — the live stream a dispatch REMIND tail would surface.
  3. read_output works mid-run and after exit; exit codes propagate; kill_job
     tears down a sleeping command's whole process group.
  4. Job names that are not a clean path component are rejected; a duplicate
     live job is refused; send_input to an unknown or finished job errors
     legibly; run_job's timeout kills and reports.
  5. The startup sweep removes crashed-holder residue and >24h-old finished
     jobs, and leaves young or live job dirs alone.
  6. The two server.py files keep their on-main relationship: the system
     server is the user server minus the open_app feature (plus its own
     serverInfo name line), and every job-model block is byte-identical in
     both.

Offline, stdlib only.

Usage:
  python3 scripts/selftest_jobs.py
"""
import difflib
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
USER_SERVER = REPO / "servers" / "jarvis-shell" / "server.py"
SYSTEM_SERVER = REPO / "servers" / "jarvis-shell-system" / "server.py"

FAILURES = []


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(description)


# ---------------------------------------------------------------------------
# A real server process on real pipes
# ---------------------------------------------------------------------------

class Server:
    """One server.py process; JSON-RPC over stdin/stdout, stderr accumulated."""

    def __init__(self, env, server_path=USER_SERVER):
        self.proc = subprocess.Popen(
            [sys.executable, str(server_path)],
            cwd=str(server_path.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.responses = {}
        self.bad_stdout = []
        self.stderr_buf = bytearray()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.bad_stdout.append(line)
                continue
            self.responses[msg.get("id")] = msg

    def _pump_stderr(self):
        fd = self.proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.stderr_buf.extend(chunk)

    def stderr_text(self):
        return bytes(self.stderr_buf).decode(errors="replace")

    def send(self, req_id, method, params):
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        self.proc.stdin.flush()

    def send_call(self, req_id, name, arguments):
        self.send(req_id, "tools/call", {"name": name, "arguments": arguments})

    def wait(self, req_id, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if req_id in self.responses:
                return self.responses[req_id]
            time.sleep(0.05)
        raise TimeoutError(f"no response for id {req_id} within {timeout}s")

    def call(self, req_id, name, arguments, timeout=20):
        """tools/call and wait; returns (isError, payload dict)."""
        self.send_call(req_id, name, arguments)
        msg = self.wait(req_id, timeout)
        result = msg["result"]
        return result.get("isError", False), json.loads(result["content"][0]["text"])

    def initialize(self):
        self.send(0, "initialize", {})
        return self.wait(0)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        check(not self.bad_stdout, "server stdout carried only JSON-RPC lines")


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# 1+2+3a: a prompting command, answered from a second server process
# ---------------------------------------------------------------------------

def interactive_tests(env, root):
    print("  interactive_round_trip")
    prompt_cmd = (
        shlex.quote(sys.executable)
        + """ -c 'name = input("What is your name? "); print("Hello, " + name)'"""
    )

    first = Server(env)
    first.initialize()
    first.send_call(2, "run_job", {"command": prompt_cmd, "job": "greet"})

    seen = wait_until(lambda: b"What is your name?" in bytes(first.stderr_buf))
    check(seen, "prompt text appears LIVE on the blocked server's stderr")
    check(2 not in first.responses, "run_job is still blocked while the prompt is up")

    second = Server(env)
    second.initialize()
    is_err, payload = second.call(1, "read_output", {"job": "greet"})
    check(not is_err and payload.get("state") == "running", "read_output mid-run reports state 'running'")
    check("What is your name?" in payload.get("output", ""), "read_output mid-run carries the prompt")
    check("exit_code" not in payload, "read_output mid-run carries no exit code yet")

    is_err, payload = second.call(2, "send_input", {"job": "greet", "text": "Alice\n"})
    check(not is_err and payload.get("success") is True, "send_input from a SECOND server process succeeds")
    check(payload.get("delivered_bytes") == 6, "send_input confirms the delivered byte count")
    second.close()

    msg = first.wait(2)
    payload = json.loads(msg["result"]["content"][0]["text"])
    check(payload.get("success") is True, "run_job returns success once the job exits")
    check(payload.get("exit_code") == 0, "run_job reports exit code 0")
    check(payload.get("job") == "greet", "run_job reports the job name")
    transcript = payload.get("transcript", "")
    check("What is your name?" in transcript, "transcript carries the prompt")
    check("Hello, Alice" in transcript, "transcript carries the post-answer output")
    check("Hello, Alice" in first.stderr_text(), "stderr stream carried the post-answer output too")
    first.close()

    print("  after_exit")
    third = Server(env)
    third.initialize()
    is_err, payload = third.call(1, "read_output", {"job": "greet"})
    check(not is_err and payload.get("state") == "exited", "read_output after exit reports state 'exited'")
    check(payload.get("exit_code") == 0, "read_output after exit carries the exit code")
    check("Hello, Alice" in payload.get("output", ""), "read_output after exit carries the full output")

    is_err, payload = third.call(2, "send_input", {"job": "greet", "text": "again\n"})
    check(is_err, "send_input to a finished job is an error")
    check("already exited" in payload.get("error", ""), "finished-job error says the job already exited")
    third.close()


# ---------------------------------------------------------------------------
# 3b: exit codes propagate; a finished name can be reused
# ---------------------------------------------------------------------------

def exit_code_tests(env):
    print("  exit_codes")
    srv = Server(env)
    srv.initialize()
    is_err, payload = srv.call(1, "run_job", {"command": "echo out; exit 7", "job": "rc"})
    check(is_err, "a non-zero job is reported as an error result")
    check(payload.get("exit_code") == 7, "the job's exit code propagates verbatim")
    check("out" in payload.get("transcript", ""), "the failing job's transcript is still returned")

    is_err, payload = srv.call(2, "run_job", {"command": "exit 0", "job": "rc"})
    check(not is_err and payload.get("exit_code") == 0, "a finished job's name can be reused")
    srv.close()


# ---------------------------------------------------------------------------
# 3c + 4a: duplicate live job refused; kill_job takes the whole process group
# ---------------------------------------------------------------------------

def kill_tests(env, root):
    print("  kill_and_duplicates")
    first = Server(env)
    first.initialize()
    # 'sleep 300; echo woke' keeps sh alive as the group leader with sleep as a
    # child in the same group — the group kill must take both.
    first.send_call(2, "run_job", {"command": "sleep 300; echo woke", "job": "snooze"})
    child_pid_file = pathlib.Path(root) / "jarvis-shell" / "snooze" / "child.pid"
    check(wait_until(child_pid_file.exists), "the job's process-group id is on disk")
    pgid = int(child_pid_file.read_text())

    def group_alive():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True

    check(group_alive(), "the sleeping command's process group is alive")

    second = Server(env)
    second.initialize()
    is_err, payload = second.call(1, "run_job", {"command": "echo dup", "job": "snooze"})
    check(is_err, "run_job refuses a job name with a live holder")
    check("still running" in payload.get("error", ""), "duplicate-job error names the live job")

    is_err, payload = second.call(2, "kill_job", {"job": "snooze"})
    check(not is_err and payload.get("state") == "exited", "kill_job reports the job exited")
    check("SIGTERM" in payload.get("result", ""), "kill_job reports the signal that did it")

    msg = first.wait(2)
    run_payload = json.loads(msg["result"]["content"][0]["text"])
    check(run_payload.get("success") is False, "the killed run_job returns failure")
    check(run_payload.get("exit_code") == -signal.SIGTERM, "the killed run_job reports death by SIGTERM")
    check(wait_until(lambda: not group_alive(), timeout=5), "the WHOLE process group is gone after kill_job")

    is_err, payload = second.call(3, "kill_job", {"job": "snooze"})
    check(not is_err and "nothing to kill" in payload.get("result", ""), "kill_job on a finished job is a legible no-op")
    is_err, payload = second.call(4, "kill_job", {"job": "no-such-job"})
    check(is_err and "Unknown job" in payload.get("error", ""), "kill_job on an unknown job errors legibly")
    second.close()
    first.close()


# ---------------------------------------------------------------------------
# 4b: name validation, unknown jobs, timeout
# ---------------------------------------------------------------------------

def validation_tests(env):
    print("  validation")
    srv = Server(env)
    srv.initialize()
    bad_names = ["../evil", "a/b", "..", ".", "", "a b", "a" * 65, "jäb"]
    for i, name in enumerate(bad_names, start=1):
        is_err, payload = srv.call(i, "run_job", {"command": "echo x", "job": name})
        check(
            is_err and "Invalid job name" in payload.get("error", ""),
            f"run_job rejects job name {name!r}",
        )
    is_err, payload = srv.call(20, "send_input", {"job": "../evil", "text": "x\n"})
    check(is_err and "Invalid job name" in payload.get("error", ""), "send_input rejects a traversal job name")
    is_err, payload = srv.call(21, "send_input", {"job": "ghost", "text": "x\n"})
    check(is_err and "Unknown job" in payload.get("error", ""), "send_input to an unknown job errors legibly")
    is_err, payload = srv.call(22, "send_input", {"job": "rc", "text": ""})
    check(is_err and "non-empty" in payload.get("error", ""), "send_input requires non-empty text")
    is_err, payload = srv.call(23, "read_output", {"job": "ghost"})
    check(is_err and "Unknown job" in payload.get("error", ""), "read_output on an unknown job errors legibly")

    is_err, payload = srv.call(24, "run_job", {"command": "sleep 60", "job": "slowpoke", "timeout": 1.5}, timeout=30)
    check(is_err and payload.get("success") is False, "a timed-out job is reported as failure")
    check(payload.get("exit_code") == -1, "timeout reports exit_code -1, matching execute_command")
    check("timed out" in payload.get("error", ""), "timeout error says the job timed out")
    srv.close()


# ---------------------------------------------------------------------------
# 5: the startup sweep
# ---------------------------------------------------------------------------

def sweep_tests(env, root):
    print("  stale_sweep")
    jobs_root = pathlib.Path(root) / "jarvis-shell"
    jobs_root.mkdir(mode=0o700, exist_ok=True)

    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait()
    dead_pid = probe.pid

    old = time.time() - 3600
    ancient = time.time() - 25 * 3600

    crashed = jobs_root / "swp_crashed"
    crashed.mkdir(mode=0o700)
    (crashed / "holder.pid").write_text(str(dead_pid))
    (crashed / "out.log").write_text("half-done\n")
    os.utime(crashed, (old, old))

    finished_old = jobs_root / "swp_finished_old"
    finished_old.mkdir(mode=0o700)
    (finished_old / "status").write_text("0")
    os.utime(finished_old / "status", (ancient, ancient))

    finished_new = jobs_root / "swp_finished_new"
    finished_new.mkdir(mode=0o700)
    (finished_new / "status").write_text("0")

    infant = jobs_root / "swp_infant"
    infant.mkdir(mode=0o700)

    # The sweep runs before the server reads its first request, so once
    # initialize has answered, the sweep is already done.
    srv = Server(env)
    srv.initialize()
    check(not crashed.exists(), "sweep removes a dead-holder dir with no status")
    check(not finished_old.exists(), "sweep removes a finished dir older than 24h")
    check(finished_new.exists(), "sweep keeps a recently finished dir")
    check(infant.exists(), "sweep keeps a young dir (grace for a job being born)")
    srv.close()
    shutil.rmtree(finished_new, ignore_errors=True)
    shutil.rmtree(infant, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6: the two server files keep their relationship
# ---------------------------------------------------------------------------

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


def extract_tool_entry(text, name):
    """Return the TOOLS list element (a 4-space-indented dict) for one tool."""
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == f'"name": "{name}",')
    start = idx
    while lines[start] != "    {":
        start -= 1
    end = idx
    while lines[end] != "    },":
        end += 1
    return "\n".join(lines[start : end + 1])


JOB_FUNCTION_HEADERS = [
    "def _jobs_unsupported(",
    "def _jobs_root(",
    "def _ensure_jobs_root(",
    "def _job_file(",
    "def _read_int(",
    "def _write_file(",
    "def _pid_alive(",
    "def _job_state(",
    "def _read_job_log(",
    "def _holder_relay(",
    "def _holder_main(",
    "def _spawn_holder(",
    "def _stream_job_output(",
    "def _job_name_error(",
    "def _job_response(",
    "def _job_error(",
    "def _group_alive(",
    "def _terminate_job(",
    "def _call_run_job(",
    "def _call_send_input(",
    "def _call_read_output(",
    "def _call_kill_job(",
    "def _sweep_stale_jobs(",
    "def main(",
]

JOB_TOOLS = ["run_job", "send_input", "read_output", "kill_job"]


def identity_tests(env):
    print("  server_identity")
    user_src = USER_SERVER.read_text()
    system_src = SYSTEM_SERVER.read_text()

    for header in JOB_FUNCTION_HEADERS:
        check(
            extract_block(user_src, header) == extract_block(system_src, header),
            f"{header[:-1]!r} body is byte-identical in both servers",
        )
    for tool in JOB_TOOLS:
        check(
            extract_tool_entry(user_src, tool) == extract_tool_entry(system_src, tool),
            f"TOOLS entry for {tool!r} is byte-identical in both servers",
        )

    # On main the system server is the user server minus the open_app feature:
    # user-only deletions are that feature, the single replaced line is the
    # serverInfo name, and the system file must add nothing of its own — so an
    # edit to shared logic in only one file cannot land.
    matcher = difflib.SequenceMatcher(
        a=user_src.splitlines(), b=system_src.splitlines(), autojunk=False
    )
    inserts = []
    replaces = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            inserts.append((j1, j2))
        elif tag == "replace":
            replaces.append((i1, i2, j1, j2))
    check(not inserts, "the system server adds no lines the user server lacks")
    replace_is_server_info = (
        len(replaces) == 1
        and replaces[0][1] - replaces[0][0] == 1
        and replaces[0][3] - replaces[0][2] == 1
        and '"serverInfo"' in user_src.splitlines()[replaces[0][0]]
    )
    check(replace_is_server_info, "the only divergent line is the serverInfo name")

    print("  tools_list")
    user_tools = {}
    system_tools = {}
    for path, into in ((USER_SERVER, user_tools), (SYSTEM_SERVER, system_tools)):
        srv = Server(env, server_path=path)
        srv.initialize()
        srv.send(1, "tools/list", {})
        for tool in srv.wait(1)["result"]["tools"]:
            into[tool["name"]] = tool
        srv.close()
    check(
        set(user_tools) - set(system_tools) == {"open_app"},
        "tools/list differs only by open_app",
    )
    for tool in JOB_TOOLS:
        check(tool in user_tools and tool in system_tools, f"both servers expose {tool}")
        if tool in user_tools and tool in system_tools:
            check(user_tools[tool] == system_tools[tool], f"{tool} schema agrees across servers")
    for tool in JOB_TOOLS:
        desc = user_tools.get(tool, {}).get("description", "")
        check(bool(desc), f"{tool} has a description for the LLM")
    run_desc = user_tools.get("run_job", {}).get("description", "")
    check("send_input" in run_desc, "run_job description spells out the send_input loop")
    check("never guess" in run_desc, "run_job description forbids guessing input")


# ---------------------------------------------------------------------------

def cleanup_jobs(root):
    """Best-effort teardown of any holder a failed test left running."""
    jobs_root = pathlib.Path(root) / "jarvis-shell"
    if not jobs_root.is_dir():
        return
    for job_dir in jobs_root.iterdir():
        for pid_file in ("child.pid", "holder.pid"):
            try:
                pid = int((job_dir / pid_file).read_text())
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass


def main():
    if sys.platform not in ("linux", "darwin"):
        print("SKIP: the job model self-test needs a Unix host (PTY + AF_UNIX).")
        return 0

    root = tempfile.mkdtemp(prefix="jarvis-shell-selftest-")
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = root
    try:
        interactive_tests(env, root)
        exit_code_tests(env)
        kill_tests(env, root)
        validation_tests(env)
        sweep_tests(env, root)
        identity_tests(env)
    finally:
        cleanup_jobs(root)
        shutil.rmtree(root, ignore_errors=True)

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} job-model self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: job-model self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
