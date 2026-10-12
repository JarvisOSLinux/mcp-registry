"""Where a wrapper keeps its token: the OS keyring, else an owner-only file.

The keyring is Secret Service through `secret-tool` (libsecret) where it exists
and answers. Anywhere else, or when it fails (no session bus, locked and
unanswered), the token goes in `$XDG_DATA_HOME/jarvis-mcp/credentials/<service>.json`
at mode 0600 inside a 0700 directory. `JARVIS_SIGNIN_STORE=file` forces the file.
The record is JSON: whatever the wrapper stores, never logged.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

KEYRING_TIMEOUT_SECONDS = 15
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class StoreError(Exception):
    pass


class TokenStore:
    def __init__(self, service: str):
        if not _SERVICE_RE.match(service):
            raise ValueError(f"bad service name {service!r}")
        self.service = service

    # -- keyring -----------------------------------------------------------

    def _keyring(self) -> str | None:
        if os.environ.get("JARVIS_SIGNIN_STORE", "").lower() == "file":
            return None
        return shutil.which("secret-tool")

    def _attrs(self) -> list[str]:
        return ["service", "jarvis-mcp", "name", self.service]

    def _keyring_run(self, tool: str, args: list[str], stdin: str | None = None):
        return subprocess.run(
            [tool, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=KEYRING_TIMEOUT_SECONDS,
        )

    # -- file --------------------------------------------------------------

    def _path(self) -> Path:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "jarvis-mcp" / "credentials" / f"{self.service}.json"

    def _file_save(self, text: str) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, path.parent.parent):
            os.chmod(directory, 0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _file_load(self) -> str | None:
        path = self._path()
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        if os.name == "posix" and mode & 0o077:
            raise StoreError(f"{path} is readable by other users; chmod 600 it or delete it")
        return path.read_text(encoding="utf-8")

    # -- public ------------------------------------------------------------

    def save(self, record: dict) -> str:
        """Store the record. Returns where it went: 'keyring' or 'file'."""
        text = json.dumps(record)
        tool = self._keyring()
        if tool:
            try:
                done = self._keyring_run(
                    tool, ["store", f"--label=JARVIS {self.service}", *self._attrs()], text
                )
                if done.returncode == 0:
                    self._path().unlink(missing_ok=True)
                    return "keyring"
            except (OSError, subprocess.SubprocessError):
                pass
        self._file_save(text)
        return "file"

    def load(self) -> dict | None:
        tool = self._keyring()
        if tool:
            try:
                done = self._keyring_run(tool, ["lookup", *self._attrs()])
                if done.returncode == 0 and done.stdout.strip():
                    return json.loads(done.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
        text = self._file_load()
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise StoreError("stored credentials are unreadable") from err

    def delete(self) -> None:
        tool = self._keyring()
        if tool:
            try:
                self._keyring_run(tool, ["clear", *self._attrs()])
            except (OSError, subprocess.SubprocessError):
                pass
        self._path().unlink(missing_ok=True)
