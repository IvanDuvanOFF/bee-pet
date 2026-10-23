"""Тесты злости от удержания мышкой (HoldController)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet.app import wire_menu_replies
from bee_pet.bee_window import BeeWindow
from bee_pet.hold import GRAB_PROMPT, HOLD_TAG, RELEASE_PROMPT, HoldController
from bee_pet.llm import BeeClient
from bee_pet.mood import Action, Mood
from bee_pet.mood_sync import MoodRequest, MoodSync

ANGRY_S, FURIOUS_S = 0.06, 0.2


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeSync:
    """Записывает запросы вместо отправки модели."""

    def __init__(self):
        self.requests = []

    def request(self, prompt, action, source):
        self.requests.append((prompt, action, source))


@pytest.fixture
def env(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "h.ini"), QSettings.IniFormat))
    bee.show()
    sync = FakeSync()
    hold = HoldController(bee, sync, angry_s=ANGRY_S, furious_s=FURIOUS_S)
    yield bee, sync, hold
    bee.bubble.hide()
    bee.close()


def grab(bee):
    QTest.mousePress(bee, Qt.LeftButton, pos=QPoint(60, 60))
    QTest.mouseMove(bee, QPoint(80, 80))
    QTest.mouseMove(bee, QPoint(90, 90))
    assert bee.is_dragging


def release(bee):
    QTest.mouseRelease(bee, Qt.LeftButton, pos=QPoint(90, 90))
    assert not bee.is_dragging


GRAB = (GRAB_PROMPT, Action.GRAB, HOLD_TAG)
RELEASE = (RELEASE_PROMPT, Action.RELEASE, HOLD_TAG)


def test_prompts():
    assert GRAB_PROMPT == "*grabs your wings*"
    assert RELEASE_PROMPT == "*lets you go* sorry"


def test_short_drag_does_nothing(env):
    bee, sync, hold = env
    grab(bee)
    QTest.qWait(20)
    release(bee)
    QTest.qWait(int(FURIOUS_S * 1000) + 50)
    assert sync.requests == []


def test_click_without_drag_does_nothing(env):
    bee, sync, hold = env
    QTest.mousePress(bee, Qt.LeftButton, pos=QPoint(60, 60))
    QTest.qWait(int(FURIOUS_S * 1000) + 50)
    QTest.mouseRelease(bee, Qt.LeftButton, pos=QPoint(60, 60))
    assert sync.requests == []


def test_held_long_grabs_once_then_twice(env):
    bee, sync, hold = env
    grab(bee)
    QTest.qWait(int(ANGRY_S * 1000) + 40)
    assert sync.requests == [GRAB] and hold.stage == 1
    QTest.qWait(int((FURIOUS_S - ANGRY_S) * 1000) + 40)
    assert sync.requests == [GRAB, GRAB] and hold.stage == 2
    release(bee)  # после второй стадии отпускание ничего не шлёт
    QTest.qWait(50)
    assert sync.requests == [GRAB, GRAB]


def test_release_between_stages_says_sorry(env):
    bee, sync, hold = env
    grab(bee)
    QTest.qWait(int(ANGRY_S * 1000) + 40)
    release(bee)
    assert sync.requests == [GRAB, RELEASE]
    QTest.qWait(int(FURIOUS_S * 1000) + 50)  # вторая стадия после отпускания не наступает
    assert sync.requests == [GRAB, RELEASE] and hold.stage == 0


def test_new_drag_resets_stages(env):
    bee, sync, hold = env
    grab(bee)
    QTest.qWait(int(ANGRY_S * 1000) + 40)
    release(bee)
    sync.requests.clear()
    grab(bee)
    QTest.qWait(20)
    release(bee)  # быстро: снова обычный перенос
    assert sync.requests == []
    grab(bee)
    QTest.qWait(int(ANGRY_S * 1000) + 40)
    assert sync.requests == [GRAB]  # первая стадия, а не вторая
    release(bee)
    assert sync.requests == [GRAB, RELEASE]


# --- ответы в пузыре и настроение через MoodSync ------------------------------

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

    def answer(self, text, from_model=True, mood=None, high=False):
        tag = self.requests[-1][1]
        if mood is not None:
            self.mood_ready.emit(tag, mood, high)
        self.reply_ready.emit(tag, text, from_model)


@pytest.fixture
def wired(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "w.ini"), QSettings.IniFormat))
    bee.show()
    client = FakeClient()
    sync = MoodSync(bee, client)
    wire_menu_replies(bee, client, sync)
    HoldController(bee, sync, angry_s=ANGRY_S, furious_s=FURIOUS_S)
    yield bee, client
    bee.bubble.hide()
    bee.close()


def test_hold_reply_shown_and_mood_follows_model(wired):
    bee, client = wired
    grab(bee)
    QTest.qWait(int(ANGRY_S * 1000) + 40)
    [(prompt, tag)] = client.requests
    assert prompt == GRAB_PROMPT and isinstance(tag, MoodRequest) and tag.source == HOLD_TAG
    assert bee.engine.mood is Mood.NEUTRAL  # ждём ответа модели
    client.answer("BZZT! let go!", mood=Mood.ANGRY)
    assert bee.bubble.text == "🐝 BZZT! let go!"
    assert bee.engine.mood is Mood.ANGRY and not bee.engine.angry_high
    release(bee)
    assert client.requests[-1][0] == RELEASE_PROMPT
    client.answer("hmph. okay.", mood=Mood.NEUTRAL)
    assert bee.bubble.text == "🐝 hmph. okay." and bee.engine.mood is Mood.NEUTRAL


def test_hold_offline_shows_fallback_and_local_rules(wired):
    bee, client = wired
    client.offline = True
    grab(bee)
    QTest.qWait(int(FURIOUS_S * 1000) + 60)
    assert [p for p, _ in client.requests] == [GRAB_PROMPT, GRAB_PROMPT]
    assert bee.engine.mood is Mood.ANGRY and bee.engine.angry_high  # локальные правила сразу
    client.answer("бззз... 🐝", from_model=False)
    assert bee.bubble.text == "🐝 бззз... 🐝"
    assert bee.engine.angry_high  # заготовка не применяет правило второй раз
    release(bee)
