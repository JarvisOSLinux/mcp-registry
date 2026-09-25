#!/usr/bin/env python3
"""
Weather MCP Server (Open-Meteo)

Current conditions, daily and hourly forecasts, and air quality, from the
Open-Meteo public API. No account and no API key: Open-Meteo serves
non-commercial use without authentication, so there is nothing for the user to
obtain and nothing for this server to store.

Uses only the Python stdlib -- urllib for HTTP, json for parsing -- so nothing
has to be installed into the user's Python environment and there is no PEP 668
venv dance. That also makes it portable to Windows and macOS unchanged, which
is why the manifest can honestly declare all three platforms.

Every tool is read-only: this server sends a latitude and longitude to a weather
API and returns numbers. It reads no user data, touches no filesystem, and takes
no action in the world.
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

TIMEOUT_SECONDS = 20
USER_AGENT = "jarvis-weather-mcp/1.0 (+https://github.com/JarvisOSLinux/mcp-registry)"

# Open-Meteo answers in whatever units it is asked for, so the choice is made
# once here and passed on every call rather than converted afterwards -- a
# conversion this server did itself would be one more thing to get wrong, and
# the API's own unit labels would then disagree with the numbers.
_UNIT_PARAMS = {
    "metric": {},
    "imperial": {
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
    },
}

DEFAULT_UNITS = os.environ.get("WEATHER_UNITS", "metric").strip().lower()
if DEFAULT_UNITS not in _UNIT_PARAMS:
    DEFAULT_UNITS = "metric"

# Optional: lets "what's the weather?" work with no argument at all. Absent is
# fine -- the tools then require a location and say so.
DEFAULT_LOCATION = os.environ.get("WEATHER_DEFAULT_LOCATION", "").strip()

# WMO 4677 present-weather codes, which is what `weather_code` carries. Without
# this the caller gets a bare integer and has to guess; a model guessing what 73
# means is exactly the kind of quiet wrongness worth spending a lookup table on.
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# The European AQI bands (EEA). Reported alongside the number because "63" is
# not actionable on its own and the bands are not linear.
_EAQI_BANDS = (
    (20, "good"),
    (40, "fair"),
    (60, "moderate"),
    (80, "poor"),
    (100, "very poor"),
)


class WeatherError(Exception):
    """Anything the caller could plausibly fix, or needs told plainly."""


_LOCATION_HELP = (
    "Give either `location` (a place name like 'Istanbul' or 'Paris, France') or an "
    "explicit `latitude` and `longitude` pair. A name is geocoded first; when it is "
    "ambiguous the most populous match wins, so call find_location instead if the "
    "user needs to choose between same-named places."
)

_UNITS_HELP = (
    "Units default to the server's configured setting (metric unless WEATHER_UNITS "
    "says otherwise). Pass `units` to override for one call. Every value comes back "
    "with its unit label, so never assume Celsius or km/h -- read the units field."
)


def _http_get_json(url: str, params: dict) -> dict:
    """GET a JSON document, turning every failure into a legible WeatherError."""
    query = urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # Open-Meteo puts the actual complaint in a JSON body on 4xx -- surfacing
        # it is the difference between "bad request" and "latitude must be in
        # range -90 to 90".
        detail = ""
        try:
            payload = json.loads(exc.read())
            detail = payload.get("reason") or ""
        except Exception:
            pass
        raise WeatherError(
            f"Open-Meteo returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise WeatherError(
            f"Could not reach Open-Meteo ({exc.reason}). This server needs outbound "
            f"HTTPS; check the network connection."
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise WeatherError("Open-Meteo returned a response that was not JSON") from exc


def _units_params(units: str | None) -> tuple[str, dict]:
    chosen = (units or DEFAULT_UNITS).strip().lower()
    if chosen not in _UNIT_PARAMS:
        raise WeatherError(
            f"Unknown units {chosen!r}. Use 'metric' or 'imperial'."
        )
    return chosen, _UNIT_PARAMS[chosen]


def _geocode(name: str, count: int = 1) -> list[dict]:
    payload = _http_get_json(
        GEOCODING_URL, {"name": name, "count": count, "format": "json"}
    )
    results = payload.get("results") or []
    if not results:
        raise WeatherError(
            f"No place matched {name!r}. Try a larger nearby city, or add a country "
            f"('Springfield, Illinois')."
        )
    return results


def _place_summary(entry: dict) -> dict:
    """The fields worth carrying forward; the raw geocoding row has ~15 more."""
    return {
        "name": entry.get("name"),
        "country": entry.get("country"),
        "admin1": entry.get("admin1"),
        "latitude": entry.get("latitude"),
        "longitude": entry.get("longitude"),
        "timezone": entry.get("timezone"),
        "population": entry.get("population"),
        "elevation_m": entry.get("elevation"),
    }


def _resolve_location(arguments: dict) -> tuple[float, float, dict | None]:
    """Return (lat, lon, place) from either explicit coordinates or a name.

    Coordinates win when both are given: they are unambiguous, and silently
    geocoding over them would move the answer somewhere the caller did not ask
    about.
    """
    lat = arguments.get("latitude")
    lon = arguments.get("longitude")

    if lat is not None and lon is not None:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            raise WeatherError("latitude and longitude must be numbers")
        if not -90 <= lat_f <= 90:
            raise WeatherError(f"latitude {lat_f} is outside -90..90")
        if not -180 <= lon_f <= 180:
            raise WeatherError(f"longitude {lon_f} is outside -180..180")
        return lat_f, lon_f, None

    if (lat is None) != (lon is None):
        raise WeatherError(
            "latitude and longitude must be given together; pass both, or pass "
            "`location` instead."
        )

    name = (arguments.get("location") or DEFAULT_LOCATION).strip()
    if not name:
        raise WeatherError(
            "No location given and no default is configured. Pass `location` (a place "
            "name) or `latitude` and `longitude`."
        )
    place = _place_summary(_geocode(name)[0])
    return float(place["latitude"]), float(place["longitude"]), place


def _describe(code: Any) -> str | None:
    try:
        return WMO_CODES.get(int(code))
    except (TypeError, ValueError):
        return None


def _located(payload: dict, place: dict | None, lat: float, lon: float) -> dict:
    """The location block every tool echoes, so an answer says where it is for."""
    return {
        "place": place,
        "latitude": payload.get("latitude", lat),
        "longitude": payload.get("longitude", lon),
        "timezone": payload.get("timezone"),
        "elevation_m": payload.get("elevation"),
    }


# --- tools -----------------------------------------------------------------


def tool_find_location(arguments: dict) -> dict:
    name = (arguments.get("name") or "").strip()
    if not name:
        raise WeatherError("`name` is required (a place name to search for)")
    try:
        count = int(arguments.get("limit", 5))
    except (TypeError, ValueError):
        raise WeatherError("`limit` must be a whole number")
    count = max(1, min(count, 20))
    return {"matches": [_place_summary(row) for row in _geocode(name, count)]}


def tool_get_current_weather(arguments: dict) -> dict:
    lat, lon, place = _resolve_location(arguments)
    units, unit_params = _units_params(arguments.get("units"))
    payload = _http_get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "current": [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "is_day",
                "precipitation",
                "weather_code",
                "cloud_cover",
                "wind_speed_10m",
                "wind_direction_10m",
            ],
            "timezone": "auto",
            **unit_params,
        },
    )
    current = payload.get("current") or {}
    return {
        "location": _located(payload, place, lat, lon),
        "units": units,
        "observed_at": current.get("time"),
        "conditions": _describe(current.get("weather_code")),
        "is_daytime": bool(current.get("is_day")),
        "current": current,
        "unit_labels": payload.get("current_units"),
    }


def tool_get_forecast(arguments: dict) -> dict:
    lat, lon, place = _resolve_location(arguments)
    units, unit_params = _units_params(arguments.get("units"))
    try:
        days = int(arguments.get("days", 7))
    except (TypeError, ValueError):
        raise WeatherError("`days` must be a whole number")
    if not 1 <= days <= 16:
        raise WeatherError("`days` must be between 1 and 16 (Open-Meteo's range)")

    payload = _http_get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "daily": [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "apparent_temperature_max",
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
                "uv_index_max",
            ],
            "forecast_days": days,
            "timezone": "auto",
            **unit_params,
        },
    )
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    # Open-Meteo answers in parallel arrays; one row per day is what a caller
    # can actually read back to a user without index arithmetic.
    rows = []
    for i, date in enumerate(dates):
        row = {"date": date}
        for key, values in daily.items():
            if key == "time" or not isinstance(values, list) or i >= len(values):
                continue
            row[key] = values[i]
        row["conditions"] = _describe(row.get("weather_code"))
        rows.append(row)

    return {
        "location": _located(payload, place, lat, lon),
        "units": units,
        "days": rows,
        "unit_labels": payload.get("daily_units"),
    }


def tool_get_hourly_forecast(arguments: dict) -> dict:
    lat, lon, place = _resolve_location(arguments)
    units, unit_params = _units_params(arguments.get("units"))
    try:
        hours = int(arguments.get("hours", 24))
    except (TypeError, ValueError):
        raise WeatherError("`hours` must be a whole number")
    if not 1 <= hours <= 168:
        raise WeatherError("`hours` must be between 1 and 168 (seven days)")

    payload = _http_get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "temperature_2m",
                "apparent_temperature",
                "precipitation_probability",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "relative_humidity_2m",
            ],
            # Ask for whole days and trim: `forecast_hours` counts from midnight
            # local, not from now, so asking for N hours would not answer "the
            # next N hours" near the end of a day.
            "forecast_days": min(8, (hours // 24) + 2),
            "timezone": "auto",
            **unit_params,
        },
    )
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    # Open-Meteo stamps every hourly row in the location's local time and
    # returns whole days, so "the next N hours" means finding now within the
    # rows. Comparing ISO-8601 strings of identical shape orders them correctly
    # without parsing any of them; stepping back one row keeps the hour the
    # user is currently in, which is the one they mean by "now".
    now_local = _local_now_iso(payload)
    start = 0
    for i, stamp in enumerate(times):
        if stamp >= now_local:
            start = max(0, i - 1)
            break

    rows = []
    for i in range(start, min(start + hours, len(times))):
        row = {"time": times[i]}
        for key, values in hourly.items():
            if key == "time" or not isinstance(values, list) or i >= len(values):
                continue
            row[key] = values[i]
        row["conditions"] = _describe(row.get("weather_code"))
        rows.append(row)

    return {
        "location": _located(payload, place, lat, lon),
        "units": units,
        "hours": rows,
        "unit_labels": payload.get("hourly_units"),
    }


def _local_now_iso(payload: dict) -> str:
    """'now' at the queried location, in Open-Meteo's hourly stamp format."""
    offset = payload.get("utc_offset_seconds") or 0
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=offset
    )
    return now.strftime("%Y-%m-%dT%H:%M")


