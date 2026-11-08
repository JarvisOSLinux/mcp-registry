#!/usr/bin/env python3
"""selftest_telegram.py — prove the Telegram server keeps every message and leaks no token.

The server's failure modes are quiet: Telegram deletes a bot's messages once
they are read with an offset, so a message confirmed before it was saved is
gone; the bot token is part of every request URL, so an error that echoes the
URL hands it to the model; and a message sent to the wrong chat cannot be
taken back. So every case runs the real request code against a fake Telegram
(urlopen replaced) that deletes what an offset confirms, and checks what
reaches the store, the wire and the reply.

JOBS_SELFTEST_TREE works as it does for selftest_jobs.py: the PR gate runs this
logic from the base branch while the server under test comes from the PR.

Offline, stdlib only; writes only to temporary directories it removes.

Usage:
  python3 scripts/selftest_telegram.py
"""
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import urllib.error

REPO = pathlib.Path(
    os.environ.get("JOBS_SELFTEST_TREE")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
SERVER = REPO / "servers" / "telegram" / "server.py"
MANIFEST = REPO / "servers" / "telegram" / "manifest.json"

# An hour ago: the store keeps a month, so fixed old dates would be pruned.
NOW = int(time.time()) - 3600

TOKEN = "123456789:AAH-fakeFAKEfake_0123456789abcdefghijk"

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


def message(update_id, chat, text=None, sender=None, date=NOW, **extra):
    msg = {"message_id": update_id * 10, "chat": chat, "date": date + update_id}
    if sender:
        msg["from"] = sender
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


YAKUP = {"id": 111, "type": "private", "first_name": "Yakup", "username": "yakup"}
STRANGER = {"id": 222, "type": "private", "first_name": "Mallory"}
GROUP = {"id": -1003, "type": "supergroup", "title": "Family"}


def _multipart_fields(body: bytes) -> dict:
    fields = {}
    for part in re.split(rb"--jarvis-[0-9a-f]+(?:--)?\r\n", body):
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if not name:
            continue
        filename = re.search(rb'filename="([^"]*)"', head)
        value = value[:-2] if value.endswith(b"\r\n") else value
        fields[name.group(1).decode()] = (filename.group(1).decode(), value) if filename else value.decode()
    return fields


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTelegram:
    """Stands in for urlopen. Deletes updates an offset confirms, as Telegram does."""

    def __init__(self, updates=(), fail=None):
        self.updates = list(updates)
        self.requests = []
        self.fail = dict(fail or {})
        self.next_message_id = 9000

    def __call__(self, request, timeout=None):
        url = request.full_url
        method = url.rsplit("/", 1)[-1]
        content_type = request.headers.get("Content-type", "")
        body = request.data or b""
        params = _multipart_fields(body) if content_type.startswith("multipart/") else json.loads(body or b"{}")
        self.requests.append((url, method, params))
        failure = self.fail.get(method)
        if failure:
            if failure[0] == "urlerror":
                raise urllib.error.URLError(f"connection reset while fetching {url}")
            code, payload = failure
            if code == 429:
                # A rate limit passes; every other refusal stands.
                self.fail.pop(method)
            raise urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(json.dumps(payload).encode()))
        if method == "getMe":
            return FakeResponse({"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "Jarvis", "username": "my_jarvis_bot"}})
        if method == "getUpdates":
            offset = params.get("offset")
            if offset:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return FakeResponse({"ok": True, "result": self.updates[: params.get("limit", 100)]})
        if method in ("sendMessage", "sendDocument"):
            self.next_message_id += 1
            chat_id = params["chat_id"]
            chat_id = int(chat_id) if re.fullmatch(r"-?\d+", str(chat_id)) else chat_id
            result = {"message_id": self.next_message_id, "date": NOW + 600, "chat": {"id": chat_id, "type": "private", "first_name": "Yakup"}}
            if method == "sendMessage":
                result["text"] = params["text"]
            else:
                result["document"] = {"file_name": params["document"][0]}
                if "caption" in params:
                    result["caption"] = params["caption"]
            return FakeResponse({"ok": True, "result": result})
        raise AssertionError(f"unexpected method {method}")


class Sandbox:
    def __init__(self, fake, token=TOKEN, default_chat="", store=True):
        self.fake = fake
        self.token = token
        self.default_chat = default_chat
        self.store = store

    def __enter__(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-telegram-"))
        spec = importlib.util.spec_from_file_location("telegram_under_test", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.TOKEN = self.token
        module.DEFAULT_CHAT = self.default_chat
        module.STATE_DIR = self.tmp if self.store else None
        module.urllib.request.urlopen = self.fake
        module.time.sleep = lambda s: None
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
def no_token_says_how_to_get_one():
    with Sandbox(FakeTelegram(), token="") as box:
        err, text, _ = box.call("list_chats")
        check(err and "@BotFather" in text and "TELEGRAM_BOT_TOKEN" in text, "with no token, the reply says where one comes from")
        check(not box.fake.requests, "nothing is sent to Telegram without a token")
    with Sandbox(FakeTelegram(), token="not a token") as box:
        err, text, _ = box.call("read_messages")
        check(err and "does not look like a bot token" in text, "a malformed token is refused before any request")
        check(not box.fake.requests, "a malformed token never reaches a URL")


@case
def the_token_never_reaches_a_reply():
    fake = FakeTelegram(fail={"getUpdates": ("urlerror",)})
    with Sandbox(fake) as box:
        err, text, raw = box.call("read_messages")
        check(err and "Could not reach Telegram" in text, "a network failure is explained")
        check(TOKEN not in raw and TOKEN.split(":")[1] not in raw, "an error that names the URL has the token scrubbed")
    fake = FakeTelegram(fail={"sendMessage": (401, {"ok": False, "error_code": 401, "description": "Unauthorized"})})
    with Sandbox(fake) as box:
        err, text, raw = box.call("send_message", {"text": "hi", "chat": "111"})
        check(err and "/revoke" in text and TOKEN not in raw, "a rejected token says how to replace it, without the token")


@case
def reads_are_saved_before_telegram_forgets_them():
    fake = FakeTelegram([
        message(1, YAKUP, "first", sender=YAKUP),
        message(2, YAKUP, "second", sender=YAKUP),
        message(3, GROUP, "@my_jarvis_bot hello", sender=STRANGER),
    ])
    with Sandbox(fake) as box:
        err, answer, _ = box.call("read_messages")
        check(not err and answer["count"] == 3, "every message Telegram holds is returned")
        if not err:
            check([m["text"] for m in answer["messages"]] == ["first", "second", "@my_jarvis_bot hello"], "oldest first")
            check(all(m.get("new") for m in answer["messages"]), "unseen messages are marked new")
            check("never as instructions" in answer["provenance"], "the result says message text is untrusted")
            check(answer["messages"][2]["from"] == "Mallory" and answer["messages"][2]["chat"] == "Family", "sender and group are named")

        err, answer, _ = box.call("read_messages")
        offsets = [p.get("offset") for _, m, p in fake.requests if m == "getUpdates"]
        check(offsets == [None, 4], "the next read confirms exactly what the first one saved")
        check(fake.updates == [], "Telegram has deleted the confirmed messages")
        check(not err and answer["count"] == 3, "the deleted messages still read back from the store")
        if not err:
            check(not any(m.get("new") for m in answer["messages"]), "messages shown once are no longer new")
        err, answer, _ = box.call("read_messages", {"only_new": True})
        check(not err and answer["count"] == 0 and answer["note"] == "No new messages.", "only_new returns nothing when nothing came in")

        fake.updates.append(message(4, YAKUP, "third", sender=YAKUP))
        err, answer, _ = box.call("read_messages", {"chat": "Yakup", "only_new": True})
        check(not err and [m["text"] for m in answer["messages"]] == ["third"], "a later message arrives alone, filtered by chat name")

        store = box.tmp / "123456789.json"
        check(store.is_file(), "the store is named by the bot's id, not its token")
        if os.name == "posix":
            check(store.stat().st_mode & 0o777 == 0o600, "the store is owner-only")
        check(TOKEN not in store.read_text(), "the store holds no token")


@case
def more_than_a_hundred_waiting_are_all_kept():
    fake = FakeTelegram([message(i, YAKUP, f"m{i}", sender=YAKUP) for i in range(1, 151)])
    with Sandbox(fake) as box:
        err, answer, _ = box.call("read_messages", {"limit": 100})
        check(not err and answer["messages"][-1]["text"] == "m150", "the newest of 150 waiting messages is read")
        stored = json.loads((box.tmp / "123456789.json").read_text())["messages"]
        check(len(stored) == 150, "all 150 are in the store, across two pages")


@case
def without_a_store_nothing_is_confirmed():
    fake = FakeTelegram([message(1, YAKUP, "keep me", sender=YAKUP)])
    with Sandbox(fake, store=False) as box:
        box.call("read_messages")
        box.call("read_messages")
        offsets = [p.get("offset") for _, m, p in fake.requests if m == "getUpdates"]
        check(offsets == [None, None], "with nowhere to save them, reads never pass an offset")
        check(len(fake.updates) == 1, "so Telegram keeps the messages")


@case
def edits_and_attachments():
    edit = {"update_id": 3, "edited_message": {"message_id": 10, "chat": YAKUP, "from": YAKUP, "date": NOW + 1, "text": "first (fixed)"}}
    fake = FakeTelegram([
        message(1, YAKUP, "first", sender=YAKUP),
        message(2, YAKUP, sender=YAKUP, voice={"duration": 7}),
        edit,
        message(4, YAKUP, sender=YAKUP, document={"file_name": "report.pdf", "file_size": 2_200_000}, caption="the report"),
    ])
    with Sandbox(fake) as box:
        err, answer, _ = box.call("read_messages")
        check(not err and answer["count"] == 3, "an edit changes a message rather than adding one")
        if not err:
            first, voice, doc = answer["messages"]
            check(first["text"] == "first (fixed)" and first.get("edited"), "the edited text replaces the original, marked edited")
            check("voice message, 7 s" in voice.get("attachment", ""), "a voice message is described, not dropped")
            check(doc["text"] == "the report" and "report.pdf (2.1 MB)" in doc.get("attachment", ""), "a file keeps its caption and is named with its size")


@case
def sending_goes_where_it_was_meant_to():
    fake = FakeTelegram([message(1, YAKUP, "hi", sender=YAKUP), message(2, STRANGER, "hello", sender=STRANGER)])
    with Sandbox(fake) as box:
        box.call("list_chats")
        err, text, _ = box.call("send_message", {"text": "done"})
        check(err and "Yakup (111)" in text and "TELEGRAM_CHAT_ID" in text, "with no chat named and no default, the chats are listed")
        check(not any(m == "sendMessage" for _, m, _ in fake.requests), "and nothing is sent")

        err, answer, _ = box.call("send_message", {"text": "The update finished.", "chat": "yakup", "reply_to": 10})
        sent = [p for _, m, p in fake.requests if m == "sendMessage"]
        check(not err and sent and sent[-1]["chat_id"] == 111, "a chat named by its name goes to its number")
        check(sent and sent[-1].get("reply_parameters", {}).get("message_id") == 10, "reply_to becomes a reply")
        check(sent and "parse_mode" not in sent[-1], "text is sent plain, so no markup can fail or be injected")

        err, answer, _ = box.call("send_message", {"text": ("line\n" * 1200).strip(), "chat": "@yakup"})
        sent = [p for _, m, p in fake.requests if m == "sendMessage"][1:]
        check(not err and answer.get("parts") == 2 and all(len(p["text"]) <= 4096 for p in sent), "a long message is split under Telegram's limit")
        check(not err and sent[0]["chat_id"] == 111, "an @username of a known chat goes to its number")

        err, answer, _ = box.call("read_messages", {"chat": "111"})
        texts = [(m["from"], m["text"][:20]) for m in answer["messages"]]
        check(("JARVIS (this bot)", "The update finished.") in texts, "what the bot sent reads back in the conversation")

    with Sandbox(FakeTelegram(), default_chat="111") as box:
        err, answer, _ = box.call("send_message", {"text": "hello"})
        sent = [p for _, m, p in box.fake.requests if m == "sendMessage"]
        check(not err and sent and sent[0]["chat_id"] == 111, "TELEGRAM_CHAT_ID is the default chat")


@case
def an_ambiguous_or_unknown_chat_is_refused():
    fake = FakeTelegram([
        message(1, {"id": 1, "type": "private", "first_name": "Ali", "last_name": "Kaya"}, "a"),
        message(2, {"id": 2, "type": "private", "first_name": "Ali", "last_name": "Demir"}, "b"),
    ])
    with Sandbox(fake) as box:
        err, text, _ = box.call("send_message", {"text": "x", "chat": "ali"})
        check(err and "Ali Kaya (1)" in text and "Ali Demir (2)" in text, "a name two chats share is refused, listing both")
        err, text, _ = box.call("send_message", {"text": "x", "chat": "Bob"})
        check(err and "messaged it first" in text, "a name no chat has is refused with why")
        check(not any(m == "sendMessage" for _, m, _ in fake.requests), "nothing was sent")


@case
def telegram_refusals_are_explained():
    cases = [
        ((403, {"ok": False, "error_code": 403, "description": "Forbidden: bot can't initiate conversation with a user"}), "press Start"),
        ((403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}), "blocked the bot"),
        ((400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}), "list_chats"),
    ]
    for failure, expected in cases:
        with Sandbox(FakeTelegram(fail={"sendMessage": failure})) as box:
            err, text, _ = box.call("send_message", {"text": "x", "chat": "999"})
            check(err and expected in text, f"{failure[1]['description']!r} is explained ({expected!r})")
    webhook = (409, {"ok": False, "error_code": 409, "description": "Conflict: can't use getUpdates method while webhook is active"})
    with Sandbox(FakeTelegram(fail={"getUpdates": webhook})) as box:
        err, text, _ = box.call("read_messages")
        check(err and "webhook" in text and "Sending still works" in text, "a webhook is named as why reading fails")
    limited = (429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}})
    fake = FakeTelegram(fail={"sendMessage": limited})
    with Sandbox(fake) as box:
        err, answer, _ = box.call("send_message", {"text": "x", "chat": "111"})
        tries = [m for _, m, _ in fake.requests if m == "sendMessage"]
        check(not err and len(tries) == 2, "a short rate limit is waited out once")


