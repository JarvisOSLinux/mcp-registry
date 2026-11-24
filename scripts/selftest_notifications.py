#!/usr/bin/env python3
"""selftest_notifications.py — prove the notifications log keeps what it should and nothing it shouldn't.

The server's failure modes are quiet: a one-time code stored in plain text
sits in a file for a week; JARVIS's own sign-in codes logged and read back to
the model; an excluded app still in the log; a log readable by other users; a
collector that hangs on a bad message, keeps restarting after an uninstall, or
silently stops. So every case runs the real collector and server code against
temporary state folders, a stand-in for `busctl monitor`, and checks what
reaches the log and the replies.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only; writes only to temporary directories it removes.

Usage:
  python3 scripts/selftest_notifications.py
"""
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER_DIR = REPO / "servers" / "notifications"
MANIFEST = SERVER_DIR / "manifest.json"

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


def notify_line(app, summary, body, entry="", urgency=1, member="Notify"):
    hints = {"urgency": {"type": "y", "data": urgency}}
    if entry:
        hints["desktop-entry"] = {"type": "s", "data": entry}
    return json.dumps({
        "type": "method_call",
        "member": member,
        "interface": "org.freedesktop.Notifications",
        "payload": {"type": "susssasa{sv}i", "data": [app, 0, "", summary, body, [], hints, 5000]},
    })


