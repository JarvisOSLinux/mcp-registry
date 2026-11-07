#!/usr/bin/env python3
"""selftest_wikipedia.py — prove the Wikipedia server reads articles correctly.

The server's failure modes are quiet: a language code that becomes someone
else's host name, an article cut mid-sentence under a heading whose text is
gone, a disambiguation page answered as if it were the article, a reference
list passed off as a section. Every case answers the server's requests from a
fake Wikipedia with known pages and checks what comes back.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only, writes nothing.

Usage:
  python3 scripts/selftest_wikipedia.py
"""
import importlib.util
import json
import os
import pathlib
import sys

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "wikipedia" / "server.py"
MANIFEST = REPO / "servers" / "wikipedia" / "manifest.json"

CASES = []
FAILURES = []
running = "?"

ARTICLE = """Ada Lovelace (; 10 December 1815 – 27 November 1852) was an English mathematician.

== Early life ==
She was born in London.

=== Education ===
She was tutored in mathematics.

== Work ==
She wrote the first published algorithm.

== See also ==

=== Related people ===
Charles Babbage

== References =="""


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


class FakeWikipedia:
    def __init__(self, server):
        self.server = server
        self.urls = []

    def __call__(self, url, params=None):
        self.urls.append(url)
        params = params or {}
        if "/rest.php/v1/search/page" in url:
            return {
                "pages": [
                    {"title": "Mercury (planet)", "description": "First planet", "excerpt": "the <span class=\"searchmatch\">planet</span> &amp; its orbit"},
                    {"title": "Mercury", "description": "Topics", "excerpt": ""},
                    {"title": "Mercury (element)", "description": "Element 80", "excerpt": ""},
                ]
            }
        if "/page/summary/" in url:
            title = url.rsplit("/", 1)[-1]
            if title == "Mercury":
                return {"type": "disambiguation", "title": "Mercury", "extract": "Mercury may refer to:"}
            if title == "Nowhere":
                raise self.server._NotFound()
            return {"type": "standard", "title": title.replace("_", " "), "extract": "Lead.", "description": "Someone"}
        if url.endswith("/w/api.php"):
            if params.get("titles") == "Missing":
                return {"query": {"pages": [{"title": "Missing", "missing": True}]}}
            return {"query": {"pages": [{"title": "Ada Lovelace", "extract": ARTICLE}]}}
        if "/feed/onthisday/" in url:
            if url.startswith("https://fi."):
                raise self.server._NotFound()
            kind = url.split("/onthisday/")[1].split("/")[0]
            return {kind: [{"year": 1900 + i, "text": f"Item {i}", "pages": [{"titles": {"normalized": f"Page {i}"}}]} for i in range(30)]}
        raise AssertionError(f"unexpected request {url}")


def load_server():
    spec = importlib.util.spec_from_file_location("wikipedia_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = FakeWikipedia(module)
    module._http_get_json = fake
    return module, fake


def call(module, tool, arguments):
    reply = module._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    result = reply["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


@case
def a_language_cannot_name_another_host():
    for bad in ("evil.com/", "en.evil", "en/../x", "e", "en@x"):
        server, fake = load_server()
        err, _ = call(server, "search", {"query": "x", "language": bad})
        check(err and not fake.urls, f"{bad!r} is refused before any request")
    server, fake = load_server()
    err, _ = call(server, "search", {"query": "x", "language": "Simple"})
    check(not err and fake.urls[0].startswith("https://simple.wikipedia.org/"), "'Simple' reads simple.wikipedia.org")


@case
def search_excerpts_are_plain_text():
    server, _ = load_server()
    err, answer = call(server, "search", {"query": "mercury"})
    check(not err and answer["results"][0]["excerpt"] == "the planet & its orbit", "tags are stripped and entities unescaped")
    check(not err and "CC BY-SA" in answer["license"], "every answer names the licence")


@case
def several_meanings_come_back_as_choices():
    server, _ = load_server()
    err, answer = call(server, "get_summary", {"title": "Mercury"})
    titles = [c["title"] for c in answer.get("candidates", [])] if not err else []
    check(not err and "several meanings" in answer.get("note", ""), "a disambiguation page says so")
    check("Mercury (planet)" in titles and "Mercury" not in titles, "the choices exclude the disambiguation page itself")
    err, answer = call(server, "get_summary", {"title": "Nowhere"})
    check(not err and answer["found"] is False and answer["candidates"], "a missing title offers the closest articles")


@case
def articles_split_into_sections():
    server, _ = load_server()
    err, answer = call(server, "get_article", {"title": "Ada Lovelace"})
    check(not err, "get_article answers")
    if err:
        return
    check(answer["sections"] == ["Early life", "  Education", "Work"], f"reference sections and their subsections are left out (got {answer['sections']})")
    check("(10 December 1815" in answer["text"], "a dropped pronunciation leaves no '(;'")
    check("Charles Babbage" not in answer["text"], "a reference section's subsection text is left out")

    err, answer = call(server, "get_article", {"title": "Ada Lovelace", "section": "early life"})
    check(not err and "tutored" in answer["text"] and "algorithm" not in answer["text"], "a section brings its subsections and stops at the next section")
    err, text = call(server, "get_article", {"title": "Ada Lovelace", "section": "Death"})
    check(err and "Early life" in text, "an unknown section is refused, listing the sections")
    err, text = call(server, "get_article", {"title": "Missing"})
    check(err and "search" in text, "a missing article points at search")


@case
def a_cut_article_says_so_and_ends_cleanly():
    server, _ = load_server()
    text, clipped = server._clip("Lead paragraph.\n\nMore lead text here.\n\n== Work ==\nShe wrote the first algorithm.", 50)
    check(clipped and not text.rstrip().endswith("=="), f"no heading is left without its text (got {text!r})")
    _, clipped = server._clip("short", 50)
    check(not clipped, "a short text is not marked as cut")


@case
def on_this_day_reads_dates_and_languages():
    server, fake = load_server()
    err, answer = call(server, "on_this_day", {"date": "2024-06-23", "kind": "births", "limit": 3})
    check(not err and answer["date"] == "06-23" and len(answer["items"]) == 3 and answer["total"] == 30, "a full date is read as its month and day, and limit applies")
    check(fake.urls[-1].endswith("/onthisday/births/06/23"), "the feed is asked for that day and kind")
    err, _ = call(server, "on_this_day", {"date": "23 June"})
    check(err, "a date that is not MM-DD is refused")
    err, text = call(server, "on_this_day", {"language": "fi"})
    check(err and "'en'" in text, "a language without the feed says so")


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
    print("\nOK: wikipedia self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