def tool_get_air_quality(arguments: dict) -> dict:
    lat, lon, place = _resolve_location(arguments)
    payload = _http_get_json(
        AIR_QUALITY_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "current": [
                "european_aqi",
                "us_aqi",
                "pm10",
                "pm2_5",
                "ozone",
                "nitrogen_dioxide",
                "sulphur_dioxide",
                "carbon_monoxide",
            ],
            "timezone": "auto",
        },
    )
    current = payload.get("current") or {}
    return {
        "location": _located(payload, place, lat, lon),
        "observed_at": current.get("time"),
        "european_aqi_band": _aqi_band(current.get("european_aqi")),
        "current": current,
        "unit_labels": payload.get("current_units"),
    }


def _aqi_band(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    for ceiling, label in _EAQI_BANDS:
        if number <= ceiling:
            return label
    return "extremely poor"


TOOLS = [
    {
        "name": "find_location",
        "description": (
            "Search for places by name and return their coordinates, country, region, "
            "timezone and population. Use this when a place name is ambiguous and the "
            "user has to choose -- the other tools geocode a name themselves and simply "
            "take the best match, so this is only needed to disambiguate or to confirm "
            "which place was meant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Place name to search for, e.g. 'Springfield' or 'Paris, France'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many matches to return, 1-20. Default 5.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_current_weather",
        "description": (
            "Current conditions at a location: temperature, what it feels like, humidity, "
            "precipitation, cloud cover, wind, and a plain-language description of the "
            "weather code. " + _LOCATION_HELP + " " + _UNITS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Place name. Omit if giving latitude/longitude, or to use the configured default.",
                },
                "latitude": {"type": "number", "description": "Latitude, -90 to 90."},
                "longitude": {"type": "number", "description": "Longitude, -180 to 180."},
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Override the configured unit system for this call.",
                },
            },
        },
    },
    {
        "name": "get_forecast",
        "description": (
            "Daily forecast: high and low temperature, precipitation total and "
            "probability, max wind, UV index, sunrise and sunset, and a plain-language "
            "description per day. Returns one object per day rather than parallel "
            "arrays. Use this for 'tomorrow' or 'this week'. " + _LOCATION_HELP
            + " " + _UNITS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Place name."},
                "latitude": {"type": "number", "description": "Latitude, -90 to 90."},
                "longitude": {"type": "number", "description": "Longitude, -180 to 180."},
                "days": {
                    "type": "integer",
                    "description": "Number of days from today, 1-16. Default 7.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Override the configured unit system for this call.",
                },
            },
        },
    },
    {
        "name": "get_hourly_forecast",
        "description": (
            "Hour-by-hour forecast starting from the current hour at the location: "
            "temperature, apparent temperature, precipitation and its probability, wind, "
            "humidity, and a plain-language description. Use this for 'this afternoon' or "
            "'will it rain later'. " + _LOCATION_HELP + " " + _UNITS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Place name."},
                "latitude": {"type": "number", "description": "Latitude, -90 to 90."},
                "longitude": {"type": "number", "description": "Longitude, -180 to 180."},
                "hours": {
                    "type": "integer",
                    "description": "How many hours ahead, 1-168. Default 24.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Override the configured unit system for this call.",
                },
            },
        },
    },
    {
        "name": "get_air_quality",
        "description": (
            "Current air quality at a location: European and US AQI, PM10, PM2.5, ozone, "
            "nitrogen dioxide, sulphur dioxide and carbon monoxide, plus the European AQI "
            "band in words ('good', 'moderate', 'poor'). The bands are not linear, so "
            "report the band rather than interpreting the number. " + _LOCATION_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Place name."},
                "latitude": {"type": "number", "description": "Latitude, -90 to 90."},
                "longitude": {"type": "number", "description": "Longitude, -180 to 180."},
            },
        },
    },
]

_HANDLERS = {
    "find_location": tool_find_location,
    "get_current_weather": tool_get_current_weather,
    "get_forecast": tool_get_forecast,
    "get_hourly_forecast": tool_get_hourly_forecast,
    "get_air_quality": tool_get_air_quality,
}


def _result(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": False,
    }


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(name: str, arguments: dict) -> dict:
    handler = _HANDLERS[name]
    try:
        return _result(handler(arguments))
    except WeatherError as exc:
        return _error(str(exc))
    except Exception as exc:
        # Same reasoning as the other first-party servers: a bare traceback on
        # stdout is neither legible nor safe, and everything the caller can act
        # on is raised as WeatherError above.
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
            "serverInfo": {"name": "weather-mcp", "version": "1.0.0"},
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
