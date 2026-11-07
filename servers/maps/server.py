#!/usr/bin/env python3
"""
Maps MCP Server (OpenStreetMap)

Places, addresses, what is nearby, and routes with distance and travel time, by
car, bike or on foot, from OpenStreetMap's public services: Nominatim for places
and addresses, and the FOSSGIS routing servers for routes. No account and no
API key.

Those services are run by volunteers and come with rules, which this server
keeps on every call:
  https://operations.osmfoundation.org/policies/nominatim/
  https://fossgis.de/arbeitsgruppen/osm-server/nutzungsbedingungen/
- at most one request a second to each service, across every running copy of
  this server (a lock file and a timestamp under the user's cache directory);
- a User-Agent that names this server and where it comes from;
- places and addresses are cached for 30 days, so asking again sends nothing;
- no search-as-you-type and no bulk or systematic queries: every request
  answers one question someone asked;
- every answer says "© OpenStreetMap contributors".
The addresses of both services are settings, so a self-hosted or commercial
instance can replace them without a new version of this server.

Uses only the Python stdlib, like the weather server, so nothing is installed
into the user's Python environment and the same file runs on Linux, macOS and
Windows.

Every tool is read-only: it sends a place name or coordinates to a map service
and returns places and numbers.
"""

import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 20
USER_AGENT = "jarvis-maps-mcp/1.0 (https://github.com/JarvisOSLinux/mcp-registry)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL), https://www.openstreetmap.org/copyright"

NOMINATIM_URL = (
    os.environ.get("MAPS_NOMINATIM_URL", "").strip() or "https://nominatim.openstreetmap.org"
).rstrip("/")
# {mode} becomes car, bike or foot: the FOSSGIS servers run one OSRM per mode.
# A self-hosted OSRM serving one mode is given without it.
ROUTING_URL = (
    os.environ.get("MAPS_ROUTING_URL", "").strip()
    or "https://routing.openstreetmap.de/routed-{mode}"
).rstrip("/")
HOME = os.environ.get("MAPS_HOME", "").strip()

MIN_SECONDS_BETWEEN_REQUESTS = 1.0
CACHE_SECONDS = 30 * 24 * 3600
MODES = ("car", "bike", "foot")
MAX_STEPS = 40
# OSRM snaps an end it cannot reach to the nearest road it can, even an ocean
# away (New York, routed from Istanbul, lands in Portugal); past this the
# route answers a different question.
MAX_SNAP_M = 5000

_COORDINATES = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*")


class MapsError(Exception):
    """Anything the caller could plausibly fix, or needs told plainly."""


def _cache_dir() -> pathlib.Path | None:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(pathlib.Path.home() / ".cache")
    path = pathlib.Path(base) / "jarvis-maps"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
    except OSError:
        return None


CACHE_DIR = _cache_dir()
_last_request: dict[str, float] = {}


