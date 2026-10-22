"""Тесты клиента BeeLLM и мини-чата (offscreen, без реальной сети)."""
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
from bee_pet.app import wire_menu_replies
from bee_pet.bee_window import BeeWindow
from bee_pet.chat import OFFLINE_NOTE, ChatWindow
from bee_pet.llm import FALLBACK_BUZZ, BeeClient, ask_bee, clean_reply
from bee_pet.mood import Action, Mood


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


# --- ask_bee против заглушки сервера --------------------------------------------

def _serve(handler_body: bytes, status: int = 200):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Length", str(len(handler_body)))
            self.end_headers()
            self.wfile.write(handler_body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_ask_bee_success(monkeypatch):
    srv = _serve(json.dumps({"choices": [{"text": " бзз! "}]}).encode())
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        assert ask_bee("hi bee!") == "бзз!"
    finally:
        srv.shutdown()


def test_ask_bee_bad_json(monkeypatch):
    srv = _serve(b"not json")
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        with pytest.raises(ValueError):
            ask_bee("hi")
    finally:
        srv.shutdown()


def test_ask_bee_connection_refused(monkeypatch):
    srv = _serve(b"{}")
    port = srv.server_port
    srv.shutdown()
    srv.server_close()
    monkeypatch.setattr(llm, "BEE_URL", f"http://127.0.0.1:{port}")
    with pytest.raises(OSError):
        ask_bee("hi", timeout=2)


# --- BeeClient -------------------------------------------------------------

def _wait_reply(qapp, client, count=1, timeout=3000):
    got = []
    client.reply_ready.connect(lambda tag, text, fm: got.append((tag, text, fm)))
    waited = 0
    while len(got) < count and waited < timeout:
        QTest.qWait(20)
        waited += 20
    return got


def test_client_model_reply(qapp):
    client = BeeClient(ask=lambda p: f"эхо {p}")
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

    def failing(prompt):
        calls.append(prompt)
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


# --- ChatWindow ------------------------------------------------------------

class FakeClient(BeeClient):
    """Клиент без сети: запросы копятся, ответ выдаётся вручную."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, prompt, tag=None):
        self.requests.append((prompt, tag))

    def answer(self, text, from_model=True):
        self.reply_ready.emit(self.requests[-1][1], text, from_model)


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
    assert bee.engine.mood is Mood.HAPPY
    assert not chat.input.isEnabled()
    assert "бзз…" in chat.history.toPlainText()
    chat.send()  # во время ожидания повторная отправка игнорируется
    assert len(client.requests) == 1

    client.answer("ура!")
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
    assert bee.engine.mood is Mood.ANGRY and emitted == []


def test_menu_wiring_requests_and_shows_model_reply(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "m.ini"), QSettings.IniFormat))
    client = FakeClient()
    wire_menu_replies(bee, client)
    bee.menu_actions[Action.PET].trigger()
    assert client.requests[0][0] == "*pets you*"
    canned = bee.bubble.text
    client.answer("мррр", from_model=False)
    assert bee.bubble.text == canned
    client.answer("мррр", from_model=True)
    assert bee.bubble.text == "🐝 мррр"
    bee.bubble.hide()
