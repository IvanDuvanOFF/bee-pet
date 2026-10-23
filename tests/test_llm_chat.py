"""Тесты клиента BeeLLM и мини-чата (offscreen, без реальной сети)."""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet import llm
from bee_pet.app import wire_history_reset, wire_menu_replies
from bee_pet.bee_window import BeeWindow
from bee_pet.chat import OFFLINE_NOTE, ChatWindow
from bee_pet.llm import (FALLBACK_BUZZ, HISTORY_TURNS, BeeClient, ask_bee, clean_reply,
                          strip_mood_tag)
from bee_pet.mood import Action, Mood
from bee_pet.mood_sync import MoodRequest


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# --- clean_reply -------------------------------------------------------------

def test_clean_reply_collapses_whitespace():
    assert clean_reply("  бзз \n\n  бзз\t") == "бзз бзз"


def test_clean_reply_empty_gives_fallback():
    assert clean_reply("   ") in FALLBACK_BUZZ


def test_clean_reply_truncates_on_word_boundary():
    out = clean_reply("слово " * 100)
    assert out.endswith("…") and len(out) <= 200
    assert out[:-1].endswith("слово")


def test_clean_reply_short_untouched():
    assert clean_reply("бзз") == "бзз"


# --- strip_mood_tag ----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("[happy:low] bzz! buzz buzz!", "bzz! buzz buzz!"),
    ("[angry:low] BZZTZZZZzzz", "BZZTZZZZzzz"),
    ("  [happy:high]   *points at herself. Scintilla.* brrr~z ", "*points at herself. Scintilla.* brrr~z"),
    ("[confused] bzz?", "bzz?"),
    ("[ sad : high ]bzz...", "bzz..."),
    ("bzz без тега", "bzz без тега"),
    ("bzz [happy:low] внутри", "bzz [happy:low] внутри"),
    ("[happy:low]", ""),
    ("  [angry:high]   ", ""),
    ("", ""),
])
def test_strip_mood_tag(raw, expected):
    assert strip_mood_tag(raw) == expected


# --- ask_bee против заглушки сервера --------------------------------------------

def _serve(handler_body: bytes, status: int = 200, seen: list | None = None):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if seen is not None:
                seen.append((self.path, json.loads(body)))
            self.send_response(status)
            self.send_header("Content-Length", str(len(handler_body)))
            self.end_headers()
            self.wfile.write(handler_body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _chat_body(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant",
                                                "content": content}}]}).encode()


def test_ask_bee_success(monkeypatch):
    seen = []
    srv = _serve(_chat_body(" [happy:low] бзз! "), seen=seen)
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{srv.server_port}")
    msgs = [{"role": "user", "content": "hi bee!"}]
    try:
        assert ask_bee(msgs) == "[happy:low] бзз!"
    finally:
        srv.shutdown()
    path, body = seen[0]
    assert path == "/v1/chat/completions"
    assert body == {"messages": msgs, "max_tokens": 64, "temperature": 1.0}


def test_ask_bee_request_shape(monkeypatch):
    captured = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured.update(url=req.full_url, body=json.loads(req.data),
                        ctype=req.get_header("Content-type"), timeout=timeout)
        return Resp(_chat_body("[sad:low] bzz..."))

    monkeypatch.setattr(llm, "BEE_URL", "http://bee.test:1")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    msgs = [{"role": "user", "content": "*swats you*"},
            {"role": "assistant", "content": "[angry:low] BZZT"},
            {"role": "user", "content": "sorry"}]
    assert ask_bee(msgs, max_tokens=32, timeout=5) == "[sad:low] bzz..."
    assert captured == {
        "url": "http://bee.test:1/v1/chat/completions",
        "body": {"messages": msgs, "max_tokens": 32, "temperature": 1.0},
        "ctype": "application/json",
        "timeout": 5,
    }


def test_ask_bee_missing_message_raises(monkeypatch):
    srv = _serve(json.dumps({"choices": [{"text": "old format"}]}).encode())
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        with pytest.raises(KeyError):
            ask_bee([{"role": "user", "content": "hi"}])
    finally:
        srv.shutdown()


def test_ask_bee_bad_json(monkeypatch):
    srv = _serve(b"not json")
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        with pytest.raises(ValueError):
            ask_bee([{"role": "user", "content": "hi"}])
    finally:
        srv.shutdown()


def test_ask_bee_connection_refused(monkeypatch):
    srv = _serve(b"{}")
    port = srv.server_port
    srv.shutdown()
    srv.server_close()
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{port}")
    with pytest.raises(OSError):
        ask_bee([{"role": "user", "content": "hi"}], timeout=2)