def _wait_turn(host: str) -> None:
    """Keep requests to one host at least a second apart.

    dmcp starts a fresh process for every call and dispatch may run calls in
    parallel, so the spacing lives in the cache directory, guarded by a lock
    file made with O_EXCL (portable, unlike flock). Without a cache directory
    the spacing still holds within this process.
    """
    if CACHE_DIR is None:
        wait = _last_request.get(host, 0) + MIN_SECONDS_BETWEEN_REQUESTS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.time()
        return
    lock = CACHE_DIR / f"{host}.lock"
    stamp = CACHE_DIR / f"{host}.last"
    deadline = time.monotonic() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                # A holder that crashed leaves its lock; nobody holds one for
                # more than a second and a bit.
                if time.time() - lock.stat().st_mtime > 10:
                    lock.unlink()
            except FileNotFoundError:
                pass
            if time.monotonic() > deadline:
                raise MapsError(f"Timed out waiting for a turn to ask {host}; try again.")
            time.sleep(0.05)
    try:
        try:
            last = float(stamp.read_text())
        except (OSError, ValueError):
            last = 0.0
        wait = last + MIN_SECONDS_BETWEEN_REQUESTS - time.time()
        if wait > 0:
            time.sleep(wait)
        stamp.write_text(repr(time.time()))
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _cached(url: str) -> Any | None:
    if CACHE_DIR is None:
        return None
    path = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    try:
        if time.time() - path.stat().st_mtime > CACHE_SECONDS:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(url: str, payload: Any) -> None:
    if CACHE_DIR is None:
        return
    path = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def _http_get_json(url: str, params: dict, cache: bool = False) -> Any:
    """GET a JSON document politely, turning every failure into a legible MapsError."""
    full = url + "?" + urllib.parse.urlencode(params)
    if cache:
        hit = _cached(full)
        if hit is not None:
            return hit
    host = urllib.parse.urlsplit(url).hostname or "unknown"
    _wait_turn(host)
    request = urllib.request.Request(
        full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise MapsError(f"{host} is rate-limiting requests; try again in a minute.") from exc
        if exc.code == 403:
            raise MapsError(
                f"{host} refused the request (HTTP 403). Public OpenStreetMap services block "
                f"clients that break their usage policy, and can withdraw access at any time; "
                f"a self-hosted or commercial service can be set in this server's settings."
            ) from exc
        raise MapsError(f"{host} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MapsError(
            f"Could not reach {host} ({exc.reason}). This server needs outbound HTTPS; "
            f"check the network connection."
        ) from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MapsError(f"{host} returned a response that was not JSON") from exc
    if cache:
        _store(full, payload)
    return payload


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _place(row: dict) -> dict:
    lat, lon = float(row["lat"]), float(row["lon"])
    place = {
        "name": row.get("name") or row.get("display_name", "").split(",")[0],
        "address": row.get("display_name"),
        "kind": f"{row.get('category')}/{row.get('type')}",
        "latitude": lat,
        "longitude": lon,
        "map": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}",
    }
    extra = row.get("extratags") or {}
    for key in ("opening_hours", "phone", "website"):
        if extra.get(key):
            place[key] = extra[key]
    return place


def _search(query: str, limit: int, **extra: Any) -> list[dict]:
    params = {"q": query, "format": "jsonv2", "limit": limit, **extra}
    return _http_get_json(f"{NOMINATIM_URL}/search", params, cache=True)


def _limit(value: Any, default: int, highest: int, what: str = "limit") -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise MapsError(f"`{what}` must be a whole number.")
    return max(1, min(number, highest))


