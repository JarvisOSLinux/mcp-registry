#!/usr/bin/env python3
"""selftest_currency.py — prove the currency server answers what was asked.

The server's failure mode is a plausible wrong number: a currency name read as
some other code, a small amount rounded to zero, a history summary taken from
the wrong end of the series. So every case answers the server's requests from
a fake Frankfurter with known rates and checks what comes back, including the
cases that must refuse (an ambiguous name, a date with no rate).

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only, writes nothing.

Usage:
  python3 scripts/selftest_currency.py
"""
import datetime
import importlib.util
import json
import os
import pathlib
import sys

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "currency" / "server.py"
MANIFEST = REPO / "servers" / "currency" / "manifest.json"

CASES = []
FAILURES = []
running = "?"

CURRENCIES = [
    {"iso_code": "USD", "name": "US Dollar", "symbol": "$", "type": "currency"},
    {"iso_code": "CAD", "name": "Canadian Dollar", "symbol": "$", "type": "currency"},
    {"iso_code": "EUR", "name": "Euro", "symbol": "€", "type": "currency"},
    {"iso_code": "TRY", "name": "Turkish Lira", "symbol": "₺", "type": "currency"},
    {"iso_code": "JPY", "name": "Japanese Yen", "symbol": "¥", "type": "currency"},
    {"iso_code": "BTC", "name": "Bitcoin", "symbol": "₿", "type": "crypto"},
    {"iso_code": "XAU", "name": "Gold (Troy Ounce)", "symbol": "oz t", "type": "metal"},
]
PER_USD = {"USD": 1.0, "CAD": 1.4, "EUR": 0.9, "TRY": 50.0, "JPY": 150.0, "BTC": 0.0000129, "XAU": 0.00025}


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


