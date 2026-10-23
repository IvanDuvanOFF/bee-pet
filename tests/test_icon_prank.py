"""Тесты «скучающей» пчелы, которая таскает ярлыки (IconPrankController) — на заглушке значков."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dataclasses
import json
import logging
import math
import random

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.desktop_icons import IconInfo
from bee_pet.icon_prank import (MAX_MINIMIZE, OCCUPIED, SETTINGS_ORIGINALS, IconPrankController,
                                covering_windows, icon_rect, minimize_skip_reason,
                                pick_destination, too_close)
from bee_pet.mood import Action
from bee_pet.winapi import WindowInfo

SCREEN = QRect(0, 0, 1920, 1080)
AREA = QRect(0, 0, 1920, 1040)
CELL = (100, 100)
FAST = dict(min_s=3600, max_s=3600, hover_ms=10, follow_ms=10, carry_speed=4000.0,
            land_retry_ms=10, fly_speed=4000.0, push_ms=10, minimize_wait_ms=100,
            minimize_poll_ms=10, autostart=False)
OWN_PID = 4242


def wait_for(cond, timeout_ms=5000):
    waited = 0
    while not cond():
        assert waited < timeout_ms, "не дождались условия"
        QTest.qWait(5)
        waited += 5


class FakeMover:
    def __init__(self, owner, key):
        self.owner, self.key = owner, key
        self.closed = False
        self.fail = False

    def move(self, x, y):
        assert not self.closed, "перенос через закрытый mover"
        if self.fail:
            return False
        self.owner.moved(self.key, x, y)
        return True

    def close(self):
        self.closed = True


class FakeIcons:
    """Заглушка desktop_icons: значки, автоупорядочение, журнал переносов."""

    def __init__(self, icons, available=True):
        self.AVAILABLE = available
        self.icons = {i.key: i for i in icons}
        self.arrange = False
        self.spacing = CELL
        self.moves: list[tuple[str, int, int]] = []
        self.movers: list[FakeMover] = []
        self.list_calls = 0

    def moved(self, key, x, y):
        self.moves.append((key, x, y))
        old = self.icons[key]
        self.icons[key] = IconInfo(key, old.name, x, y)

    def list_icons(self):
        self.list_calls += 1
        return list(self.icons.values())

    def auto_arrange(self):
        return self.arrange

    def icon_spacing(self):
        return self.spacing

    def open_mover(self, key):
        if key not in self.icons:
            return None
        mover = FakeMover(self, key)
        self.movers.append(mover)
        return mover

    def move_icon(self, key, x, y):
        if key not in self.icons:
            return False
        self.moved(key, x, y)
        return True


def win(hwnd, rect, *, pid=100, proc="notepad.exe", cls="Notepad", elevated=False,
        minimized=False, cloaked=False, tool=False):
    return WindowInfo(hwnd=hwnd, title=f"окно {hwnd}", class_name=cls, pid=pid,
                      process_name=proc, rect=rect, elevated=elevated, is_tool=tool,
                      cloaked=cloaked, minimized=minimized)


class FakeWinapi:
    """Заглушка winapi: окна сверху вниз; свёрнутое окно видно со следующего list_windows."""

    AVAILABLE = True

    def __init__(self, windows=()):
        self.windows = list(windows)
        self.minimize_calls: list[int] = []
        self.refuse: set[int] = set()   # эти окна просьбу свернуться игнорируют
        self._pending: set[int] = set()
        self.list_calls = 0

    def list_windows(self):
        self.list_calls += 1
        self.windows = [dataclasses.replace(w, minimized=True) if w.hwnd in self._pending else w
                        for w in self.windows]
        self._pending.clear()
        return list(self.windows)

    def minimize_window(self, hwnd):
        self.minimize_calls.append(hwnd)
        if hwnd not in self.refuse:
            self._pending.add(hwnd)
        return True

    def close_window(self, hwnd):  # шалость с ярлыками окна не закрывает
        raise AssertionError("close_window из шалости с ярлыками")

    def focus_window(self, hwnd):
        raise AssertionError("focus_window из шалости с ярлыками")


class FakeWander(QObject):
    landed = Signal(object)

    def __init__(self):
        super().__init__()
        self.nearest = 0

    def land_nearest(self):
        self.nearest += 1
        return True


class FakeBite:
    IDLE = "idle"

    def __init__(self):
        self.state = self.IDLE


def icon(n, x=20, y=None):
    return IconInfo(f"C:\\Desktop\\i{n}.lnk", f"i{n}", x, 20 + 120 * n if y is None else y)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "b.ini"), QSettings.IniFormat)
    bee = BeeWindow(settings=settings)
    bee.mood_deferred = False
    bee.show()
    bee.move(200, 200)
    parts = dict(bee=bee, settings=settings, icons=FakeIcons([icon(0), icon(1), icon(2)]),
                 wander=FakeWander(), bite=FakeBite(), api=FakeWinapi())
    created = []

    def make(**kw):
        params = dict(FAST, icons_module=parts["icons"], winapi_module=parts["api"],
                      own_pid=OWN_PID, rng=random.Random(5),
                      screens_provider=lambda: [(SCREEN, 1.0)],
                      areas_provider=lambda: [AREA])
        params.update(kw)
        ctl = IconPrankController(bee, parts["wander"], parts["bite"], **params)
        created.append(ctl)
        return ctl

    parts["make"] = make
    yield parts
    for ctl in created:
        for t in (ctl._timer, ctl._hover_timer, ctl._follow_timer, ctl._land_timer,
                  ctl._push_timer, ctl._poll_timer):
            t.stop()
    bee.cancel_flight()
    bee.bubble.hide()
    bee.close()


def enable(bee):
    bee.icon_prank_action.setChecked(True)


def center(x, y):
    return QPoint(x + CELL[0] // 2, y + CELL[1] // 2)


# --- геометрия ------------------------------------------------------------------

def test_pick_destination_clamped_and_away_from_others():
    rng = random.Random(1)
    start = QPoint(70, 70)
    others = [QPoint(70, 190), QPoint(70, 310), QPoint(190, 70)]
    for _ in range(50):
        dest = pick_destination(start, AREA, others, CELL, rng)
        assert dest is not None
        assert 100 <= math.hypot(dest.x() - start.x(), dest.y() - start.y()) <= 300 + 1
        assert AREA.left() + 50 <= dest.x() <= AREA.right() + 1 - 50
        assert AREA.top() + 50 <= dest.y() <= AREA.bottom() + 1 - 50
        assert not too_close(dest, others, CELL)


def test_pick_destination_none_when_no_room():
    tiny = QRect(0, 0, 150, 150)
    assert pick_destination(QPoint(75, 75), tiny, [], CELL, random.Random(1)) is None


# --- выключатель и меню ---------------------------------------------------------

def test_menu_defaults_and_persist(qapp, tmp_path):
    settings = QSettings(str(tmp_path / "m.ini"), QSettings.IniFormat)
    w = BeeWindow(settings=settings)
    assert w.icon_prank_enabled is False and not w.icon_prank_action.isChecked()
    acts = w.menu.actions()
    assert acts.index(w.icon_prank_action) == acts.index(w.focus_switch_action) + 1
    assert acts.index(w.icon_restore_action) == acts.index(w.icon_prank_action) + 1
    assert not w.icon_restore_action.isEnabled()
    got = []
    w.icon_prank_toggled.connect(got.append)
    w.icon_prank_action.setChecked(True)
    assert got == [True] and w.icon_prank_enabled
    assert BeeWindow(settings=settings).icon_prank_enabled is True
    w.set_icon_prank_available(False)
    assert not w.icon_prank_action.isEnabled() and w.icon_prank_action.toolTip()
    w.set_icons_restorable(True)
    assert not w.icon_restore_action.isEnabled()


def test_unavailable_disables_menu(env):
    env["icons"].AVAILABLE = False
    ctl = env["make"]()
    assert not env["bee"].icon_prank_action.isEnabled()
    enable(env["bee"])
    assert ctl.prank_now() is False
    assert env["icons"].list_calls == 0


def test_disabled_by_default_no_action(env):
    ctl = env["make"]()
    assert ctl.prank_now() is False
    ctl._on_timer()
    assert env["icons"].moves == [] and env["icons"].list_calls == 0
    assert env["api"].list_calls == 0 and env["api"].minimize_calls == []
    assert not env["bee"].is_flying


def test_auto_arrange_no_action_logged_once(env, caplog):
    env["icons"].arrange = True
    ctl = env["make"]()
    enable(env["bee"])
    with caplog.at_level(logging.INFO, logger="bee_pet.icon_prank"):
        assert ctl.prank_now() is False
        assert ctl.prank_now() is False
    assert sum("вернутся в сетку" in r.getMessage() for r in caplog.records) == 1
    assert env["icons"].moves == [] and not env["bee"].is_flying


def test_refuses_when_angry_or_busy(env):
    ctl = env["make"]()
    bee = env["bee"]
    enable(bee)
    env["bite"].state = "flying"
    assert ctl.prank_now() is False
    env["bite"].state = env["bite"].IDLE
    bee.occupied_by = "кто-то"
    assert ctl.prank_now() is False
    bee.occupied_by = None
    bee.apply_action(Action.SWAT)
    assert ctl.prank_now() is False
    assert env["icons"].moves == []


# --- перенос --------------------------------------------------------------------

def test_carry_moves_icon_along_and_lands(env):
    ctl = env["make"]()
    bee, icons = env["bee"], env["icons"]
    before = {k: (i.x, i.y) for k, i in icons.icons.items()}
    enable(bee)
    assert ctl.prank_now() is True
    assert ctl.state == ctl.TO_ICON and bee.occupied_by == OCCUPIED
    key = ctl.icon.key
    dest = ctl.destination
    wait_for(lambda: ctl.state == ctl.CARRYING)
    wait_for(lambda: ctl.state == ctl.IDLE)

    moves = [m for m in icons.moves if m[0] == key]
    assert len(moves) >= 3, "значок должен ехать за пчелой"
    assert moves[-1] == (key, dest.x() - CELL[0] // 2, dest.y() - CELL[1] // 2)
    assert {m[0] for m in icons.moves} == {key}  # другие значки не трогаем
    start = center(*before[key])
    assert 100 <= math.hypot(dest.x() - start.x(), dest.y() - start.y()) <= 301
    others = [center(*p) for k, p in before.items() if k != key]
    assert not too_close(dest, others, CELL)
    assert AREA.contains(QRect(dest.x() - 50, dest.y() - 50, 100, 100))
    # приземлилась, освободила пчелу, всё закрыла, исходное место записано
    assert env["wander"].nearest == 1
    assert bee.occupied_by is None
    assert all(m.closed for m in icons.movers)
    assert ctl.originals() == {key: before[key]}
    assert ctl.pranks == 1
    assert bee.icon_restore_action.isEnabled()


def _start_carry(env):
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now()
    wait_for(lambda: ctl.state == ctl.CARRYING)
    wait_for(lambda: len(env["icons"].moves) >= 2)
    return ctl


def _assert_frozen(env, ctl):
    icons = env["icons"]
    assert ctl.state == ctl.IDLE and env["bee"].occupied_by is None
    assert all(m.closed for m in icons.movers)
    n = len(icons.moves)
    QTest.qWait(60)
    assert len(icons.moves) == n, "после отмены значок не двигается"
    assert len(ctl.originals()) == 1  # исходное место записано


def test_cancel_on_drag(env):
    ctl = _start_carry(env)
    env["bee"].cancel_flight()  # как при настоящем переносе мышкой
    env["bee"].drag_started.emit()
    _assert_frozen(env, ctl)
    assert env["wander"].nearest == 0  # садиться будет WalkController после переноса


def test_cancel_on_anger(env):
    ctl = _start_carry(env)
    env["bee"].apply_action(Action.SWAT)
    _assert_frozen(env, ctl)
    assert not env["bee"].is_flying
    assert env["wander"].nearest == 0


def test_cancel_on_toggle_off_lands(env):
    ctl = _start_carry(env)
    env["bee"].icon_prank_action.setChecked(False)
    _assert_frozen(env, ctl)
    assert env["wander"].nearest == 1


def test_mover_failure_aborts(env):
    ctl = _start_carry(env)
    env["icons"].movers[-1].fail = True
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert all(m.closed for m in env["icons"].movers)


# --- запись и возврат -----------------------------------------------------------

def test_originals_recorded_once_and_persisted(env):
    env["icons"].icons = {icon(0).key: icon(0)}
    ctl = env["make"]()
    enable(env["bee"])
    for _ in range(2):
        assert ctl.prank_now()
        wait_for(lambda: ctl.state == ctl.IDLE)
    assert len(env["icons"].moves) > 0
    assert ctl.originals() == {icon(0).key: (20, 20)}
    raw = json.loads(env["settings"].value(SETTINGS_ORIGINALS))
    assert raw == {icon(0).key: [20, 20]}
    # новая пчела (перезапуск) видит ту же запись
    again = env["make"]()
    assert again.originals() == {icon(0).key: (20, 20)}


def test_restore_moves_back_skips_missing_and_clears(env):
    icons = env["icons"]
    env["settings"].setValue(SETTINGS_ORIGINALS, json.dumps(
        {icon(0).key: [5, 6], "C:\\Desktop\\gone.lnk": [1, 2]}))
    ctl = env["make"]()
    bee = env["bee"]
    assert bee.icon_restore_action.isEnabled()
    got = []
    bee.icon_restore_requested.connect(lambda: got.append(1))
    bee.icon_restore_action.trigger()
    assert got == [1]
    assert icons.moves == [(icon(0).key, 5, 6)]
    assert ctl.originals() == {}
    assert not bee.icon_restore_action.isEnabled()
    assert ctl.restore() == 0


def test_restore_keeps_record_if_icons_unreadable(env):
    env["settings"].setValue(SETTINGS_ORIGINALS, json.dumps({icon(0).key: [5, 6]}))
    env["icons"].icons = {}
    ctl = env["make"]()
    assert ctl.restore() == 0
    assert ctl.originals() == {icon(0).key: (5, 6)}


# --- прогулки и ходьба не мешают ------------------------------------------------

def test_wander_and_walk_respect_occupied(env):
    from bee_pet.walk import WalkController
    from bee_pet.wander import WanderController

    class NoWin:
        AVAILABLE = False

        @staticmethod
        def list_windows():
            return []

    bee = env["bee"]
    wander = WanderController(bee, None, winapi_module=NoWin, autostart=False,
                              screens_provider=lambda: [(SCREEN, 1.0)],
                              areas_provider=lambda: [AREA],
                              screen_provider=lambda p: (AREA, SCREEN))
    walk = WalkController(bee, wander, None, None, winapi_module=NoWin, autostart=False,
                          screens_provider=lambda: [(SCREEN, 1.0)],
                          screen_provider=lambda p: (AREA, SCREEN))
    bee.occupied_by = OCCUPIED
    assert wander.wander_now() is False
    assert wander.land_nearest() is False
    assert walk._busy()
    bee.occupied_by = None
    assert walk._busy() is None


# --- окна поверх значков: чистая проверка ---------------------------------------

def test_covering_windows_ignores_system_minimized_cloaked_tool_own():
    cell = icon_rect(20, 20, CELL)  # (20, 20, 120, 120)
    windows = [
        win(1, (100, 100, 500, 500)),                       # задевает угол — закрывает
        win(2, (120, 0, 500, 500)),                         # вплотную справа — нет
        win(3, (0, 0, 1920, 1080), cls="Progman", proc="explorer.exe"),
        win(4, (0, 0, 1920, 1080), cls="WorkerW", proc="explorer.exe"),
        win(5, (0, 0, 1920, 1080), cls="Shell_TrayWnd", proc="explorer.exe"),
        win(6, (0, 0, 300, 300), minimized=True),
        win(7, (0, 0, 300, 300), cloaked=True),
        win(8, (0, 0, 300, 300), tool=True),
        win(9, (0, 0, 300, 300), pid=OWN_PID),
        win(10, (0, 0, 300, 300), elevated=True),           # закрывает (но свернуть нельзя)
    ]
    assert [w.hwnd for w in covering_windows(cell, windows, OWN_PID)] == [1, 10]


def test_minimize_skip_reason():
    rect = (0, 0, 9, 9)
    assert minimize_skip_reason(win(1, rect), OWN_PID) is None
    assert minimize_skip_reason(win(1, rect, elevated=True), OWN_PID)
    assert minimize_skip_reason(win(1, rect, elevated=None), OWN_PID)
    assert minimize_skip_reason(win(1, rect, pid=OWN_PID), OWN_PID)
    assert minimize_skip_reason(win(1, rect, cls="Progman"), OWN_PID)


# --- окна поверх значков: контроллер --------------------------------------------

BIG = (0, 0, 700, 700)  # закрывает все значки и все места назначения рядом с ними


def test_uncovered_icon_no_minimize(env):
    env["api"].windows = [win(1, (1000, 500, 1600, 900)),
                          win(2, (0, 0, 1920, 1080), cls="Progman", proc="explorer.exe")]
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is True
    assert ctl.state == ctl.TO_ICON
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert env["api"].minimize_calls == []
    assert env["icons"].moves and ctl.pranks == 1


def test_covered_by_one_window_minimizes_then_carries(env):
    api, icons, bee = env["api"], env["icons"], env["bee"]
    api.windows = [win(1, BIG)]
    ctl = env["make"](push_ms=80)
    enable(bee)
    assert ctl.prank_now() is True
    assert ctl.state == ctl.TO_WINDOW and bee.occupied_by == OCCUPIED
    assert api.minimize_calls == [] and icons.moves == []
    wait_for(lambda: ctl.state == ctl.PUSHING)
    assert bee.stinging and api.minimize_calls == []
    assert bee.geometry().top() <= 1  # у заголовка окна (верхняя кромка — край экрана)
    wait_for(lambda: ctl.state == ctl.CARRYING)
    assert api.minimize_calls == [1] and not bee.stinging
    assert [w.hwnd for w in ctl.minimized] == [1]
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert icons.moves and ctl.pranks == 1
    assert api.windows[0].minimized  # обратно не разворачивает
    assert api.minimize_calls == [1]


def test_two_windows_minimized_in_order(env):
    api = env["api"]
    api.windows = [win(1, (0, 0, 200, 400)), win(2, BIG)]
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is True
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert api.minimize_calls == [1, 2]
    assert env["icons"].moves and ctl.pranks == 1


def test_window_refuses_to_minimize_aborts(env):
    api, icons = env["api"], env["icons"]
    api.windows = [win(1, BIG)]
    api.refuse = {1}
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is True
    wait_for(lambda: ctl.state == ctl.MINIMIZING)
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert api.minimize_calls == [1]
    assert icons.moves == [] and icons.movers == [] and ctl.originals() == {}
    assert env["bee"].occupied_by is None and not env["bee"].stinging
    assert env["wander"].nearest == 1


def test_too_many_windows_no_minimize(env):
    api = env["api"]
    api.windows = [win(i, BIG) for i in range(1, MAX_MINIMIZE + 2)]
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is False
    assert ctl.state == ctl.IDLE and not env["bee"].is_flying
    assert api.minimize_calls == [] and env["icons"].moves == []


def test_elevated_window_no_minimize(env):
    api = env["api"]
    api.windows = [win(1, BIG, elevated=True)]
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is False
    api.windows = [win(1, BIG, elevated=None)]
    assert ctl.prank_now() is False
    assert api.minimize_calls == [] and env["icons"].moves == []


def test_other_icon_chosen_when_covered_unminimizable(env):
    api, icons = env["api"], env["icons"]
    # Значки i0 и i1 под окном администратора, i2 свободен (окно до y=250).
    api.windows = [win(1, (0, 0, 200, 250), elevated=True)]
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is True
    assert ctl.icon.key == icon(2).key and ctl.state == ctl.TO_ICON
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert api.minimize_calls == []
    assert {m[0] for m in icons.moves} == {icon(2).key}


def test_prefers_uncovered_icon_over_minimizing(env):
    api = env["api"]
    api.windows = [win(1, (0, 0, 200, 250))]  # можно свернуть, но i2 свободен
    ctl = env["make"]()
    enable(env["bee"])
    assert ctl.prank_now() is True
    assert ctl.icon.key == icon(2).key and ctl.state == ctl.TO_ICON
    wait_for(lambda: ctl.state == ctl.IDLE)
    assert api.minimize_calls == []


def _assert_untouched(env, ctl, calls):
    QTest.qWait(150)
    assert ctl.state == ctl.IDLE and env["bee"].occupied_by is None
    assert not env["bee"].stinging
    assert env["api"].minimize_calls == calls
    assert env["icons"].moves == [] and env["icons"].movers == []


def test_drag_during_push_cancels_minimize(env):
    env["api"].windows = [win(1, BIG)]
    ctl = env["make"](push_ms=300)
    enable(env["bee"])
    assert ctl.prank_now()
    wait_for(lambda: ctl.state == ctl.PUSHING)
    env["bee"].drag_started.emit()
    _assert_untouched(env, ctl, [])


def test_anger_between_windows_stops_second_minimize(env):
    api = env["api"]
    api.windows = [win(1, (0, 0, 200, 400)), win(2, BIG)]
    ctl = env["make"](push_ms=300)
    enable(env["bee"])
    assert ctl.prank_now()
    wait_for(lambda: api.minimize_calls == [1] and ctl.state == ctl.PUSHING)
    env["bee"].apply_action(Action.SWAT)
    _assert_untouched(env, ctl, [1])


def test_toggle_off_while_waiting_for_minimize(env):
    api = env["api"]
    api.windows = [win(1, BIG)]
    api.refuse = {1}
    ctl = env["make"](minimize_wait_ms=2000)
    enable(env["bee"])
    assert ctl.prank_now()
    wait_for(lambda: ctl.state == ctl.MINIMIZING)
    env["bee"].icon_prank_action.setChecked(False)
    _assert_untouched(env, ctl, [1])


def test_toggle_off_never_minimizes(env):
    env["api"].windows = [win(1, BIG)]
    ctl = env["make"]()
    assert ctl.prank_now() is False
    ctl._on_timer()
    assert env["api"].minimize_calls == [] and env["api"].list_calls == 0