# --- BeeClient -------------------------------------------------------------

def _wait_reply(qapp, client, count=1, timeout=3000):
    got = []
    client.reply_ready.connect(lambda tag, text, fm: got.append((tag, text, fm)))
    waited = 0
    while len(got) < count and waited < timeout:
        QTest.qWait(20)
        waited += 20
    return got


def _wait_count(got, count, timeout=3000):
    """Ждёт, пока в got наберётся count ответов (история обновляется по ответу)."""
    waited = 0
    while len(got) < count and waited < timeout:
        QTest.qWait(10)
        waited += 10


def test_client_model_reply(qapp):
    client = BeeClient(ask=lambda msgs: f"[happy:low] эхо {msgs[-1]['content']}")
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    client.request("hi", tag="t")
    QTest.qWait(300)
    client.shutdown()
    assert got == [("t", "эхо hi", True)]
    assert client.online is True


def test_client_fallback_and_backoff(qapp):
    calls = []
    now = [100.0]

    def failing(messages):
        calls.append(messages[-1]["content"])
        raise ConnectionRefusedError

    client = BeeClient(ask=failing, clock=lambda: now[0], backoff=30)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    client.request("a", tag=1)
    QTest.qWait(300)
    assert len(got) == 1 and got[0][2] is False and got[0][1] in FALLBACK_BUZZ
    assert client.online is False and calls == ["a"]

    now[0] = 110.0  # в пределах 30 с: сеть не трогаем
    client.request("b", tag=2)
    QTest.qWait(100)
    assert len(got) == 2 and got[1][2] is False and calls == ["a"]

    now[0] = 131.0  # пауза прошла: пробуем снова
    client.request("c", tag=3)
    QTest.qWait(300)
    client.shutdown()
    assert calls == ["a", "c"] and len(got) == 3


def test_client_tag_only_reply_is_failure(qapp):
    client = BeeClient(ask=lambda msgs: "[happy:low]  ")
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    client.request("hi", tag="t")
    QTest.qWait(300)
    client.shutdown()
    assert len(got) == 1 and got[0][1] in FALLBACK_BUZZ and got[0][2] is False
    assert client.online is False


def test_client_sends_history_and_caps_it(qapp):
    seen = []

    def ask(messages):
        seen.append(messages)
        return f"[happy:low] re {messages[-1]['content']}"

    client = BeeClient(ask=ask)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    total = HISTORY_TURNS + 2
    for i in range(total):
        client.request(f"m{i}")
        _wait_count(got, i + 1)
    client.shutdown()
    assert len(got) == total
    # Пузырь получает текст без тега.
    assert got[1][1] == "re m1"
    # Второй запрос несёт первую пару целиком, ответ — сырой, с тегом.
    assert seen[1] == [
        {"role": "user", "content": "m0"},
        {"role": "assistant", "content": "[happy:low] re m0"},
        {"role": "user", "content": "m1"},
    ]
    # Последний запрос: только HISTORY_TURNS последних пар + новая реплика.
    last = seen[-1]
    assert len(last) == 2 * HISTORY_TURNS + 1
    assert last[0] == {"role": "user", "content": f"m{total - 1 - HISTORY_TURNS}"}
    assert last[-1] == {"role": "user", "content": f"m{total - 1}"}


def test_client_history_not_updated_on_failure(qapp):
    seen = []
    answers = iter(["[angry:low] BZZT", ConnectionRefusedError(), "", "[neutral:low] bzz"])

    def ask(messages):
        seen.append(messages)
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    client = BeeClient(ask=ask, backoff=0)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    for i, msg in enumerate(("*swats you*", "hi", "hey", "sorry")):
        client.request(msg)
        _wait_count(got, i + 1)
    client.shutdown()
    swat = [{"role": "user", "content": "*swats you*"},
            {"role": "assistant", "content": "[angry:low] BZZT"}]
    assert seen[1] == swat + [{"role": "user", "content": "hi"}]
    assert seen[2] == swat + [{"role": "user", "content": "hey"}]
    assert seen[3] == swat + [{"role": "user", "content": "sorry"}]


def test_client_remember_false_sends_single_message_and_skips_history(qapp):
    seen = []

    def ask(messages):
        seen.append(messages)
        return f"[neutral:low] re {messages[-1]['content']}"

    client = BeeClient(ask=ask)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    client.request("*swats you*")
    _wait_count(got, 1)
    client.request("*flies to another window*", tag="fly", remember=False)
    _wait_count(got, 2)
    client.request("sorry")
    _wait_count(got, 3)
    client.shutdown()
    # Разовая реплика уходит без истории...
    assert seen[1] == [{"role": "user", "content": "*flies to another window*"}]
    assert got[1] == ("fly", "re *flies to another window*", True)
    # ...и в историю не попадает.
    assert seen[2] == [
        {"role": "user", "content": "*swats you*"},
        {"role": "assistant", "content": "[neutral:low] re *swats you*"},
        {"role": "user", "content": "sorry"},
    ]