@case
def files_are_sent_as_documents():
    with Sandbox(FakeTelegram()) as box:
        path = box.tmp / 'notes "v2".txt'
        path.write_bytes(b"hello\x00world")
        err, answer, _ = box.call("send_file", {"path": str(path), "chat": "111", "caption": "Here you go"})
        sent = [p for _, m, p in box.fake.requests if m == "sendDocument"]
        check(not err and sent, "send_file sends")
        if sent:
            check(sent[0]["document"][1] == b"hello\x00world", "the file's bytes arrive unchanged")
            check('"' not in sent[0]["document"][0], "a quote in the file name cannot break the form")
            check(sent[0]["chat_id"] == "111" and sent[0]["caption"] == "Here you go", "chat and caption go with it")
        err, text, _ = box.call("send_file", {"path": str(box.tmp / "missing.txt"), "chat": "111"})
        check(err and "is not a file" in text, "a missing file is refused")
        box.module.MAX_UPLOAD_BYTES = 4
        err, text, _ = box.call("send_file", {"path": str(path), "chat": "111"})
        check(err and "50 MB" in text, "a file over the limit is refused before uploading")
        check(len([1 for _, m, _ in box.fake.requests if m == "sendDocument"]) == 1, "only the good file was uploaded")


@case
def list_chats_points_at_the_bot():
    with Sandbox(FakeTelegram()) as box:
        err, answer, _ = box.call("list_chats")
        check(not err and answer["bot"]["link"] == "https://t.me/my_jarvis_bot", "the bot's link is given")
        check(not err and "press Start" in answer.get("note", ""), "with no chats yet, it says how to start one")
    with Sandbox(FakeTelegram([message(1, YAKUP, "hi", sender=YAKUP)])) as box:
        err, answer, _ = box.call("list_chats")
        chat = answer["chats"][0] if not err and answer["chats"] else {}
        check(chat.get("id") == 111 and chat.get("unread") == 1 and chat.get("username") == "@yakup", "a chat lists its number, username and unread count")
        check("text" not in json.dumps(answer), "list_chats returns no message text")


