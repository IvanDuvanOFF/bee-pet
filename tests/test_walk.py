"""Тесты ходьбы пчелы по кромке окна и панели задач (WalkController) и поз ходьбы."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.mood import Action, Mood
from bee_pet.painter import feet_y
from bee_pet.walk import WalkController
from bee_pet.winapi import WindowInfo

OWN = 4242
SCREEN = QRect(0, 0, 1920, 1080)
AREA = QRect(0, 0, 1920, 1040)        # панель задач снизу, 40 px
QT_SCREENS = [(SCREEN, 1.0)]
FEET = feet_y(120)
FAST = dict(pause_s=(0.01, 0.02), poll_ms=10, track_ms=60_000, speed=4000.0,
            land_after_drop_s=0.01, land_after_calm_s=0.01)


def wait_for(cond, timeout_ms=3000):
    """Крутит цикл событий, пока cond() не станет истинным."""
    waited = 0
    while not cond():
        assert waited < timeout_ms, "не дождались условия"
        QTest.qWait(5)
        waited += 5


def win(**kw) -> WindowInfo:
    base = dict(hwnd=7, title="t", class_name="Notepad", pid=100, process_name="notepad.exe",
                rect=(300, 400, 1100, 800), elevated=False, is_tool=False, cloaked=False,
                minimized=False)
    base.update(kw)
    return WindowInfo(**base)


class FakeWinapi:
    def __init__(self, windows, available=True):
        self.AVAILABLE = available
        self.windows = windows
        self.closed, self.focused = [], []

    def list_windows(self):
        return list(self.windows)

    def close_window(self, hwnd):  # не должен вызываться никогда
        self.closed.append(hwnd)

    def focus_window(self, hwnd):  # и этот тоже
        self.focused.append(hwnd)


class FakeWander(QObject):
    landed = Signal(object)

    def __init__(self):
        super().__init__()
        self.hops: list[bool] = []
        self.nearest = 0
        self.refuse = False

    def wander_now(self, allow_focus=True):
        self.hops.append(allow_focus)
        return not self.refuse

    def land_nearest(self):
        self.nearest += 1
        return not self.refuse


class FakeChatter:
    def __init__(self):
        self.pings = 0
        self.allow = True

    def ping_now(self):
        if self.allow:
            self.pings += 1
        return self.allow


class FakeBite:
    IDLE = "idle"

    def __init__(self):
        self.state = self.IDLE


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(qapp, tmp_path):
    bee = BeeWindow(settings=QSettings(str(tmp_path / "b.ini"), QSettings.IniFormat))
    bee.mood_deferred = False
    bee.show()
    parts = dict(bee=bee, api=FakeWinapi([win()]), wander=FakeWander(),
                 chatter=FakeChatter(), bite=FakeBite(), clock=Clock())
    created = []

    def make(**kw):
        params = dict(FAST, winapi_module=parts["api"], own_pid=OWN, rng=random.Random(3),
                      clock=parts["clock"], screens_provider=lambda: QT_SCREENS,
                      screen_provider=lambda point: (AREA, SCREEN), autostart=False)
        params.update(kw)
        ctl = WalkController(bee, parts["wander"], parts["chatter"], parts["bite"], **params)
        created.append(ctl)
        return ctl

    parts["make"] = make
    yield parts
    for ctl in created:
        ctl._step_timer.stop()
        ctl._land_timer.stop()
        ctl._track_timer.stop()
    bee.stop_walking()
    bee.cancel_flight()
    bee.bubble.hide()
    bee.close()


def land_on_window(env, ctl, w=None):
    """Пчела будто долетела до окна: ставим её над кромкой и шлём landed."""
    w = w or env["api"].windows[0]
    env["bee"].move(w.rect[0] + 200, w.rect[1] - FEET)
    env["wander"].landed.emit(w)


# --- посадка ------------------------------------------------------------------

def test_initial_perch_on_taskbar(env):
    bee = env["bee"]
    bee.move(1500, 300)
    ctl = env["make"](autostart=True)
    assert bee.perched and ctl.surface.is_taskbar
    assert bee.y() + FEET == AREA.bottom() + 1
    assert bee.x() == 1500 and not bee.is_flying


def test_initial_window_position_on_taskbar(qapp, tmp_path):
    """Без контроллера окно пчелы уже стоит лапками на панели задач основного экрана."""
    from PySide6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    area, geom = screen.availableGeometry(), screen.geometry()
    bee = BeeWindow(settings=QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat))
    expected = min(area.bottom() + 1 - bee.feet_offset(), geom.bottom() - 120 + 1)
    assert bee.y() == expected


def test_landed_on_window_sets_surface_and_feet(env):
    ctl = env["make"]()
    land_on_window(env, ctl)
    bee = env["bee"]
    assert bee.perched and ctl.surface.hwnd == 7
    assert bee.y() + FEET == 401
    assert (ctl.surface.left, ctl.surface.right) == (300, 1099)


def test_landed_taskbar_fallback(env):
    ctl = env["make"]()
    env["bee"].move(700, 200)
    env["wander"].landed.emit(None)
    assert ctl.surface.is_taskbar and env["bee"].perched
    assert env["bee"].y() + FEET == 1040


# --- ходьба -------------------------------------------------------------------

def test_walks_within_segment_and_pauses(env):
    ctl = env["make"]()
    land_on_window(env, ctl)
    bee = env["bee"]
    y = bee.y()
    xs = set()

    def watch():
        xs.add(bee.x())
        assert bee.y() == y
        return ctl.walks >= 6

    env["clock"].t += 100  # для BeeWindow время настоящее; часы контроллера — для жужжания
    wait_for(watch)
    lo, hi = ctl.x_range(ctl.surface)
    assert (lo, hi) == (300, 1099 - 119)
    assert all(lo <= x <= hi for x in xs) and len(xs) > 3


def test_pause_between_walks(env):
    ctl = env["make"](pause_s=(1.5, 5.0))
    land_on_window(env, ctl)
    assert 1500 <= ctl._step_timer.interval() <= 5000
    ctl._on_walk_done()
    assert 1500 <= ctl._step_timer.interval() <= 5000


def test_pick_walk_x_turns_back_at_edge(env):
    ctl = env["make"](step_px=(100, 100))
    land_on_window(env, ctl)
    bee = env["bee"]
    lo, hi = ctl.x_range(ctl.surface)
    bee.move(hi, bee.y())
    for _ in range(10):
        assert ctl.pick_walk_x() == hi - 100


def test_buzz_probability_and_rate_limit(env):
    ctl = env["make"](buzz_chance=1.0, buzz_min_gap_s=20.0, pause_s=(100, 100))
    land_on_window(env, ctl)
    bee, chatter, clock = env["bee"], env["chatter"], env["clock"]

    def step():
        bee.stop_walking()
        ctl._next_step()
        assert bee.is_walking

    step()
    assert chatter.pings == 1
    clock.t += 5
    step()
    assert chatter.pings == 1  # чаще раза в 20 с не жужжит
    clock.t += 20
    step()
    assert chatter.pings == 2
    chatter.allow = False  # ChatterController отказал (чужой пузырь и т. п.)
    clock.t += 30
    step()
    assert ctl.buzzes == 2
    chatter.allow = True
    step()  # отказ не считается: можно жужжать сразу
    assert chatter.pings == 3


def test_buzz_chance_zero_never_buzzes(env):
    ctl = env["make"](buzz_chance=0.0, pause_s=(100, 100))
    land_on_window(env, ctl)
    for _ in range(5):
        env["bee"].stop_walking()
        env["clock"].t += 100
        ctl._next_step()
    assert env["chatter"].pings == 0 and ctl.walks == 5


def test_buzz_chance_is_random(env):
    ctl = env["make"](buzz_chance=0.35, buzz_min_gap_s=0.0, pause_s=(100, 100),
                      rng=random.Random(5))
    land_on_window(env, ctl)
    for _ in range(200):
        env["bee"].stop_walking()
        ctl._next_step()
    assert 40 <= env["chatter"].pings <= 100


# --- никаких движений, пока пчела занята ---------------------------------------

def test_no_walk_while_bite_dragging_or_flying(env):
    ctl = env["make"](pause_s=(100, 100))
    land_on_window(env, ctl)
    bee, bite = env["bee"], env["bite"]
    bite.state = "warning"
    ctl._next_step()
    assert not bee.is_walking
    bite.state = bite.IDLE
    bee._dragging = True
    ctl._next_step()
    assert not bee.is_walking
    bee._dragging = False
    bee.fly_to(bee.pos() + QPoint(0, -50))
    assert not bee.perched  # полёт — значит, в воздухе
    ctl._next_step()
    assert not bee.is_walking
    assert ctl.walks == 0


def test_no_tracking_moves_while_busy(env):
    ctl = env["make"]()
    land_on_window(env, ctl)
    bee, bite, api = env["bee"], env["bite"], env["api"]
    pos = bee.pos()
    api.windows = [win(rect=(500, 500, 1300, 900))]
    bite.state = "flying"
    ctl.track()
    bite.state = bite.IDLE
    bee._dragging = True
    ctl.track()
    bee._dragging = False
    assert bee.pos() == pos and env["wander"].hops == []


# --- слежение за окном ---------------------------------------------------------

def test_follows_moved_window(env):
    ctl = env["make"](pause_s=(100, 100))
    land_on_window(env, ctl)
    bee, api = env["bee"], env["api"]
    x = bee.x()
    api.windows = [win(rect=(400, 450, 1200, 850))]
    ctl.track()
    assert bee.x() == x + 100 and bee.y() + FEET == 451
    assert ctl.surface.rect == (400, 450, 1200, 850) and env["wander"].hops == []
    # окно стало уже — пчела прижимается к новой кромке
    api.windows = [win(rect=(400, 450, 600, 850))]
    ctl.track()
    lo, hi = ctl.x_range(ctl.surface)
    assert lo <= bee.x() <= hi


def test_big_move_stops_walk(env):
    ctl = env["make"](pause_s=(100, 100), speed=1.0)
    land_on_window(env, ctl)
    bee, api = env["bee"], env["api"]
    ctl._next_step()
    assert bee.is_walking
    api.windows = [win(rect=(310, 405, 1110, 805))]  # чуть-чуть — идёт дальше
    ctl.track()
    assert bee.is_walking
    api.windows = [win(rect=(700, 405, 1500, 805))]  # далеко — шаг прерван
    ctl.track()
    assert not bee.is_walking and bee.perched and bee.y() + FEET == 406


@pytest.mark.parametrize("change", ["vanished", "minimized", "cloaked", "covered"])
def test_takes_off_when_window_gone_or_covered(env, change):
    ctl = env["make"]()
    land_on_window(env, ctl)
    bee, api = env["bee"], env["api"]
    if change == "vanished":
        api.windows = []
    elif change == "covered":
        feet_x = bee.geometry().center().x()
        api.windows = [win(hwnd=9, rect=(feet_x - 50, 300, feet_x + 50, 600),
                           process_name="chrome.exe"), win()]
    else:
        api.windows = [win(**{change: True})]
    ctl.track()
    assert env["wander"].hops == [False]  # перелёт без переключения окон
    assert ctl.surface is None and not bee.perched
    assert api.closed == [] and api.focused == []


def test_cover_elsewhere_on_edge_is_fine(env):
    ctl = env["make"]()
    land_on_window(env, ctl)
    env["api"].windows = [win(hwnd=9, rect=(0, 0, 310, 600)), win()]
    env["bee"].move(800, env["bee"].y())
    ctl.track()
    assert env["wander"].hops == [] and ctl.surface is not None


def test_no_tracking_on_taskbar_or_without_winapi(env):
    ctl = env["make"]()
    env["wander"].landed.emit(None)
    env["api"].windows = []
    ctl.track()
    assert env["wander"].hops == []
    env["api"].AVAILABLE = False
    land_on_window(env, ctl, win())
    ctl.track()
    assert env["wander"].hops == []


# --- настроение и перенос ------------------------------------------------------

def test_anger_stops_walking_and_unperches(env):
    ctl = env["make"](speed=1.0)
    land_on_window(env, ctl)
    bee = env["bee"]
    ctl._next_step()
    assert bee.is_walking
    bee.apply_action(Action.SWAT)
    assert not bee.is_walking and not bee.perched and ctl.surface is None
    ctl._next_step()
    assert not bee.is_walking
    assert env["wander"].nearest == 0


def test_lands_after_calm(env):
    ctl = env["make"]()
    land_on_window(env, ctl)
    bee, wander = env["bee"], env["wander"]
    bee.apply_action(Action.SWAT)
    QTest.qWait(50)
    assert wander.nearest == 0
    bee.apply_action(Action.SORRY)
    wait_for(lambda: wander.nearest == 1)
    assert bee.engine.mood is Mood.NEUTRAL


def test_land_waits_for_flight_to_finish(env):
    ctl = env["make"]()
    bee, wander = env["bee"], env["wander"]
    bee.fly_to(bee.pos() + QPoint(30, 0))  # последний прыжок метаний ещё летит
    bee.drag_finished.emit()
    QTest.qWait(30)
    assert wander.nearest == 0
    wait_for(lambda: wander.nearest == 1)
    assert not bee.is_flying


def test_lands_after_drop_when_calm_only(env):
    ctl = env["make"]()
    bee, wander = env["bee"], env["wander"]
    bee.drag_finished.emit()
    wait_for(lambda: wander.nearest == 1)
    bee.apply_action(Action.SWAT)
    bee.drag_finished.emit()
    QTest.qWait(60)
    assert wander.nearest == 1


def test_drag_unperches_and_cancels_walk(env):
    from PySide6.QtCore import Qt
    ctl = env["make"](speed=1.0)
    land_on_window(env, ctl)
    bee = env["bee"]
    ctl._next_step()
    assert bee.is_walking
    QTest.mousePress(bee, Qt.LeftButton, pos=QPoint(60, 60))
    QTest.mouseMove(bee, QPoint(90, 90))
    QTest.mouseMove(bee, QPoint(100, 100))
    assert bee.is_dragging and not bee.is_walking and not bee.perched
    assert ctl.surface is None
    QTest.mouseRelease(bee, Qt.LeftButton, pos=QPoint(100, 100))
    wait_for(lambda: env["wander"].nearest == 1)
