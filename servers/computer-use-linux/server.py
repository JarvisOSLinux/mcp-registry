#!/usr/bin/env python3
"""
JARVIS Computer Use — Linux Desktop Control MCP Server

Lets JARVIS control the local Wayland/KDE Plasma desktop via:
  - AT-SPI2 accessibility tree (read windows, find/activate elements, set text)
  - ydotool via ydotoold (keyboard/mouse injection through /dev/uinput)
  - Spectacle or grim (Wayland screenshots)

Security posture
----------------
This server is the highest-privilege interactive surface the registry exposes.
It can drive any GUI app, inject keystrokes, click arbitrary coordinates, and
capture screen content that may contain passwords and private data.

Threat classification mirrors the comments in Project-JARVIS threat_level.py:
  - Read-only AT-SPI calls (list_windows, get_accessibility_tree, find_elements)
    are SAFE — they enumerate public accessibility metadata.
  - take_screenshot is ELEVATED — screen content may include sensitive data.
  - GUI input (click_element, focus_window, set_element_text, type_text,
    key_press, mouse_click) is DANGEROUS — equivalent to physical keyboard and
    mouse access; can drive any app on behalf of the user.

Every DANGEROUS tool's description opens with an explicit warning so the TLA
gate receives it as part of the tool description, regardless of whether the
daemon is currently enforcing per-tool threat levels.

AT-SPI requires:
  - DBUS_SESSION_BUS_ADDRESS and/or AT_SPI_BUS_ADDRESS inherited from the
    user's Wayland session.
  - python3-pyatspi (or pyatspi via pip) installed.
  - The AT-SPI2 registry daemon must be running (auto-started on KDE/GNOME).

ydotool requires:
  - ydotool + ydotoold installed (setup.sh handles this).
  - ydotoold daemon running (setup.sh configures a systemd user unit).
  - The running user must be in the 'input' group (setup.sh adds this).
  - YDOTOOL_SOCKET if non-default.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

try:
    import pyatspi  # type: ignore[import]
    _ATSPI_OK = True
except ImportError:
    _ATSPI_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVER_NAME = "jarvis-computer-use-linux"
_SERVER_VERSION = "1.0.0"

_DANGEROUS_BANNER = (
    "DANGEROUS — INPUT INJECTION. This tool injects keyboard or mouse events "
    "into the running Wayland session. It can drive any GUI application, type "
    "passwords, click buttons, and trigger irreversible actions. It must be "
    "confirmed through JARVIS's TLA gate before execution."
)

_ELEVATED_BANNER = (
    "ELEVATED — SCREEN CAPTURE. Screenshots may contain passwords, private "
    "messages, financial data, or any other content visible on the screen at "
    "the moment of capture. Confirm with the user before capturing."
)

_MAX_TREE_DEPTH = 6
_MAX_TREE_NODES = 500

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_windows",
        "description": (
            "List all top-level application windows currently visible to the "
            "AT-SPI2 accessibility stack. Returns the application name, PID, "
            "role and number of children for each. Use this to discover the "
            "app_name to pass to other tools."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_accessibility_tree",
        "description": (
            "Return a text representation of the AT-SPI2 accessibility tree for "
            "an application. Each node shows its role and name. Use it to plan "
            "which element to target with click_element or set_element_text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name as returned by list_windows.",
                },
                "pid": {
                    "type": "integer",
                    "description": "Application PID (alternative to app_name).",
                },
                "max_depth": {
                    "type": "integer",
                    "description": f"Maximum tree depth (default {_MAX_TREE_DEPTH}).",
                    "default": _MAX_TREE_DEPTH,
                },
            },
        },
    },
    {
        "name": "find_elements",
        "description": (
            "Search the AT-SPI2 tree of an application for elements matching a "
            "role and/or name substring. Returns matching elements with their "
            "role, name, description and available actions. Use to verify an "
            "element exists before calling click_element or set_element_text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name as returned by list_windows.",
                },
                "pid": {
                    "type": "integer",
                    "description": "Application PID (alternative to app_name).",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "AT-SPI role to filter by, e.g. 'push button', 'text', "
                        "'check box', 'combo box', 'menu item', 'entry'."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Substring to match against the element's accessible name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 20).",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "focus_window",
        "description": (
            "DANGEROUS — INPUT INJECTION. Bring an application window to the "
            "foreground by activating its top-level AT-SPI frame. Required "
            "before coordinate-based mouse operations so clicks land on the "
            "right window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name as returned by list_windows.",
                },
                "pid": {
                    "type": "integer",
                    "description": "Application PID (alternative to app_name).",
                },
            },
        },
    },
    {
        "name": "click_element",
        "description": (
            "DANGEROUS — INPUT INJECTION. Click a UI element inside an "
            "application using its AT-SPI accessibility action (not a raw "
            "coordinate). Prefers the 'click' action, falls back to 'press', "
            "then 'activate'. More reliable than mouse_click for semantic UI "
            "actions because it is not affected by window position, DPI or "
            "Wayland coordinate mapping."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["app_name", "element_name"],
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name as returned by list_windows.",
                },
                "pid": {
                    "type": "integer",
                    "description": "Application PID (alternative to app_name).",
                },
                "element_name": {
                    "type": "string",
                    "description": "Accessible name of the element to click.",
                },
                "role": {
                    "type": "string",
                    "description": "AT-SPI role to disambiguate when multiple elements share a name.",
                },
            },
        },
    },
    {
        "name": "set_element_text",
        "description": (
            "DANGEROUS — INPUT INJECTION. Replace the text contents of an "
            "editable text field using the AT-SPI2 EditableText interface. "
            "Overwrites the entire current value. More reliable than type_text "
            "for form fields because it does not depend on focus or clipboard "
            "state, and never garbles non-ASCII characters. Fails if the "
            "element does not expose EditableText."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["app_name", "element_name", "text"],
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name as returned by list_windows.",
                },
                "pid": {
                    "type": "integer",
                    "description": "Application PID (alternative to app_name).",
                },
                "element_name": {
                    "type": "string",
                    "description": "Accessible name of the text field to write into.",
                },
                "text": {
                    "type": "string",
                    "description": "New text to place in the field.",
                },
            },
        },
    },
    {
        "name": "take_screenshot",
        "description": (
            "ELEVATED — SCREEN CAPTURE. Capture the current Wayland display as "
            "a PNG and return it base64-encoded. Uses 'spectacle' on KDE Plasma "
            "or 'grim' on other compositors. Screenshots may contain passwords, "
            "private messages or financial data — confirm with the user before "
            "capturing."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "type_text",
        "description": (
            "DANGEROUS — INPUT INJECTION. Type text into the currently focused "
            "element by copying to the Wayland clipboard (wl-copy) and pasting "
            "via ydotool Ctrl+V. Works across all apps and layouts; avoids the "
            "ydotool key-code layout problem. Requires wl-clipboard and ydotool "
            "with ydotoold running. Prefer set_element_text when the target is "
            "an AT-SPI editable field."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to type.",
                },
            },
        },
    },
    {
        "name": "key_press",
        "description": (
            "DANGEROUS — INPUT INJECTION. Inject a keyboard shortcut or key "
            "sequence via ydotool. Key names follow ydotool / X11 keysym "
            "conventions: 'Return', 'Escape', 'Tab', 'ctrl+c', 'ctrl+shift+t', "
            "'alt+F4', 'super+l', 'BackSpace'. Multiple keys separated by "
            "spaces are pressed in sequence. Requires ydotool + ydotoold."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["keys"],
            "properties": {
                "keys": {
                    "type": "string",
                    "description": (
                        "Key or combo to press, e.g. 'ctrl+c', 'Return', "
                        "'alt+F4', 'ctrl+shift+t Return'."
                    ),
                },
            },
        },
    },
    {
        "name": "mouse_click",
        "description": (
            "DANGEROUS — INPUT INJECTION. Move the mouse to absolute Wayland "
            "compositor coordinates (x, y) and click. Use click_element for "
            "semantic UI actions; reserve this for apps with no AT-SPI "
            "coverage. Requires ydotool + ydotoold. Focus the target window "
            "first with focus_window."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["x", "y"],
            "properties": {
                "x": {
                    "type": "integer",
                    "description": "X coordinate in Wayland compositor space.",
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate in Wayland compositor space.",
                },
                "button": {
                    "type": "string",
                    "description": "'left' (default), 'right', or 'middle'.",
                    "default": "left",
                    "enum": ["left", "right", "middle"],
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# AT-SPI helpers
# ---------------------------------------------------------------------------


def _require_atspi() -> None:
    if not _ATSPI_OK:
        raise ToolError(
            "pyatspi is not installed. Run setup.sh or: "
            "pacman -S python-pyatspi  /  pip install pyatspi"
        )


def _get_desktop():
    return pyatspi.Registry.getDesktop(0)


def _find_app(app_name: str | None, pid: int | None):
    desktop = _get_desktop()
    for app in desktop:
        if app is None:
            continue
        if pid is not None and app.get_process_id() == pid:
            return app
        if app_name is not None and app.name.lower() == app_name.lower():
            return app
    # Substring fallback
    if app_name is not None:
        for app in desktop:
            if app is not None and app_name.lower() in (app.name or "").lower():
                return app
    return None


def _walk_tree(node, depth: int, max_depth: int, node_count: list[int]) -> list[str]:
    if depth > max_depth or node_count[0] >= _MAX_TREE_NODES:
        return []
    node_count[0] += 1
    indent = "  " * depth
    role = (node.getRoleName() or "unknown").replace("\n", " ")
    name = (node.name or "").replace("\n", " ")[:80]
    line = f"{indent}[{role}] {name!r}" if name else f"{indent}[{role}]"
    lines = [line]
    try:
        count = node.getChildCount()
    except Exception:
        count = 0
    for i in range(count):
        try:
            child = node.getChildAtIndex(i)
            if child is not None:
                lines.extend(_walk_tree(child, depth + 1, max_depth, node_count))
        except Exception:
            continue
    return lines


def _find_elements_recursive(
    node,
    role_filter: str | None,
    name_filter: str | None,
    results: list[dict],
    limit: int,
    depth: int = 0,
    max_depth: int = 12,
) -> None:
    if len(results) >= limit or depth > max_depth:
        return
    try:
        node_role = (node.getRoleName() or "").lower()
        node_name = node.name or ""
    except Exception:
        return

    role_match = role_filter is None or role_filter.lower() in node_role
    name_match = name_filter is None or name_filter.lower() in node_name.lower()

    if role_match and name_match and (role_filter or name_filter):
        actions = []
        try:
            action_iface = node.queryAction()
            for j in range(action_iface.nActions):
                actions.append(action_iface.getName(j))
        except Exception:
            pass
        results.append(
            {
                "role": node_role,
                "name": node_name[:120],
                "description": (node.description or "")[:120],
                "actions": actions,
            }
        )

    try:
        count = node.getChildCount()
    except Exception:
        return
    for i in range(count):
        if len(results) >= limit:
            return
        try:
            child = node.getChildAtIndex(i)
            if child is not None:
                _find_elements_recursive(
                    child, role_filter, name_filter, results, limit, depth + 1, max_depth
                )
        except Exception:
            continue


def _find_and_click(app, element_name: str, role: str | None) -> str:
    target = None
    results: list = []
    _find_elements_recursive(app, role, element_name, results, limit=5)
    if not results:
        raise ToolError(
            f"No element with name containing {element_name!r}"
            + (f" and role {role!r}" if role else "")
            + f" found in {app.name!r}"
        )

    def _find_node(node, name_filter, role_filter, depth=0, max_depth=12):
        if depth > max_depth:
            return None
        try:
            node_role = (node.getRoleName() or "").lower()
            node_name = node.name or ""
        except Exception:
            return None
        r_ok = role_filter is None or role_filter.lower() in node_role
        n_ok = name_filter.lower() in node_name.lower()
        if r_ok and n_ok:
            return node
        try:
            count = node.getChildCount()
        except Exception:
            return None
        for i in range(count):
            try:
                child = node.getChildAtIndex(i)
                if child is not None:
                    found = _find_node(child, name_filter, role_filter, depth + 1, max_depth)
                    if found is not None:
                        return found
            except Exception:
                continue
        return None

    target = _find_node(app, element_name, role)
    if target is None:
        raise ToolError(f"Could not locate element {element_name!r} in {app.name!r}")

    # Try actions in preference order
    try:
        action_iface = target.queryAction()
        preferred = ["click", "press", "activate"]
        action_names = [action_iface.getName(i) for i in range(action_iface.nActions)]
        for want in preferred:
            for i, aname in enumerate(action_names):
                if aname.lower() == want:
                    action_iface.doAction(i)
                    return f"Performed '{aname}' on [{target.getRoleName()}] {target.name!r}"
        if action_names:
            action_iface.doAction(0)
            return f"Performed '{action_names[0]}' on [{target.getRoleName()}] {target.name!r}"
        raise ToolError(f"Element [{target.getRoleName()}] {target.name!r} exposes no actions")
    except pyatspi.NotImplementedException:
        raise ToolError(f"Element [{target.getRoleName()}] {target.name!r} does not support actions")


def _find_and_set_text(app, element_name: str, text: str) -> str:
    def _find_node(node, name_filter, depth=0, max_depth=12):
        if depth > max_depth:
            return None
        try:
            node_name = node.name or ""
        except Exception:
            return None
        if name_filter.lower() in node_name.lower():
            return node
        try:
            count = node.getChildCount()
        except Exception:
            return None
        for i in range(count):
            try:
                child = node.getChildAtIndex(i)
                if child is not None:
                    found = _find_node(child, name_filter, depth + 1, max_depth)
                    if found is not None:
                        return found
            except Exception:
                continue
        return None

    target = _find_node(app, element_name)
    if target is None:
        raise ToolError(f"No element with name containing {element_name!r} found in {app.name!r}")

    try:
        editable = target.queryEditableText()
        editable.setTextContents(text)
        return f"Set text on [{target.getRoleName()}] {target.name!r}"
    except pyatspi.NotImplementedException:
        raise ToolError(
            f"[{target.getRoleName()}] {target.name!r} does not expose EditableText. "
            "Try type_text after clicking the field."
        )


# ---------------------------------------------------------------------------
# ydotool helpers
# ---------------------------------------------------------------------------

_BUTTON_MAP = {"left": "0x40000000", "right": "0x80000000", "middle": "0x100000000"}


def _ydotool(*args: str) -> str:
    if not shutil.which("ydotool"):
        raise ToolError(
            "ydotool not found. Run setup.sh or: pacman -S ydotool  /  apt install ydotool"
        )
    env = os.environ.copy()
    result = subprocess.run(
        ["ydotool", *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise ToolError(f"ydotool {args[0]!r} failed (exit {result.returncode}): {msg}")
    return (result.stdout or "").strip()


def _wl_copy(text: str) -> None:
    if not shutil.which("wl-copy"):
        raise ToolError(
            "wl-copy not found. Run setup.sh or: pacman -S wl-clipboard  /  apt install wl-clipboard"
        )
    result = subprocess.run(
        ["wl-copy", "--", text],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise ToolError(f"wl-copy failed (exit {result.returncode})")


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------


def _take_screenshot_spectacle(out_path: str) -> bool:
    if not shutil.which("spectacle"):
        return False
    result = subprocess.run(
        ["spectacle", "--background", "--nonotify", "--bordereffect", "none",
         "--fullscreen", "--output", out_path],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def _take_screenshot_grim(out_path: str) -> bool:
    if not shutil.which("grim"):
        return False
    result = subprocess.run(
        ["grim", out_path],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def tool_list_windows(_args: dict) -> dict:
    _require_atspi()
    desktop = _get_desktop()
    apps = []
    for app in desktop:
        if app is None:
            continue
        try:
            apps.append(
                {
                    "name": app.name or "",
                    "pid": app.get_process_id(),
                    "role": app.getRoleName() or "",
                    "child_count": app.getChildCount(),
                }
            )
        except Exception:
            continue
    return {"windows": apps, "count": len(apps)}


def tool_get_accessibility_tree(args: dict) -> dict:
    _require_atspi()
    app_name = args.get("app_name")
    pid = args.get("pid")
    max_depth = int(args.get("max_depth") or _MAX_TREE_DEPTH)
    max_depth = min(max_depth, 12)

    if not app_name and pid is None:
        raise ToolError("Provide app_name or pid")

    app = _find_app(app_name, pid)
    if app is None:
        raise ToolError(f"Application not found: {app_name or pid}")

    node_count = [0]
    lines = _walk_tree(app, 0, max_depth, node_count)
    truncated = node_count[0] >= _MAX_TREE_NODES
    tree_text = "\n".join(lines)
    if truncated:
        tree_text += f"\n... (tree truncated at {_MAX_TREE_NODES} nodes; use max_depth to limit)"
    return {
        "app": app.name,
        "pid": app.get_process_id(),
        "node_count": node_count[0],
        "truncated": truncated,
        "tree": tree_text,
    }


def tool_find_elements(args: dict) -> dict:
    _require_atspi()
    app_name = args.get("app_name")
    pid = args.get("pid")
    role = args.get("role")
    name = args.get("name")
    limit = int(args.get("limit") or 20)

    if not app_name and pid is None:
        raise ToolError("Provide app_name or pid")
    if not role and not name:
        raise ToolError("Provide at least one of role or name")

    app = _find_app(app_name, pid)
    if app is None:
        raise ToolError(f"Application not found: {app_name or pid}")

    results: list[dict] = []
    _find_elements_recursive(app, role, name, results, limit)
    return {"app": app.name, "pid": app.get_process_id(), "elements": results, "count": len(results)}


def tool_focus_window(args: dict) -> dict:
    _require_atspi()
    app_name = args.get("app_name")
    pid = args.get("pid")

    if not app_name and pid is None:
        raise ToolError("Provide app_name or pid")

    app = _find_app(app_name, pid)
    if app is None:
        raise ToolError(f"Application not found: {app_name or pid}")

    # Activate the first frame child
    try:
        for i in range(app.getChildCount()):
            child = app.getChildAtIndex(i)
            if child is not None:
                try:
                    comp = child.queryComponent()
                    comp.grabFocus()
                    return {"result": f"Focused {app.name!r} window"}
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback: AT-SPI activate on the app node
    try:
        action_iface = app.queryAction()
        for i in range(action_iface.nActions):
            if "activate" in action_iface.getName(i).lower():
                action_iface.doAction(i)
                return {"result": f"Activated {app.name!r}"}
    except Exception:
        pass

    raise ToolError(f"Could not focus {app.name!r} — try mouse_click after identifying the window position")


def tool_click_element(args: dict) -> dict:
    _require_atspi()
    app_name = args.get("app_name")
    pid = args.get("pid")
    element_name = args.get("element_name")
    role = args.get("role")

    if not app_name and pid is None:
        raise ToolError("Provide app_name or pid")
    if not element_name:
        raise ToolError("element_name is required")

    app = _find_app(app_name, pid)
    if app is None:
        raise ToolError(f"Application not found: {app_name or pid}")

    result = _find_and_click(app, element_name, role)
    return {"result": result}


def tool_set_element_text(args: dict) -> dict:
    _require_atspi()
    app_name = args.get("app_name")
    pid = args.get("pid")
    element_name = args.get("element_name")
    text = args.get("text", "")

    if not app_name and pid is None:
        raise ToolError("Provide app_name or pid")
    if not element_name:
        raise ToolError("element_name is required")

    app = _find_app(app_name, pid)
    if app is None:
        raise ToolError(f"Application not found: {app_name or pid}")

    result = _find_and_set_text(app, element_name, text)
    return {"result": result}


def tool_take_screenshot(_args: dict) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out_path = f.name

    try:
        captured = _take_screenshot_spectacle(out_path) or _take_screenshot_grim(out_path)
        if not captured:
            available = [t for t in ("spectacle", "grim") if shutil.which(t)]
            if not available:
                raise ToolError(
                    "No screenshot tool found. Run setup.sh or install spectacle (KDE) or grim."
                )
            raise ToolError(
                f"Screenshot failed (tried: {', '.join(available)}). "
                "Check that WAYLAND_DISPLAY is set in the environment."
            )
        with open(out_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return {
            "format": "png",
            "encoding": "base64",
            "data": data,
            "note": _ELEVATED_BANNER,
        }
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def tool_type_text(args: dict) -> dict:
    text = args.get("text", "")
    if not text:
        raise ToolError("text is required")
    _wl_copy(text)
    _ydotool("key", "ctrl+v")
    return {"result": f"Typed {len(text)} characters via clipboard paste"}


def tool_key_press(args: dict) -> dict:
    keys = args.get("keys", "").strip()
    if not keys:
        raise ToolError("keys is required")
    # Support space-separated sequence: "ctrl+t Return" → two calls
    key_list = keys.split()
    for k in key_list:
        _ydotool("key", k)
    return {"result": f"Pressed: {' '.join(key_list)}"}


def tool_mouse_click(args: dict) -> dict:
    x = args.get("x")
    y = args.get("y")
    button = args.get("button", "left")
    if x is None or y is None:
        raise ToolError("x and y are required")
    btn_code = _BUTTON_MAP.get(button, _BUTTON_MAP["left"])
    _ydotool("mousemove", "--absolute", f"--xpos={int(x)}", f"--ypos={int(y)}")
    _ydotool("click", "--clearmodifiers", btn_code)
    return {"result": f"Clicked {button} at ({x}, {y})"}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, tuple] = {
    "list_windows":           (tool_list_windows,      ""),
    "get_accessibility_tree": (tool_get_accessibility_tree, ""),
    "find_elements":          (tool_find_elements,     ""),
    "focus_window":           (tool_focus_window,      _DANGEROUS_BANNER),
    "click_element":          (tool_click_element,     _DANGEROUS_BANNER),
    "set_element_text":       (tool_set_element_text,  _DANGEROUS_BANNER),
    "take_screenshot":        (tool_take_screenshot,   _ELEVATED_BANNER),
    "type_text":              (tool_type_text,         _DANGEROUS_BANNER),
    "key_press":              (tool_key_press,         _DANGEROUS_BANNER),
    "mouse_click":            (tool_mouse_click,       _DANGEROUS_BANNER),
}


def _ok_result(payload: dict, banner: str = "") -> dict:
    content: list[dict] = []
    if banner:
        content.append({"type": "text", "text": banner})
    # Image tool: return an image block when the payload has base64 data
    if payload.get("encoding") == "base64" and payload.get("format") == "png":
        content.append({"type": "text", "text": payload.get("note", "")})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": payload["data"],
                },
            }
        )
    else:
        content.append({"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)})
    return {"content": content, "isError": False}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(name: str, arguments: dict) -> dict | None:
    entry = _HANDLERS.get(name)
    if entry is None:
        return None
    handler, banner = entry
    try:
        return _ok_result(handler(arguments), banner)
    except ToolError as exc:
        return _error_result(f"{name}: {exc}")
    except subprocess.TimeoutExpired:
        return _error_result(f"{name}: timed out")
    except Exception as exc:
        return _error_result(f"{name}: unexpected {type(exc).__name__}: {exc}")


def _handle(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    if req_id is None:
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            }
        )

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ok(_error_result(f"{name}: 'arguments' must be an object"))
        result = call_tool(name, arguments)
        if result is None:
            return err(-32601, f"Unknown tool: {name}")
        return ok(result)

    return err(-32601, f"Method not found: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
