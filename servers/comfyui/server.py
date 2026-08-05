#!/usr/bin/env python3
"""
JARVIS ComfyUI MCP Server

Local text-to-image generation via a running ComfyUI instance. Builds and
queues a minimal Stable Diffusion workflow, polls until completion, writes the
output PNG to disk and returns its file path plus small metadata — all
on-device, no cloud API, no telemetry.

Requires a ComfyUI server running at COMFYUI_URL (default http://127.0.0.1:8188).
GPU-dependent: ComfyUI needs a GPU with enough VRAM for the loaded model.

Tools
-----
generate_image   Queue text-to-image, block until done, save the PNG and return
                 its path (blocking: true — long op, suggestedRemindAfter: 60s)
list_models      List available checkpoints/LoRAs/VAEs from ComfyUI
get_queue        Show pending and running prompts
interrupt        Stop the current generation
get_history      Recent generation history

Security posture
----------------
All requests go to the configured COMFYUI_URL — a local endpoint by default.
No credentials. A generated image is written to the output directory
(COMFYUI_OUTPUT_DIR, default $XDG_DATA_HOME/jarvis/comfyui/ or
~/.local/share/jarvis/comfyui/, created 0700) and the tool result returns only
that path plus small metadata (dimensions, prompt_id). The image bytes are
never inlined into the tool result: a Stable Diffusion PNG is 0.5–2 MB, and
returning it base64-encoded would land the whole image in the model's context
on every call (the JARVIS "Bloated Context" threat). A caller that needs the
pixels reads them from the returned path.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_URL = "http://127.0.0.1:8188"
_SERVER_NAME = "jarvis-comfyui-mcp"
_SERVER_VERSION = "1.0.0"

_POLL_INTERVAL = 2.0   # seconds between /history polls
_GENERATION_TIMEOUT = 600  # 10 minutes hard cap

# Relative fallback under the XDG data dir when COMFYUI_OUTPUT_DIR is unset.
_OUTPUT_SUBDIR = ("jarvis", "comfyui")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "generate_image",
        "description": (
            "Generate an image from a text prompt using a locally running ComfyUI "
            "instance (Stable Diffusion). Queues a generation workflow, waits for "
            "completion, writes the resulting PNG to the output directory and "
            "returns its file path plus small metadata (path, width, height, "
            "format, prompt_id). The image bytes are NOT returned inline — read "
            "the PNG from the returned path if you need the pixels. "
            "Runs entirely on-device — no cloud API, no data leaves the machine. "
            "GPU-dependent: requires a model loaded in ComfyUI and enough VRAM. "
            "This tool blocks until generation finishes (typically 10–90 seconds "
            "depending on GPU and steps)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Positive prompt describing what to generate.",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Negative prompt describing what to avoid.",
                    "default": "blurry, low quality, nsfw",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Checkpoint filename to use (e.g. 'v1-5-pruned-emaonly.safetensors'). "
                        "Defaults to the first available checkpoint from list_models."
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (must be divisible by 8, default 512).",
                    "default": 512,
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (must be divisible by 8, default 512).",
                    "default": 512,
                },
                "steps": {
                    "type": "integer",
                    "description": "Sampling steps (default 20, higher = better quality, slower).",
                    "default": 20,
                },
                "cfg": {
                    "type": "number",
                    "description": "CFG scale / guidance strength (default 7.0, range 1–20).",
                    "default": 7.0,
                },
                "sampler": {
                    "type": "string",
                    "description": "Sampler name (default 'euler'). Options: euler, euler_a, dpm++, ddim, lcm.",
                    "default": "euler",
                },
                "seed": {
                    "type": "integer",
                    "description": "RNG seed for reproducibility. Omit for a random seed.",
                },
            },
        },
        "blocking": True,
        "suggestedRemindAfter": 60,
    },
    {
        "name": "list_models",
        "description": (
            "List the model files available in the running ComfyUI instance, "
            "grouped by type (checkpoints, loras, vae, etc.). Use to discover "
            "valid model filenames for the 'model' parameter of generate_image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "Model folder to list: 'checkpoints', 'loras', 'vae', "
                        "'controlnet', 'embeddings'. Omit to list all."
                    ),
                },
            },
        },
    },
    {
        "name": "get_queue",
        "description": (
            "Show the current ComfyUI queue: pending prompts (queue_running) and "
            "prompts waiting to run (queue_pending). Use to check if a generation "
            "is in progress before queuing another."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "interrupt",
        "description": (
            "Stop the currently running ComfyUI generation. Sends an interrupt "
            "signal to ComfyUI; the current step finishes before the run halts. "
            "The queued prompt is cancelled, not paused."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_history",
        "description": (
            "Return the most recent generation history from ComfyUI: prompt IDs, "
            "parameters and output filenames. Does not return image data — use "
            "generate_image to get the actual image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum history entries to return (default 10).",
                    "default": 10,
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class ToolError(Exception):
    pass


def _base_url() -> str:
    url = os.environ.get("COMFYUI_URL", _DEFAULT_URL).rstrip("/")
    return url


def _get(path: str, timeout: float = 15.0) -> Any:
    url = _base_url() + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise ToolError(f"GET {path} → HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Cannot reach ComfyUI at {_base_url()}: {exc.reason}. "
            "Is ComfyUI running? Check COMFYUI_URL."
        )


def _post_json(path: str, data: Any, timeout: float = 30.0) -> Any:
    url = _base_url() + path
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:500]
        raise ToolError(f"POST {path} → HTTP {exc.code}: {body_text}")
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Cannot reach ComfyUI at {_base_url()}: {exc.reason}. "
            "Is ComfyUI running? Check COMFYUI_URL."
        )


def _post_empty(path: str, timeout: float = 10.0) -> None:
    url = _base_url() + path
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        raise ToolError(f"POST {path} → HTTP {exc.code}")
    except urllib.error.URLError as exc:
        raise ToolError(f"Cannot reach ComfyUI at {_base_url()}: {exc.reason}")


def _get_image_bytes(filename: str, subfolder: str, folder_type: str) -> bytes:
    params = urllib.parse.urlencode({
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
    })
    url = _base_url() + f"/view?{params}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ToolError(f"Failed to retrieve image {filename!r}: {exc}")


# ---------------------------------------------------------------------------
# Output-file helpers
# ---------------------------------------------------------------------------


def _output_dir() -> str:
    """Directory the generated PNG is written to.

    COMFYUI_OUTPUT_DIR wins if set (the user's own choice — its permissions are
    left as they are). Otherwise a private per-user dir under the XDG data dir,
    forced to 0700 because it holds the images this server generates.
    """
    custom = os.environ.get("COMFYUI_OUTPUT_DIR")
    if custom:
        path = os.path.expanduser(custom)
        os.makedirs(path, exist_ok=True)
        return path

    xdg = os.environ.get("XDG_DATA_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, *_OUTPUT_SUBDIR)
    os.makedirs(path, exist_ok=True)
    # exist_ok skips the mode on an existing dir and makedirs' mode is umask-masked
    # anyway; set it explicitly so the dir is private regardless of how it arose.
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) read from a PNG's IHDR, or None if not a PNG.

    Layout: 8-byte signature, then the IHDR chunk (4-byte length, 4-byte type),
    so width is bytes 16:20 and height 20:24, big-endian.
    """
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if width > 0 and height > 0:
            return width, height
    return None