# --- ChatWindow ------------------------------------------------------------

class FakeClient(BeeClient):
    """Клиент без сети: запросы копятся, ответ выдаётся вручную."""

    def __init__(self):
        super().__init__()
        self.requests = []
        self.offline = False

    def likely_online(self):
        return not self.offline

    def request(self, prompt, tag=None, remember=True):
        self.requests.append((prompt, tag))

    def answer(self, text, from_model=True, mood=None, high=False, tag=None):
        """Ответ на последний (или указанный) запрос; mood — тег модели перед ответом."""
        tag = self.requests[-1][1] if tag is None else tag
        if mood is not None:
            self.mood_ready.emit(tag, mood, high)
        self.reply_ready.emit(tag, text, from_model)


@pytest.fixture
def chat_env(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "b.ini"), QSettings.IniFormat))
    bee.show()
    client = FakeClient()
    chat = ChatWindow(bee, client)
    yield bee, client, chat
    chat.hide()
    bee.bubble.hide()
    bee.close()


def test_chat_send_flow(chat_env):
    bee, client, chat = chat_env
    chat.open_chat()
    assert chat.isVisible()
    chat.input.setText("*gives you a flower*")
    chat.send()
    assert client.requests[0][0] == "*gives you a flower*"
    assert client.requests[0][1].action is Action.FLOWER
    assert bee.engine.mood is Mood.NEUTRAL  # ждём ответа модели
    assert not chat.input.isEnabled()
    assert "бзз…" in chat.history.toPlainText()
    chat.send()  # во время ожидания повторная отправка игнорируется
    assert len(client.requests) == 1

    client.answer("ура!")  # без тега — локальное правило
    assert bee.engine.mood is Mood.HAPPY
    assert chat.input.isEnabled()
    text = chat.history.toPlainText()
    assert "Ты: *gives you a flower*" in text and "🐝 ура!" in text
    assert "бзз…" not in text
    assert bee.bubble.text == "🐝 ура!"


def test_chat_offline_note_once_per_streak(chat_env):
    bee, client, chat = chat_env
    for msg in ("hi bee!", "hello"):
        chat.input.setText(msg)
        chat.send()
        client.answer("бзз", from_model=False)
    assert chat.history.toPlainText().count(OFFLINE_NOTE) == 1
    chat.input.setText("hey")
    chat.send()
    client.answer("привет", from_model=True)
    chat.input.setText("hey")
    chat.send()
    client.answer("бзз", from_model=False)
    assert chat.history.toPlainText().count(OFFLINE_NOTE) == 2


def test_chat_ignores_foreign_tags_and_second_open_raises(chat_env):
    bee, client, chat = chat_env
    client.reply_ready.emit(("menu", Action.PET), "чужое", True)
    assert "чужое" not in chat.history.toPlainText()
    chat.open_chat()
    chat.open_chat()
    chat.close()
    assert not chat.isVisible()


def test_chat_mood_via_classify_does_not_emit_action_performed(chat_env):
    bee, client, chat = chat_env
    emitted = []
    bee.action_performed.connect(emitted.append)
    chat.input.setText("*swats you*")
    chat.send()
    client.answer("BZZT", mood=Mood.ANGRY)
    assert bee.engine.mood is Mood.ANGRY and emitted == []


def test_menu_wiring_requests_and_shows_model_reply(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "m.ini"), QSettings.IniFormat))
    client = FakeClient()
    wire_menu_replies(bee, client)
    bee.menu_actions[Action.PET].trigger()
    assert client.requests[0][0] == "*pets you*"
    assert not bee.bubble.isVisible()  # заготовок нет: ждём ответ
    client.answer("мррр", from_model=True)
    assert bee.bubble.text == "🐝 мррр"
    bee.bubble.hide()


def test_menu_wiring_offline_shows_fallback_buzz(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "o.ini"), QSettings.IniFormat))
    client = FakeClient()
    wire_menu_replies(bee, client)
    bee.menu_actions[Action.SWAT].trigger()
    buzz = FALLBACK_BUZZ[0]
    client.answer(buzz, from_model=False)
    assert bee.bubble.isVisible() and bee.bubble.text == f"🐝 {buzz}"
    bee.bubble.hide()