class Sandbox:
    """A temporary state and config home, with the server's modules loaded fresh."""

    def __init__(self, env=None):
        self.env = env or {}

    def __enter__(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-notifications-"))
        self.saved = {k: os.environ.get(k) for k in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "NOTIFICATIONS_EXCLUDE", "NOTIFICATIONS_KEEP_DAYS")}
        os.environ["XDG_STATE_HOME"] = str(self.tmp / "state")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        os.environ.pop("NOTIFICATIONS_EXCLUDE", None)
        os.environ.pop("NOTIFICATIONS_KEEP_DAYS", None)
        os.environ.update(self.env)
        for name in ("logbook", "collector", "server"):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(SERVER_DIR))
        try:
            import logbook  # noqa: F401
            import collector
            import server
        finally:
            sys.path.remove(str(SERVER_DIR))
        self.logbook, self.collector, self.server = sys.modules["logbook"], collector, server
        self.started = []
        server._start_collector = lambda: self.started.append(True) or "process"
        return self

    def alive(self):
        """Pretend the collector is running (a fresh heartbeat from this process)."""
        self.logbook.state_dir()
        self.collector.beat(time.time())  # the collector's own writer: this process's pid, owner-only

    def feed(self, *lines, settings=None):
        conn = self.logbook.open_db()
        settings = settings or self.logbook.load_settings()
        return [self.collector.handle_line(conn, settings, line) for line in lines]

    def stored(self):
        conn = self.logbook.open_db()
        return conn.execute("SELECT app, summary, body, redacted FROM notifications ORDER BY id").fetchall()

    def call(self, tool, arguments=None):
        reply = self.server._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
        )
        result = reply["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text)), text

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in ("logbook", "collector", "server"):
            sys.modules.pop(name, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


@case
def tools_match_the_manifest():
    with Sandbox() as box:
        listed = box.server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    declared = {t["name"]: t["threat_level"] for t in json.loads(MANIFEST.read_text())["tools"]}
    check(sorted(t["name"] for t in listed) == sorted(declared), "the server offers exactly the tools its manifest declares")
    check(
        declared.get("what_did_i_miss") == "elevated" and declared.get("search_notifications") == "elevated",
        "reading message text asks first, like email",
    )
    check(declared.get("notification_apps") == "safe", "app names and counts, with no text, are safe")


@case
def one_time_codes_are_blanked_before_storing():
    with Sandbox() as box:
        box.feed(
            notify_line("Google", "Verification", "Your Google verification code is G-482913. Do not share it."),
            notify_line("Bank", "Sign-in", "Your code: 123 456"),
            notify_line("Steam", "Steam Guard", "Doğrulama kodunuz 551234."),
            notify_line("Calendar", "Room booked", "Room 1234 at 15:00, order 12345678901"),
        )
        rows = box.stored()
        text = " ".join(f"{r[1]} {r[2]}" for r in rows)
        check("482913" not in text and "123 456" not in text and "551234" not in text, "no code reaches the log")
        check(text.count("[code hidden]") == 3, "each code is replaced by a marker")
        check([r[3] for r in rows] == [1, 1, 1, 0], "redacted rows are flagged")
        check("Room 1234" in text and "12345678901" in text, "numbers in messages that aren't about codes stay")


@case
def jarvis_and_excluded_apps_are_never_stored():
    with Sandbox(env={"NOTIFICATIONS_EXCLUDE": "Signal, KeePassXC"}) as box:
        box.alive()
        box.feed(notify_line("Signal", "Eve", "secret"))  # logged before the server wrote settings down
        box.call("notification_apps")  # the server hands its settings to the collector
        check(not any(r[0] == "Signal" for r in box.stored()), "a newly excluded app's old entries are deleted")
        box.feed(
            notify_line("JARVIS", "Sign in to GitHub", "Enter code ABCD-1234"),
            notify_line("jarvis", "Approve?", "Run pacman -Syu"),
            notify_line("Signal", "Eve", "another secret"),
            notify_line("Something", "Hi", "there", entry="org.keepassxc.KeePassXC"),
            notify_line("Something", "Hi", "there", entry="keepassxc"),
            notify_line("Discord", "Alice", "hello"),
        )
        apps = [r[0] for r in box.stored()]
        check(apps == ["Something", "Discord"], "JARVIS's own and excluded apps (by name or desktop entry) are dropped")


@case
def markup_duplicates_and_bad_messages():
    with Sandbox() as box:
        stored = box.feed(
            notify_line("Discord", "Alice", "up for <b>Deep Rock</b>?<br/>8pm &amp; later"),
            notify_line("Discord", "Alice", "up for <b>Deep Rock</b>?<br/>8pm &amp; later"),
            "not json",
            json.dumps({"type": "method_return"}),
            notify_line("Discord", "Alice", "x", member="CloseNotification"),
            json.dumps({"type": "method_call", "member": "Notify", "payload": {"data": ["short"]}}),
            notify_line("Empty", "", ""),
        )
        check(stored == [True, False, False, False, False, False, False], "one copy of a repeat; junk ignored")
        rows = box.stored()
        check(rows and rows[0][2] == "up for Deep Rock?\n8pm & later", "markup becomes plain text")


@case
def phone_notifications_group_under_the_phone_app():
    with Sandbox() as box:
        box.alive()
        box.feed(
            notify_line("KDE Connect", "WhatsApp: Mom", "Call me"),
            notify_line("KDE Connect", "WhatsApp: Dad", "Dinner?"),
            notify_line("KDE Connect", "Battery low", "Your phone is at 10%"),
        )
        _, result, _ = box.call("what_did_i_miss")
        apps = {a["app"]: a for a in result["apps"]}
        check("WhatsApp (phone)" in apps and apps["WhatsApp (phone)"]["count"] == 2, "phone WhatsApp groups as one app")
        check(sorted(s["name"] for s in apps["WhatsApp (phone)"]["senders"]) == ["Dad", "Mom"], "with each sender")
        check("KDE Connect" in apps, "KDE Connect's own notifications stay under KDE Connect")
        _, apps_result, _ = box.call("notification_apps")
        check("WhatsApp (phone)" in [a["app"] for a in apps_result["apps"]], "notification_apps names the phone app too")


@case
def what_did_i_miss_remembers_the_last_check():
    with Sandbox() as box:
        box.alive()
        conn = box.logbook.open_db()
        settings = box.logbook.load_settings()
        box.logbook.record(conn, settings, app="Discord", summary="Old", body="from yesterday", when=time.time() - 30 * 3600)
        box.logbook.record(conn, settings, app="Discord", summary="Alice", body="new one")
        _, first, _ = box.call("what_did_i_miss")
        check(first["total"] == 1 and first["window"] == "the last 24 hours", "the first time, the last 24 hours")
        _, again, _ = box.call("what_did_i_miss")
        check(again["total"] == 0 and again["window"] == "since you last asked", "asking again shows only what's new")
        box.logbook.record(conn, settings, app="Telegram", summary="Bob", body="hey")
        _, filtered, _ = box.call("what_did_i_miss", {"apps": ["discord"]})
        _, after, _ = box.call("what_did_i_miss")
        check(after["total"] == 1, "a filtered look doesn't mark the rest as seen")
        _, wide, _ = box.call("what_did_i_miss", {"hours": 48})
        check(wide["total"] == 3, "hours looks back further")
        error, _, _ = box.call("what_did_i_miss", {"hours": "lots"})
        check(error, "a bad hours value is refused")
        _, found, _ = box.call("search_notifications", {"query": "YESTERDAY"})
        check(found["found"] == 1 and found["notifications"][0]["from"] == "Old", "search is case-insensitive")


@case
def old_entries_are_pruned_and_files_are_private():
    with Sandbox(env={"NOTIFICATIONS_KEEP_DAYS": "2"}) as box:
        box.alive()
        conn = box.logbook.open_db()
        settings = box.logbook.load_settings()
        box.logbook.record(conn, settings, app="A", summary="old", body="x", when=time.time() - 3 * 86400)
        box.logbook.record(conn, settings, app="A", summary="new", body="y")
        box.call("notification_apps")
        check([r[1] for r in box.stored()] == ["new"], "entries older than NOTIFICATIONS_KEEP_DAYS are removed")
        check(box.logbook.load_settings()["keep_days"] == 2, "the collector is told the setting")
        folder = box.logbook.STATE_DIR
        check(stat.S_IMODE(folder.stat().st_mode) == 0o700, "the log folder is owner-only")
        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in folder.iterdir()}
        check(all(m == 0o600 for m in modes.values()), f"every file in it is owner-only ({modes})")


@case
def forgetting_deletes_from_the_log_only():
    with Sandbox() as box:
        box.alive()
        box.feed(
            notify_line("Discord", "Alice", "a"),
            notify_line("KDE Connect", "WhatsApp: Mom", "b"),
            notify_line("Telegram", "Bob", "c"),
        )
        error, _, _ = box.call("forget_notifications")
        check(error and len(box.stored()) == 3, "nothing is deleted without apps or everything")
        _, result, _ = box.call("forget_notifications", {"apps": ["whatsapp"]})
        check(result["forgotten"] == 1 and [r[0] for r in box.stored()] == ["Discord", "Telegram"], "by app, phone apps included")
        _, result, _ = box.call("forget_notifications", {"everything": True})
        check(result["forgotten"] == 2 and not box.stored(), "everything empties the log")


@case
def a_stopped_collector_is_started_and_said():
    with Sandbox() as box:
        _, result, _ = box.call("what_did_i_miss")
        check(box.started == [True], "a missing collector is started")
        check("not recorded" in result["collector"]["note"], "and the reply says what was missed")
        box.started.clear()
        box.alive()
        _, result, _ = box.call("what_did_i_miss")
        check(not box.started and result["collector"]["running"] is True, "a running collector is left alone")
        old = time.time() - 600
        os.utime(box.logbook.heartbeat_path(), (old, old))
        box.call("notification_apps")
        check(box.started == [True], "a stale heartbeat counts as stopped")


@case
def the_collector_runs_stops_and_never_hangs():
    with Sandbox() as box:
        feed = box.tmp / "fake_busctl.py"
        feed.write_text(textwrap.dedent(f"""
            import sys, time
            lines = {[notify_line("Discord", "Alice", "first"), "garbage", notify_line("Telegram", "Bob", "second")]!r}
            for line in lines:
                sys.stdout.write(line + "\\n"); sys.stdout.flush(); time.sleep(0.1)
        """))
        code = box.collector.run([sys.executable, str(feed)])
        check(code == 1, "when busctl ends (session gone) the collector exits non-zero, so systemd restarts it")
        check([r[0] for r in box.stored()] == ["Discord", "Telegram"], "it stored every Notify and skipped the garbage")
        check(not box.logbook.heartbeat_path().exists(), "and removed its heartbeat")
        box.collector.SELF = box.tmp / "uninstalled" / "collector.py"
        code = box.collector.run([sys.executable, "-c", "import time; time.sleep(30)"])
        check(code == 0, "with its own file gone (uninstalled) it stops for good, exit 0")
        check(box.collector.monitor_command()[:3] == ["busctl", "--user", "monitor"], "it watches the user's own bus")
        check(any("member='Notify'" in part for part in box.collector.monitor_command()), "only Notify calls")


@case
def setup_installs_a_user_service_that_survives_uninstall():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-notifications-setup-"))
    try:
        install = tmp / "install dir"
        shutil.copytree(SERVER_DIR, install)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        log = tmp / "systemctl.log"
        for name, body in (("systemctl", f'echo "$*" >> "{log}"'), ("busctl", "exit 0")):
            shim = bin_dir / name
            shim.write_text(f"#!/bin/sh\n{body}\n")
            shim.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", XDG_CONFIG_HOME=str(tmp / "config"),
                   XDG_STATE_HOME=str(tmp / "state"))
        done = subprocess.run(["bash", "setup.sh"], cwd=install, env=env, capture_output=True, text=True, timeout=60)
        check(done.returncode == 0, f"setup succeeds ({done.stderr.strip()[:200]})")
        unit = (tmp / "config" / "systemd" / "user" / "jarvis-notifications.service").read_text()
        check(f'"{install}/collector.py"' in unit, "the service runs this install's collector, quoted (paths may have spaces)")
        check(f"ConditionPathExists={install}/collector.py" in unit, "and is skipped, not restarted forever, once uninstalled")
        check("Restart=on-failure" in unit and "NoNewPrivileges=yes" in unit, "restarts on failure, no new privileges")
        calls = log.read_text() if log.exists() else ""
        check("--user enable jarvis-notifications.service" in calls and "--user restart" in calls,
              "it is enabled and (re)started as a user service, never system-wide")
        check("sudo" not in (install / "setup.sh").read_text(), "setup never asks for root")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def loading_the_server_creates_nothing():
    with Sandbox() as box:
        check(not (box.tmp / "state").exists(), "importing the server and collector makes no folder")


def main() -> int:
    global running
    if not (SERVER_DIR / "server.py").is_file():
        print(f"FAIL: {SERVER_DIR / 'server.py'} not found")
        return 1

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        # A case that raises is a failure, not the end of the run: a crash in
        # one would otherwise hide every result after it.
        try:
            fn()
        except Exception as exc:
            check(False, f"raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: notifications self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
