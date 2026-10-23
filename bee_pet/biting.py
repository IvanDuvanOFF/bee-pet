"""Злая пчела закрывает окна: политика выбора цели и последовательность укуса.

Модуль не зависит от платформы: вся работа с чужими окнами идёт через `winapi`,
которую в тестах подменяют заглушкой. Закрывает окна только `winapi.close_window`.
"""
from __future__ import annotations

import logging
import os
import random
import time
from types import ModuleType
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QGuiApplication

from bee_pet import winapi
from bee_pet.mood import Mood, MoodChange
from bee_pet.winapi import WindowInfo

log = logging.getLogger("bee_pet.biting")

DEFAULT_WHITELIST: frozenset[str] = frozenset(
    {"notepad.exe", "calc.exe", "calculatorapp.exe", "explorer.exe"})
EXPLORER = "explorer.exe"
EXPLORER_CLASS = "CabinetWClass"  # только окна Проводника, не рабочий стол и не панель задач

BLOCKED_CLASSES: frozenset[str] = frozenset({
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"})
BLOCKED_PROCESSES: frozenset[str] = frozenset({
    # терминалы
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe",
    "conhost.exe", "openconsole.exe", "wt.exe", "wsl.exe", "bash.exe", "mintty.exe",
    # IDE и редакторы кода
    "code.exe", "cursor.exe", "idea64.exe", "idea.exe", "pycharm64.exe", "pycharm.exe",
    "webstorm64.exe", "clion64.exe", "goland64.exe", "rider64.exe", "phpstorm64.exe",
    "rubymine64.exe", "datagrip64.exe", "studio64.exe", "devenv.exe", "sublime_text.exe",
    "atom.exe", "eclipse.exe"})

MIN_WIDTH, MIN_HEIGHT = 100, 60  # физические пиксели
MAX_MOVE_PX = 50                 # на сколько окно может сдвинуться за время предупреждения
TOP_CHOICES = 3

# Тайминги по умолчанию (в тестах подменяются через конструктор).
COUNTDOWN_S = 5
COOLDOWN_S = 5 * 60.0
STING_MS = 400

Rect = tuple[int, int, int, int]


# --- политика -------------------------------------------------------------


def _intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def skip_reason(win: WindowInfo, own_pid: int, whitelist: frozenset[str] | set[str],
                screens: Sequence[Rect] | None = None) -> str | None:
    """Почему окно нельзя закрывать; None, если можно."""
    name = win.process_name.lower()
    if win.pid == own_pid:
        return "окно собственного процесса"
    if win.class_name in BLOCKED_CLASSES:
        return f"системный класс {win.class_name}"
    if name in BLOCKED_PROCESSES:
        return f"терминал или IDE ({name})"
    if win.elevated is True:
        return "процесс с правами администратора"
    if win.elevated is None:
        return "права процесса неизвестны (считаем небезопасным)"
    if win.minimized:
        return "окно свёрнуто"
    if win.cloaked:
        return "окно cloaked"
    if win.is_tool:
        return "tool-окно"
    if name not in {w.lower() for w in whitelist}:
        return f"процесс {name or '?'} не в белом списке"
    if name == EXPLORER and win.class_name != EXPLORER_CLASS:
        return f"explorer.exe с классом {win.class_name}"
    left, top, right, bottom = win.rect
    if right - left < MIN_WIDTH or bottom - top < MIN_HEIGHT:
        return "окно слишком маленькое"
    if screens is not None and not any(_intersects(win.rect, s) for s in screens):
        return "окно вне экранов"
    return None


def eligible(win: WindowInfo, own_pid: int, whitelist: frozenset[str] | set[str] = DEFAULT_WHITELIST,
             screens: Sequence[Rect] | None = None) -> bool:
    """Можно ли закрыть это окно. Неизвестное считается небезопасным."""
    reason = skip_reason(win, own_pid, whitelist, screens)
    if reason is not None:
        log.debug("пропускаю окно %r (%s): %s", win.title, win.process_name, reason)
    return reason is None


def pick_target(windows: Sequence[WindowInfo], own_pid: int,
                whitelist: frozenset[str] | set[str] = DEFAULT_WHITELIST,
                rng: random.Random | None = None,
                screens: Sequence[Rect] | None = None) -> WindowInfo | None:
    """Верхнее подходящее окно; случайно среди первых трёх подходящих (окна идут сверху вниз)."""
    rng = rng or random.Random()
    candidates = [w for w in windows if eligible(w, own_pid, whitelist, screens)]
    if not candidates:
        return None
    return rng.choice(candidates[:TOP_CHOICES])


# --- координаты -----------------------------------------------------------


def physical_to_logical(point: tuple[int, int],
                        screens: Sequence[tuple[QRect, float]]) -> QPoint:
    """Переводит физические пиксели в логические координаты Qt.

    screens — пары (geometry экрана в логических координатах, devicePixelRatio).
    Допущение (best effort): родной левый верхний угол экрана = geometry.topLeft() * dpr.
    Для точки берётся экран, чей физический прямоугольник её содержит (иначе ближайший первый).
    """
    if not screens:
        return QPoint(*point)
    px, py = point
    chosen = screens[0]
    for geom, dpr in screens:
        nx, ny = geom.left() * dpr, geom.top() * dpr
        if nx <= px < nx + geom.width() * dpr and ny <= py < ny + geom.height() * dpr:
            chosen = (geom, dpr)
            break
    geom, dpr = chosen
    return QPoint(round(geom.left() + (px - geom.left() * dpr) / dpr),
                  round(geom.top() + (py - geom.top() * dpr) / dpr))


def qt_screens() -> list[tuple[QRect, float]]:
    """Экраны Qt: (логическая геометрия, devicePixelRatio)."""
    return [(s.geometry(), s.devicePixelRatio()) for s in QGuiApplication.screens()]


def physical_screen_rects(screens: Sequence[tuple[QRect, float]] | None = None) -> list[Rect]:
    """Физические прямоугольники экранов (по тому же допущению)."""
    result: list[Rect] = []
    for geom, dpr in (screens if screens is not None else qt_screens()):
        left, top = round(geom.left() * dpr), round(geom.top() * dpr)
        result.append((left, top, left + round(geom.width() * dpr), top + round(geom.height() * dpr)))
    return result


def _moved(a: Rect, b: Rect) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


# --- последовательность ---------------------------------------------------


class BiteController(QObject):
    """Ведёт пчелу от приступа злости до закрытия (или отмены) одного окна."""

    IDLE, FLYING, WARNING, STINGING = "idle", "flying", "warning", "stinging"

    def __init__(self, bee, *, winapi_module: ModuleType = winapi, own_pid: int | None = None,
                 whitelist: frozenset[str] | set[str] = DEFAULT_WHITELIST,
                 rng: random.Random | None = None, clock: Callable[[], float] = time.monotonic,
                 countdown_s: int = COUNTDOWN_S, tick_ms: int = 1000,
                 cooldown_s: float = COOLDOWN_S, sting_ms: int = STING_MS,
                 screens_provider: Callable[[], list[tuple[QRect, float]]] = qt_screens) -> None:
        super().__init__(bee)
        self._bee = bee
        self._winapi = winapi_module
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._whitelist = frozenset(whitelist)
        self._rng = rng or random.Random()
        self._clock = clock
        self._countdown_s = countdown_s
        self._cooldown_s = cooldown_s
        self._sting_ms = sting_ms
        self._screens_provider = screens_provider

        self.state = self.IDLE
        self._bitten_this_episode = False
        self._last_bite: float | None = None
        self._target: WindowInfo | None = None
        self._left = 0
        self._last_refusal: str | None = None  # чтобы не повторять в логе одну и ту же причину

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(tick_ms)
        self._tick_timer.timeout.connect(self._on_tick)
        self._sting_timer = QTimer(self)
        self._sting_timer.setSingleShot(True)
        self._sting_timer.setInterval(sting_ms)
        self._sting_timer.timeout.connect(self._on_sting_done)

        bee.mood_changed.connect(self.on_mood_changed)
        bee.biting_toggled.connect(self.on_biting_toggled)
        bee.drag_started.connect(self.on_drag_started)
        bee.drag_finished.connect(self.on_drag_finished)

    # --- события -----------------------------------------------------------

    def on_mood_changed(self, change: MoodChange) -> None:
        """Попытка укуса при каждом angry-high (не только при переходе в него).

        Так пчела, которую разозлили, пока её тащили (или пока укус был невозможен),
        идёт кусать при следующей провокации. Отмена — когда злость спала; конец
        эпизода — выход из ANGRY. Все предохранители проверяет _try_start.
        """
        if change.new is not Mood.ANGRY:
            self._bitten_this_episode = False  # приступ закончился
            self._last_refusal = None
        if self.state != self.IDLE and not change.angry_high:
            self._cancel("настроение больше не angry-high")
        if change.angry_high:
            self._try_start()

    def on_biting_toggled(self, enabled: bool) -> None:
        if not enabled and self.state != self.IDLE:
            self._cancel("кусание выключено")

    def on_drag_started(self) -> None:
        if self.state != self.IDLE:
            self._cancel("пчелу взяли в руки")

    def on_drag_finished(self) -> None:
        """Отпустили всё ещё разъярённую пчелу — она снова пробует укусить."""
        if self._bee.engine.angry_high:
            self._try_start()

    # --- старт -------------------------------------------------------------

    def _try_start(self) -> None:
        if self.state != self.IDLE:
            return
        refuse = None
        if not self._winapi.AVAILABLE:
            refuse = "WinAPI недоступен"
        elif not self._bee.biting_enabled:
            refuse = "кусание выключено"
        elif self._bitten_this_episode:
            refuse = "в этом приступе окно уже закрыто"
        elif self._last_bite is not None and self._clock() - self._last_bite < self._cooldown_s:
            refuse = "пауза между укусами"
        elif self._bee.is_dragging:
            refuse = "пчелу тащат"
        if refuse:
            self._refuse(refuse)
            return
        screens = self._screens_provider()
        target = pick_target(self._winapi.list_windows(), self._own_pid, self._whitelist,
                             self._rng, physical_screen_rects(screens))
        if target is None:
            self._refuse("нет подходящих окон")
            return
        self._last_refusal = None
        log.info("цель: %r (%s, hwnd=%s)", target.title, target.process_name, target.hwnd)
        self._target = target
        self.state = self.FLYING
        self._bee.fly_to(self._bee_corner(target, screens), self._on_arrived)

    def _refuse(self, reason: str) -> None:
        """Пишет отказ в лог; та же причина подряд повторно не пишется (до смены причины или эпизода)."""
        if reason != self._last_refusal:
            log.info("не кусаю: %s", reason)
        else:
            log.debug("не кусаю: %s", reason)
        self._last_refusal = reason

    def _bee_corner(self, target: WindowInfo, screens) -> QPoint:
        """Левый верхний угол окна пчелы: она висит над заголовком, примерно на 1/3 его ширины."""
        left, top, right, _ = target.rect
        spot = physical_to_logical((left + (right - left) // 3, top), screens)
        size = self._bee.width()
        return QPoint(spot.x() - size // 2, spot.y() - size + 30)

    # --- шаги --------------------------------------------------------------

    def _on_arrived(self) -> None:
        if self.state != self.FLYING:
            return
        reason = self._check_target()
        if reason:
            self._cancel(reason)
            return
        self.state = self.WARNING
        self._left = self._countdown_s
        self._announce()
        self._tick_timer.start()

    def _countdown_text(self) -> str:
        done = range(self._countdown_s, self._left - 1, -1)
        return "…".join(str(n) for n in done) + "…"

    def _announce(self) -> None:
        self._bee.say(self._countdown_text(), 1500)

    def _on_tick(self) -> None:
        if self.state != self.WARNING:
            self._tick_timer.stop()
            return
        reason = self._check_target()
        if reason:
            self._cancel(reason)
            return
        self._left -= 1
        if self._left > 0:
            self._announce()
            return
        self._tick_timer.stop()
        self.state = self.STINGING
        self._bee.stinging = True
        self._bee.update()
        self._sting_timer.start()

    def _on_sting_done(self) -> None:
        if self.state != self.STINGING:
            return
        self._bee.stinging = False
        reason = self._check_target()  # последняя проверка прямо перед закрытием
        if reason:
            self._cancel(reason)
            return
        target = self._target
        assert target is not None
        ok = self._winapi.close_window(target.hwnd)
        log.info("WM_CLOSE отправлен окну %r (hwnd=%s): %s", target.title, target.hwnd,
                 "ok" if ok else "не удалось")
        self._last_bite = self._clock()
        self._bitten_this_episode = True
        self._reset()

    # --- проверки и отмена -------------------------------------------------

    def _check_target(self) -> str | None:
        """Причина отмены или None, если можно продолжать."""
        if not self._bee.engine.angry_high:
            return "настроение больше не angry-high"
        if not self._bee.biting_enabled:
            return "кусание выключено"
        if self._bee.is_dragging:
            return "пчелу взяли в руки"
        target = self._target
        assert target is not None
        current = next((w for w in self._winapi.list_windows() if w.hwnd == target.hwnd), None)
        if current is None:
            return "окно исчезло"
        if not eligible(current, self._own_pid, self._whitelist,
                        physical_screen_rects(self._screens_provider())):
            return "окно больше не подходит"
        if _moved(current.rect, target.rect) >= MAX_MOVE_PX:
            return "окно сдвинулось"
        return None

    def _cancel(self, reason: str) -> None:
        log.info("отмена укуса: %s", reason)
        if self.state == self.FLYING:
            self._bee.cancel_flight()
        self._reset()

    def _reset(self) -> None:
        self._tick_timer.stop()
        self._sting_timer.stop()
        self._bee.stinging = False
        self._bee.update()
        self.state = self.IDLE
        self._target = None
