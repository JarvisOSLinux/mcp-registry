#!/usr/bin/env python3
"""
Wikipedia MCP Server

Search Wikipedia, read an article's summary or its full text by section, and
list what happened on a day in history, in any of Wikipedia's languages. No
account and no API key: Wikimedia serves anonymous reads to a client that
identifies itself (https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits),
which this server does in its User-Agent.

Uses only the Python stdlib, like the weather server, so nothing is installed
into the user's Python environment and the same file runs on Linux, macOS and
Windows.

Every tool is read-only. Wikipedia's text is written by anyone, so what comes
back is third-party content (see the manifest's securityNotes); every answer
carries the article's address and the licence it is published under.
"""

import datetime
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 20
USER_AGENT = "jarvis-wikipedia-mcp/1.0 (https://github.com/JarvisOSLinux/mcp-registry)"
LICENSE = "Text from Wikipedia, CC BY-SA 4.0"

# The language is a subdomain, so it is checked before it goes near a host
# name: letters, digits and hyphens only ("en", "tr", "simple", "zh-yue").
_LANGUAGE = re.compile(r"[a-z][a-z0-9-]{1,15}")
DEFAULT_LANGUAGE = os.environ.get("WIKIPEDIA_LANGUAGE", "en").strip().lower() or "en"

DEFAULT_ARTICLE_CHARS = 8000
MAX_ARTICLE_CHARS = 30000

# Sections the plain-text extract leaves empty or near-empty: their content is
# lists of links and citations, which the extract drops.
_REFERENCE_SECTIONS = {
    "see also",
    "notes",
    "references",
    "citations",
    "sources",
    "bibliography",
    "further reading",
    "external links",
    "footnotes",
}

_ON_THIS_DAY_KINDS = ("selected", "events", "births", "deaths", "holidays")


class WikipediaError(Exception):
    """Anything the caller could plausibly fix, or needs told plainly."""


def _language(arguments: dict) -> str:
    language = str(arguments.get("language") or DEFAULT_LANGUAGE).strip().lower()
    if not _LANGUAGE.fullmatch(language):
        raise WikipediaError(
            f"{language!r} is not a Wikipedia language code. Use one like 'en', 'tr', "
            f"'de' or 'simple'."
        )
    return language


