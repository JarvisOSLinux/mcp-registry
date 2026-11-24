#!/usr/bin/env python3
"""
The notifications collector: a small always-on listener that keeps a local log
of the desktop's notifications, so JARVIS can answer "what did I miss?".

Linux keeps no notification log of its own (the freedesktop spec has none, and
Plasma holds its history in memory only), so this is the log. It watches the
user's session bus for org.freedesktop.Notifications.Notify calls through
systemd's `busctl monitor`, which prints each message as JSON, so no D-Bus
library is needed. It only listens: it never answers, shows, closes or changes
a notification, and it needs no root (monitoring one's own session bus is
allowed to the user).

What it stores is cleaned first (see logbook.py): markup stripped, one-time
codes blanked, JARVIS's own notifications and excluded apps dropped. Old
entries are pruned after the configured number of days.

setup.sh installs it as the systemd user service jarvis-notifications.service.
It exits when busctl does (the session ended; systemd restarts it with the
next one) and stops for good when its own file is gone (the server was
uninstalled).
"""

import json
import os
import pathlib
import selectors
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import logbook  # noqa: E402

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
TICK_SECONDS = 30
PRUNE_EVERY_SECONDS = 3600
SELF = pathlib.Path(__file__).resolve()


def monitor_command() -> list[str]:
    return ["busctl", "--user", "monitor", "--json=short", f"--match={MATCH}"]


def _hint(hints: dict, key: str):
    value = hints.get(key) if isinstance(hints, dict) else None
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def handle_line(conn, settings: dict, line: str) -> bool:
    """One line of busctl's JSON: store it if it is a Notify call."""
    try:
        message = json.loads(line)
    except ValueError:
        return False
    if message.get("type") != "method_call" or message.get("member") != "Notify":
        return False
    data = (message.get("payload") or {}).get("data")
    if not isinstance(data, list) or len(data) < 7:
        return False
    app, _replaces, _icon, summary, body, _actions, hints = data[:7]
    urgency = _hint(hints, "urgency")
    return logbook.record(
        conn,
        settings,
        app=str(app or ""),
        summary=str(summary or ""),
        body=str(body or ""),
        desktop_entry=str(_hint(hints, "desktop-entry") or ""),
        urgency=urgency if isinstance(urgency, int) else None,
        category=str(_hint(hints, "category") or ""),
    )


def beat(started: float) -> None:
    path = logbook.heartbeat_path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": os.getpid(), "started": started}, handle)
    os.replace(tmp, path)


def run(command: list[str] | None = None) -> int:
    conn = logbook.open_db()
    if conn is None:
        print("jarvis-notifications: cannot create the log folder", file=sys.stderr)
        return 1
    settings = logbook.load_settings()
    settings_mtime = _mtime(logbook.settings_path())
    started = time.time()
    logbook.set_meta(conn, "collector_started", str(started))
    try:
        proc = subprocess.Popen(
            command or monitor_command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("jarvis-notifications: busctl not found (it comes with systemd)", file=sys.stderr)
        return 1
    # Raw reads split into lines here: a buffered readline after select() can
    # leave whole lines sitting in Python's buffer until the next message.
    fd = proc.stdout.fileno()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    pending = b""
    last_tick = 0.0
    last_prune = 0.0
    try:
        while True:
            now = time.time()
            if now - last_tick >= TICK_SECONDS:
                last_tick = now
                if not SELF.exists():
                    return 0  # uninstalled: stop for good (exit 0, so systemd won't restart)
                beat(started)
                mtime = _mtime(logbook.settings_path())
                if mtime != settings_mtime:
                    settings, settings_mtime = logbook.load_settings(), mtime
                if now - last_prune >= PRUNE_EVERY_SECONDS:
                    last_prune = now
                    logbook.prune(conn, settings["keep_days"])
            if selector.select(timeout=TICK_SECONDS):
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break  # busctl ended: the session bus went away
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    try:
                        handle_line(conn, settings, line.decode("utf-8", errors="replace"))
                    except Exception as exc:  # one bad message must not stop the log
                        print(f"jarvis-notifications: skipped a message: {type(exc).__name__}", file=sys.stderr)
            elif proc.poll() is not None:
                break
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            logbook.heartbeat_path().unlink()
        except OSError:
            pass
    return 1  # busctl stopped: let systemd start us again with the next session


def _mtime(path: pathlib.Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


if __name__ == "__main__":
    sys.exit(run())