def test_jitter_swat_requests_model(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "j.ini"), QSettings.IniFormat))
    client = FakeClient()
    wire_menu_replies(bee, client)
    bee._on_menu_action(Action.SWAT)  # путь мельтешения курсором
    [(prompt, tag)] = client.requests
    assert prompt == "*swats you*"
    assert isinstance(tag, MoodRequest) and (tag.source, tag.action) == ("menu", Action.SWAT)


# --- сброс истории при затухании ---------------------------------------------

def test_client_clear_history(qapp):
    seen = []

    def ask(messages):
        seen.append(messages)
        return "[angry:high] *STINGS YOU*"

    client = BeeClient(ask=ask)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    client.request("*swats you*")
    _wait_count(got, 1)
    client.clear_history()
    client.request("*pets you*")
    _wait_count(got, 2)
    client.shutdown()
    assert seen[1] == [{"role": "user", "content": "*pets you*"}]


class _HistoryClient(BeeClient):
    def __init__(self):
        super().__init__()
        self.cleared = 0

    def clear_history(self):
        self.cleared += 1


def test_history_reset_on_decay_to_neutral(qapp, tmp_path):
    from bee_pet.mood import ANGRY_DECAY, ANGRY_HIGH_DECAY, MoodEngine
    now = [1000.0]
    clock = lambda: now[0]
    bee = BeeWindow(settings=QSettings(str(tmp_path / "h.ini"), QSettings.IniFormat),
                    engine=MoodEngine(clock), clock=clock)
    client = _HistoryClient()
    wire_history_reset(bee, client)
    bee.apply_action(Action.SWAT)
    bee.apply_action(Action.FLOWER)       # angry high
    now[0] += ANGRY_HIGH_DECAY
    bee._on_mood_tick()                   # angry high -> angry: модель ещё злится
    assert bee.engine.mood is Mood.ANGRY and client.cleared == 0
    now[0] += ANGRY_DECAY
    bee._on_mood_tick()                   # angry -> neutral: история забыта
    assert bee.engine.mood is Mood.NEUTRAL and client.cleared == 1


def test_history_kept_after_sorry(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    client = _HistoryClient()
    wire_history_reset(bee, client)
    bee.apply_action(Action.SWAT)
    bee.apply_action(Action.SORRY)
    assert bee.engine.mood is Mood.NEUTRAL and client.cleared == 0


# --- настроение по ответу модели ---------------------------------------------

def test_client_mood_ready_before_reply_only_for_tagged_model_replies(qapp):
    answers = iter(["[Angry : HIGH] BZZT", "без тега", "[grumpy:low] bzz",
                    ConnectionRefusedError()])

    def ask(messages):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    client = BeeClient(ask=ask, backoff=0)
    events, replies = [], []
    client.mood_ready.connect(lambda tag, mood, high: events.append(("mood", tag, mood, high)))
    client.reply_ready.connect(lambda tag, text, fm: events.append(("reply", tag, fm)))
    client.reply_ready.connect(lambda *a: replies.append(a))
    for i in range(4):
        client.request("x", tag=i)
        _wait_count(replies, i + 1)
    client.shutdown()
    assert events == [("mood", 0, Mood.ANGRY, True), ("reply", 0, True),
                      ("reply", 1, True), ("reply", 2, True), ("reply", 3, False)]


def test_client_likely_online(qapp):
    now = [100.0]
    client = BeeClient(ask=lambda m: "", clock=lambda: now[0], backoff=30)
    assert client.likely_online()  # ещё не спрашивали
    client._on_done(None, "бзз", False, "hi", "", True)
    assert not client.likely_online()
    now[0] = 131.0
    assert client.likely_online()  # пауза прошла: пробуем снова


@pytest.fixture
def menu_env(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "mm.ini"), QSettings.IniFormat))
    client = FakeClient()
    wire_menu_replies(bee, client)
    changes = []
    bee.mood_changed.connect(changes.append)
    yield bee, client, changes
    bee.bubble.hide()
    bee.close()


def test_menu_online_mood_waits_for_reply_and_follows_tag(menu_env):
    bee, client, changes = menu_env
    bee.menu_actions[Action.SWAT].trigger()
    assert bee.engine.mood is Mood.NEUTRAL and changes == []
    client.answer("bzz~", mood=Mood.HAPPY)  # модель решила не злиться
    assert bee.engine.mood is Mood.HAPPY and len(changes) == 1
    assert bee.bubble.text == "🐝 bzz~"


