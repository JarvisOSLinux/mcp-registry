#!/usr/bin/env python3
"""selftest_news.py — prove the news server reads feeds right and fetches only what it should.

The server's failure modes: a feed format read wrongly (RSS 1.0's dates, Atom's
links), a feed that declares XML entities parsed anyway, an address the model
supplies fetched as if it were a source, GDELT asked faster than it allows or
in a query form it refuses. Every case answers the server's requests from fake
outlets and a fake GDELT and checks what comes back, and what was asked.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only, writes only to a temp directory (the server's cache).

Usage:
  python3 scripts/selftest_news.py
"""
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "news" / "server.py"
MANIFEST = REPO / "servers" / "news" / "manifest.json"

CASES = []
FAILURES = []
running = "?"

RSS2 = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>BBC</title>
<item><title>Older &amp; wiser</title><description>&lt;p&gt;Some &lt;b&gt;bold&lt;/b&gt; text&lt;/p&gt;</description>
<link>https://bbc.example/1</link><pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Newest from BBC</title><description>Short.</description>
<link>https://bbc.example/2</link><pubDate>Fri, 06 Nov 2026 12:00:00 +0000</pubDate></item>
</channel></rss>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>DW story</title><link>https://dw.example/a</link><description>From DW.</description>
<dc:date>2026-11-06T11:00:00Z</dc:date></item></rdf:RDF>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom story</title><link rel="replies" href="https://verge.example/comments"/>
<link rel="alternate" href="https://verge.example/story"/><updated>2026-11-06T11:30:00Z</updated>
<summary>""" + b"word " * 200 + b"""</summary></entry></feed>"""

ENTITIES = b"""<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>"""


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


class FakeWeb:
    def __init__(self, feeds):
        self.feeds = feeds
        self.requests = []
        self.times = []
        self.gdelt_status = None
        self.gdelt = {"articles": [
            {"url": "https://x.example/a", "title": "Data centers took a beating . A reckoning may be ahead", "seendate": "20261106T140000Z", "domain": "x.example", "language": "English", "sourcecountry": "United States"},
        ]}

    def __call__(self, request, timeout=None):
        self.requests.append(request.full_url)
        self.times.append(time.time())
        url = request.full_url
        if url.startswith("https://api.gdeltproject.org/"):
            if self.gdelt_status:
                raise urllib.error.HTTPError(url, self.gdelt_status, "error", {}, io.BytesIO(b"Please limit requests"))
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if "(" in query["query"]:
                return io.BytesIO(b"Parentheses may only be used around OR'd statements.")
            return io.BytesIO(json.dumps(self.gdelt).encode())
        body = self.feeds.get(url, 404)
        if isinstance(body, int):
            raise urllib.error.HTTPError(url, body, "error", {}, io.BytesIO(b""))
        return io.BytesIO(body)


def load_server(feeds=None, **env):
    cache = tempfile.mkdtemp(prefix="news-selftest-")
    saved = {k: os.environ.get(k) for k in ("XDG_CACHE_HOME", "NEWS_FEEDS", "NEWS_DEFAULT_SOURCES")}
    os.environ["XDG_CACHE_HOME"] = cache
    os.environ.pop("NEWS_FEEDS", None)
    os.environ.pop("NEWS_DEFAULT_SOURCES", None)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("news_under_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    urls = {name: url for name, (_, _, url) in module.ALL_SOURCES.items()}
    fake = FakeWeb({urls[name]: body for name, body in (feeds or {}).items()})
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
def three_feed_formats_read_alike():
    server, _ = load_server({"bbc": RSS2, "dw": RDF, "the-verge": ATOM})
    err, answer = call(server, "get_headlines", {"sources": ["bbc", "dw", "the-verge"], "limit": 10})
    check(not err, "get_headlines answers")
    if err:
        return
    titles = [i["title"] for i in answer["items"]]
    check(titles == ["Newest from BBC", "Atom story", "DW story", "Older & wiser"], f"merged newest first across formats (got {titles})")
    by = {i["title"]: i for i in answer["items"]}
    check(by["DW story"]["published"] == "2026-11-06T11:00:00+00:00", "an RSS 1.0 dc:date is read")
    check(by["Atom story"]["link"] == "https://verge.example/story", "an Atom entry links to its alternate, not its replies")
    check(by["Older & wiser"]["summary"] == "Some bold text", "summaries are plain text")
    check(len(by["Atom story"]["summary"]) <= 301 and by["Atom story"]["summary"].endswith("…"), "long summaries are clipped")


@case
def a_feed_with_entities_is_not_parsed():
    server, _ = load_server({"bbc": ENTITIES})
    err, text = call(server, "get_headlines", {"sources": ["bbc"]})
    check(err and "entities" in text, "a feed declaring XML entities is refused")


@case
def only_named_sources_are_fetched():
    server, fake = load_server({"bbc": RSS2})
    for bad in ("http://localhost:8080/x", "file:///etc/passwd", "nosuch"):
        err, text = call(server, "get_headlines", {"sources": [bad]})
        check(err and "Unknown source" in text, f"{bad!r} is refused")
    check(fake.requests == [], "nothing was fetched for them")

    server, _ = load_server(NEWS_FEEDS="mine=https://feeds.example/rss, bad=file:///etc/passwd, BAD NAME=https://x")
    names = [s["name"] for s in call(server, "list_sources", {})[1]["sources"]]
    check("mine" in names and "bad" not in names and "BAD NAME" not in names, "a feed added in settings must be http(s) and well named")

    server, fake = load_server({"aljazeera": RSS2}, NEWS_DEFAULT_SOURCES="aljazeera")
    err, answer = call(server, "get_headlines", {})
    check(not err and answer["sources"] == ["aljazeera"], "the configured default is read when no source is named")


@case
def one_failing_source_does_not_sink_the_rest():
    server, _ = load_server({"bbc": RSS2, "npr": 503})
    err, answer = call(server, "get_headlines", {"sources": ["bbc", "npr"]})
    check(not err and answer["items"] and "npr" in answer.get("unavailable", {}), "the others answer and the failure is named")
    server, _ = load_server({"npr": 503})
    err, _ = call(server, "get_headlines", {"sources": ["npr"]})
    check(err, "with nothing readable the call fails")


@case
def feeds_are_cached_and_size_capped():
    server, fake = load_server({"bbc": RSS2})
    call(server, "get_headlines", {"sources": ["bbc"]})
    call(server, "get_headlines", {"sources": ["bbc"]})
    check(len(fake.requests) == 1, "a feed read a moment ago is not fetched again")
    server, _ = load_server({"bbc": RSS2})
    server.MAX_BYTES = 100
    err, text = call(server, "get_headlines", {"sources": ["bbc"]})
    check(err and "not read" in text, "an oversized answer is refused")


@case
def gdelt_is_asked_politely_and_read_right():
    server, fake = load_server()
    server.GDELT_SECONDS_BETWEEN_REQUESTS = 0.5
    err, answer = call(server, "search_news", {"query": "artificial intelligence", "language": "english", "days": 400})
    check(not err, "search_news answers a multi-word query")
    if err:
        return
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.requests[-1]).query))
    check(query["query"] == "artificial intelligence sourcelang:english", "words go as given, with the language filter")
    check(query["timespan"] == "90d", "days are capped at GDELT's 90")
    article = answer["articles"][0]
    check(article["title"] == "Data centers took a beating. A reckoning may be ahead", "GDELT's spaced punctuation is closed up")
    check(article["published"] == "2026-11-06T14:00:00+00:00", "the seen date is read")

    again, fake2 = load_server()
    again.CACHE_DIR = server.CACHE_DIR
    again.GDELT_SECONDS_BETWEEN_REQUESTS = 0.5
    call(again, "search_news", {"query": "istanbul"})
    check(fake2.times[0] - fake.times[-1] >= 0.45, "a second process waits GDELT's interval")

    err, text = call(server, "search_news", {"query": "x", "language": "en glish"})
    check(err, "a malformed language is refused")
    server.urllib.request.urlopen = lambda request, timeout=None: io.BytesIO(b"Your query was too short.")
    err, answer = call(server, "search_news", {"query": "x"})
    check(not err and "too short" in answer["from"], "GDELT's plain-text refusal is passed on, with the feeds searched instead")


class _Frozen:
    """The datetime module as seen a month after the fake feeds' stories."""

    import datetime as _real

    timezone = _real.timezone
    timedelta = _real.timedelta

    class datetime(_real.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 12, 6, tzinfo=tz)


@case
def a_blocked_gdelt_falls_back_to_the_feeds_and_is_left_alone():
    server, fake = load_server({"bbc": RSS2, "dw": RDF})
    server.GDELT_SECONDS_BETWEEN_REQUESTS = 0
    fake.gdelt_status = 429
    err, answer = call(server, "search_news", {"query": "DW story"})
    check(not err and [a["title"] for a in answer["articles"]] == ["DW story"], "the feeds' headlines answer, matching every word")
    check(not err and "GDELT was unavailable" in answer["from"], "the answer says where it came from and why")
    asked = sum(1 for u in fake.requests if "gdeltproject" in u)
    call(server, "search_news", {"query": "BBC"})
    check(sum(1 for u in fake.requests if "gdeltproject" in u) == asked, "GDELT is not asked again while it wants a pause")
    err, answer = call(server, "search_news", {"query": "nothing matches this"})
    check(not err and answer["articles"] == [] and "note" in answer, "no match is said plainly")
    server.datetime = _Frozen
    err, answer = call(server, "search_news", {"query": "DW story", "days": 3})
    check(not err and answer["articles"] == [], "a matching story older than the window is left out")
    server.datetime = __import__("datetime")


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
    print("\nOK: news self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
