#!/usr/bin/env python3
"""selftest_maps.py — prove the maps server keeps the services' rules and answers right.

Two kinds of failure matter here. One is breaking the rules the public
OpenStreetMap services set, which gets every JARVIS user blocked: more than one
request a second, asking again for what was already answered, no identifying
User-Agent, no attribution. The other is a plausible wrong answer: a route whose
end the routing service quietly moved an ocean away, a "nearby" place outside
the radius, a home location ignored. Every case answers the server's requests
from a fake service and checks both.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only, writes only to a temp directory (the server's cache).

Usage:
  python3 scripts/selftest_maps.py
"""
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "maps" / "server.py"
MANIFEST = REPO / "servers" / "maps" / "manifest.json"

CASES = []
FAILURES = []
running = "?"

PLACES = {
    "Moda": {"lat": "40.9850", "lon": "29.0250", "name": "Moda", "display_name": "Moda, Kadıköy", "category": "place", "type": "suburb"},
    "Ankara": {"lat": "39.9208", "lon": "32.8541", "name": "Ankara", "display_name": "Ankara, Türkiye", "category": "place", "type": "city"},
    "New York": {"lat": "40.7128", "lon": "-74.0060", "name": "New York", "display_name": "New York, United States", "category": "place", "type": "city"},
}


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


