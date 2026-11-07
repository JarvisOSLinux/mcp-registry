#!/usr/bin/env python3
"""
Currency MCP Server (Frankfurter)

Exchange rates and conversions from the Frankfurter public API: about 160
currencies from central banks and other official sources, plus a few dozen
cryptocurrencies and four precious metals, daily, with history back to 1999 for
most currencies. No account and no API key.

Uses only the Python stdlib, like the weather server, so nothing is installed
into the user's Python environment and the same file runs on Linux, macOS and
Windows.

Every tool is read-only: the server asks a public API for numbers and returns
them. It reads no user data, touches no filesystem, and moves no money.
"""

import datetime
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_URL = "https://api.frankfurter.dev/v2"

TIMEOUT_SECONDS = 20
USER_AGENT = "jarvis-currency-mcp/1.0 (+https://github.com/JarvisOSLinux/mcp-registry)"

# Optional: the currency "how much is 100 euros?" converts into when the caller
# names no target, and the base for rates asked for with none.
HOME_CURRENCY = os.environ.get("CURRENCY_HOME", "").strip()

# A history answered day by day over years would be thousands of points in the
# model's context; past these spans the series is grouped unless the caller
# asked for a grouping.
_DAILY_UP_TO_DAYS = 92
_WEEKLY_UP_TO_DAYS = 731
_MAX_POINTS = 400

MID_MARKET_NOTE = (
    "Mid-market reference rate. Banks, card networks and exchange offices add a "
    "margin, so the amount a user actually gets is usually somewhat less."
)


class CurrencyError(Exception):
    """Anything the caller could plausibly fix, or needs told plainly."""


