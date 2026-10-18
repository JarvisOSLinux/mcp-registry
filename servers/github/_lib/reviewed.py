"""The tools the registry reviewed for a wrapper: the ones its manifest declares.

A wrapper forwards an upstream server, and the upstream decides what it offers.
Notion's hosted server added ten tools in three days, and carries tools whose
descriptions are instructions to the model (an upsell "next steps" tool); the
GitHub server offered 77 tools against 13 reviewed, among them creating and
deleting repository and organization webhooks. So a wrapper passes on only the
tools its manifest declares, the ones a registry review saw and classified,
and a new upstream tool reaches the model only through a registry PR.

The manifest read is the one dmcp installed next to the wrapper. If it cannot
be read, nothing upstream is offered: a wrapper that cannot tell what was
reviewed fails closed, keeping only its own tools (sign-in, status).
"""

import json
import sys
from pathlib import Path
from typing import Callable, Iterable

# The flag that makes a wrapper print what its upstream offers and exit, for
# scripts/check_tool_drift.py. dmcp starts a wrapper with its manifest's
# arguments only, so an agent never reaches it.
LIST_FLAG = "--list-upstream-tools"


def declared_tools(wrapper_file: str) -> frozenset:
    """Tool names declared in the manifest beside `wrapper_file`; empty if unreadable."""
    path = Path(wrapper_file).resolve().with_name("manifest.json")
    try:
        tools = json.loads(path.read_text(encoding="utf-8")).get("tools")
        return frozenset(t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str))
    except (OSError, ValueError, AttributeError, TypeError) as err:
        print(f"{path.parent.name}: cannot read the reviewed tool list ({err}); offering only the wrapper's own tools",
              file=sys.stderr, flush=True)
        return frozenset()


def print_upstream_tools(fetch: Callable[[], list], local_tools: Iterable[dict]) -> int:
    """Print {"upstream": [...], "local": [...]} as JSON: every upstream tool, nothing withheld."""
    try:
        upstream = fetch()
    except OSError as err:
        print(f"cannot list the upstream's tools: {err}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"upstream": sorted(set(upstream)), "local": sorted(t["name"] for t in local_tools)}))
    return 0
