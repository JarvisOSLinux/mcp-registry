"""Shared sign-in helpers for registry wrappers (Project-JARVIS#229).

Canonical source. dmcp installs only a server's own directory (`source.path`),
so every wrapper that uses this package carries a byte-identical copy at
`servers/<wrapper>/_lib/`. `scripts/sync_lib.py` refreshes the copies and the
PR gate fails if one drifts.

Standard library only, Python 3.10+.
"""
