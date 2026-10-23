"""Тесты прогулок пчелы между окнами, переключения окон в простое и жужжания в полёте."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet import wander
from bee_pet.mood import Action, Mood, MoodEngine
from bee_pet.wander import (FLIGHT_PROMPT, WANDER_TAG, WanderController, fallback_corner,
                            landing_corner, landing_skip_reason, pick_landing, spot_covered)
from bee_pet.winapi import WindowInfo

OWN = 4242
SCREENS = [(0, 0, 1920, 1080)]
QT_SCREENS = [(QRect(0, 0, 1920, 1080), 1.0)]
AREA = QRect(0, 0, 1920, 1040)


def win(**kw) -> WindowInfo:
    base = dict(hwnd=1, title="t", class_name="Notepad", pid=100, process_name="notepad.exe",
                rect=(100, 100, 700, 500), elevated=False, is_tool=False, cloaked=False,
                minimized=False)
    base.update(kw)
    return WindowInfo(**base)


# --- политика ---------------------------------------------------------------

def test_landing_policy_skips():
    assert landing_skip_reason(win(), OWN) is None
    assert landing_skip_reason(win(pid=OWN), OWN)
    assert landing_skip_reason(win(minimized=True), OWN)
    assert landing_skip_reason(win(cloaked=True), OWN)
    assert landing_skip_reason(win(is_tool=True), OWN)
    assert landing_skip_reason(win(title=""), OWN)
    assert landing_skip_reason(win(rect=(0, 0, 99, 400)), OWN)
    assert landing_skip_reason(win(rect=(5000, 0, 5600, 400)), OWN, SCREENS)
    for cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
        assert landing_skip_reason(win(class_name=cls, process_name="explorer.exe"), OWN)


@pytest.mark.parametrize("name", ["windowsterminal.exe", "code.exe", "pycharm64.exe", "chrome.exe"])
def test_terminals_ide_and_any_app_allowed(name):
    assert landing_skip_reason(win(process_name=name), OWN) is None
    # для посадки неважны и права администратора
    assert landing_skip_reason(win(process_name=name, elevated=True), OWN) is None


def test_spot_covered():
    above = [win(rect=(0, 0, 400, 400))]
    assert spot_covered((200, 101), above)
    assert not spot_covered((500, 101), above)


def test_pick_landing_avoids_occluded_edge():
    # окно 2 целиком закрыто сверху окном 1 (оно выше по Z-порядку)
    ws = [win(hwnd=1, rect=(0, 0, 1000, 800), process_name="chrome.exe"),
          win(hwnd=2, rect=(100, 100, 700, 500))]
    for seed in range(20):
        target, spot = pick_landing(ws, OWN, random.Random(seed), SCREENS)
        assert target.hwnd == 1


def test_pick_landing_partly_covered_uses_open_part():
    ws = [win(hwnd=1, rect=(0, 0, 400, 300), process_name="chrome.exe"),
          win(hwnd=2, rect=(100, 100, 700, 500))]
    for seed in range(20):
        target, spot = pick_landing(ws, OWN, random.Random(seed), SCREENS, current_hwnd=1)
        assert target.hwnd == 2 and spot[0] >= 400 and spot[1] == 101


def test_own_and_hidden_windows_do_not_occlude():
    ws = [win(hwnd=1, pid=OWN, rect=(0, 0, 1920, 1080)),
          win(hwnd=3, minimized=True, rect=(0, 0, 1920, 1080)),
          win(hwnd=2)]
    target, _ = pick_landing(ws, OWN, random.Random(1), SCREENS)
    assert target.hwnd == 2


def test_pick_landing_prefers_other_window():
    ws = [win(hwnd=1, rect=(0, 0, 500, 400)), win(hwnd=2, rect=(600, 0, 1200, 400))]
    picks = {pick_landing(ws, OWN, random.Random(s), SCREENS, current_hwnd=1)[0].hwnd
             for s in range(20)}
    assert picks == {2}
    # других нет — можно и на то же
    assert pick_landing(ws[:1], OWN, random.Random(1), SCREENS, current_hwnd=1)[0].hwnd == 1
    assert pick_landing([], OWN, random.Random(1), SCREENS) is None


def test_landing_corner_sits_on_edge_and_clamps():
    pos = landing_corner((400, 300), QT_SCREENS, [AREA], 120)
    assert pos == QPoint(400 - 60, 300 - 120 + wander.LAND_OFFSET)
    # развёрнутое окно: кромка у верха экрана — пчела не вылезает за экран
    assert landing_corner((10, 1), QT_SCREENS, [AREA], 120) == QPoint(0, 0)
    scaled = [(QRect(0, 0, 1280, 720), 1.5)]
    assert landing_corner((600, 300), scaled, [QRect(0, 0, 1280, 700)], 120) == \
        QPoint(400 - 60, 200 - 120 + wander.LAND_OFFSET)


def test_fallback_corner_near_bottom():
    for s in range(20):
        pos = fallback_corner(AREA, 120, random.Random(s))
        assert 0 <= pos.x() <= 1920 - 120
        assert 1040 - 120 - wander.BOTTOM_JITTER <= pos.y() <= 1040 - 120


# --- контроллер --------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBee(QObject):
    mood_changed = Signal(object)
    drag_started = Signal()
    focus_switch_toggled = Signal(bool)

    def __init__(self):
        super().__init__()
        self.engine = MoodEngine()
        self.is_dragging = False
        self.focus_switch_enabled = True
        self.said: list[tuple[str, int]] = []
        self.flights: list[QPoint] = []
        self.cancelled = 0
        self._cb = None

    @property
    def is_flying(self):
        return self._cb is not None

    def width(self):
        return 120

    def say(self, text, duration_ms=4000):
        self.said.append((text, duration_ms))

    def fly_to(self, target, on_arrived=None):
        self.flights.append(target)
        self._cb = on_arrived or (lambda: None)

    def cancel_flight(self):
        self.cancelled += 1
        self._cb = None

    def land(self):
        cb, self._cb = self._cb, None
        cb()

    def act(self, action):
        change = self.engine.apply(action)
        self.mood_changed.emit(change)
        return change


class FakeWinapi:
    def __init__(self, windows, available=True):
        self.AVAILABLE = available
        self.windows = windows
        self.idle = 0.0
        self.foreground = 0
        self.focused: list[int] = []
        self.closed: list[int] = []

    def list_windows(self):
        return list(self.windows)

    def idle_seconds(self):
        return self.idle

    def foreground_window(self):
        return self.foreground

    def focus_window(self, hwnd):
        self.focused.append(hwnd)
        return True

    def close_window(self, hwnd):  # не должен вызываться никогда
        self.closed.append(hwnd)
        return True


class FakeClient(QObject):
    reply_ready = Signal(object, str, bool)

    def __init__(self):
        super().__init__()
        self.requests: list[tuple[str, object, bool]] = []

    def request(self, prompt, tag=None, remember=True):
        self.requests.append((prompt, tag, remember))

    def answer(self, text, from_model=True, tag=None):
        self.reply_ready.emit(tag if tag is not None else self.requests[-1][1], text, from_model)


def make(qapp, windows=None, available=True, **kw):
    bee = FakeBee()
    api = FakeWinapi(windows if windows is not None else [win(hwnd=7)], available)
    client = FakeClient()
    params = dict(winapi_module=api, own_pid=OWN, rng=random.Random(1), idle_focus_s=120,
                  screens_provider=lambda: QT_SCREENS, areas_provider=lambda: [AREA],
                  autostart=False)
    params.update(kw)
    ctl = WanderController(bee, client, **params)
    return bee, api, client, ctl


def test_wander_flies_to_window_edge(qapp):
    bee, api, client, ctl = make(qapp, [win(hwnd=7, rect=(100, 300, 700, 700))])
    assert ctl.wander_now()
    assert ctl.flying and ctl.target.hwnd == 7 and not ctl.focus_planned
    pos = bee.flights[0]
    assert pos.y() == 301 - 120 + wander.LAND_OFFSET
    assert 100 <= pos.x() + 60 < 700
    bee.land()
    assert not ctl.flying and ctl.current_hwnd == 7
    assert api.focused == [] and api.closed == []


def test_fallback_when_no_windows_or_no_winapi(qapp):
    for available, windows in ((True, []), (False, [win(hwnd=7)])):
        bee, api, client, ctl = make(qapp, windows, available)
        assert ctl.wander_now()
        assert ctl.target is None
        assert bee.flights[0].y() >= 1040 - 120 - wander.BOTTOM_JITTER
        bee.land()
        assert ctl.current_hwnd is None and api.focused == []


def test_no_wander_when_angry_dragging_or_flying(qapp):
    bee, api, client, ctl = make(qapp)
    bee.act(Action.SWAT)
    assert not ctl.wander_now()
    bee.act(Action.SORRY)
    bee.is_dragging = True
    assert not ctl.wander_now()
    bee.is_dragging = False
    bee.fly_to(QPoint(0, 0))  # чужой полёт (например, укус)
    assert not ctl.wander_now()
    assert len(bee.flights) == 1 and client.requests == []


def test_anger_cancels_wander_flight(qapp):
    bee, api, client, ctl = make(qapp)
    ctl.wander_now()
    bee.act(Action.SWAT)
    assert bee.cancelled == 1 and not ctl.flying
    # дальнейшая злость (угроза укуса) чужой полёт не отменяет
    bee.fly_to(QPoint(0, 0))
    bee.act(Action.FLOWER)
    bee.act(Action.SWAT)
    assert bee.cancelled == 1


def test_drag_cancels_wander_flight(qapp):
    bee, api, client, ctl = make(qapp)
    ctl.wander_now()
    bee.drag_started.emit()
    assert bee.cancelled == 1 and not ctl.flying and ctl.current_hwnd is None


def test_timer_schedules_wanders(qapp):
    bee, api, client, ctl = make(qapp, min_s=0.02, max_s=0.04, autostart=True)
    waited = 0
    while not bee.flights and waited < 1000:
        QTest.qWait(10)
        waited += 10
    assert len(bee.flights) == 1


# --- переключение окон в простое ---------------------------------------------

def focus_env(qapp, **kw):
    windows = [win(hwnd=1, rect=(0, 0, 500, 400), process_name="code.exe"),     # активное
               win(hwnd=2, rect=(600, 0, 1100, 400), elevated=True),            # админ
               win(hwnd=3, rect=(1200, 0, 1700, 400), elevated=None),           # неизвестно
               win(hwnd=4, rect=(100, 500, 900, 900), process_name="chrome.exe")]
    bee, api, client, ctl = make(qapp, windows, **kw)
    api.foreground = 1
    api.idle = 300.0
    return bee, api, client, ctl


def test_idle_focus_picks_non_foreground_non_elevated(qapp):
    for seed in range(10):
        bee, api, client, ctl = focus_env(qapp, rng=random.Random(seed))
        ctl.wander_now()
        assert ctl.target.hwnd == 4 and ctl.focus_planned
        bee.land()
        assert api.focused == [4] and api.closed == []


def test_no_focus_when_user_not_idle(qapp):
    bee, api, client, ctl = focus_env(qapp)
    api.idle = 5.0
    ctl.wander_now()
    assert not ctl.focus_planned
    bee.land()
    assert api.focused == []


def test_no_focus_if_user_came_back_mid_flight(qapp):
    bee, api, client, ctl = focus_env(qapp)
    ctl.wander_now()
    api.idle = 0.5
    bee.land()
    assert api.focused == []


def test_no_focus_when_toggle_off(qapp):
    bee, api, client, ctl = focus_env(qapp)
    bee.focus_switch_enabled = False
    ctl.wander_now()
    assert not ctl.focus_planned
    bee.land()
    assert api.focused == []
    # выключили посреди полёта
    bee.focus_switch_enabled = True
    ctl.wander_now()
    assert ctl.focus_planned
    bee.focus_switch_enabled = False
    bee.focus_switch_toggled.emit(False)
    bee.land()
    assert api.focused == []


def test_no_focus_if_target_moved_or_vanished(qapp):
    bee, api, client, ctl = focus_env(qapp)
    ctl.wander_now()
    api.windows = [w if w.hwnd != 4 else win(hwnd=4, rect=(300, 500, 1100, 900))
                   for w in api.windows]
    bee.land()
    assert api.focused == []
    bee.act(Action.HI)
    ctl.current_hwnd = None
    api.windows = api.windows[:3] + [win(hwnd=4, rect=(100, 500, 900, 900))]
    ctl.wander_now()
    api.windows = api.windows[:3]
    bee.land()
    assert api.focused == []


def test_bee_without_focus_attribute_defaults_on(qapp):
    bee, api, client, ctl = focus_env(qapp)
    del bee.focus_switch_enabled
    ctl.wander_now()
    bee.land()
    assert api.focused == [4]


# --- жужжание в полёте --------------------------------------------------------

def test_flight_buzz_requests_without_history_and_shows_bubble(qapp):
    bee, api, client, ctl = make(qapp)
    mood = bee.engine.mood
    ctl.wander_now()
    prompt, tag, remember = client.requests[0]
    assert prompt == FLIGHT_PROMPT and remember is False and tag[0] == WANDER_TAG
    client.answer("buzzz zz bzz")
    assert bee.said == [("🐝 buzzz zz bzz", wander.FLIGHT_BUZZ_MS)]
    assert bee.engine.mood is mood
    # заготовка тоже показывается
    bee.land()
    ctl.wander_now()
    client.answer("бззз... 🐝", from_model=False)
    assert bee.said[-1][0] == "🐝 бззз... 🐝"


def test_flight_buzz_ignores_foreign_and_stale_tags(qapp):
    bee, api, client, ctl = make(qapp)
    ctl.wander_now()
    stale = client.requests[0][1]
    client.answer("чат", tag="chat")
    bee.land()
    ctl.wander_now()
    client.answer("старое", tag=stale)
    assert bee.said == []


def test_flight_buzz_suppressed_when_angry(qapp):
    bee, api, client, ctl = make(qapp)
    ctl.wander_now()
    bee.act(Action.SWAT)
    client.answer("buzzz")
    assert bee.said == [] and bee.engine.mood is Mood.ANGRY