class FakeFrankfurter:
    """Answers the two paths the server uses from the tables above."""

    def __init__(self, series_days=None):
        self.calls = []
        self.series_days = series_days

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "currencies":
            return CURRENCIES
        assert path == "rates", path
        params = params or {}
        base = params["base"]
        quotes = params["quotes"].split(",") if "quotes" in params else list(PER_USD)
        if params.get("date", "") > "2026-11-06":
            return []
        if "from" in params:
            start = datetime.date.fromisoformat(params["from"])
            end = datetime.date.fromisoformat(params["to"])
            step = {"week": 7, "month": 30}.get(params.get("group"), 1)
            rows, day, i = [], start, 0
            while day <= end:
                rows.append({"date": day.isoformat(), "base": base, "quote": quotes[0], "rate": 40.0 + i})
                day += datetime.timedelta(days=step)
                i += 1
            # A dip in the middle, so low is not simply the first point.
            if len(rows) > 2:
                rows[len(rows) // 2]["rate"] = 1.0
            return rows
        date = params.get("date", "2026-11-06")
        return [
            {"date": date, "base": base, "quote": q, "rate": PER_USD[q] / PER_USD[base]}
            for q in quotes
            if q in PER_USD and q != base
        ]


def load_server(fake, home=""):
    spec = importlib.util.spec_from_file_location("currency_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._http_get_json = fake
    module.HOME_CURRENCY = home
    return module


def call(module, tool, arguments):
    reply = module._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    result = reply["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


@case
def names_resolve_to_codes():
    fake = FakeFrankfurter()
    server = load_server(fake)
    err, answer = call(server, "convert", {"amount": 100, "from": "Turkish lira", "to": ["yen", "euros", "usd"]})
    check(not err, "a name, a code-shaped name, a plural and a lower-case code all resolve")
    if not err:
        check(answer["from"] == "TRY", "'Turkish lira' is TRY")
        check([r["to"] for r in answer["results"]] == ["JPY", "EUR", "USD"], "'yen' is JPY, 'euros' EUR, 'usd' USD")
    check(sum(1 for c in fake.calls if c[0] == "currencies") == 1, "the currency list is fetched once per call")

    err, text = call(load_server(FakeFrankfurter()), "convert", {"from": "dollar", "to": ["EUR"]})
    check(err and "USD" in text and "CAD" in text, "an ambiguous name is refused, naming the candidates")
    err, text = call(load_server(FakeFrankfurter()), "convert", {"from": "XYZ", "to": ["EUR"]})
    check(err and "list_currencies" in text, "an unknown currency is refused, pointing at list_currencies")


@case
def amounts_keep_their_digits():
    server = load_server(FakeFrankfurter())
    err, answer = call(server, "convert", {"amount": 250, "from": "USD", "to": ["BTC", "JPY", "EUR"]})
    check(not err, "convert answers")
    if not err:
        got = {r["to"]: r["amount"] for r in answer["results"]}
        check(got["BTC"] == 0.003225, f"a small amount keeps significant digits (got {got['BTC']})")
        check(got["JPY"] == 37500.0, "a large amount has two decimals")
        check(got["EUR"] == 225.0, "the rate is applied to the amount")
        check("Mid-market" in answer["note"], "every conversion says it is a mid-market rate")


@case
def home_currency_fills_in():
    server = load_server(FakeFrankfurter(), home="Turkish Lira")
    err, answer = call(server, "convert", {"amount": 10, "from": "EUR"})
    check(not err and answer["results"][0]["to"] == "TRY", "with no target, the home currency is used")
    err, text = call(load_server(FakeFrankfurter()), "convert", {"amount": 10, "from": "EUR"})
    check(err and "CURRENCY_HOME" in text, "with no target and no home currency, the call says so")


@case
def a_date_without_a_rate_is_refused():
    server = load_server(FakeFrankfurter())
    err, text = call(server, "get_rates", {"base": "USD", "date": "2031-01-01"})
    check(err and "future" in text, "a date with no published rate is refused, not answered empty")
    err, text = call(server, "get_rates", {"base": "USD", "date": "next week"})
    check(err and "YYYY-MM-DD" in text, "a date that is not ISO is refused")


@case
def history_is_grouped_and_summarised():
    for days, expected in ((30, "day"), (400, "week"), (1000, "month")):
        fake = FakeFrankfurter()
        server = load_server(fake)
        end = datetime.date(2026, 11, 1)
        err, answer = call(
            server,
            "get_rate_history",
            {"base": "USD", "quote": "TRY", "start": (end - datetime.timedelta(days=days)).isoformat(), "end": end.isoformat()},
        )
        sent = [c[1] for c in fake.calls if c[0] == "rates"][0]
        check(not err and answer["group"] == expected, f"{days} days are grouped by {expected}")
        check(sent.get("group") == (None if expected == "day" else expected), f"the API is asked for group={expected}")
        if not err:
            s = answer["summary"]
            series = answer["series"]
            check(s["first"] == series[0] and s["last"] == series[-1], "first and last come from the ends of the series")
            check(s["low"]["rate"] == 1.0, "the low is the series minimum, wherever it falls")
            check(s["change"] == round(series[-1]["rate"] - series[0]["rate"], 2), "the change is last minus first")

    server = load_server(FakeFrankfurter())
    err, answer = call(
        server, "get_rate_history", {"base": "USD", "quote": "TRY", "start": "2024-01-01", "end": "2026-01-01", "group": "day"}
    )
    check(not err and len(answer["series"]) == 400 and "series_truncated" in answer, "a daily series over 400 points is cut, and says so")
    err, text = call(server, "get_rate_history", {"base": "USD", "quote": "TRY", "start": "2026-02-01", "end": "2026-01-01"})
    check(err and "after" in text, "a start after the end is refused")


@case
def tools_match_the_manifest():
    server = load_server(FakeFrankfurter())
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

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        # A case that raises is a failure, not the end of the run: a crash in
        # one would otherwise hide every result after it.
        try:
            fn()
        except Exception as exc:
            check(False, f"raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: currency self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
