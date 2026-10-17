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
