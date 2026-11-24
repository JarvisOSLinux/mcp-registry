"""
The notifications log, shared by the collector (which writes it) and the MCP
server (which reads it).

The log is a SQLite file in the user's state directory, owner-only. What goes
in has already been cleaned: notification markup stripped, one-time codes
blanked out, excluded apps dropped. Nothing here talks to the network.

Settings reach the collector through settings.json beside the log: the MCP
server gets its settings from JARVIS as environment variables, but the
collector runs as a systemd user service and never sees them, so the server
writes them down on every call and the collector reloads the file when it
changes.
"""

import datetime
import html
import json
import os
import pathlib
import re
import sqlite3
import time

DEFAULT_KEEP_DAYS = 7
MAX_KEEP_DAYS = 90
# JARVIS's own notifications (sign-in codes, approval prompts) are never logged.
ALWAYS_EXCLUDED = ("jarvis",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    received REAL NOT NULL,
    app TEXT NOT NULL,
    desktop_entry TEXT,
    summary TEXT,
    body TEXT,
    urgency INTEGER,
    category TEXT,
    redacted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notifications_received ON notifications(received);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _state_dir() -> pathlib.Path:
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local" / "state")
    return pathlib.Path(base) / "jarvis-notifications"


STATE_DIR = _state_dir()


def state_dir(create: bool = True) -> pathlib.Path | None:
    """The log's folder, made owner-only on first use (never on import)."""
    if create:
        try:
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(STATE_DIR, 0o700)
        except OSError:
            return None
    return STATE_DIR if STATE_DIR.is_dir() else None


def db_path() -> pathlib.Path:
    return STATE_DIR / "notifications.db"


def heartbeat_path() -> pathlib.Path:
    return STATE_DIR / "collector.alive"


def settings_path() -> pathlib.Path:
    return STATE_DIR / "settings.json"


def open_db(create: bool = True) -> sqlite3.Connection | None:
    folder = state_dir(create)
    if folder is None:
        return None
    path = db_path()
    if not create and not path.exists():
        return None
    if not path.exists():
        # Made owner-only before SQLite writes a byte to it.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    for extra in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(extra, 0o600)
        except OSError:
            pass
    return conn


# ---------------------------------------------------------------------------
# Cleaning what goes in
# ---------------------------------------------------------------------------

# Words that mark a message as carrying a one-time code, in the languages most
# likely here. The digits next to them are blanked before anything is stored.
_CODE_WORDS = re.compile(
    r"\b(code|codes|otp|passcode|one[- ]time|verification|verify|security|2fa|two[- ]factor|"
    r"sign[- ]in|log[- ]?in|pin|kod|kodu|doğrulama|şifre|código|codice|bestätigung|"
    r"einmal|код|验证码)\b|验证码",
    re.I,
)
# A run of 4-8 digits (or 3+3 / 4+4 split by a space or dash) that is not part
# of a longer number, a decimal, a time or a date. A full stop after it is fine.
_CODE = re.compile(r"(?<![\d/:])(?<!\d[.,])(?:\d{3,4}[ -]\d{3,4}|\d{4,8})(?!\d|[.,:/]\d|[ -]\d)")
CODE_HIDDEN = "[code hidden]"


def redact(summary: str, body: str) -> tuple[str, str, bool]:
    """Blank one-time codes: digit runs in a notification that talks about codes."""
    if not _CODE_WORDS.search(f"{summary}\n{body}"):
        return summary, body, False
    new_summary = _CODE.sub(CODE_HIDDEN, summary)
    new_body = _CODE.sub(CODE_HIDDEN, body)
    return new_summary, new_body, (new_summary, new_body) != (summary, body)


def clean(text: str, limit: int = 2000) -> str:
    """The notification spec allows a little markup in the body; keep the words."""
    text = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.I)
    text = re.sub(r"<img\b[^>]*\balt=\"([^\"]*)\"[^>]*>", r"\1", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def load_settings() -> dict:
    settings = {"keep_days": DEFAULT_KEEP_DAYS, "exclude": []}
    try:
        loaded = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(loaded, dict):
        days = loaded.get("keep_days")
        if isinstance(days, int) and 1 <= days <= MAX_KEEP_DAYS:
            settings["keep_days"] = days
        exclude = loaded.get("exclude")
        if isinstance(exclude, list):
            settings["exclude"] = [str(e).strip().casefold() for e in exclude if str(e).strip()]
    return settings


def save_settings(settings: dict) -> bool:
    """Write settings.json if it changed; True when it did."""
    if state_dir() is None:
        return False
    if load_settings() == settings and settings_path().exists():
        return False
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(settings, handle)
    os.replace(tmp, path)
    return True


def is_excluded(app: str, desktop_entry: str, settings: dict) -> bool:
    names = {str(app or "").casefold(), str(desktop_entry or "").casefold()}
    names.discard("")
    blocked = set(ALWAYS_EXCLUDED) | set(settings.get("exclude") or [])
    return bool(names & blocked)


def record(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    app: str,
    summary: str,
    body: str,
    desktop_entry: str = "",
    urgency: int | None = None,
    category: str = "",
    when: float | None = None,
) -> bool:
    """Store one notification, cleaned; False when it was dropped."""
    app = clean(app, 120) or "unknown app"
    desktop_entry = clean(desktop_entry, 120)
    if is_excluded(app, desktop_entry, settings):
        return False
    summary, body = clean(summary, 300), clean(body)
    if not summary and not body:
        return False
    summary, body, redacted = redact(summary, body)
    when = time.time() if when is None else when
    last = conn.execute(
        "SELECT received, app, summary, body FROM notifications ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # Apps sometimes send the same notification twice in a row (an update that
    # replaces it, or two windows); one copy is enough.
    if last and last[1:] == (app, summary, body) and when - last[0] < 5:
        return False
    conn.execute(
        "INSERT INTO notifications (received, app, desktop_entry, summary, body, urgency, category, redacted)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (when, app, desktop_entry or None, summary, body, urgency, category or None, int(redacted)),
    )
    conn.commit()
    return True


def prune(conn: sqlite3.Connection, keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    cursor = conn.execute("DELETE FROM notifications WHERE received < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def split_phone_app(app: str, summary: str) -> tuple[str, str]:
    """KDE Connect shows a phone's notifications as 'WhatsApp: Alice'; report
    them under the phone app, so WhatsApp on the phone groups as WhatsApp."""
    if re.fullmatch(r"kde ?connect", app, re.I):
        match = re.match(r"^([^:]{1,40}): (.+)$", summary)
        if match:
            return f"{match.group(1)} (phone)", match.group(2)
    return app, summary