def _http_get_json(path: str, params: dict | None = None) -> Any:
    """GET a JSON document, turning every failure into a legible CurrencyError."""
    url = f"{API_URL}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("message") or ""
        except Exception:
            pass
        if exc.code == 422 and detail.startswith("invalid currency"):
            code = detail.split(":", 1)[-1].strip()
            raise CurrencyError(
                f"{code!r} is not a currency Frankfurter knows. Pass an ISO code "
                f"(USD, EUR, TRY) or a currency name ('Turkish lira'); "
                f"list_currencies searches by name."
            ) from exc
        if exc.code == 429:
            raise CurrencyError(
                "Frankfurter is rate-limiting requests; try again in a minute."
            ) from exc
        raise CurrencyError(
            f"Frankfurter returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise CurrencyError(
            f"Could not reach Frankfurter ({exc.reason}). This server needs outbound "
            f"HTTPS; check the network connection."
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CurrencyError("Frankfurter returned a response that was not JSON") from exc


_currency_rows: list[dict] | None = None


def _currencies() -> list[dict]:
    global _currency_rows
    if _currency_rows is None:
        _currency_rows = _http_get_json("currencies")
    return _currency_rows


def _named(rows: list[dict], wanted: str) -> list[dict]:
    exact = [r for r in rows if r.get("name", "").lower() == wanted]
    return exact or [r for r in rows if wanted in r.get("name", "").lower()]


def _resolve(text: Any, what: str) -> str:
    """An ISO code from a code or a currency name, checked against the list.

    A name cannot be told from a code by its shape: "yen", "euro", "lira" and
    "yuan" all look like codes, and the API answers an unknown quote with no
    rows rather than an error. So every value is looked up, and "Turkish lira"
    or "yen" works in one call.
    """
    if not isinstance(text, str) or not text.strip():
        raise CurrencyError(f"`{what}` must be a currency code or name.")
    text = text.strip()
    rows = _currencies()
    for row in rows:
        if row.get("iso_code", "").lower() == text.lower():
            return row["iso_code"]
    wanted = text.lower()
    matches = _named(rows, wanted)
    if not matches and wanted.endswith("s"):
        matches = _named(rows, wanted[:-1])
    if len(matches) == 1:
        return matches[0]["iso_code"]
    if not matches:
        raise CurrencyError(
            f"No currency is named like {text!r}. Pass an ISO code, or search with "
            f"list_currencies."
        )
    listed = ", ".join(f"{r['iso_code']} ({r['name']})" for r in matches[:8])
    more = f", and {len(matches) - 8} more" if len(matches) > 8 else ""
    raise CurrencyError(f"{text!r} could mean {listed}{more}. Pass the code.")


def _home(what: str) -> str:
    if not HOME_CURRENCY:
        raise CurrencyError(
            f"No `{what}` given and no home currency is configured (CURRENCY_HOME). "
            f"Pass `{what}`."
        )
    return _resolve(HOME_CURRENCY, "CURRENCY_HOME")


def _date(value: Any, what: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except ValueError:
        raise CurrencyError(f"`{what}` must be a date as YYYY-MM-DD, got {value!r}.")


def _round(value: float) -> float:
    """Two decimals for everyday amounts; significant digits for small ones, so
    0.00129 BTC does not come back as 0.0."""
    if value == 0 or not math.isfinite(value):
        return value
    if abs(value) >= 1:
        return round(value, 2)
    return round(value, 5 - int(math.floor(math.log10(abs(value)))))


def _quotes(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [_resolve(item, "to") for item in items if str(item).strip()]


def _rates(base: str, quotes: list[str], date: datetime.date | None) -> list[dict]:
    params: dict = {"base": base}
    if quotes:
        params["quotes"] = ",".join(quotes)
    if date:
        params["date"] = date.isoformat()
    rows = _http_get_json("rates", params)
    if not rows:
        when = f" on {date.isoformat()}" if date else ""
        raise CurrencyError(
            f"No rate is published for {base}{when}. History for most currencies starts "
            f"in 1999 (a few go back to 1948, and cryptocurrencies start later); a date "
            f"in the future has no rate."
        )
    return rows


def tool_convert(arguments: dict) -> dict:
    try:
        amount = float(arguments.get("amount", 1))
    except (TypeError, ValueError):
        raise CurrencyError("`amount` must be a number.")
    if not math.isfinite(amount):
        raise CurrencyError("`amount` must be a finite number.")

    base = (
        _resolve(arguments["from"], "from") if arguments.get("from") else _home("from")
    )
    targets = _quotes(arguments.get("to")) or [_home("to")]
    date = _date(arguments["date"], "date") if arguments.get("date") else None

    rows = {r["quote"]: r for r in _rates(base, targets, date)}
    results = []
    for code in targets:
        row = rows.get(code)
        if row is None:
            results.append({"to": code, "error": f"no {base}->{code} rate published"})
            continue
        results.append(
            {
                "to": code,
                "amount": _round(amount * row["rate"]),
                "rate": row["rate"],
                "rate_date": row["date"],
            }
        )
    answer = {"from": base, "amount": amount, "results": results, "note": MID_MARKET_NOTE}
    if date:
        answer["requested_date"] = date.isoformat()
    return answer


def tool_get_rates(arguments: dict) -> dict:
    base = (
        _resolve(arguments["base"], "base") if arguments.get("base") else _home("base")
    )
    quotes = [_resolve(q, "quotes") for q in (arguments.get("quotes") or [])]
    date = _date(arguments["date"], "date") if arguments.get("date") else None
    rows = _rates(base, quotes, date)
    answer = {
        "base": base,
        "rates": {r["quote"]: {"rate": r["rate"], "date": r["date"]} for r in rows},
        "note": MID_MARKET_NOTE,
    }
    if date:
        answer["requested_date"] = date.isoformat()
    return answer


def _grouping(days: int, asked: str | None) -> str:
    if asked:
        if asked not in ("day", "week", "month"):
            raise CurrencyError("`group` must be 'day', 'week' or 'month'.")
        return asked
    if days <= _DAILY_UP_TO_DAYS:
        return "day"
    if days <= _WEEKLY_UP_TO_DAYS:
        return "week"
    return "month"


def tool_get_rate_history(arguments: dict) -> dict:
    base = _resolve(arguments.get("base"), "base")
    quote = (
        _resolve(arguments["quote"], "quote") if arguments.get("quote") else _home("quote")
    )
    end = _date(arguments["end"], "end") if arguments.get("end") else datetime.date.today()
    start = (
        _date(arguments["start"], "start")
        if arguments.get("start")
        else end - datetime.timedelta(days=30)
    )
    if start > end:
        raise CurrencyError("`start` must not be after `end`.")
    group = _grouping((end - start).days, arguments.get("group"))

    params = {"base": base, "quotes": quote, "from": start.isoformat(), "to": end.isoformat()}
    if group != "day":
        params["group"] = group
    rows = _http_get_json("rates", params)
    series = [{"date": r["date"], "rate": r["rate"]} for r in rows if r.get("quote") == quote]
    if not series:
        raise CurrencyError(
            f"No {base}->{quote} rates are published between {start} and {end}."
        )

    first, last = series[0], series[-1]
    low = min(series, key=lambda p: p["rate"])
    high = max(series, key=lambda p: p["rate"])
    change = last["rate"] - first["rate"]
    answer = {
        "base": base,
        "quote": quote,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "group": group,
        "summary": {
            "first": first,
            "last": last,
            "change": _round(change),
            "change_percent": round(100 * change / first["rate"], 2) if first["rate"] else None,
            "low": low,
            "high": high,
        },
        "series": series,
    }
    if len(series) > _MAX_POINTS:
        answer["series"] = series[-_MAX_POINTS:]
        answer["series_truncated"] = (
            f"Showing the last {_MAX_POINTS} of {len(series)} points; the summary covers "
            f"all of them. Ask with group 'week' or 'month' for the whole span."
        )
    return answer


def tool_list_currencies(arguments: dict) -> dict:
    search = str(arguments.get("search") or "").strip().lower()
    kind = arguments.get("type")
    if kind and kind not in ("currency", "crypto", "metal"):
        raise CurrencyError("`type` must be 'currency', 'crypto' or 'metal'.")
    rows = _currencies()
    if kind:
        rows = [r for r in rows if r.get("type") == kind]
    if search:
        rows = [
            r
            for r in rows
            if search in r.get("name", "").lower() or search == r.get("iso_code", "").lower()
        ]
    return {
        "count": len(rows),
        "currencies": [
            {
                "code": r["iso_code"],
                "name": r.get("name"),
                "symbol": r.get("symbol"),
                "type": r.get("type"),
                "history": f"{r.get('start_date')} to {r.get('end_date')}",
            }
            for r in rows
        ],
    }


_CODE_HELP = (
    "Currencies may be ISO codes (USD, EUR, TRY, JPY), cryptocurrency codes (BTC, "
    "ETH), metals (XAU gold, XAG silver, per troy ounce) or a currency's name "
    "('Turkish lira', 'yen')."
)

TOOLS = [
    {
        "name": "convert",
        "description": (
            "Convert an amount of money from one currency into one or more others at "
            "the latest daily rate, or at a past date's rate. Returns each converted "
            "amount with the rate and the date it was published (rates are daily, so a "
            "weekend or holiday gives the last published one). " + _CODE_HELP + " "
            "Mid-market rates: say so when it matters, since a bank or card gives less."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "How much to convert. Default 1."},
                "from": {
                    "type": "string",
                    "description": "Currency to convert from. Omit to use the configured home currency.",
                },
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Currencies to convert into. Omit to use the configured home currency.",
                },
                "date": {
                    "type": "string",
                    "description": "A past date (YYYY-MM-DD) to convert at that day's rate. Omit for the latest.",
                },
            },
        },
    },
    {
        "name": "get_rates",
        "description": (
            "Exchange rates for one base currency: the rate of each quote currency per "
            "one unit of the base, latest or on a past date. Omit `quotes` for every "
            "currency at once. " + _CODE_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {
                    "type": "string",
                    "description": "Base currency. Omit to use the configured home currency.",
                },
                "quotes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Currencies to quote against the base. Omit for all.",
                },
                "date": {"type": "string", "description": "A past date (YYYY-MM-DD). Omit for the latest."},
            },
        },
    },
    {
        "name": "get_rate_history",
        "description": (
            "How one currency moved against another over a period: a summary (first and "
            "last rate, change and percent change, the low and the high with their "
            "dates) and the series itself. Defaults to the last 30 days. Long spans are "
            "grouped by week (over three months) or month (over two years) unless "
            "`group` says otherwise. " + _CODE_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "The currency whose value is tracked."},
                "quote": {
                    "type": "string",
                    "description": "The currency it is priced in. Omit to use the configured home currency.",
                },
                "start": {"type": "string", "description": "First date, YYYY-MM-DD. Default: 30 days before `end`."},
                "end": {"type": "string", "description": "Last date, YYYY-MM-DD. Default: today."},
                "group": {
                    "type": "string",
                    "enum": ["day", "week", "month"],
                    "description": "One point per day, week or month.",
                },
            },
            "required": ["base"],
        },
    },
    {
        "name": "list_currencies",
        "description": (
            "The currencies, cryptocurrencies and metals with rates, with their codes, "
            "names, symbols and how far back their history goes. Search by name or "
            "code to find a code ('peso' lists every peso)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Part of a name, or a code."},
                "type": {
                    "type": "string",
                    "enum": ["currency", "crypto", "metal"],
                    "description": "Only this kind.",
                },
            },
        },
    },
]

_HANDLERS = {
    "convert": tool_convert,
    "get_rates": tool_get_rates,
    "get_rate_history": tool_get_rate_history,
    "list_currencies": tool_list_currencies,
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
    except CurrencyError as exc:
        return _error(str(exc))
    except Exception as exc:
        # A bare traceback on stdout is neither legible nor safe, and everything
        # the caller can act on is raised as CurrencyError above.
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
            "serverInfo": {"name": "currency-mcp", "version": "1.0.0"},
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
