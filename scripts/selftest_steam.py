#!/usr/bin/env python3
"""selftest_steam.py — prove the Steam server never buys, leaks no key, and reads Steam's files right.

The server's failure modes are quiet: the Web API key rides in every
api.steampowered.com URL, so an error that echoes the URL hands it to the
model; a redirect could carry a request somewhere else; install_game must
never become a purchase; uninstalling a game without Steam Cloud can take its
saves; and Steam's own files (libraryfolders.vdf, appmanifest_*.acf,
loginusers.vdf) hold the login name, which must never be read out. So every
case runs the real code against a fake Steam (the opener replaced) and a fake
Steam folder, with steam:// launches captured instead of opened.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only; writes only to temporary directories it removes.

Usage:
  python3 scripts/selftest_steam.py
"""
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "steam" / "server.py"
MANIFEST = REPO / "servers" / "steam" / "manifest.json"

KEY = "0123456789ABCDEF0123456789ABCDEF"
ME = "76561198000000001"
ALICE = "76561198000000002"
BOB = "76561198000000003"
NOW = int(time.time())

CASES = []
FAILURES = []
running = "?"


def case(fn):
    CASES.append(fn)
    return fn


def check(condition, description):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        FAILURES.append(f"{running}: {description}")


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def app(appid, name, *, free=False, price=1999, cloud=True, multiplayer=False):
    categories = [{"id": 2, "description": "Single-player"}]
    if cloud:
        categories.append({"id": 23, "description": "Steam Cloud"})
    if multiplayer:
        categories.append({"id": 38, "description": "Online Co-op"})
    data = {
        "type": "game",
        "name": name,
        "steam_appid": appid,
        "is_free": free,
        "short_description": "<b>Bold</b> claims &amp; more",
        "categories": categories,
        "platforms": {"windows": True, "linux": False},
        "pc_requirements": {"minimum": "<strong>Storage:</strong> 12 GB available space"},
        "release_date": {"coming_soon": False, "date": "Jan 1, 2020"},
    }
    if not free:
        data["price_overview"] = {"currency": "USD", "final_formatted": f"${price / 100:.2f}", "discount_percent": 0}
    return data