def _locate(value: Any, what: str) -> tuple[float, float, str]:
    """Coordinates and a label for a place name or a "lat,lon" pair.

    A pair is used as given, with no request; a name is geocoded and its best
    match taken, so the answer says which place that was.
    """
    text = str(value or "").strip()
    if not text:
        if HOME:
            return _locate(HOME, "MAPS_HOME")
        raise MapsError(
            f"No `{what}` given and no home location is configured (MAPS_HOME). Pass a place "
            f"name or 'latitude,longitude'."
        )
    pair = _COORDINATES.fullmatch(text)
    if pair:
        lat, lon = float(pair.group(1)), float(pair.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise MapsError(f"{text!r} is not a valid latitude,longitude pair.")
        return lat, lon, f"{lat:.5f},{lon:.5f}"
    rows = _search(text, 1)
    if not rows:
        raise MapsError(
            f"No place matched {text!r}. Add a city or country ('Moda, Istanbul'), or use "
            f"find_place to see the options."
        )
    return float(rows[0]["lat"]), float(rows[0]["lon"]), rows[0].get("display_name") or text


def _coordinates_or_place(arguments: dict, name_key: str) -> tuple[float, float, str]:
    lat, lon = arguments.get("latitude"), arguments.get("longitude")
    if lat is not None and lon is not None:
        return _locate(f"{lat},{lon}", name_key)
    return _locate(arguments.get(name_key), name_key)


def tool_find_place(arguments: dict) -> dict:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise MapsError("`query` is required: a place, an address or a landmark.")
    extra = {}
    if arguments.get("country"):
        code = str(arguments["country"]).strip().lower()
        if not re.fullmatch(r"[a-z]{2}", code):
            raise MapsError("`country` must be a two-letter country code, like 'tr' or 'us'.")
        extra["countrycodes"] = code
    rows = _search(query, _limit(arguments.get("limit"), 5, 10), **extra)
    return {"query": query, "places": [_place(r) for r in rows], "attribution": ATTRIBUTION}


def tool_reverse_geocode(arguments: dict) -> dict:
    try:
        lat, lon = float(arguments["latitude"]), float(arguments["longitude"])
    except (KeyError, TypeError, ValueError):
        raise MapsError("`latitude` and `longitude` are required, as numbers.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise MapsError("latitude must be in -90..90 and longitude in -180..180.")
    # Rounded to about 10 m: enough for an address, and repeated nearby
    # questions then hit the cache instead of the service.
    row = _http_get_json(
        f"{NOMINATIM_URL}/reverse",
        {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "format": "jsonv2", "zoom": 18, "addressdetails": 1},
        cache=True,
    )
    if not row or row.get("error"):
        raise MapsError("No address is known at those coordinates (open sea, or an unmapped area).")
    place = _place(row)
    place["address_parts"] = row.get("address")
    return {"place": place, "attribution": ATTRIBUTION}


def tool_find_nearby(arguments: dict) -> dict:
    what = str(arguments.get("what") or "").strip()
    if not what:
        raise MapsError("`what` is required: the kind of place, like 'pharmacy' or 'cafe'.")
    lat, lon, centre = _coordinates_or_place(arguments, "near")
    radius = _limit(arguments.get("radius_m"), 1000, 5000, "radius_m")
    limit = _limit(arguments.get("limit"), 10, 20)
    dlat = radius / 111320
    dlon = radius / (111320 * max(math.cos(math.radians(lat)), 0.01))
    rows = _search(
        what,
        min(limit * 2, 40),
        viewbox=f"{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f},{lat - dlat:.5f}",
        bounded=1,
        extratags=1,
    )
    places = []
    for row in rows:
        place = _place(row)
        place["distance_m"] = round(_distance_m(lat, lon, place["latitude"], place["longitude"]))
        if place["distance_m"] <= radius:
            places.append(place)
    places.sort(key=lambda p: p["distance_m"])
    answer = {
        "what": what,
        "near": centre,
        "radius_m": radius,
        "places": places[:limit],
        "attribution": ATTRIBUTION,
    }
    if not places:
        answer["note"] = (
            f"Nothing called or tagged '{what}' within {radius} m. Try a wider radius, or a "
            f"plainer word ('pharmacy', 'cafe', 'supermarket', 'fuel', 'atm')."
        )
    return answer


def _duration_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


_VERBS = {
    "turn": "Turn",
    "new name": "Continue",
    "continue": "Continue",
    "merge": "Merge",
    "on ramp": "Take the ramp",
    "off ramp": "Take the exit",
    "fork": "Keep",
    "end of road": "At the end of the road, turn",
    "notification": "Continue",
}


def _instruction(step: dict) -> str:
    """OSRM gives a maneuver, not a sentence; this makes the sentence."""
    maneuver = step.get("maneuver") or {}
    kind = maneuver.get("type", "")
    modifier = maneuver.get("modifier") or ""
    name = step.get("name") or step.get("ref") or ""
    if kind == "depart":
        return f"Start on {name}" if name else "Start"
    if kind == "arrive":
        return "Arrive at the destination" + (f", on the {modifier}" if modifier in ("left", "right") else "")
    if kind in ("roundabout", "rotary", "roundabout turn", "exit roundabout", "exit rotary"):
        exit_number = maneuver.get("exit")
        text = "At the roundabout, take " + (f"exit {exit_number}" if exit_number else "the exit")
        return text + (f" onto {name}" if name else "")
    verb = _VERBS.get(kind, "Continue")
    text = f"{verb} {modifier}".strip()
    return text + (f" onto {name}" if name else "")


def tool_get_route(arguments: dict) -> dict:
    mode = arguments.get("mode") or "car"
    if mode not in MODES:
        raise MapsError("`mode` must be 'car', 'bike' or 'foot'.")
    from_lat, from_lon, from_label = _locate(arguments.get("from"), "from")
    to_lat, to_lon, to_label = _locate(arguments.get("to"), "to")
    want_steps = bool(arguments.get("steps"))
    base = ROUTING_URL.replace("{mode}", mode)
    payload = _http_get_json(
        f"{base}/route/v1/driving/{from_lon:.6f},{from_lat:.6f};{to_lon:.6f},{to_lat:.6f}",
        {"overview": "false", "steps": "true" if want_steps else "false"},
    )
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise MapsError(
            f"No {mode} route was found between those places ({payload.get('code')}). They may "
            f"be on different landmasses, or one may be off the road network."
        )
    for label, waypoint in zip(("from", "to"), payload.get("waypoints") or []):
        snapped = float(waypoint.get("distance") or 0)
        if snapped > MAX_SNAP_M:
            raise MapsError(
                f"There is no {mode} route between {from_label} and {to_label}: the nearest "
                f"road the route could reach is {snapped / 1000:,.0f} km from the `{label}` "
                f"place. They may be across water from each other, or off the road network."
            )
    route = payload["routes"][0]
    leg = route["legs"][0]
    answer = {
        "from": from_label,
        "to": to_label,
        "mode": mode,
        "distance_km": round(route["distance"] / 1000, 1),
        "distance_mi": round(route["distance"] / 1609.344, 1),
        "duration": _duration_text(route["duration"]),
        "duration_min": round(route["duration"] / 60),
        "note": "Typical travel time with no live traffic.",
        "attribution": ATTRIBUTION,
    }
    if leg.get("summary"):
        answer["via"] = leg["summary"]
    if want_steps:
        steps = [
            {"instruction": _instruction(s), "distance_m": round(s.get("distance", 0))}
            for s in leg.get("steps") or []
        ]
        answer["steps"] = steps[:MAX_STEPS]
        if len(steps) > MAX_STEPS:
            answer["steps_truncated"] = f"First {MAX_STEPS} of {len(steps)} steps."
    return answer


_PLACE_HELP = (
    "A place is a name or address ('Kadıköy, Istanbul', 'Eiffel Tower') or a "
    "'latitude,longitude' pair; a name is looked up and its best match used, and the "
    "answer says which place that was."
)

TOOLS = [
    {
        "name": "find_place",
        "description": (
            "Find places by name or address and return their full address, kind, "
            "coordinates and a map link. Use it to confirm which place was meant before "
            "routing, or to answer 'where is'. Not for search-as-you-type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A place, an address or a landmark."},
                "country": {"type": "string", "description": "Two-letter country code to search within."},
                "limit": {"type": "integer", "description": "How many matches, 1-10. Default 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "reverse_geocode",
        "description": "The address at a latitude and longitude: street, district, city and country.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "description": "Latitude, -90 to 90."},
                "longitude": {"type": "number", "description": "Longitude, -180 to 180."},
            },
            "required": ["latitude", "longitude"],
        },
    },
    {
        "name": "find_nearby",
        "description": (
            "Places of a kind near a location, nearest first, with distance, address, and "
            "opening hours, phone and website where the map has them: 'pharmacies near "
            "Moda', 'a cafe near the station'. " + _PLACE_HELP + " Omit the location to "
            "use the configured home."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "The kind of place: 'pharmacy', 'cafe', 'atm', 'fuel', 'supermarket'."},
                "near": {"type": "string", "description": "The place to search around."},
                "latitude": {"type": "number", "description": "Or the centre's latitude."},
                "longitude": {"type": "number", "description": "Or the centre's longitude."},
                "radius_m": {"type": "integer", "description": "Search radius in metres, up to 5000. Default 1000."},
                "limit": {"type": "integer", "description": "How many places, 1-20. Default 10."},
            },
            "required": ["what"],
        },
    },
    {
        "name": "get_route",
        "description": (
            "Distance and travel time between two places by car, bike or on foot, the main "
            "roads it takes, and turn-by-turn steps on request. Times are typical, with no "
            "live traffic. " + _PLACE_HELP + " Omit `from` to start at the configured home."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Starting place."},
                "to": {"type": "string", "description": "Destination."},
                "mode": {"type": "string", "enum": list(MODES), "description": "How to travel. Default 'car'."},
                "steps": {"type": "boolean", "description": "Include turn-by-turn steps. Default false."},
            },
            "required": ["to"],
        },
    },
]

_HANDLERS = {
    "find_place": tool_find_place,
    "reverse_geocode": tool_reverse_geocode,
    "find_nearby": tool_find_nearby,
    "get_route": tool_get_route,
}


def _result(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}],
        "isError": False,
    }


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(name: str, arguments: dict) -> dict:
    handler = _HANDLERS[name]
    try:
        return _result(handler(arguments))
    except MapsError as exc:
        return _error(str(exc))
    except Exception as exc:
        # A bare traceback on stdout is neither legible nor safe, and everything
        # the caller can act on is raised as MapsError above.
        return _error(f"{name} failed: {type(exc).__name__}: {exc}")


def _handle(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    # Notifications have no id and require no response.
    if req_id is None:
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "maps-mcp", "version": "1.0.0"},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _HANDLERS:
            return err(-32601, f"Unknown tool: {name}")
        return ok(_call_tool(name, arguments))

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
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
