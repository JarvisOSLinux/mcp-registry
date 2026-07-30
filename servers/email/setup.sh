#!/usr/bin/env bash
# Setup for the JARVIS Email MCP server. CWD = install dir (the cloned
# servers/email directory). Stdlib only — nothing is downloaded or installed;
# this verifies the interpreter can actually run the server.
set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Python 3.9+ (e.g. pacman -S python, apt install python3) before running setup." >&2
  exit 1
}

# Compared numerically in Python itself so a future release is never rejected by
# a hardcoded version list.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  found="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Python >= 3.9 is required (found ${found}); imaplib/smtplib need the timeout argument added in 3.9." >&2
  exit 1
fi

# The whole dependency list, all of it stdlib. A Python built without ssl would
# fail here instead of at the first IMAPS connection.
python3 -c 'import imaplib, smtplib, ssl, email, base64, html.parser' || {
  echo "This Python is missing a standard-library module the server needs (see the traceback above)." >&2
  exit 1
}

chmod +x server.py

# Actually import the module, not just ast.parse it. Parsing only proves the
# source is syntactically valid for THIS interpreter's grammar; a construct this
# Python cannot evaluate (a newer annotation syntax, say) still parses and then
# fails at def time, which would let setup certify an interpreter that cannot
# run a single tool call. main() is guarded by __name__, so importing defines
# everything and starts nothing.
# -B: loading the module would otherwise leave a __pycache__ in the install dir,
# and this server writes nothing to disk.
python3 -B -c 'import importlib.util
spec = importlib.util.spec_from_file_location("jarvis_email_server", "server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)' || {
  echo "server.py cannot be loaded by this python3 (see the traceback above)." >&2
  exit 1
}

echo "OK: jarvis-email needs no third-party packages; python3 can run server.py."