class FakeSteam:
    """Stands in for the pinned opener: answers by path, records every URL."""

    def __init__(self):
        self.urls = []
        self.apps = {
            100: app(100, "Detroit: Become Human"),
            200: app(200, "Hades", multiplayer=False),
            300: app(300, "Deep Rock Galactic", multiplayer=True),
            400: app(400, "Free Thing", free=True),
            500: app(500, "Saveless Game", cloud=False),
            600: app(600, "Paid Unowned"),
        }
        self.owned = {
            ME: [
                {"appid": 100, "name": "Detroit: Become Human", "playtime_forever": 600},
                {"appid": 300, "name": "Deep Rock Galactic", "playtime_forever": 1200, "rtime_last_played": NOW - 86400},
                {"appid": 200, "name": "Hades", "playtime_forever": 0},
                {"appid": 500, "name": "Saveless Game", "playtime_forever": 30},
            ],
            ALICE: [
                {"appid": 300, "name": "Deep Rock Galactic", "playtime_forever": 900},
                {"appid": 200, "name": "Hades", "playtime_forever": 300},
            ],
            BOB: None,  # private game details
        }
        self.fail = {}
        self.my_visibility = 3

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        path = parts.path
        if path in self.fail:
            kind, payload = self.fail[path]
            if kind == "urlerror":
                raise urllib.error.URLError(f"connection reset while fetching {url}")
            raise urllib.error.HTTPError(url, kind, "error", {}, io.BytesIO(json.dumps(payload).encode()))
        if path == "/api/storesearch/":
            term = params["term"].casefold()
            hits = [
                {"type": "app", "id": a, "name": d["name"], "price": {"currency": "USD", "initial": 2999, "final": 1499},
                 "platforms": {"linux": False}}
                for a, d in self.apps.items() if term in d["name"].casefold()
            ]
            return FakeResponse({"total": len(hits), "items": hits})
        if path == "/api/appdetails":
            appid = int(params["appids"])
            data = self.apps.get(appid)
            return FakeResponse({str(appid): {"success": bool(data), **({"data": data} if data else {})}})
        if path == "/IPlayerService/GetOwnedGames/v1/":
            games = self.owned.get(params["steamid"])
            return FakeResponse({"response": {} if games is None else {"game_count": len(games), "games": games}})
        if path == "/ISteamUser/GetFriendList/v1/":
            return FakeResponse({"friendslist": {"friends": [{"steamid": ALICE}, {"steamid": BOB}]}})
        if path == "/ISteamUser/GetPlayerSummaries/v2/":
            players = [
                {"steamid": ME, "personaname": "Me", "personastate": 1, "communityvisibilitystate": self.my_visibility},
                {"steamid": ALICE, "personaname": "Alice <3", "personastate": 6, "gameextrainfo": "Deep Rock Galactic", "gameid": "300"},
                {"steamid": BOB, "personaname": "Bob", "personastate": 0, "lastlogoff": NOW - 3600},
            ]
            return FakeResponse({"response": {"players": [p for p in players if p["steamid"] in params["steamids"]]}})
        if path == "/ISteamUser/ResolveVanityURL/v1/":
            return FakeResponse({"response": {"success": 1, "steamid": ME}})
        if path == "/IWishlistService/GetWishlist/v1/":
            return FakeResponse({"response": {"items": [
                {"appid": 600, "priority": 2, "date_added": NOW - 100},
                {"appid": 200, "priority": 1, "date_added": NOW - 200},
            ]}})
        if path == "/IStoreBrowseService/GetItems/v1/":
            wanted = [i["appid"] for i in json.loads(params["input_json"])["ids"]]
            items = []
            for appid in wanted:
                item = {"appid": appid, "name": self.apps.get(appid, {}).get("name", f"App {appid}"),
                        "platforms": {"steamos_linux": appid == 300, "steam_deck_compat_category": 3}}
                if appid == 600:
                    item["best_purchase_option"] = {"formatted_final_price": "$9.99", "formatted_original_price": "$19.99",
                                                    "discount_pct": 50, "active_discounts": [{"discount_end_date": NOW + 86400}]}
                else:
                    item["best_purchase_option"] = {"formatted_final_price": "$24.99"}
                items.append(item)
            return FakeResponse({"response": {"store_items": items}})
        if path.startswith("/api/v1/reports/summaries/"):
            appid = int(path.rsplit("/", 1)[1].split(".")[0])
            if appid == 200:
                return FakeResponse({"tier": "platinum", "trendingTier": "platinum", "total": 50, "confidence": "strong"})
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))
        if path == "/ISteamNews/GetNewsForApp/v2/":
            return FakeResponse({"appnews": {"newsitems": [{
                "title": "Patch 1.2", "date": NOW, "feedlabel": "Community Announcements",
                "contents": "[h1]Fixes[/h1][list][*]Crash fixed[/list]<br>See <a href='x'>notes</a>", "url": "https://x"}]}})
        if path == "/ISteamUserStats/GetNumberOfCurrentPlayers/v1/":
            return FakeResponse({"response": {"player_count": 1234, "result": 1}})
        if path == "/api/featuredcategories":
            special = {"id": 300, "name": "Deep Rock Galactic", "discounted": True, "discount_percent": 60,
                       "original_price": 2999, "final_price": 1199, "currency": "USD", "linux_available": True,
                       "discount_expiration": NOW + 3600}
            return FakeResponse({"specials": {"items": [special, special]}})
        raise AssertionError(f"unexpected request {path}")


def vdf_app(appid, name, flags, size, to_download=0, downloaded=0, last_played=0):
    return (
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"StateFlags"\t\t"{flags}"\n'
        f'\t"SizeOnDisk"\t\t"{size}"\n\t"BytesToDownload"\t\t"{to_download}"\n'
        f'\t"BytesDownloaded"\t\t"{downloaded}"\n\t"LastPlayed"\t\t"{last_played}"\n}}\n'
    )


