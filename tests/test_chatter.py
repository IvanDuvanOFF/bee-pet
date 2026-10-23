"""Тесты жужжания самой по себе (ChatterController): частота по настроению, пропуски, пузырь."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet import chatter
from bee_pet.bee_window import BeeWindow
from bee_pet.chatter import (ANGRY, CALM, CHATTER_TAG, FURIOUS, PING_PROMPT,
                             ChatterController)
from bee_pet.llm import BeeClient
from bee_pet.mood import Mood
from bee_pet.mood_sync import MoodSync

FAST = dict(calm_interval_s=(0.03, 0.03), angry_interval_s=(0.03, 0.03),
            furious_interval_s=(0.03, 0.03))


def wait_for(cond, timeout_ms=2000):
    """Крутит цикл событий, пока cond() не станет истинным (в PySide6 нет QTest.qWaitFor)."""
    waited = 0
    while not cond():
        assert waited < timeout_ms, "не дождались условия"
        QTest.qWait(10)
        waited += 10


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeClient(QObject):
    """Клиент без сети: запросы копятся, ответ выдаётся вручную."""
    reply_ready = Signal(object, str, bool)
    mood_ready = Signal(object, object, bool)

    def __init__(self):
        super().__init__()
        self.requests = []

    def likely_online(self):
        return True

    def request(self, prompt, tag=None, remember=True, use_history=None):
        self.requests.append((prompt, tag, remember, use_history))

    def answer(self, text, tag=None, from_model=True):
        tag = self.requests[-1][1] if tag is None else tag
        self.reply_ready.emit(tag, text, from_model)


class FakeBite:
    IDLE = "idle"

    def __init__(self):
        self.state = self.IDLE


@pytest.fixture
def env(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "b.ini"), QSettings.IniFormat))
    bee.show()
    client, bite = FakeClient(), FakeBite()
    created = []

    def make(**kw):
        params = dict(FAST)
        params.update(kw)
        ctl = ChatterController(bee, client, bite, **params)
        created.append(ctl)
        return ctl

    yield bee, client, bite, make
    for ctl in created:
        ctl._timer.stop()
    bee.bubble.hide()
    bee.close()


def test_ping_sends_neutral_prompt_with_history_without_remembering(env):
    bee, client, bite, make = env
    make()
    QTest.qWait(60)
    assert client.requests
    prompt, tag, remember, use_history = client.requests[0]
    assert prompt == PING_PROMPT == "hmm"
    assert tag[0] == CHATTER_TAG
    assert remember is False and use_history is True


def test_intervals_chosen_from_range_per_mood(env):
    bee, client, bite, make = env
    ctl = make(calm_interval_s=(90, 240), angry_interval_s=(6, 15),
               furious_interval_s=(1.5, 4), rng=random.Random(1), autostart=False)
    assert ctl.category == CALM
    assert all(90 <= ctl.next_interval_s() <= 240 for _ in range(50))
    bee.apply_model_mood(Mood.ANGRY, False)
    assert ctl.category == ANGRY
    assert all(6 <= ctl.next_interval_s() <= 15 for _ in range(50))
    bee.apply_model_mood(Mood.ANGRY, True)
    assert ctl.category == FURIOUS
    assert all(1.5 <= ctl.next_interval_s() <= 4 for _ in range(50))


def test_default_constants():
    assert chatter.CALM_INTERVAL_S == (90.0, 240.0)
    assert chatter.ANGRY_INTERVAL_S == (6.0, 15.0)
    assert chatter.FURIOUS_INTERVAL_S == (1.5, 4.0)


def test_reschedule_on_category_change(env):
    bee, client, bite, make = env
    ctl = make(calm_interval_s=(100, 100), angry_interval_s=(0.03, 0.03))
    assert 99_000 <= ctl._timer.remainingTime() <= 100_000
    bee.apply_model_mood(Mood.ANGRY, False)  # разозлилась — жужжит скоро
    assert ctl._timer.remainingTime() <= 30
    QTest.qWait(60)
    assert client.requests
    bee.apply_model_mood(Mood.NEUTRAL, False)  # успокоилась — снова редко
    assert ctl._timer.remainingTime() > 90_000


def test_same_category_does_not_reschedule(env):
    bee, client, bite, make = env
    ctl = make(angry_interval_s=(100, 100))
    bee.apply_model_mood(Mood.ANGRY, False)
    QTest.qWait(30)
    before = ctl._timer.remainingTime()
    bee.apply_model_mood(Mood.ANGRY, False)  # та же категория — таймер не трогаем
    assert ctl._timer.remainingTime() <= before


def test_no_new_ping_while_previous_pending(env):
    bee, client, bite, make = env
    ctl = make()
    QTest.qWait(150)
    assert len(client.requests) == 1  # ответа нет — новых пингов тоже
    assert ctl.pending
    client.answer("buzz")
    assert not ctl.pending
    QTest.qWait(60)
    assert len(client.requests) >= 2


def test_lost_reply_times_out(env):
    bee, client, bite, make = env
    now = [0.0]
    make(clock=lambda: now[0], pending_timeout_s=10)
    QTest.qWait(60)
    assert len(client.requests) == 1
    now[0] = 11.0
    QTest.qWait(60)
    assert len(client.requests) >= 2


def test_no_ping_while_bite_busy(env):
    bee, client, bite, make = env
    bite.state = "warning"
    make()
    QTest.qWait(100)
    assert client.requests == []
    bite.state = bite.IDLE
    QTest.qWait(60)
    assert client.requests


def test_reply_not_shown_if_bite_started_meanwhile(env):
    bee, client, bite, make = env
    make()
    wait_for(lambda: client.requests)  # дождаться первого пинга
    bee.say("⚠️ 5…")
    bite.state = "warning"
    client.answer("BZZZ")
    assert bee.bubble.text == "⚠️ 5…"


def test_reply_shown_with_prefix(env):
    bee, client, bite, make = env
    make()
    wait_for(lambda: client.requests)  # дождаться первого пинга
    client.answer("zzz buzzz")
    assert bee.bubble.isVisible()
    assert bee.bubble.text == "🐝 zzz buzzz"


def test_offline_fallback_shown_too(env):
    bee, client, bite, make = env
    make()
    wait_for(lambda: client.requests)  # дождаться первого пинга
    client.answer("бззз... 🐝", from_model=False)
    assert bee.bubble.text == "🐝 бззз... 🐝"


def test_foreign_tags_ignored(env):
    bee, client, bite, make = env
    make()
    wait_for(lambda: client.requests)  # дождаться первого пинга
    client.answer("чужое", tag="menu")
    assert not bee.bubble.isVisible()


def test_does_not_overwrite_foreign_bubble(env):
    bee, client, bite, make = env
    bee.say("🐝 ответ меню", 10_000)
    make()
    QTest.qWait(100)
    assert client.requests == []  # чужой пузырь — не пингуем
    bee.bubble.hide()
    QTest.qWait(60)
    assert client.requests
    bee.say("🐝 ответ чата", 10_000)  # пока ждали ответа, появился чужой текст
    client.answer("buzz")
    assert bee.bubble.text == "🐝 ответ чата"


def test_own_bubble_replaced_by_next_ping(env):
    bee, client, bite, make = env
    make(bubble_ms=10_000)
    wait_for(lambda: client.requests)  # дождаться первого пинга
    client.answer("one")
    QTest.qWait(60)
    assert len(client.requests) == 2  # свой пузырь не мешает
    client.answer("two")
    assert bee.bubble.text == "🐝 two"


def test_mood_never_changes_from_ping_reply(env):
    """Настоящие BeeClient и MoodSync: ответ «[angry:high]» на пинг спокойной пчеле не злит её."""
    bee, _, bite, _ = env
    client = BeeClient(ask=lambda messages: "[angry:high] *STINGS YOU* BZZZ")
    MoodSync(bee, client)
    changes = []
    bee.mood_changed.connect(changes.append)
    ctl = ChatterController(bee, client, bite, **FAST)
    for _ in range(100):
        QTest.qWait(20)
        if bee.bubble.isVisible():
            break
    ctl._timer.stop()
    client.shutdown()
    QTest.qWait(20)
    assert bee.bubble.text == "🐝 *STINGS YOU* BZZZ"
    assert bee.engine.mood is Mood.NEUTRAL and not bee.engine.angry_high
    assert changes == []  # mood_changed не испускался — укусу не с чего начаться
    assert bite.state == bite.IDLE
