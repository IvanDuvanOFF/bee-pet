"""Тесты политики и последовательности укуса (без Windows, с заглушками)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random
import re
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet import biting, winapi
from bee_pet.biting import BiteController, eligible, pick_target, physical_to_logical
from bee_pet.mood import Action, Mood, MoodChange, MoodEngine
from bee_pet.winapi import WindowInfo

OWN = 4242
SCREENS = [(0, 0, 1920, 1080)]


def win(**kw) -> WindowInfo:
    base = dict(hwnd=1, title="t", class_name="Notepad", pid=100, process_name="notepad.exe",
                rect=(100, 100, 700, 500), elevated=False, is_tool=False, cloaked=False,
                minimized=False)
    base.update(kw)
    return WindowInfo(**base)


# --- политика ---------------------------------------------------------------

def test_whitelist_basic():
    assert eligible(win(), OWN)
    assert eligible(win(process_name="calc.exe"), OWN)
    assert eligible(win(process_name="calculatorapp.exe", class_name="ApplicationFrameWindow"), OWN)
    assert not eligible(win(process_name="chrome.exe"), OWN)
    assert not eligible(win(process_name=""), OWN)


def test_custom_whitelist():
    assert not eligible(win(), OWN, {"calc.exe"})
    assert eligible(win(process_name="mspaint.exe"), OWN, {"mspaint.exe"})


def test_explorer_only_file_explorer_class():
    assert eligible(win(process_name="explorer.exe", class_name="CabinetWClass"), OWN)
    for cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                "NotifyIconOverflowWindow", "SomethingElse"):
        assert not eligible(win(process_name="explorer.exe", class_name=cls), OWN)


def test_blocked_classes_even_if_whitelisted():
    assert not eligible(win(class_name="Progman"), OWN)


@pytest.mark.parametrize("name", ["windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
                                  "conhost.exe", "openconsole.exe", "wt.exe", "code.exe",
                                  "idea64.exe", "pycharm64.exe", "devenv.exe"])
def test_blocked_processes_even_if_whitelisted(name):
    assert not eligible(win(process_name=name), OWN, {name, "notepad.exe"})


def test_own_pid_elevation_and_flags():
    assert not eligible(win(pid=OWN), OWN)
    assert not eligible(win(elevated=True), OWN)
    assert not eligible(win(elevated=None), OWN)
    assert not eligible(win(minimized=True), OWN)
    assert not eligible(win(cloaked=True), OWN)
    assert not eligible(win(is_tool=True), OWN)


def test_size_and_screens():
    assert not eligible(win(rect=(0, 0, 99, 400)), OWN)
    assert not eligible(win(rect=(0, 0, 400, 59)), OWN)
    assert eligible(win(rect=(0, 0, 100, 60)), OWN)
    assert eligible(win(), OWN, screens=SCREENS)
    assert not eligible(win(rect=(5000, 100, 5600, 500)), OWN, screens=SCREENS)


def test_pick_target_prefers_top_and_is_deterministic():
    ws = [win(hwnd=1, process_name="chrome.exe"), win(hwnd=2), win(hwnd=3), win(hwnd=4),
          win(hwnd=5)]
    picks = {pick_target(ws, OWN, rng=random.Random(s)).hwnd for s in range(30)}
    assert picks <= {2, 3, 4}  # только первые три подходящих
    a = pick_target(ws, OWN, rng=random.Random(7))
    b = pick_target(ws, OWN, rng=random.Random(7))
    assert a == b
    assert pick_target([win(process_name="chrome.exe")], OWN) is None
    assert pick_target([], OWN) is None


def test_physical_to_logical():
    scaled = [(QRect(0, 0, 1280, 720), 1.5)]
    assert physical_to_logical((300, 150), scaled) == QPoint(200, 100)
    two = [(QRect(0, 0, 1920, 1080), 1.0), (QRect(1920, 0, 1280, 720), 1.5)]
    # второй экран: родной старт = 1920*1.5 = 2880
    assert physical_to_logical((2880 + 300, 150), two) == QPoint(1920 + 200, 100)
    assert physical_to_logical((10, 20), two) == QPoint(10, 20)


# --- последовательность ------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBee(QObject):
    mood_changed = Signal(object)
    biting_toggled = Signal(bool)
    drag_started = Signal()
    drag_finished = Signal()

    def __init__(self):
        super().__init__()
        self.engine = MoodEngine()
        self.biting_enabled = True
        self.is_dragging = False
        self.stinging = False
        self.said: list[str] = []
        self.flights: list[QPoint] = []
        self.cancelled = 0
        self._cb = None

    def width(self):
        return 120

    def update(self):
        pass

    def say(self, text, duration_ms=4000):
        self.said.append(text)

    def fly_to(self, target, on_arrived=None):
        self.flights.append(target)
        self._cb = on_arrived

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

    def model(self, mood, angry_high):
        """Как BeeWindow.apply_model_mood: настроение из тега ответа модели."""
        change = self.engine.set_from_model(mood, angry_high)
        self.mood_changed.emit(change)
        return change


class FakeWinapi:
    AVAILABLE = True

    def __init__(self, windows):
        self.windows = windows
        self.closed: list[int] = []

    def list_windows(self):
        return list(self.windows)

    def close_window(self, hwnd):
        self.closed.append(hwnd)
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(qapp, windows=None, **kw):
    bee = FakeBee()
    api = FakeWinapi(windows if windows is not None else [win(hwnd=7)])
    clock = Clock()
    params = dict(winapi_module=api, own_pid=OWN, rng=random.Random(1), clock=clock,
                  countdown_s=3, tick_ms=10, cooldown_s=300, sting_ms=10,
                  screens_provider=lambda: [(QRect(0, 0, 1920, 1080), 1.0)])
    params.update(kw)
    ctl = BiteController(bee, **params)
    return bee, api, clock, ctl


def rage(bee):
    """Шлёпнуть, потом задобрить цветком: angry-high."""
    bee.act(Action.SWAT)
    return bee.act(Action.FLOWER)


def wait_state(ctl, state, ms=1000):
    deadline = 0
    while ctl.state != state and deadline < ms:
        QTest.qWait(10)
        deadline += 10
    assert ctl.state == state


def test_happy_path_closes_exactly_once(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    assert ctl.state == ctl.FLYING and len(bee.flights) == 1
    # цель: заголовок на 1/3 ширины окна (100..700 -> x=300, y=100)
    assert bee.flights[0] == QPoint(300 - 60, 100 - 120 + 30)
    bee.land()
    assert ctl.state == ctl.WARNING
    assert bee.said[0] == "3…"
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]
    # пузырь — только обратный отсчёт, после укуса пчела молчит
    assert bee.said == ["3…", "3…2…", "3…2…1…"]
    assert all(re.fullmatch(r"(\d…)+", s) for s in bee.said)
    # тот же приступ: повторный angry-high ничего не делает
    bee.act(Action.FLOWER)
    bee.act(Action.SWAT)
    clock.t += 10_000
    assert ctl.state == ctl.IDLE and len(bee.flights) == 1 and api.closed == [7]


def test_sorry_during_countdown_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    bee.act(Action.SORRY)
    assert ctl.state == ctl.IDLE
    QTest.qWait(150)
    assert api.closed == []
    assert bee.said == ["3…"]  # отмена без реплики: отвечает модель


def test_sorry_during_flight_cancels_flight(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    bee.act(Action.SORRY)
    assert bee.cancelled == 1 and ctl.state == ctl.IDLE and api.closed == []


def test_target_vanished_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    api.windows = []
    QTest.qWait(150)
    assert ctl.state == ctl.IDLE and api.closed == []


def test_target_moved_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    api.windows = [win(hwnd=7, rect=(200, 100, 800, 500))]
    QTest.qWait(150)
    assert ctl.state == ctl.IDLE and api.closed == []


def test_target_becomes_ineligible_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    api.windows = [win(hwnd=7, minimized=True)]
    QTest.qWait(150)
    assert ctl.state == ctl.IDLE and api.closed == []


def test_toggle_off_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    bee.biting_enabled = False
    bee.biting_toggled.emit(False)
    assert ctl.state == ctl.IDLE
    QTest.qWait(150)
    assert api.closed == []


def test_drag_cancels(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    bee.is_dragging = True
    bee.drag_started.emit()
    assert ctl.state == ctl.IDLE and api.closed == []


def test_disabled_or_unavailable_does_nothing(qapp):
    bee, api, clock, ctl = make(qapp)
    bee.biting_enabled = False
    rage(bee)
    assert ctl.state == ctl.IDLE and not bee.flights
    bee2, api2, _, ctl2 = make(qapp)
    api2.AVAILABLE = False
    rage(bee2)
    assert not bee2.flights


def test_no_eligible_window_does_nothing(qapp):
    bee, api, clock, ctl = make(qapp, windows=[win(process_name="chrome.exe")])
    rage(bee)
    assert ctl.state == ctl.IDLE and not bee.flights


def test_unknown_elevation_never_closed(qapp):
    bee, api, clock, ctl = make(qapp, windows=[win(elevated=None)])
    rage(bee)
    assert not bee.flights and api.closed == []


def test_cooldown_and_new_episode(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    bee.land()
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]
    # приступ кончился (sorry -> neutral), новый приступ внутри паузы — ничего
    bee.act(Action.SORRY)
    clock.t += 60
    rage(bee)
    assert ctl.state == ctl.IDLE and len(bee.flights) == 1
    # после паузы новый приступ работает
    bee.act(Action.SORRY)
    clock.t += 301
    rage(bee)
    assert ctl.state == ctl.FLYING and len(bee.flights) == 2


def test_cancelled_attempt_does_not_use_up_episode(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    bee.act(Action.SORRY)  # отмена, укуса не было, пауза не началась
    rage(bee)
    assert ctl.state == ctl.FLYING


def test_decay_to_plain_angry_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    change = bee.engine._set(Mood.ANGRY, False)
    bee.mood_changed.emit(change)
    assert ctl.state == ctl.IDLE and api.closed == []


def test_inert_on_linux_by_default(qapp):
    if winapi.AVAILABLE:
        pytest.skip("только не-Windows")
    assert winapi.list_windows() == [] and winapi.close_window(1) is False


def test_only_winapi_close_posts_messages():
    """Ни один модуль, кроме winapi.py, не трогает чужие окна и процессы."""
    root = Path(biting.__file__).parent
    forbidden = re.compile(r"PostMessage|SendMessage|TerminateProcess|DestroyWindow|taskkill|"
                           r"WM_QUIT|os\.kill|subprocess", re.I)
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "winapi.py":
            assert len(re.findall(r"PostMessageW\(hwnd", text)) == 1
            code = re.sub(r'""".*?"""|#.*', "", text, flags=re.S)
            assert not re.search(r"TerminateProcess|DestroyWindow|taskkill|WM_QUIT|SendMessage",
                                 code, re.I)
        else:
            code = re.sub(r'""".*?"""|#.*', "", text, flags=re.S)
            assert not forbidden.search(code), path.name


# --- настроение от модели ----------------------------------------------------

def test_model_angry_high_starts_bite_and_model_neutral_cancels(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    bee.model(Mood.ANGRY, False)
    assert ctl.state == ctl.IDLE
    bee.model(Mood.ANGRY, True)  # модель ответила [angry:high]
    assert ctl.state == ctl.FLYING and len(bee.flights) == 1
    bee.land()
    assert ctl.state == ctl.WARNING
    bee.model(Mood.NEUTRAL, False)  # после sorry модель ответила [neutral:low]
    assert ctl.state == ctl.IDLE
    QTest.qWait(150)
    assert api.closed == []


def test_model_angry_high_closes_window(qapp):
    bee, api, clock, ctl = make(qapp)
    bee.model(Mood.ANGRY, True)
    bee.land()
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]


# --- повторные попытки ---------------------------------------------------------

def grab(bee):
    bee.is_dragging = True
    bee.drag_started.emit()


def release(bee):
    bee.is_dragging = False
    bee.drag_finished.emit()


def test_angry_high_while_dragged_bites_after_release(qapp):
    """Пчелу держат 8 с (дважды *grabs your wings*): укус после отпускания."""
    bee, api, clock, ctl = make(qapp)
    grab(bee)
    bee.act(Action.GRAB)
    bee.act(Action.GRAB)
    assert bee.engine.angry_high and ctl.state == ctl.IDLE and not bee.flights
    release(bee)
    assert ctl.state == ctl.FLYING and len(bee.flights) == 1
    bee.land()
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]


def test_release_when_not_angry_high_does_nothing(qapp):
    bee, api, clock, ctl = make(qapp)
    grab(bee)
    bee.act(Action.SWAT)  # просто angry
    release(bee)
    assert ctl.state == ctl.IDLE and not bee.flights


def test_retry_on_further_provocation_while_angry_high(qapp):
    """angry-high уже был (укус был невозможен), новая провокация без became — укус."""
    bee, api, clock, ctl = make(qapp, windows=[])
    rage(bee)
    assert ctl.state == ctl.IDLE  # нет окон
    api.windows = [win(hwnd=7)]
    change = bee.act(Action.SWAT)
    assert change.angry_high and not change.became_angry_high
    assert ctl.state == ctl.FLYING and len(bee.flights) == 1


def test_retry_does_not_start_second_sequence(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    assert ctl.state == ctl.FLYING
    bee.act(Action.SWAT)
    release(bee)  # лишний drag_finished тоже не мешает
    assert ctl.state == ctl.FLYING and len(bee.flights) == 1
    bee.land()
    bee.act(Action.FLOWER)
    assert ctl.state == ctl.WARNING and len(bee.flights) == 1
    assert ctl._target is not None and ctl._target.hwnd == 7


def test_retries_respect_episode_and_cooldown(qapp):
    bee, api, clock, ctl = make(qapp)
    rage(bee)
    bee.land()
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]
    clock.t += 10_000  # пауза прошла, но приступ тот же
    bee.act(Action.SWAT)
    grab(bee)
    release(bee)
    assert ctl.state == ctl.IDLE and len(bee.flights) == 1
    # новый приступ внутри паузы: повторные попытки тоже отказывают
    bee2, api2, clock2, ctl2 = make(qapp)
    rage(bee2)
    bee2.land()
    wait_state(ctl2, ctl2.IDLE)
    bee2.act(Action.SORRY)
    clock2.t += 60
    rage(bee2)
    bee2.act(Action.SWAT)
    grab(bee2)
    release(bee2)
    assert ctl2.state == ctl2.IDLE and len(bee2.flights) == 1


def test_drag_mid_countdown_cancels_then_retries_after_release(qapp):
    bee, api, clock, ctl = make(qapp, tick_ms=50)
    rage(bee)
    bee.land()
    assert ctl.state == ctl.WARNING
    grab(bee)
    assert ctl.state == ctl.IDLE
    bee.act(Action.SWAT)  # провокация, пока держат: не стартует
    QTest.qWait(120)
    assert ctl.state == ctl.IDLE and len(bee.flights) == 1 and api.closed == []
    release(bee)
    assert ctl.state == ctl.FLYING and len(bee.flights) == 2
    bee.land()
    wait_state(ctl, ctl.IDLE)
    assert api.closed == [7]


def test_repeated_refusal_logged_once(qapp, caplog):
    bee, api, clock, ctl = make(qapp)
    bee.biting_enabled = False
    with caplog.at_level("INFO", logger="bee_pet.biting"):
        rage(bee)
        bee.act(Action.SWAT)
        bee.act(Action.FLOWER)
    refusals = [r for r in caplog.records
                if r.levelname == "INFO" and "не кусаю" in r.getMessage()]
    assert len(refusals) == 1