class Sandbox:
    def __init__(self, fake=None, key=KEY, steam_id=""):
        self.fake = fake or FakeSteam()
        self.key = key
        self.steam_id = steam_id

    def __enter__(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-steam-"))
        root = self.tmp / "Steam"
        second = self.tmp / "Games"
        (root / "steamapps").mkdir(parents=True)
        (root / "config").mkdir()
        (second / "steamapps").mkdir(parents=True)
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
            % (root, second)
        )
        (root / "steamapps" / "appmanifest_100.acf").write_text(
            vdf_app(100, "Detroit: Become Human", 4, 63_000_000_000, last_played=NOW - 86400)
        )
        (root / "steamapps" / "appmanifest_1493710.acf").write_text(vdf_app(1493710, "Proton Experimental", 4, 1_400_000_000))
        (second / "steamapps" / "appmanifest_500.acf").write_text(vdf_app(500, "Saveless Game", 4, 2_000_000_000))
        (second / "steamapps" / "appmanifest_300.acf").write_text(
            vdf_app(300, "Deep Rock Galactic", 1026 | 1048576, 1_000_000_000, 4_000_000_000, 2_000_000_000)
        )
        (root / "config" / "loginusers.vdf").write_text(
            '"users"\n{\n\t"76561198000000009"\n\t{\n\t\t"AccountName"\t\t"oldlogin"\n\t\t"MostRecent"\t\t"0"\n\t\t"Timestamp"\t\t"1"\n\t}\n'
            f'\t"{ME}"\n\t{{\n\t\t"AccountName"\t\t"secretlogin"\n\t\t"PersonaName"\t\t"Yak"\n\t\t"MostRecent"\t\t"1"\n\t\t"Timestamp"\t\t"2"\n\t}}\n}}\n'
        )
        spec = importlib.util.spec_from_file_location("steam_under_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.API_KEY = self.key
        module.STEAM_ID_SETTING = self.steam_id
        module.COUNTRY_SETTING = "US"
        module.STEAM_PATH_SETTING = str(root)
        module.CACHE_DIR = None
        module._open = self.fake
        module._steam_running = lambda: True
        self.launched = []
        module._launch = self.launched.append
        self.module = module
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def call(self, tool, arguments=None):
        reply = self.module._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
        )
        result = reply["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text)), text


@case
def tools_match_the_manifest():
    with Sandbox() as box:
        listed = box.module._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    declared = {t["name"]: t["threat_level"] for t in json.loads(MANIFEST.read_text())["tools"]}
    check(sorted(t["name"] for t in listed) == sorted(declared), "the server offers exactly the tools its manifest declares")
    check(declared.get("uninstall_game") == "dangerous", "uninstalling is dangerous: saves can go with the game")
    check(
        all(declared.get(t) == "elevated" for t in ("friends", "games_in_common", "install_game", "play_game")),
        "other people's activity and acting on this computer ask first",
    )
    check(not any("buy" in t or "purchase" in t for t in declared), "no tool buys anything")


@case
def the_key_goes_only_to_the_api_and_never_reaches_a_reply():
    with Sandbox() as box:
        box.call("game_details", {"game": 200})
        box.call("my_library")
        box.call("linux_compatibility", {"game": 200})
        for url in box.fake.urls:
            host = urllib.parse.urlsplit(url).hostname
            if KEY in url:
                check(host == "api.steampowered.com", f"the key went only to api.steampowered.com ({host})")
        check(any(KEY in u for u in box.fake.urls), "the key was used where it is needed")
        check(not any(KEY in u for u in box.fake.urls if "GetItems" in u or "GetNumberOfCurrentPlayers" in u),
              "keyless Web API calls don't carry the key")
        box.fake.fail["/IPlayerService/GetOwnedGames/v1/"] = ("urlerror", None)
        error, _, text = box.call("my_library")
        check(error and KEY not in text and "<api key>" in text, "an error that echoes the URL has the key scrubbed")


@case
def a_redirect_elsewhere_is_refused():
    with Sandbox() as box:
        module = box.module
        handler = module._PinnedRedirects()
        req = module.urllib.request.Request("https://store.steampowered.com/api/appdetails?appids=1")
        try:
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/steal")
            refused = False
        except module.SteamError:
            refused = True
        check(refused, "a redirect to another host is refused")
        kept = handler.redirect_request(req, None, 302, "Found", {}, "https://store.steampowered.com/app/1")
        check(kept is not None, "a redirect within Steam's hosts is followed")


@case
def loading_the_server_creates_nothing():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-steam-load-"))
    saved = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(tmp)
    try:
        spec = importlib.util.spec_from_file_location("steam_load_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        check(not any(tmp.iterdir()), "importing the server makes no cache folder")
        check(module.CACHE_DIR == tmp / "jarvis-steam", "the cache lives under XDG_CACHE_HOME")
    finally:
        if saved is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


@case
def every_library_is_read_and_runtimes_are_not_games():
    with Sandbox() as box:
        _, result, _ = box.call("installed_games")
        names = [g["name"] for g in result["installed"]]
        check(result["games"] == 3, "three games across two library folders")
        check("Proton Experimental" not in names, "Proton is not listed as a game")
        check(names[0] == "Detroit: Become Human", "sorted by size, biggest first")
        check(len(result["libraries"]) == 2 and all("free_gb" in lib for lib in result["libraries"]),
              "each library shows its free space")
        _, with_tools, _ = box.call("installed_games", {"include_tools": True})
        check(any(g["name"] == "Proton Experimental" for g in with_tools["installed"]), "include_tools lists runtimes")
        _, downloads, _ = box.call("downloads")
        rows = downloads["downloads"]
        check(len(rows) == 1 and rows[0]["name"] == "Deep Rock Galactic", "only the game mid-download is listed")
        check(rows and rows[0]["progress_percent"] == 50.0 and rows[0]["status"] == "downloading",
              "progress comes from BytesDownloaded / BytesToDownload")


@case
def the_login_name_never_leaves_the_computer():
    with Sandbox() as box:
        texts = [box.call(t)[2] for t in ("my_wishlist", "my_library", "friends", "installed_games")]
        check(all("secretlogin" not in t and "oldlogin" not in t for t in texts), "no reply contains a login name")
        check(any(f"steamid={ME}" in u for u in box.fake.urls), "the most recently signed-in account is the one asked about")
        check(not any("secretlogin" in u for u in box.fake.urls), "the login name is never sent anywhere")


@case
def the_profile_setting_takes_ids_links_and_names():
    with Sandbox(steam_id=f"https://steamcommunity.com/profiles/{ALICE}/") as box:
        check(box.module._my_steam_id() == ALICE, "a profile link gives its SteamID64")
    with Sandbox(steam_id="https://steamcommunity.com/id/yakup") as box:
        check(box.module._my_steam_id() == ME, "a custom URL name is resolved through Steam")
        check(any("ResolveVanityURL" in u and "vanityurl=yakup" in u for u in box.fake.urls), "ResolveVanityURL was asked")
    with Sandbox(steam_id="yakup", key="") as box:
        error, _, text = box.call("my_wishlist")
        check(error and "steamcommunity.com/dev/apikey" in text, "a custom URL name without a key says how to get one")


@case
def games_are_found_on_this_computer_first():
    with Sandbox() as box:
        check(box.module._resolve("detroit")[0] == 100, "a partial name matches the installed game")
        check(box.module._resolve("detroit")[2] == "installed on this computer", "and says where it was found")
        check(box.module._resolve("Hades")[0] == 200, "a name not installed is found in the library or store")
        check(box.module._resolve("620")[0] == 620, "digits are an app id")
        check(box.module._resolve("https://store.steampowered.com/app/400/Free_Thing/")[0] == 400, "a store link works")


@case
def installing_never_buys():
    with Sandbox() as box:
        _, result, _ = box.call("install_game", {"game": 600})
        check(not box.launched, "a paid game the user doesn't own opens nothing")
        check("can't buy" in result["message"], "and the reply says JARVIS can't buy it")
        _, result, _ = box.call("install_game", {"game": "Hades"})
        check(box.launched == ["steam://install/200"], "an owned game opens Steam's install window")
        box.launched.clear()
        box.call("install_game", {"game": 400})
        check(box.launched == ["steam://install/400"], "a free game can be installed without owning it")
        box.launched.clear()
        _, result, _ = box.call("install_game", {"game": "detroit"})
        check(not box.launched and result["message"] == "Already installed.", "an installed game is not reinstalled")
    with Sandbox(key="") as box:
        _, result, _ = box.call("install_game", {"game": 600})
        check(box.launched == ["steam://install/600"], "without a key, Steam's own window decides")
        check(any("ownership wasn't checked" in n for n in result["notes"]), "and the reply says ownership wasn't checked")


@case
def uninstalling_warns_about_saves():
    with Sandbox() as box:
        box.module.UNINSTALL_WATCH_SECONDS = 0.2
        _, result, _ = box.call("uninstall_game", {"game": "Saveless"})
        check(box.launched == ["steam://uninstall/500"], "uninstall opens Steam's own confirmation")
        check(result["uninstalled"] is False and "still installed" in result["message"],
              "while Steam's question is open, the game is reported as still installed")
        check(any("Steam Cloud" in n for n in result["notes"]), "a game without Steam Cloud gets a saves warning")
        box.launched.clear()
        _, result, _ = box.call("uninstall_game", {"game": "detroit"})
        check(not any("Steam Cloud" in n for n in result["notes"]), "a Steam Cloud game gets no warning")
        box.launched.clear()
        error, _, _ = box.call("uninstall_game", {"game": 600})
        check(error and not box.launched, "a game that isn't installed is refused")
        error, _, text = box.call("play_game", {"game": "Deep Rock"})
        check(error and not box.launched and "not installed yet" in text, "a game mid-download can't be started")
        box.call("play_game", {"game": "detroit"})
        check(box.launched == ["steam://rungameid/100"], "an installed game starts through Steam")


@case
def private_profiles_are_explained():
    with Sandbox() as box:
        box.fake.owned[ME] = None
        error, _, text = box.call("my_library")
        check(error and "Privacy Settings" in text, "hidden game details point at the privacy setting")
    with Sandbox() as box:
        box.fake.fail["/ISteamUser/GetFriendList/v1/"] = (401, {})
        error, _, text = box.call("friends")
        # The first live test: a working key, a hidden list, and "the key is wrong".
        check(error and "isn't public" in text and "Friends List to Public" in text,
              "a hidden friends list is named as the cause")
        check("STEAM_API_KEY" not in text and "revoked" not in text, "and the working key is not blamed")
        error, _, text = box.call("games_in_common", {"friends": ["alice"]})
        check(error and "isn't public" in text, "games_in_common says the same")
        box.fake.my_visibility = 1
        error, _, text = box.call("friends")
        check(error and "My Profile and Friends List to Public" in text, "a private profile is named as the cause")
        box.fake.fail["/ISteamUser/GetPlayerSummaries/v2/"] = (401, {})
        error, _, text = box.call("friends")
        check(error and "STEAM_API_KEY" in text, "a key Steam refuses everywhere is still blamed on the key")
    with Sandbox(key="") as box:
        error, _, text = box.call("my_library")
        check(error and "steamcommunity.com/dev/apikey" in text, "no key says where to get one")


@case
def the_wishlist_keeps_its_order_and_finds_sales():
    with Sandbox() as box:
        _, result, _ = box.call("my_wishlist")
        check([g["appid"] for g in result["games"]] == [200, 600], "the user's priority order is kept")
        _, result, _ = box.call("my_wishlist", {"on_sale_only": True})
        games = result["games"]
        check(len(games) == 1 and games[0]["discount_percent"] == 50 and games[0].get("sale_ends"),
              "on_sale_only keeps the discounted game, with when the sale ends")


@case
def friends_and_shared_games():
    with Sandbox() as box:
        _, result, _ = box.call("friends")
        check(len(result["shown"]) == 1 and result["shown"][0]["playing"] == "Deep Rock Galactic",
              "online friends are listed with what they're playing")
        check(result["shown"][0]["status"] == "looking to play", "persona states are named")
        _, result, _ = box.call("friends", {"online_only": False})
        check(len(result["shown"]) == 2, "online_only false lists everyone")
        _, result, _ = box.call("games_in_common", {"friends": ["alice", "bob"]})
        check([g["appid"] for g in result["games"]] == [300], "only shared multiplayer games, by default")
        check(any("private" in n for n in result["notes"]), "a friend with private game details is named, not guessed")
        _, result, _ = box.call("games_in_common", {"friends": ["alice"], "multiplayer_only": False})
        check(sorted(g["appid"] for g in result["games"]) == [200, 300], "multiplayer_only false lists every shared game")


@case
def store_text_is_plain_and_linux_is_judged():
    with Sandbox() as box:
        _, news, _ = box.call("game_news", {"game": 300})
        summary = news["news"][0]["summary"]
        check("[" not in summary and "<" not in summary and "Crash fixed" in summary, "BBCode and HTML are stripped")
        check(any("feeds=steam_community_announcements" in u for u in box.fake.urls), "news defaults to official announcements")
        _, details, _ = box.call("game_details", {"game": 200})
        check(details["description"] == "Bold claims & more", "descriptions are plain text")
        check(details["storage_needed"] == "12 GB" and details["playing_now"] == 1234, "storage and players are read")
        _, compat, _ = box.call("linux_compatibility", {"game": 300})
        check(compat["linux_native"] and compat["verdict"] == "runs natively on Linux", "a native game is native")
        check("no reports" in str(compat["protondb"]), "a game ProtonDB doesn't know says so")
        _, compat, _ = box.call("linux_compatibility", {"game": 200})
        check(compat["protondb"]["tier"] == "platinum" and "perfectly" in compat["verdict"], "ProtonDB's tier sets the verdict")
        _, sales, _ = box.call("store_highlights")
        check(len(sales["games"]) == 1 and sales["games"][0]["discount_percent"] == 60, "duplicate store-front entries are merged")


@case
def choices_are_read_as_a_model_spells_them():
    with Sandbox() as box:
        # The first live test: "games in trend" came as kind "top sellers" and was refused.
        for spelled in ("top sellers", "Top-Sellers", "trending"):
            failed, result, text = box.call("store_highlights", {"kind": spelled})
            check(not failed and result["kind"] == "top_sellers", f"kind {spelled!r} is top_sellers")
        _, result, _ = box.call("store_highlights", {"kind": "sales"})
        check(result["kind"] == "specials", "kind 'sales' is specials")
        failed, _, _ = box.call("installed_games", {"sort": "Last played"})
        check(not failed, "sort 'Last played' is last_played")
        failed, _, text = box.call("store_highlights", {"kind": "cheapest"})
        check(failed and "specials, top_sellers, new_releases, coming_soon" in text, "anything else is refused with the choices")


@case
def an_uninstall_reports_what_steam_did():
    # The live test: steam://uninstall on a game mid-download cancelled and
    # removed it at once, while the tool said Steam was asking to confirm.
    with Sandbox() as box:
        box.module.UNINSTALL_WATCH_SECONDS = 2

        def steam_removes(url):
            box.launched.append(url)
            for found in box.tmp.rglob(f"appmanifest_{url.rsplit('/', 1)[1]}.acf"):
                found.unlink()

        box.module._launch = steam_removes
        _, result, _ = box.call("uninstall_game", {"game": "Deep Rock"})
        check(result["uninstalled"] is True and "cancelled the download" in result["message"],
              "a download Steam cancels is reported as cancelled")
        check("still installed" not in result["message"] and "until you confirm" not in result["message"],
              "and not as waiting for a question")
        _, result, _ = box.call("uninstall_game", {"game": "Saveless"})
        check(result["uninstalled"] is True and "uninstalled Saveless Game without asking" in result["message"],
              "an installed game Steam removes at once is reported as removed")


@case
def steam_links_are_handed_off_detached():
    with Sandbox() as box:
        module = importlib.util.module_from_spec(importlib.util.spec_from_file_location("steam_launch", SERVER))
        module.__spec__.loader.exec_module(module)
        seen = {}

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen.update(kwargs)

        module.subprocess.Popen = fake_popen
        module.shutil.which = lambda name: "/usr/bin/" + name
        if sys.platform.startswith("linux"):
            module._launch("steam://install/200")
            check(seen.get("command") == ["xdg-open", "steam://install/200"], "the link goes to xdg-open")
            check(seen.get("stdout") is module.subprocess.DEVNULL and seen.get("stdin") is module.subprocess.DEVNULL,
                  "the child gets nothing of the MCP wire")
            check(seen.get("start_new_session") is True, "and outlives the call")


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
    print("\nOK: steam self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