# ---------------------------------------------------------------------------
# Workflow builder
# ---------------------------------------------------------------------------

def _pick_default_model() -> str:
    try:
        data = _get("/object_info/CheckpointLoaderSimple")
        models = (
            data.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[], {}])[0]
        )
        if models:
            return models[0]
    except ToolError:
        pass
    return "v1-5-pruned-emaonly.safetensors"


def _build_workflow(
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    sampler: str,
    seed: int,
) -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": model},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "jarvis"},
        },
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_generate_image(args: dict) -> dict:
    prompt_text = (args.get("prompt") or "").strip()
    if not prompt_text:
        raise ToolError("prompt is required")

    negative = args.get("negative_prompt") or "blurry, low quality, nsfw"
    width = int(args.get("width") or 512)
    height = int(args.get("height") or 512)
    steps = int(args.get("steps") or 20)
    cfg = float(args.get("cfg") or 7.0)
    sampler = args.get("sampler") or "euler"
    seed = int(args.get("seed")) if args.get("seed") is not None else random.randint(0, 2**32 - 1)

    # Snap dimensions to multiples of 8
    width = (width // 8) * 8
    height = (height // 8) * 8
    width = max(64, min(width, 4096))
    height = max(64, min(height, 4096))

    model = (args.get("model") or "").strip() or _pick_default_model()

    workflow = _build_workflow(
        prompt_text, negative, model, width, height, steps, cfg, sampler, seed
    )

    client_id = f"jarvis-{seed}"
    resp = _post_json("/prompt", {"prompt": workflow, "client_id": client_id})
    prompt_id: str = resp.get("prompt_id")
    if not prompt_id:
        raise ToolError(f"ComfyUI did not return a prompt_id: {resp}")

    # Poll until done
    deadline = time.monotonic() + _GENERATION_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        history = _get(f"/history/{prompt_id}", timeout=10.0)
        entry = history.get(prompt_id)
        if entry is not None:
            break
    else:
        raise ToolError(
            f"Generation timed out after {_GENERATION_TIMEOUT}s (prompt_id={prompt_id}). "
            "Try interrupt then check get_history."
        )

    # Find the output image
    outputs = entry.get("outputs", {})
    image_info = None
    for node_id, node_output in outputs.items():
        images = node_output.get("images", [])
        if images:
            image_info = images[0]
            break

    if image_info is None:
        raise ToolError(
            f"Generation completed but no image output found (prompt_id={prompt_id}). "
            f"Outputs: {list(outputs.keys())}"
        )

    img_bytes = _get_image_bytes(
        image_info["filename"],
        image_info.get("subfolder", ""),
        image_info.get("type", "output"),
    )

    # Write the PNG to disk and return its path — never the bytes. Inlining a
    # 0.5–2 MB image into the tool result would put the whole thing in the
    # model's context on every call (the "Bloated Context" threat). The
    # prompt_id is unique per generation, so it makes a collision-free filename.
    out_dir = _output_dir()
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(prompt_id)) or "image"
    out_path = os.path.join(out_dir, f"{safe_id}.png")
    try:
        with open(out_path, "wb") as fh:
            fh.write(img_bytes)
    except OSError as exc:
        raise ToolError(f"Failed to write image to {out_path!r}: {exc}")

    # Prefer the actual pixel dimensions from the PNG header; fall back to the
    # requested (latent) dimensions if the header is unreadable.
    dims = _png_dimensions(img_bytes)
    out_width, out_height = dims if dims else (width, height)

    return {
        "path": out_path,
        "format": "png",
        "width": out_width,
        "height": out_height,
        "prompt_id": prompt_id,
        "seed": seed,
        "model": model,
        "steps": steps,
        "cfg": cfg,
        "sampler": sampler,
        "filename": image_info["filename"],
    }


def tool_list_models(args: dict) -> dict:
    folder = (args.get("type") or "").strip().lower()
    model_folders = ["checkpoints", "loras", "vae", "controlnet", "embeddings"]
    if folder and folder not in model_folders:
        raise ToolError(f"Unknown model type {folder!r}. Options: {', '.join(model_folders)}")

    targets = [folder] if folder else model_folders
    result: dict[str, list] = {}

    for t in targets:
        try:
            data = _get(f"/models/{t}")
            result[t] = data if isinstance(data, list) else []
        except ToolError:
            result[t] = []

    total = sum(len(v) for v in result.values())
    return {"models": result, "total": total}


def tool_get_queue(_args: dict) -> dict:
    data = _get("/queue")
    running = data.get("queue_running", [])
    pending = data.get("queue_pending", [])
    return {
        "queue_running": len(running),
        "queue_pending": len(pending),
        "running": [
            {"prompt_id": item[1], "number": item[0]} for item in running
        ] if running else [],
        "pending": [
            {"prompt_id": item[1], "number": item[0]} for item in pending
        ] if pending else [],
    }


def tool_interrupt(_args: dict) -> dict:
    _post_empty("/interrupt")
    return {"result": "Interrupt signal sent to ComfyUI"}


def tool_get_history(args: dict) -> dict:
    limit = int(args.get("limit") or 10)
    history = _get("/history")
    entries = list(history.items())[-limit:]
    result = []
    for prompt_id, entry in entries:
        outputs = entry.get("outputs", {})
        filenames = []
        for node_output in outputs.values():
            for img in node_output.get("images", []):
                filenames.append(img.get("filename", ""))
        result.append(
            {
                "prompt_id": prompt_id,
                "outputs": filenames,
            }
        )
    return {"history": result, "count": len(result)}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

_HANDLERS = {
    "generate_image": tool_generate_image,
    "list_models": tool_list_models,
    "get_queue": tool_get_queue,
    "interrupt": tool_interrupt,
    "get_history": tool_get_history,
}


def _ok_result(payload: dict) -> dict:
    content = [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]
    return {"content": content, "isError": False}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(name: str, arguments: dict) -> dict | None:
    handler = _HANDLERS.get(name)
    if handler is None:
        return None
    try:
        return _ok_result(handler(arguments))
    except ToolError as exc:
        return _error_result(f"{name}: {exc}")
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