def _http_get_json(url: str, params: dict | None = None) -> Any:
    """GET a JSON document, turning every failure into a legible WikipediaError."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NotFound() from exc
        if exc.code == 429:
            wait = exc.headers.get("Retry-After") or "a minute"
            raise WikipediaError(
                f"Wikipedia is rate-limiting requests; try again in {wait} seconds."
                if wait.isdigit()
                else f"Wikipedia is rate-limiting requests; try again in {wait}."
            ) from exc
        raise WikipediaError(f"Wikipedia returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise WikipediaError(
            f"Could not reach Wikipedia ({exc.reason}). This server needs outbound "
            f"HTTPS; check the network connection, and that the language code exists."
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise WikipediaError("Wikipedia returned a response that was not JSON") from exc


class _NotFound(Exception):
    pass


def _site(language: str) -> str:
    return f"https://{language}.wikipedia.org"


def _plain(fragment: str | None) -> str:
    """Text from the small HTML fragments search excerpts come in."""
    if not fragment:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _article_url(language: str, title: str) -> str:
    return f"{_site(language)}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


def _search(language: str, query: str, limit: int) -> list[dict]:
    payload = _http_get_json(
        f"{_site(language)}/w/rest.php/v1/search/page", {"q": query, "limit": limit}
    )
    return [
        {
            "title": page.get("title"),
            "description": page.get("description"),
            "excerpt": _plain(page.get("excerpt")),
            "url": _article_url(language, page.get("title") or ""),
        }
        for page in payload.get("pages") or []
    ]


def _limit(value: Any, default: int, highest: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise WikipediaError("`limit` must be a whole number.")
    return max(1, min(number, highest))


def _title(arguments: dict) -> str:
    title = str(arguments.get("title") or "").strip()
    if not title:
        raise WikipediaError("`title` is required: an article's title, as search returns it.")
    return title


def tool_search(arguments: dict) -> dict:
    language = _language(arguments)
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise WikipediaError("`query` is required.")
    results = _search(language, query, _limit(arguments.get("limit"), 5, 20))
    return {"language": language, "query": query, "results": results, "license": LICENSE}


def tool_get_summary(arguments: dict) -> dict:
    language = _language(arguments)
    title = _title(arguments)
    try:
        page = _http_get_json(
            f"{_site(language)}/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_"), safe="")
        )
    except _NotFound:
        candidates = _search(language, title, 5)
        if not candidates:
            raise WikipediaError(f"No {language} Wikipedia article matches {title!r}.")
        return {
            "found": False,
            "note": f"No article is titled {title!r}. These are the closest; ask for one by its title.",
            "candidates": candidates,
            "license": LICENSE,
        }

    answer = {
        "title": page.get("title"),
        "description": page.get("description"),
        "summary": page.get("extract"),
        "url": (page.get("content_urls") or {}).get("desktop", {}).get("page")
        or _article_url(language, page.get("title") or title),
        "last_edited": page.get("timestamp"),
        "license": LICENSE,
    }
    if page.get("coordinates"):
        answer["coordinates"] = page["coordinates"]
    if page.get("type") == "disambiguation":
        # "Mercury" is a planet, an element, a god and a band; the page says
        # only that, so the likeliest meanings come from a search instead.
        answer["note"] = (
            f"{title!r} has several meanings. These are the likeliest; ask for one by its title."
        )
        answer["candidates"] = [
            c for c in _search(language, title, 8) if c["title"] != page.get("title")
        ]
    return answer


def _sections(extract: str) -> list[dict]:
    """Split a plain-text extract on its "== Heading ==" lines.

    The lead has no heading. Subsections ("=== … ===") stay inside the section
    they belong to, so asking for "Early life" brings its subsections along.
    """
    sections = [{"heading": None, "level": 1, "text": []}]
    for line in extract.splitlines():
        match = re.fullmatch(r"(={2,6})\s*(.+?)\s*\1", line.strip())
        if match:
            sections.append({"heading": match.group(2), "level": len(match.group(1)), "text": []})
        else:
            sections[-1]["text"].append(line)
    # The extract keeps a pronunciation's punctuation after dropping the
    # pronunciation itself: "Alan Mathison Turing (; 23 June 1912 – …".
    for section in sections:
        section["text"] = re.sub(r"\(\s*[;,]\s*", "(", "\n".join(section["text"])).strip()
    skip_below = None
    for section in sections[1:]:
        if skip_below is not None and section["level"] > skip_below:
            section["reference"] = True
            continue
        skip_below = None
        if section["heading"].lower() in _REFERENCE_SECTIONS:
            section["reference"] = True
            skip_below = section["level"]
    return sections


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    kept = text[: cut if cut > limit // 2 else limit].rstrip()
    # A heading whose section was cut away would promise text that is not there.
    kept = re.sub(r"\n+(={2,6})[^\n]*\1$", "", kept).rstrip()
    return kept, True


def tool_get_article(arguments: dict) -> dict:
    language = _language(arguments)
    title = _title(arguments)
    limit = _limit(arguments.get("max_chars"), DEFAULT_ARTICLE_CHARS, MAX_ARTICLE_CHARS)
    payload = _http_get_json(
        f"{_site(language)}/w/api.php",
        {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "exsectionformat": "wiki",
            "titles": title,
            "redirects": 1,
            "format": "json",
            "formatversion": 2,
        },
    )
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing") or not pages[0].get("extract"):
        raise WikipediaError(
            f"No {language} Wikipedia article is titled {title!r}. Use search to find the exact title."
        )
    page = pages[0]
    sections = _sections(page["extract"])
    contents = [
        ("  " * (s["level"] - 2)) + s["heading"] for s in sections[1:] if not s.get("reference")
    ]

    wanted = str(arguments.get("section") or "").strip().lower()
    if wanted:
        start = next(
            (i for i, s in enumerate(sections) if s["heading"] and s["heading"].lower() == wanted),
            None,
        )
        if start is None:
            raise WikipediaError(
                f"{page['title']!r} has no section {arguments['section']!r}. Its sections: "
                + "; ".join(c.strip() for c in contents)
            )
        level = sections[start]["level"]
        chosen = [sections[start]]
        for s in sections[start + 1 :]:
            if s["level"] <= level:
                break
            chosen.append(s)
        text = "\n\n".join(
            (f"{'=' * s['level']} {s['heading']} {'=' * s['level']}\n" if i else "") + s["text"]
            for i, s in enumerate(chosen)
        ).strip()
    else:
        text = "\n\n".join(
            (f"{'=' * s['level']} {s['heading']} {'=' * s['level']}\n" if s["heading"] else "")
            + s["text"]
            for s in sections
            if not s.get("reference") and (s["text"] or s["heading"])
        ).strip()

    text, clipped = _clip(text, limit)
    answer = {
        "title": page["title"],
        "url": _article_url(language, page["title"]),
        "sections": contents,
        "text": text,
        "license": LICENSE,
    }
    if wanted:
        answer["section"] = arguments["section"]
    if clipped:
        answer["clipped"] = (
            f"Cut at {limit} characters. Ask for one section by name, or raise max_chars "
            f"(up to {MAX_ARTICLE_CHARS})."
        )
    return answer


def tool_on_this_day(arguments: dict) -> dict:
    language = _language(arguments)
    kind = arguments.get("kind") or "selected"
    if kind not in _ON_THIS_DAY_KINDS:
        raise WikipediaError("`kind` must be one of " + ", ".join(_ON_THIS_DAY_KINDS) + ".")
    if arguments.get("date"):
        try:
            day = datetime.date.fromisoformat("2000-" + str(arguments["date"]).strip()[-5:])
        except ValueError:
            raise WikipediaError("`date` must be a month and day as MM-DD (or a full YYYY-MM-DD).")
    else:
        day = datetime.date.today()
    try:
        payload = _http_get_json(
            f"{_site(language)}/api/rest_v1/feed/onthisday/{kind}/{day.month:02d}/{day.day:02d}"
        )
    except _NotFound:
        raise WikipediaError(
            f"The {language} Wikipedia has no 'on this day' feed. It exists in a few "
            f"languages only (English, German, French, Spanish, Turkish and some others); "
            f"try language 'en'."
        )
    items = payload.get(kind) or []
    limit = _limit(arguments.get("limit"), 10, 50)
    return {
        "date": f"{day.month:02d}-{day.day:02d}",
        "kind": kind,
        "items": [
            {
                "year": item.get("year"),
                "text": item.get("text"),
                "articles": [
                    (p.get("titles") or {}).get("normalized") or p.get("title")
                    for p in (item.get("pages") or [])[:3]
                ],
            }
            for item in items[:limit]
        ],
        "total": len(items),
        "license": LICENSE,
    }


_LANGUAGE_HELP = (
    "`language` is a Wikipedia language code ('en', 'tr', 'de', 'simple'); it defaults "
    "to the configured one. Answer in the user's language, but search the Wikipedia "
    "most likely to cover the topic."
)

TOOLS = [
    {
        "name": "search",
        "description": (
            "Search Wikipedia for articles matching a query. Returns titles, one-line "
            "descriptions and a matching excerpt, best match first. Use it to find the "
            "exact title before reading an article. " + _LANGUAGE_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "limit": {"type": "integer", "description": "How many results, 1-20. Default 5."},
                "language": {"type": "string", "description": "Wikipedia language code."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_summary",
        "description": (
            "The lead of a Wikipedia article: a one-line description and the opening "
            "paragraph, with the article's address and when it was last edited. Answers "
            "'who is', 'what is' and 'where is' questions in one call. A title with several "
            "meanings, or no exact match, comes back with the likeliest articles to choose "
            "from. " + _LANGUAGE_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Article title, e.g. 'Alan Turing'."},
                "language": {"type": "string", "description": "Wikipedia language code."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_article",
        "description": (
            "The text of a Wikipedia article, with its list of sections. Long articles are "
            "cut at max_chars (default 8000); ask for one section by name to read just "
            "that part, its subsections included. " + _LANGUAGE_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Article title, as search returns it."},
                "section": {
                    "type": "string",
                    "description": "A section heading from the article's list, e.g. 'Early life'.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"Most characters to return, up to {MAX_ARTICLE_CHARS}. Default {DEFAULT_ARTICLE_CHARS}.",
                },
                "language": {"type": "string", "description": "Wikipedia language code."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "on_this_day",
        "description": (
            "What happened on a day of the year in history, from Wikipedia: selected "
            "events (default), all events, births, deaths or holidays, each with its year "
            "and the articles it links. Defaults to today. Available in a few languages, "
            "English among them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Month and day as MM-DD. Default: today."},
                "kind": {
                    "type": "string",
                    "enum": list(_ON_THIS_DAY_KINDS),
                    "description": "Which list. Default 'selected'.",
                },
                "limit": {"type": "integer", "description": "How many items, 1-50. Default 10."},
                "language": {"type": "string", "description": "Wikipedia language code."},
            },
        },
    },
]

_HANDLERS = {
    "search": tool_search,
    "get_summary": tool_get_summary,
    "get_article": tool_get_article,
    "on_this_day": tool_on_this_day,
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
    except WikipediaError as exc:
        return _error(str(exc))
    except _NotFound:
        return _error("Wikipedia has no page there. Check the title with search.")
    except Exception as exc:
        # A bare traceback on stdout is neither legible nor safe, and everything
        # the caller can act on is raised as WikipediaError above.
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
            "serverInfo": {"name": "wikipedia-mcp", "version": "1.0.0"},
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
