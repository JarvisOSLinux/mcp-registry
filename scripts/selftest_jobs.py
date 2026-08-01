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
  4. Job names that are not a clean path component are rejected — including
     a newline-suffixed name, which a $-anchored regex would admit; a
     duplicate live job is refused; send_input to an unknown or finished job
     errors legibly; run_job's timeout kills and reports.
  5. The startup sweep removes crashed-holder residue and >24h-old finished
     jobs, and leaves young or live job dirs alone. Before a crashed job's
     dir is removed — by the sweep or by run_job name reuse — its
     still-running process group is signalled: child.pid in that dir was the
     only handle left to it.
  6. A holder that comes up wedged — alive but never signalling ready —
     fails run_job startup within the ready timeout and is killed, instead
     of blocking the call forever on the ready pipe.
  7. The two server.py files keep their on-main relationship: the system
     server is the user server minus the open_app feature (plus its own
     serverInfo name line), and every job-model block is byte-identical in
     both.
  8. Both manifests declare run_job's reminder need (issue #68): `blocking`
     and `suggestedRemindAfter` on the run_job tool entry only, agreeing
     across the two manifests, with the tool descriptions telling a caller to
     set remind_after even if it never reads the fields.
  9. Output is sterilized at read time and bounded (issue #69): a carriage-
     return progress bar renders as its final visible state, ANSI and control
     noise is gone, runs of identical lines fold into a count — while out.log
     on disk stays byte-exact, so nothing is destroyed. read_output's
     tail/offset window clamps instead of erroring, run_job's transcript is
     capped with a truncation marker naming the read_output call that
     fetches the omitted head, and the live stderr stream gets the same
     treatment (it is the same flood one hop later).

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
USER_MANIFEST = REPO / "servers" / "jarvis-shell" / "manifest.json"
SYSTEM_MANIFEST = REPO / "servers" / "jarvis-shell-system" / "manifest.json"

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
    bad_names = ["../evil", "a/b", "..", ".", "", "a b", "a" * 65, "jäb", "evil\n", "..\n"]
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
# 5b: a crashed holder's process group is signalled before its dir is removed
# ---------------------------------------------------------------------------

def crashed_holder_tests(env, root):
    print("  crashed_holder_release")
    jobs_root = pathlib.Path(root) / "jarvis-shell"

    def group_alive(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True

    def start_and_crash(server, job):
        """run_job a sleeper, SIGKILL its holder, return (job_dir, pgid).

        The sleeper ignores HUP: a dead holder closes the PTY master, whose
        hangup kills a well-behaved command — the runaway case is precisely a
        command that survives it, like anything daemonizing.
        """
        server.send_call(2, "run_job", {"command": "trap '' HUP; sleep 300", "job": job})
        job_dir = jobs_root / job
        check(wait_until((job_dir / "child.pid").exists), f"job '{job}' recorded its process group")
        pgid = int((job_dir / "child.pid").read_text())
        os.kill(int((job_dir / "holder.pid").read_text()), signal.SIGKILL)
        msg = server.wait(2)
        payload = json.loads(msg["result"]["content"][0]["text"])
        check(payload.get("success") is False, f"run_job of '{job}' reports the crashed holder")
        check("holder died" in payload.get("error", ""), f"crashed-holder error for '{job}' is legible")
        check(group_alive(pgid), f"the command of '{job}' outlives its dead holder")
        return job_dir, pgid

    first = Server(env)
    first.initialize()
    job_dir, pgid = start_and_crash(first, "orphan")
    first.close()
    # Backdate past the sweep grace so a fresh server start sweeps the dir.
    old = time.time() - 3600
    os.utime(job_dir, (old, old))
    sweeper = Server(env)
    sweeper.initialize()
    check(not job_dir.exists(), "sweep removes the crashed job's dir")
    check(
        wait_until(lambda: not group_alive(pgid), timeout=5),
        "sweep kills the crashed job's process group before dropping child.pid",
    )
    sweeper.close()

    second = Server(env)
    second.initialize()
    job_dir, pgid = start_and_crash(second, "reuse")
    is_err, payload = second.call(3, "run_job", {"command": "echo fresh", "job": "reuse"})
    check(not is_err and payload.get("exit_code") == 0, "a crashed job's name can be reused")
    check("fresh" in payload.get("transcript", ""), "the reused name runs the new command")
    check(
        wait_until(lambda: not group_alive(pgid), timeout=5),
        "name reuse kills the crashed job's old process group before dropping child.pid",
    )
    second.close()


# ---------------------------------------------------------------------------
# 6: a wedged holder cannot block run_job startup forever
# ---------------------------------------------------------------------------

def spawn_timeout_tests(root):
    print("  spawn_holder_timeout")
    import importlib.util

    spec = importlib.util.spec_from_file_location("jarvis_shell_under_test", USER_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    job_dir = pathlib.Path(root) / "jarvis-shell" / "wedged"
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def wedged_holder(holder_dir, command, ready_fd):
        # Alive but wedged mid-setup: records its pid (real startup order),
        # then hangs without ever touching the ready pipe.
        mod._write_file(mod._job_file(holder_dir, mod._JOB_HOLDER_PID), str(os.getpid()))
        time.sleep(30)

    mod._holder_main = wedged_holder
    mod._JOB_HOLDER_READY_TIMEOUT_SECS = 1

    started = time.monotonic()
    err_text = None
    try:
        mod._spawn_holder(str(job_dir), "true")
    except RuntimeError as exc:
        err_text = str(exc)
    elapsed = time.monotonic() - started

    check(err_text is not None, "a holder that never signals ready fails startup")
    check(elapsed < 5, "the ready timeout is honored instead of a blocking read")
    check("did not become ready" in (err_text or ""), "the startup error is legible")
    holder_pid = int((job_dir / "holder.pid").read_text())
    check(
        wait_until(lambda: not mod._pid_alive(holder_pid), timeout=5),
        "the wedged holder is killed, not left running",
    )
    shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7: the two server files keep their relationship
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
    "class _TerminalLines:",
    "def _render_lines(",
    "def _collapse_repeats(",
    "def _sterilize(",
    "def _output_window(",
    "def _optional_count(",
    "def _job_transcript(",
    "class _LiveStream:",
    "def _holder_relay(",
    "def _holder_main(",
    "def _spawn_holder(",
    "def _stream_job_output(",
    "def _job_name_error(",
    "def _job_response(",
    "def _job_error(",
    "def _group_alive(",
    "def _terminate_job(",
    "def _release_crashed_job(",
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
    check("remind_after" in run_desc, "run_job description tells the caller to set remind_after")


# ---------------------------------------------------------------------------
# 8: the manifests declare run_job's reminder need
# ---------------------------------------------------------------------------

def manifest_blocking_tests():
    """run_job carries `blocking` + `suggestedRemindAfter` in both manifests.

    run_job parks for as long as the command waits on input, so a task
    dispatched without a reminder never reports that a prompt is up. The two
    manifest keys are how the tool says that to an orchestrator that reads
    manifests; the description says it to a model that does not.
    """
    print("  manifest_blocking_fields")
    manifests = {
        "jarvis-shell": json.loads(USER_MANIFEST.read_text()),
        "jarvis-shell-system": json.loads(SYSTEM_MANIFEST.read_text()),
    }
    values = {}
    for tag, doc in manifests.items():
        tools = {t["name"]: t for t in doc.get("tools", [])}
        run_job = tools.get("run_job", {})
        check(run_job.get("blocking") is True, f"{tag}: run_job declares blocking: true")
        interval = run_job.get("suggestedRemindAfter")
        check(
            isinstance(interval, int) and not isinstance(interval, bool) and interval > 0,
            f"{tag}: run_job's suggestedRemindAfter is a positive integer",
        )
        values[tag] = (run_job.get("blocking"), interval)
        check(
            "remind_after" in run_job.get("description", ""),
            f"{tag}: run_job's manifest description tells the caller to set remind_after",
        )
        # Only run_job blocks. send_input / read_output / kill_job all return
        # at once, and marking them would have an orchestrator schedule
        # reminders for calls that were never going to wait.
        for name, tool in tools.items():
            if name == "run_job":
                continue
            check(
                "blocking" not in tool,
                f"{tag}: {name} does not claim to block",
            )
    check(
        values["jarvis-shell"] == values["jarvis-shell-system"],
        "both manifests declare the same blocking / suggestedRemindAfter values",
    )


# ---------------------------------------------------------------------------
# 9a: sterilization, as a pure function on hand-built terminal output
# ---------------------------------------------------------------------------

# Each case is (label, raw bytes a command wrote to its PTY, what a terminal
# would have shown). The overwrite cases are the load-bearing ones: a bare
# carriage return moves the cursor and nothing more, so a short redraw leaves
# the tail of a longer line visible — which is exactly what a terminal shows —
# and only an explicit erase wipes it.
STERILIZE_CASES = [
    ("CRLF normalizes to LF", "a\r\nb\r\n", "a\nb\n"),
    ("a redraw renders as its final visible state", "10%\r20%\r100% done\n", "100% done\n"),
    (
        "a SHORT redraw does not erase the longer line under it",
        "downloading big-file.tar\rdone\n",
        "doneloading big-file.tar\n",
    ),
    (
        "...unless the program clears to end of line",
        "downloading big-file.tar\rdone\x1b[K\n",
        "done\n",
    ),
    ("erase-whole-line drops what came before it", "junk\x1b[2K\rfresh\n", "fresh\n"),
    ("SGR colour is stripped", "\x1b[1;32mOK\x1b[0m\n", "OK\n"),
    ("an OSC title sequence is stripped", "\x1b]0;my title\x07hello\n", "hello\n"),
    ("BEL is dropped and backspace erases", "abc\x07\bX\n", "abX\n"),
    ("cursor-left is honoured like a backspace", "abcdef\x1b[3DXYZ\n", "abcXYZ\n"),
    ("column-absolute is honoured", "abcdef\x1b[1GZ\n", "Zbcdef\n"),
    (
        "a trailing prompt survives verbatim",
        "Setting up\r\nContinue? [Y/n] ",
        "Setting up\nContinue? [Y/n] ",
    ),
    ("empty output stays empty", "", ""),
    ("a log with no trailing newline keeps its last line", "one\ntwo", "one\ntwo"),
    ("a run of three identical lines is left alone", "x\nx\nx\ny\n", "x\nx\nx\ny\n"),
    (
        "a longer run folds into the line plus a count",
        "x\nx\nx\nx\ny\n",
        "x\n[jarvis-shell] previous line repeated 3 more times\ny\n",
    ),
]


def load_server_module(path, name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sterilize_unit_tests():
    print("  sterilize_unit")
    user = load_server_module(USER_SERVER, "shell_user_sterilize")
    system = load_server_module(SYSTEM_SERVER, "shell_system_sterilize")

    for label, raw, expected in STERILIZE_CASES:
        check(user._sterilize(raw) == expected, label)
        check(user._sterilize(raw) == system._sterilize(raw), f"both servers agree: {label}")

    flood = "".join(f"[{'#' * (i % 40):<40}] {i}%\r" for i in range(5000)) + "ready\x1b[K\n"
    rendered = user._sterilize(flood)
    check(rendered == "ready\n", "5000 carriage-return redraws collapse to the final line")
    check(len(flood) > 200 * len(rendered), "the collapse is the difference between 235 KB and a line")

    repeated = "retry\n" * 1000 + "done\n"
    rendered = user._sterilize(repeated)
    check(
        rendered == "retry\n[jarvis-shell] previous line repeated 999 more times\ndone\n",
        "1000 identical lines fold into one line plus a count",
    )

    # An unterminated OSC has no terminator to wait for; without a bound the
    # renderer would swallow the rest of the log looking for one.
    rendered = user._sterilize("\x1b]0;" + "A" * 5000 + "\nreal output\n")
    check(rendered.endswith("real output\n"), "an unterminated escape cannot swallow the log")

    # The renderer feeds the live stream too, where a chunk boundary lands
    # wherever the reader stopped — inside an escape, or between CR and LF.
    for _label, raw, expected in STERILIZE_CASES:
        renderer = user._TerminalLines()
        completed = []
        pending = ""
        for ch in raw:
            done, pending = renderer.feed(ch)
            completed.extend(done)
        piecemeal = "".join(line + "\n" for line in user._collapse_repeats(completed)) + pending
        check(piecemeal == expected, f"one character at a time renders the same: {_label}")

    windows = [
        ((None, None), (0, "0123456789")),
        ((None, 3), (7, "789")),
        ((2, None), (2, "23456789")),
        ((2, 3), (2, "234")),
        ((99, 3), (10, "")),
        ((None, 99), (0, "0123456789")),
        ((None, 0), (10, "")),
    ]
    for (offset, tail), expected in windows:
        got = user._output_window("0123456789", offset, tail)
        check(got == expected, f"_output_window(offset={offset}, tail={tail}) == {expected}")
    check(user._output_window("", None, 5) == (0, ""), "a window over empty output clamps to empty")

    for name in ("_STERILIZE_TAG", "_LOG_REPEAT_RUN_MIN", "_TRANSCRIPT_MAX_CHARS", "_ESC_MAX_LEN"):
        check(
            getattr(user, name) == getattr(system, name),
            f"both servers share the same {name}",
        )
    check(
        isinstance(user._TRANSCRIPT_MAX_CHARS, int) and user._TRANSCRIPT_MAX_CHARS > 0,
        "the transcript cap is a positive number of characters",
    )


# ---------------------------------------------------------------------------
# 9b: the real server — sterilized transcript, byte-exact log, tail/offset
# ---------------------------------------------------------------------------

# Written to a file and run, rather than squeezed through `sh -c`: the point is
# the exact bytes reaching the PTY, and quoting them twice invites a typo that
# would silently weaken the test.
NOISY_SCRIPT = r"""
import sys
w = sys.stdout.write
w('\x1b]0;build\x07')
for i in range(0, 101, 10):
    w('downloading pkg [%3d%%]\r' % i)
w('\rdone\x1b[K\n')
w('fetching big-file.tar\rgot\n')
w('\x1b[1;32mOK\x1b[0m\n')
for _ in range(50):
    w('retrying...\n')
w('finished\a\n')
sys.stdout.flush()
"""

BULK_SCRIPT = r"""
import sys
for i in range(4000):
    sys.stdout.write('line %05d padding padding padding\n' % i)
sys.stdout.flush()
"""


def write_script(root, name, body):
    path = pathlib.Path(root) / name
    path.write_text(body)
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def sterilize_job_tests(env, root):
    print("  sterilized_transcript")
    srv = Server(env)
    srv.initialize()
    command = write_script(root, "noisy.py", NOISY_SCRIPT)
    is_err, payload = srv.call(1, "run_job", {"command": command, "job": "noisy"}, timeout=60)
    transcript = payload.get("transcript", "")
    check(not is_err and payload.get("exit_code") == 0, "the noisy job ran to completion")
    check("\x1b" not in transcript, "no escape sequence survives into the transcript")
    check("\x07" not in transcript, "no BEL survives into the transcript")
    check("\r" not in transcript, "no carriage return survives into the transcript")
    check("done\n" in transcript, "the cleared progress line renders as its final state")
    check("[ 50%]" not in transcript, "no intermediate progress frame is kept")
    check(
        "gotching big-file.tar\n" in transcript,
        "an UNCLEARED short redraw leaves the tail of the line under it",
    )
    check("OK\n" in transcript, "the colour-wrapped line keeps its text")
    check(transcript.count("retrying...") == 1, "the repeated line appears once")
    check(
        "previous line repeated 49 more times" in transcript,
        "the repeat count says how many were folded",
    )
    check(transcript.rstrip("\n").endswith("finished"), "the last line is intact")

    # Sterilization is a READ-time rendering: the log on disk is the evidence
    # and must still hold every byte the command wrote.
    raw = (pathlib.Path(root) / "jarvis-shell" / "noisy" / "out.log").read_bytes()
    check(b"\x1b]0;build\x07" in raw, "out.log still holds the raw OSC sequence")
    check(b"\x1b[1;32m" in raw, "out.log still holds the raw colour escape")
    check(raw.count(b"\r") > 10, "out.log still holds every carriage return")
    check(raw.count(b"retrying...") == 50, "out.log still holds all 50 repeated lines")
    check(len(raw) > 2 * len(transcript), "the transcript is a fraction of the raw log")

    print("  read_output_window")
    is_err, whole = srv.call(2, "read_output", {"job": "noisy"})
    total = whole.get("total_length")
    check(not is_err and whole.get("output") == transcript, "a whole read matches the transcript")
    check(whole.get("output_offset") == 0, "a whole read starts at offset 0")
    check(whole.get("output_length") == total, "a whole read returns every character")

    is_err, tail = srv.call(3, "read_output", {"job": "noisy", "tail": 20})
    check(not is_err and tail.get("output") == transcript[-20:], "tail returns the LAST characters")
    check(tail.get("output_offset") == total - 20, "tail reports where the window starts")
    check(tail.get("total_length") == total, "tail still reports the full length")

    is_err, clamped = srv.call(4, "read_output", {"job": "noisy", "tail": total * 10})
    check(not is_err and clamped.get("output") == transcript, "an oversized tail clamps to the whole output")

    is_err, offset = srv.call(5, "read_output", {"job": "noisy", "offset": 10})
    check(not is_err and offset.get("output") == transcript[10:], "offset alone reads through to the end")

    is_err, window = srv.call(6, "read_output", {"job": "noisy", "offset": 10, "tail": 25})
    check(not is_err and window.get("output") == transcript[10:35], "offset + tail is a window of that size")
    check(window.get("output_offset") == 10, "the window reports its own start")

    is_err, past = srv.call(7, "read_output", {"job": "noisy", "offset": total * 10})
    check(not is_err and past.get("output") == "", "an offset past the end clamps to empty")
    check(past.get("output_offset") == total, "a clamped offset reports the end")

    is_err, zero = srv.call(8, "read_output", {"job": "noisy", "tail": 0})
    check(not is_err and zero.get("output") == "", "tail 0 returns nothing and is not an error")

    for bad, label in (("12", "a string"), (-5, "a negative"), (1.5, "a fraction"), (True, "a bool")):
        is_err, payload = srv.call(9, "read_output", {"job": "noisy", "tail": bad})
        check(is_err, f"{label} tail is refused")
        check("tail" in payload.get("error", ""), f"{label} tail error names the argument")
    srv.close()

    print("  transcript_cap")
    srv = Server(env)
    srv.initialize()
    cap = load_server_module(USER_SERVER, "shell_cap")._TRANSCRIPT_MAX_CHARS
    command = write_script(root, "bulk.py", BULK_SCRIPT)
    is_err, payload = srv.call(1, "run_job", {"command": command, "job": "bulk"}, timeout=120)
    transcript = payload.get("transcript", "")
    check(not is_err and payload.get("exit_code") == 0, "the bulk job ran to completion")
    check("TRANSCRIPT TRUNCATED" in transcript, "an over-cap transcript carries a truncation marker")
    check(transcript.startswith("[jarvis-shell] TRANSCRIPT TRUNCATED"), "the marker is the first thing read")
    check("read_output" in transcript, "the marker names the tool that fetches the rest")
    check('"offset"' in transcript, "the marker names the argument that fetches the rest")

    is_err, whole = srv.call(2, "read_output", {"job": "bulk"})
    full = whole.get("output", "")
    check(not is_err and len(full) > cap, "the whole output is still readable through read_output")
    check(transcript.endswith(full[-cap:]), "the transcript kept the LAST cap characters")
    check(
        len(transcript) - len(full[-cap:]) == len(transcript) - cap,
        "nothing beyond the marker is added to the capped tail",
    )
    check(str(len(full)) in transcript, "the marker says how many characters there were")

    omitted = len(full) - cap
    is_err, head = srv.call(3, "read_output", {"job": "bulk", "offset": 0, "tail": cap})
    check(not is_err and head.get("output") == full[:cap], "the marker's recipe fetches the omitted head")
    check(str(omitted) in transcript, "the marker says how many characters were omitted")
    srv.close()


# ---------------------------------------------------------------------------
# 9c: the live stderr stream is sterilized too
# ---------------------------------------------------------------------------

LIVE_SCRIPT = r"""
import sys, time
w = sys.stdout.write
for i in range(2000):
    w('\x1b[1;36m[%-40s]\x1b[0m %d%%\r' % ('#' * (i % 40), i % 101))
    if i % 500 == 0:
        sys.stdout.flush()
        time.sleep(0.15)
sys.stdout.flush()
w('\ndownload complete\n')
sys.stdout.flush()
answer = input('Proceed with install? [Y/n] ')
print('answered ' + answer.strip())
"""


def live_stream_tests(env, root):
    print("  live_stream_sterilized")
    first = Server(env)
    first.initialize()
    command = write_script(root, "live.py", LIVE_SCRIPT)
    first.send_call(2, "run_job", {"command": command, "job": "flood"})

    seen = wait_until(
        lambda: b"Proceed with install? [Y/n]" in bytes(first.stderr_buf), timeout=60
    )
    check(seen, "the prompt reaches the live stream despite the flood ahead of it")
    check(2 not in first.responses, "run_job is still blocked on the prompt")
    streamed = first.stderr_text()
    check("\x1b" not in streamed, "no escape sequence reaches the live stream")
    check("download complete" in streamed, "ordinary lines still stream through verbatim")

    second = Server(env)
    second.initialize()
    is_err, _payload = second.call(1, "send_input", {"job": "flood", "text": "Y\n"})
    check(not is_err, "the flooded job still answers send_input")
    second.close()

    msg = first.wait(2, timeout=60)
    payload = json.loads(msg["result"]["content"][0]["text"])
    check(payload.get("exit_code") == 0, "the flooded job exits cleanly once answered")
    check("answered Y" in payload.get("transcript", ""), "the answer took effect")

    raw = (pathlib.Path(root) / "jarvis-shell" / "flood" / "out.log").read_bytes()
    streamed = first.stderr_text()
    check(len(streamed) * 4 < len(raw), "the live stream is a small fraction of the raw log")
    check(
        streamed.count("Proceed with install?") == 1,
        "the prompt is not re-sent on every poll",
    )
    first.close()


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
        crashed_holder_tests(env, root)
        spawn_timeout_tests(root)
        identity_tests(env)
        manifest_blocking_tests()
        sterilize_unit_tests()
        sterilize_job_tests(env, root)
        live_stream_tests(env, root)
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
