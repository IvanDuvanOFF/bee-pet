"""Спокойная пчела ходит пешком по кромке, на которой сидит.

Кромка («поверхность») — горизонтальный отрезок: верхний край окна, на которое пчела
села (WanderController), или верхний край панели задач её экрана. Пока пчела спокойна
и сидит, она то стоит на месте, то проходит немного в случайную сторону, иногда
жужжа (ChatterController.ping_now). Перелёты на другое окно по-прежнему редкие и
ими ведает WanderController; злостью — FrenzyController и BiteController.

Окно под пчелой проверяется раз в секунду: сдвинулось — пчела едет вместе с ним,
исчезло, свернулось или место под пчелой закрыло другое окно — она перелетает
(WanderController.wander_now без переключения фокуса). Над чужими окнами здесь
никаких действий нет: только чтение списка окон через `winapi.list_windows`.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRect, QTimer

from bee_pet import winapi
from bee_pet.bee_window import WALK_SPEED
from bee_pet.biting import (Rect, _moved, physical_screen_rects, physical_to_logical,
                            qt_screens)
from bee_pet.mood import Mood, MoodChange
from bee_pet.painter import feet_y
from bee_pet.wander import (ScreenProvider, _occluders, headroom_px, landing_skip_reason,
                            logical_to_physical, screen_at, spot_covered)
from bee_pet.winapi import WindowInfo

log = logging.getLogger("bee_pet.walk")

# Тайминги и расстояния по умолчанию (в тестах подменяются через конструктор).
PAUSE_S = (1.5, 5.0)        # сколько стоять между прогулками, с
STEP_PX = (60, 250)         # на сколько пройти за раз, px
MIN_STEP_PX = 20            # короче этого не идём (упёрлась в край — постоим)
BUZZ_CHANCE = 0.35          # вероятность зажужжать, отправляясь в путь
BUZZ_MIN_GAP_S = 20.0       # и не чаще, чем раз в столько секунд
TRACK_MS = 1000             # как часто проверять окно под пчелой
POLL_MS = 300               # пчела занята (летит, укус, злится) — проверить снова через
LAND_AFTER_DROP_S = 0.8     # отпустили спокойную пчелу — через столько она садится
LAND_AFTER_CALM_S = 1.0     # злость прошла — через столько садится
FOLLOW_STOP_PX = 30         # окно уехало дальше этого (физ. px) — шаг прерывается


@dataclass
class Surface:
    """Кромка, на которой сидит пчела (логические координаты Qt).

    left..right — отрезок по x, y — строка кромки (на неё встают лапки), screen —
    геометрия экрана, за который пчела не выходит. hwnd и rect (физические пиксели) —
    окно под пчелой; для панели задач оба None.
    """
    left: int
    right: int
    y: int
    screen: QRect
    hwnd: int | None = None
    rect: Rect | None = None

    @property
    def is_taskbar(self) -> bool:
        return self.hwnd is None


class WalkController(QObject):
    """Ходьба по кромке, слежение за окном под пчелой и посадка после переноса и злости."""

    def __init__(self, bee, wander, chatter=None, bite_controller=None, *,
                 winapi_module: ModuleType = winapi, own_pid: int | None = None,
                 rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 pause_s: tuple[float, float] = PAUSE_S,
                 step_px: tuple[int, int] = STEP_PX, speed: float = WALK_SPEED,
                 buzz_chance: float = BUZZ_CHANCE, buzz_min_gap_s: float = BUZZ_MIN_GAP_S,
                 track_ms: int = TRACK_MS, poll_ms: int = POLL_MS,
                 land_after_drop_s: float = LAND_AFTER_DROP_S,
                 land_after_calm_s: float = LAND_AFTER_CALM_S,
                 screens_provider: Callable[[], list[tuple[QRect, float]]] = qt_screens,
                 screen_provider: ScreenProvider = screen_at,
                 autostart: bool = True) -> None:
        super().__init__(bee)
        self._bee, self._wander, self._chatter, self._bite = bee, wander, chatter, bite_controller
        self._winapi = winapi_module
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._rng = rng or random.Random()
        self._clock = clock
        self._pause_s, self._step_px, self._speed = pause_s, step_px, speed
        self._buzz_chance, self._buzz_min_gap_s = buzz_chance, buzz_min_gap_s
        self._poll_ms = poll_ms
        self._land_after_drop_s, self._land_after_calm_s = land_after_drop_s, land_after_calm_s
        self._screens_provider = screens_provider
        self._screen_provider = screen_provider

        self.surface: Surface | None = None
        self.walks = 0     # сколько раз пошла (для логов и тестов)
        self.buzzes = 0    # сколько раз зажужжала на ходу
        self._last_buzz: float | None = None

        self._step_timer = QTimer(self)
        self._step_timer.setSingleShot(True)
        self._step_timer.timeout.connect(self._next_step)
        self._land_timer = QTimer(self)
        self._land_timer.setSingleShot(True)
        self._land_timer.timeout.connect(self._land)
        self._track_timer = QTimer(self)
        self._track_timer.setInterval(track_ms)
        self._track_timer.timeout.connect(self.track)

        bee.mood_changed.connect(self.on_mood_changed)
        bee.drag_started.connect(self.on_drag_started)
        bee.drag_finished.connect(self.on_drag_finished)
        wander.landed.connect(self.on_landed)
        if autostart:
            self._track_timer.start()
            self.perch_on_taskbar()

    # --- состояние ---------------------------------------------------------

    def _calm(self) -> bool:
        return self._bee.engine.mood is not Mood.ANGRY

    def _bite_busy(self) -> bool:
        return self._bite is not None and self._bite.state != self._bite.IDLE

    def _busy(self) -> str | None:
        """Почему пчелу сейчас нельзя двигать; None, если можно."""
        if not self._calm():
            return "пчела злится"
        if self._bee.is_dragging:
            return "пчелу тащат"
        if self._bite_busy():
            return "идёт укус"
        if self._bee.is_flying:
            return "пчела летит"
        occupied = getattr(self._bee, "occupied_by", None)
        if occupied:
            return f"пчела занята ({occupied})"
        return None

    def _blocked(self) -> str | None:
        """Почему сейчас нельзя ходить; None, если можно."""
        reason = self._busy()
        if reason:
            return reason
        if self.surface is None or not self._bee.perched:
            return "пчела не сидит на кромке"
        return None

    @property
    def walking(self) -> bool:
        """Идёт ли пчела пешком."""
        return self._bee.is_walking

    # --- кромка ------------------------------------------------------------

    def _size(self) -> int:
        return self._bee.width()

    def x_range(self, surface: Surface) -> tuple[int, int]:
        """Допустимые x левого края окна пчелы: целиком на кромке и на экране."""
        size = self._size()
        screen = surface.screen
        lo = max(surface.left, screen.left())
        hi = min(surface.right, screen.right()) - size + 1
        if hi < lo:  # кромка уже пчелы: стоим посередине
            mid = (max(surface.left, screen.left()) + min(surface.right, screen.right())) // 2
            lo = hi = max(screen.left(), min(mid - size // 2, screen.right() - size + 1))
        return lo, hi

    def corner_y(self, surface: Surface) -> int:
        """y левого верхнего угла окна пчелы, чтобы лапки стояли на кромке (в пределах экрана)."""
        size = self._size()
        y = surface.y - feet_y(size)
        return max(surface.screen.top(), min(y, surface.screen.bottom() - size + 1))

    def taskbar_surface(self) -> Surface:
        """Верхний край панели задач экрана, где сейчас пчела."""
        area, geom = self._screen_provider(self._bee.geometry().center())
        return Surface(area.left(), area.right(), area.bottom() + 1, geom)

    def window_surface(self, win: WindowInfo) -> Surface:
        """Верхняя кромка окна win в логических координатах (экран — тот, где пчела)."""
        screens = self._screens_provider()
        left, top, right, _ = win.rect
        size = self._size()
        center = self._bee.geometry().center()
        feet = logical_to_physical(QPoint(center.x(), self._bee.y() + feet_y(size)), screens)
        under = (max(left, min(feet[0], right - 1)), top + 1)
        point = physical_to_logical(under, screens)
        lo = physical_to_logical((left, top + 1), screens).x()
        hi = physical_to_logical((right - 1, top + 1), screens).x()
        _, geom = self._screen_provider(point)
        return Surface(lo, hi, point.y(), geom, win.hwnd, win.rect)

    def _place(self, surface: Surface, x: int | None = None) -> None:
        """Ставит пчелу на кромку: x прижимается к отрезку, лапки — на кромку."""
        lo, hi = self.x_range(surface)
        x = self._bee.x() if x is None else x
        self._bee.move(max(lo, min(x, hi)), self.corner_y(surface))

    def _perch(self, surface: Surface) -> None:
        self.surface = surface
        self._place(surface)
        self._bee.perched = True
        self._schedule_pause()

    def perch_on_taskbar(self) -> None:
        """Сажает пчелу на панель задач прямо там, где она есть (без полёта): старт приложения."""
        surface = self.taskbar_surface()
        log.info("сижу на панели задач (y=%d, x=%d..%d)", surface.y, surface.left, surface.right)
        self._perch(surface)

    # --- события -----------------------------------------------------------

    def on_landed(self, target: WindowInfo | None) -> None:
        """WanderController посадил пчелу: на окно или (None) на панель задач."""
        if not self._calm() or self._bee.is_dragging:
            return
        surface = self.taskbar_surface() if target is None else self.window_surface(target)
        log.info("села на %s", "панель задач" if target is None
                 else f"окно {target.title!r} ({target.process_name})")
        self._perch(surface)

    def on_mood_changed(self, change: MoodChange) -> None:
        """Злость — пчела взлетает (дальше мечется); злость прошла — снова садится."""
        if change.new is Mood.ANGRY:
            self._land_timer.stop()
            if self.surface is not None or self._bee.is_walking or self._bee.perched:
                log.info("разозлилась: взлетаю с кромки")
            self.surface = None
            self._bee.stop_walking()
            self._bee.perched = False
            return
        if change.old is Mood.ANGRY:
            self._land_timer.start(int(self._land_after_calm_s * 1000))

    def on_drag_started(self) -> None:
        self._land_timer.stop()
        self.surface = None

    def on_drag_finished(self) -> None:
        if self._calm():
            self._land_timer.start(int(self._land_after_drop_s * 1000))

    def _land(self) -> None:
        """Садится на ближайшую кромку (окно или панель задач), когда пчела свободна."""
        if not self._calm() or self._bee.is_dragging:
            return  # злостью ведает frenzy; после переноса таймер заведёт drag_finished
        if self._bee.perched and self.surface is not None:
            return
        if self._busy() or not self._wander.land_nearest():
            self._land_timer.start(self._poll_ms)

    # --- ходьба ------------------------------------------------------------

    def _schedule_pause(self) -> None:
        lo, hi = self._pause_s
        self._step_timer.start(max(1, int(self._rng.uniform(lo, hi) * 1000)))

    def pick_walk_x(self) -> int | None:
        """Куда пойти: на step_px в случайную сторону, внутри кромки; None — некуда."""
        assert self.surface is not None
        lo, hi = self.x_range(self.surface)
        x = self._bee.x()
        dist = self._rng.randint(*self._step_px)
        direction = self._rng.choice((-1, 1))
        for d in (direction, -direction):
            target = max(lo, min(x + d * dist, hi))
            if abs(target - x) >= MIN_STEP_PX:
                return target
        return None

    def _next_step(self) -> None:
        reason = self._blocked()
        if reason or self._bee.is_walking:
            log.debug("не хожу: %s", reason or "уже иду")
            self._step_timer.start(self._poll_ms)
            return
        target = self.pick_walk_x()
        if target is None:
            self._schedule_pause()
            return
        log.debug("иду с x=%d на x=%d", self._bee.x(), target)
        if not self._bee.walk_to(target, self._on_walk_done, self._speed):
            self._step_timer.start(self._poll_ms)
            return
        self.walks += 1
        self._maybe_buzz()

    def _on_walk_done(self) -> None:
        self._schedule_pause()

    def _maybe_buzz(self) -> None:
        """Иногда жужжит, отправляясь в путь (не чаще раза в buzz_min_gap_s)."""
        if self._chatter is None:
            return
        now = self._clock()
        if self._last_buzz is not None and now - self._last_buzz < self._buzz_min_gap_s:
            return
        if self._rng.random() >= self._buzz_chance:
            return
        if self._chatter.ping_now():
            self._last_buzz = now
            self.buzzes += 1

    # --- слежение за окном -------------------------------------------------

    def track(self) -> None:
        """Проверяет окно под пчелой: едет вместе с ним или улетает, если сесть больше некуда."""
        surface = self.surface
        if surface is None or surface.is_taskbar or not self._winapi.AVAILABLE:
            return
        if not self._bee.perched or self._busy():
            return
        windows = self._winapi.list_windows()
        index = next((i for i, w in enumerate(windows) if w.hwnd == surface.hwnd), None)
        if index is None:
            self._take_off("окно исчезло")
            return
        win = windows[index]
        screens = self._screens_provider()
        reason = landing_skip_reason(win, self._own_pid, physical_screen_rects(screens),
                                     headroom_px(screens, self._bee.feet_offset()))
        if reason:
            self._take_off(reason)
            return
        assert surface.rect is not None
        if win.rect != surface.rect:
            self._follow(surface, win)
        left, top, right, _ = win.rect
        size = self._size()
        center = self._bee.geometry().center()
        feet = logical_to_physical(QPoint(center.x(), self._bee.y() + feet_y(size)),
                                   self._screens_provider())
        spot = (max(left, min(feet[0], right - 1)), top + 1)
        if spot_covered(spot, _occluders(windows, index, self._own_pid)):
            self._take_off("место на кромке закрыло другое окно")

    def _follow(self, old: Surface, win: WindowInfo) -> None:
        """Окно сдвинулось или изменило размер — пчела переезжает на его новую кромку."""
        assert old.rect is not None
        moved = _moved(win.rect, old.rect)
        if self._bee.is_walking and moved >= FOLLOW_STOP_PX:
            self._bee.stop_walking()
            self._schedule_pause()
        new = self.window_surface(win)
        log.debug("окно сдвинулось на %d px: еду вместе с ним", moved)
        self.surface = new
        self._place(new, self._bee.x() + (new.left - old.left))

    def _take_off(self, reason: str) -> None:
        log.info("улетаю с окна: %s", reason)
        self.surface = None
        self._bee.stop_walking()
        self._bee.perched = False
        if not self._wander.wander_now(allow_focus=False):
            self._land_timer.start(self._poll_ms)