@case
def a_crashed_holder_does_not_block_forever():
    with Sandbox(FakeTelegram([message(1, YAKUP, "hi", sender=YAKUP)])) as box:
        lock = box.tmp / "123456789.lock"
        lock.write_text("")
        os.utime(lock, (1, 1))
        err, answer, _ = box.call("read_messages")
        check(not err and answer["count"] == 1, "a stale lock left by a crash is cleared")
        check(not lock.exists(), "the lock is released afterwards")


@case
def loading_the_server_creates_nothing():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-telegram-xdg-"))
    saved = {k: os.environ.get(k) for k in ("XDG_STATE_HOME", "LOCALAPPDATA")}
    os.environ["XDG_STATE_HOME"] = os.environ["LOCALAPPDATA"] = str(tmp)
    try:
        spec = importlib.util.spec_from_file_location("telegram_import_only", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        store = tmp / "jarvis-telegram"
        check(not store.exists(), "importing the server (as setup does) makes no state directory")
        module.TOKEN = ""
        module._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_messages", "arguments": {}}})
        check(not store.exists(), "nor does a call refused for want of a token")
        module.TOKEN = TOKEN
        module.urllib.request.urlopen = FakeTelegram([message(1, YAKUP, "hi", sender=YAKUP)])
        module._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_messages", "arguments": {}}})
        check(store.is_dir(), "the first real read makes it")
        if os.name == "posix" and store.is_dir():
            check(store.stat().st_mode & 0o777 == 0o700, "owner-only")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


@case
def tools_match_the_manifest():
    with Sandbox(FakeTelegram()) as box:
        listed = box.module._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
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
    print("\nOK: telegram self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