def test_menu_online_model_angry_high(menu_env):
    bee, client, changes = menu_env
    bee.menu_actions[Action.FLOWER].trigger()
    client.answer("BZZT", mood=Mood.ANGRY, high=True)
    assert bee.engine.mood is Mood.ANGRY and bee.engine.angry_high
    assert changes[-1].became_angry_high


def test_menu_fallback_reply_applies_local_rule_once(menu_env):
    bee, client, changes = menu_env
    bee.menu_actions[Action.SWAT].trigger()
    assert changes == []
    client.answer(FALLBACK_BUZZ[0], from_model=False)
    assert bee.engine.mood is Mood.ANGRY and not bee.engine.angry_high
    assert len(changes) == 1


def test_menu_unparsable_tag_applies_local_rule(menu_env):
    bee, client, changes = menu_env
    bee.menu_actions[Action.SPRAY].trigger()
    client.answer("bzz", from_model=True)  # модель ответила, но mood_ready не было
    assert bee.engine.mood is Mood.SAD and len(changes) == 1


def test_menu_offline_applies_local_rule_immediately_not_twice(menu_env):
    bee, client, changes = menu_env
    client.offline = True
    bee.menu_actions[Action.SWAT].trigger()
    assert bee.engine.mood is Mood.ANGRY and len(changes) == 1
    bee.menu_actions[Action.FLOWER].trigger()
    assert bee.engine.angry_high and len(changes) == 2
    client.answer(FALLBACK_BUZZ[0], from_model=False, tag=client.requests[0][1])
    client.answer(FALLBACK_BUZZ[1], from_model=False)
    assert bee.engine.angry_high and len(changes) == 2


def test_jitter_swat_waits_for_model(menu_env):
    bee, client, changes = menu_env
    bee._on_menu_action(Action.SWAT)  # путь мельтешения курсором
    assert bee.engine.mood is Mood.NEUTRAL
    client.answer("BZZT", mood=Mood.ANGRY)
    assert bee.engine.mood is Mood.ANGRY


def test_chat_online_follows_model_tag(chat_env):
    bee, client, chat = chat_env
    chat.input.setText("*gives you a flower*")
    chat.send()
    assert bee.engine.mood is Mood.NEUTRAL
    client.answer("hmm", mood=Mood.CONFUSED)
    assert bee.engine.mood is Mood.CONFUSED


def test_chat_fallback_and_offline_local_rule_once(chat_env):
    bee, client, chat = chat_env
    changes = []
    bee.mood_changed.connect(changes.append)
    chat.input.setText("i love you")
    chat.send()
    client.answer("бзз", from_model=False)
    assert bee.engine.mood is Mood.LOVING and len(changes) == 1
    client.offline = True
    chat.input.setText("*sprays you with bug spray*")
    chat.send()
    assert bee.engine.mood is Mood.SAD and len(changes) == 2
    client.answer("бзз", from_model=False)
    assert len(changes) == 2


def test_foreign_replies_do_not_touch_mood(menu_env):
    bee, client, changes = menu_env
    client.mood_ready.emit(("wander", 1), Mood.ANGRY, True)
    client.reply_ready.emit(("wander", 1), "bzz", True)
    client.mood_ready.emit("t", Mood.SAD, False)
    client.reply_ready.emit("t", "bzz", False)
    assert bee.engine.mood is Mood.NEUTRAL and changes == []


def test_wander_buzz_never_changes_mood(qapp, tmp_path):
    from bee_pet.wander import WanderController
    bee = BeeWindow(settings=QSettings(str(tmp_path / "w.ini"), QSettings.IniFormat))
    client = BeeClient(ask=lambda m: "[angry:high] BZZZT")
    wire_menu_replies(bee, client)
    chat = ChatWindow(bee, client)
    wander = WanderController(bee, client)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    wander._request_buzz()
    _wait_count(got, 1)
    client.shutdown()
    assert got[0][2] is True
    assert bee.engine.mood is Mood.NEUTRAL and not bee.engine.angry_high
    chat.hide()
    bee.bubble.hide()
    bee.close()


def test_real_client_menu_flow_follows_tag(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "r.ini"), QSettings.IniFormat))
    client = BeeClient(ask=lambda m: "[happy:low] bzz!")
    wire_menu_replies(bee, client)
    got = []
    client.reply_ready.connect(lambda *a: got.append(a))
    bee.menu_actions[Action.SWAT].trigger()
    assert bee.engine.mood is Mood.NEUTRAL
    _wait_count(got, 1)
    client.shutdown()
    assert bee.engine.mood is Mood.HAPPY
    assert bee.bubble.text == "🐝 bzz!"
    bee.bubble.hide()
    bee.close()