class FakeServices:
    """Stands in for urlopen: Nominatim search/reverse and OSRM route."""

    def __init__(self):
        self.requests = []
        self.times = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.times.append(time.time())
        url = urllib.parse.urlsplit(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path.endswith("/search"):
            if query.get("bounded") == "1":
                body = [
                    {"lat": "40.9860", "lon": "29.0250", "name": "Near", "display_name": "Near", "category": "amenity", "type": "pharmacy", "extratags": {"opening_hours": "Mo-Sa 09:00-19:00"}},
                    {"lat": "40.9851", "lon": "29.0250", "name": "Nearest", "display_name": "Nearest", "category": "amenity", "type": "pharmacy"},
                    {"lat": "40.9990", "lon": "29.0250", "name": "Corner of the box", "display_name": "Far", "category": "amenity", "type": "pharmacy"},
                ]
            else:
                body = [PLACES[query["q"]]] if query["q"] in PLACES else []
        elif url.path.endswith("/reverse"):
            body = {"lat": query["lat"], "lon": query["lon"], "name": "Some Street", "display_name": "Some Street, Town", "category": "highway", "type": "residential", "address": {"road": "Some Street"}}
        elif "/route/v1/" in url.path:
            coords = url.path.rsplit("/", 1)[-1].split(";")
            far = any(c.startswith("-74") for c in coords)
            body = {
                "code": "Ok",
                "routes": [{"distance": 450000.0, "duration": 16200.0, "legs": [{"summary": "O-4", "steps": [
                    {"maneuver": {"type": "depart"}, "name": "Moda Caddesi", "distance": 100},
                    {"maneuver": {"type": "turn", "modifier": "left"}, "name": "Bahariye", "distance": 200},
                    {"maneuver": {"type": "roundabout", "exit": 2}, "name": "", "distance": 50},
                    {"maneuver": {"type": "arrive", "modifier": "right"}, "name": "", "distance": 0},
                ]}]}],
                "waypoints": [{"distance": 12.0}, {"distance": 5534803.0 if far else 30.0}],
            }
        else:
            raise AssertionError(f"unexpected request {request.full_url}")
        return io.BytesIO(json.dumps(body).encode())


def load_server(**env):
    cache = tempfile.mkdtemp(prefix="maps-selftest-")
    saved = {k: os.environ.get(k) for k in ("XDG_CACHE_HOME", "MAPS_HOME", "MAPS_ROUTING_URL", "MAPS_NOMINATIM_URL")}
    os.environ["XDG_CACHE_HOME"] = cache
    for key in ("MAPS_HOME", "MAPS_ROUTING_URL", "MAPS_NOMINATIM_URL"):
        os.environ.pop(key, None)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("maps_under_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    fake = FakeServices()
    module.urllib.request.urlopen = fake
    return module, fake


def call(module, tool, arguments):
    reply = module._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    result = reply["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


@case
def requests_are_a_second_apart_and_identify_the_server():
    server, fake = load_server()
    call(server, "find_place", {"query": "Moda"})
    call(server, "find_place", {"query": "Ankara"})
    check(len(fake.times) == 2 and fake.times[1] - fake.times[0] >= 0.95, "two requests to one service are a second apart")
    agent = fake.requests[0].get_header("User-agent") or ""
    check("jarvis-maps-mcp" in agent and "github.com" in agent, "the User-Agent names the server and its repository")

    # A second copy of the server (dmcp starts one per call) shares the spacing.
    again, fake2 = load_server()
    again.CACHE_DIR = server.CACHE_DIR
    call(again, "find_place", {"query": "New York"})
    check(fake2.times and fake2.times[0] - fake.times[-1] >= 0.95, "another process waits its turn too")


@case
def answers_are_cached():
    server, fake = load_server()
    call(server, "find_place", {"query": "Moda"})
    err, answer = call(server, "find_place", {"query": "Moda"})
    check(len(fake.requests) == 1, "asking again for the same place sends nothing")
    check(not err and "OpenStreetMap contributors" in answer["attribution"], "every answer carries the attribution")
    cached = [p for p in server.CACHE_DIR.iterdir() if p.suffix == ".json"]
    check(cached and all((p.stat().st_mode & 0o077) == 0 for p in cached), "cached answers are owner-only")


@case
def coordinates_need_no_lookup():
    server, fake = load_server()
    err, answer = call(server, "get_route", {"from": "40.98,29.02", "to": "40.99,29.03"})
    paths = [urllib.parse.urlsplit(r.full_url).path for r in fake.requests]
    check(not err and not any(p.endswith("/search") for p in paths), "a latitude,longitude pair is routed without a place search")
    err, text = call(server, "get_route", {"from": "95,29", "to": "Moda"})
    check(err and "valid" in text, "an impossible latitude is refused")


@case
def routes_read_right():
    server, fake = load_server()
    err, answer = call(server, "get_route", {"from": "Moda", "to": "Ankara", "mode": "bike", "steps": True})
    check(not err, "a route answers")
    if err:
        return
    check(answer["distance_km"] == 450.0 and answer["duration"] == "4 h 30 min", "distance and duration are converted")
    check(answer["via"] == "O-4" and "traffic" in answer["note"], "the main roads are named and the time is marked typical")
    check("/routed-bike/route/v1/" in fake.requests[-1].full_url, "the mode picks the routing server")
    check(
        [s["instruction"] for s in answer["steps"]]
        == ["Start on Moda Caddesi", "Turn left onto Bahariye", "At the roundabout, take exit 2", "Arrive at the destination, on the right"],
        "maneuvers become sentences",
    )
    err, text = call(server, "get_route", {"from": "Moda", "to": "New York"})
    check(err and "no car route" in text and "5,535 km" in text, "an end moved an ocean away is refused, not answered")

    server, fake = load_server(MAPS_ROUTING_URL="http://127.0.0.1:5000")
    call(server, "get_route", {"from": "40.98,29.02", "to": "40.99,29.03", "mode": "foot"})
    check(fake.requests[-1].full_url.startswith("http://127.0.0.1:5000/route/v1/"), "a routing address without {mode} is used as given")


@case
def home_fills_in_a_missing_start():
    server, _ = load_server(MAPS_HOME="40.98,29.02")
    err, answer = call(server, "get_route", {"to": "Ankara"})
    check(not err and answer["from"] == "40.98000,29.02000", "the home location is the start when none is given")
    server, _ = load_server()
    err, text = call(server, "get_route", {"to": "Ankara"})
    check(err and "MAPS_HOME" in text, "with no start and no home, the call says so")


@case
def nearby_is_within_the_radius_and_nearest_first():
    server, fake = load_server()
    err, answer = call(server, "find_nearby", {"what": "pharmacy", "near": "Moda", "radius_m": 500})
    check(not err, "find_nearby answers")
    if err:
        return
    names = [p["name"] for p in answer["places"]]
    check(names == ["Nearest", "Near"], f"a match in the box's corner but beyond the radius is dropped, and the rest are nearest first (got {names})")
    check(answer["places"][1].get("opening_hours") == "Mo-Sa 09:00-19:00", "opening hours come along when mapped")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.requests[-1].full_url).query))
    left, top, right, bottom = map(float, query["viewbox"].split(","))
    check(query.get("bounded") == "1" and left < 29.025 < right and bottom < 40.985 < top, "the search is bounded to a box around the place")


@case
def tools_match_the_manifest():
    server, _ = load_server()
    listed = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    declared = json.loads(MANIFEST.read_text())["tools"]
    check(
        sorted(t["name"] for t in listed) == sorted(t["name"] for t in declared),
        "the server offers exactly the tools its manifest declares",
    )


def main() -> int:
    global running
    if not SERVER.is_file():
        print(f"FAIL: {SERVER} not found")
        return 1

    original = urllib.request.urlopen
    try:
        for fn in CASES:
            running = fn.__name__
            print(f"  {running}")
            # A case that raises is a failure, not the end of the run: a crash in
            # one would otherwise hide every result after it.
            try:
                fn()
            except Exception as exc:
                check(False, f"raised {type(exc).__name__}: {exc}")
    finally:
        urllib.request.urlopen = original

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: maps self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
