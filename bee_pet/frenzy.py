"""Злая пчела мечется: короткие резкие перелёты вокруг текущего места.

Полётами злой пчелы в первую очередь ведает укус (BiteController): пока он не в IDLE,
пчела висит там, куда он её привёл. Мечется она только в свободное от укуса время,
когда её не тащат и она не летит по чужой команде.

Злая пчела уворачивается от курсора: прыгает подальше от него, а если курсор
подобрался ближе радиуса уклонения, сразу отскакивает в сторону от него.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QCursor, QGuiApplication

from bee_pet.mood import Mood, MoodChange

log = logging.getLogger("bee_pet.frenzy")


@dataclass(frozen=True)
class FrenzyStyle:
    """Параметры метаний: радиус прыжка (px), скорость (px/с), паузы (с), кривая полёта.

    dodge_radius — если курсор ближе этого (px) к центру пчелы, она отскакивает.
    """
    radius: float
    speed: float
    pause_min_s: float
    pause_max_s: float
    min_ms: int
    easing: QEasingCurve.Type
    dodge_radius: float = 150.0


ANGRY_STYLE = FrenzyStyle(radius=250.0, speed=600.0, pause_min_s=0.4, pause_max_s=1.2,
                          min_ms=150, easing=QEasingCurve.OutQuad, dodge_radius=150.0)
# angry high — «очень беспорядочно и быстро»
FURIOUS_STYLE = FrenzyStyle(radius=600.0, speed=1500.0, pause_min_s=0.0, pause_max_s=0.25,
                            min_ms=80, easing=QEasingCurve.Linear, dodge_radius=220.0)

MIN_HOP_FRACTION = 0.25  # прыжок не короче этой доли радиуса
POLL_MS = 300            # как часто проверять, не освободилась ли пчела (укус, перенос)
LANDING_POLL_MS = 30     # свой прыжок ещё не долетел: проверить снова почти сразу
START_DELAY_MS = 0       # первый прыжок — со следующего оборота цикла событий

HOP_CANDIDATES = 6           # сколько точек прыжка перебрать, чтобы не прыгнуть к курсору
SAFE_CURSOR_FACTOR = 1.5     # точка прыжка годится, если дальше 1.5 радиуса уклонения от курсора
DODGE_POLL_MS = 50           # как часто смотреть, где курсор (только пока пчела злая)
DODGE_MIN_INTERVAL_S = 0.15  # отскоки не чаще, чтобы пчела не дёргалась на месте
DODGE_SPEED_FACTOR = 1.5     # отскок быстрее обычного прыжка
DODGE_EASING = QEasingCurve.OutQuad
# Направления отскока относительно «прямо от курсора» (градусы): у края экрана
# прямой отскок упирается в границу, тогда берётся лучший из боковых.
DODGE_ANGLES = (0, 30, -30, 60, -60, 90, -90, 120, -120)


Areas = QRect | Sequence[QRect]


def screen_area(center: QPoint) -> list[QRect]:  # noqa: ARG001 — сигнатура провайдера
    """Рабочие области всех экранов: злая пчела может перелететь на соседний монитор."""
    return [s.availableGeometry() for s in QGuiApplication.screens()]


def _clamp_to(x: float, y: float, area: QRect, size: int) -> QPoint:
    return QPoint(max(area.left(), min(round(x), area.right() - size + 1)),
                  max(area.top(), min(round(y), area.bottom() - size + 1)))


def _clamp(x: float, y: float, areas: Areas, size: int) -> QPoint:
    """Левый верхний угол окна пчелы, чтобы окно целиком было внутри одной из областей.

    Берётся область, ближайшая к желаемой точке: общий прямоугольник всех экранов
    не годится, у мониторов разной высоты в нём есть «дыры» вне экранов.
    """
    if isinstance(areas, QRect):
        return _clamp_to(x, y, areas, size)
    best: QPoint | None = None
    best_d = math.inf
    for area in areas:
        point = _clamp_to(x, y, area, size)
        d = math.hypot(point.x() - x, point.y() - y)
        if d < best_d:
            best, best_d = point, d
    assert best is not None, "нет ни одной области экрана"
    return best


def _dist_to(cursor: QPoint, corner: QPoint, size: int) -> float:
    """Расстояние от курсора до центра пчелы, чей левый верхний угол — corner."""
    return math.hypot(corner.x() + size / 2 - cursor.x(), corner.y() + size / 2 - cursor.y())


def hop_target(pos: QPoint, style: FrenzyStyle, area: Areas, size: int,
               rng: random.Random, cursor: QPoint | None = None,
               candidates: int = 1) -> QPoint:
    """Случайная точка в радиусе style.radius от pos; окно пчелы целиком на одном из экранов.

    Если задан cursor, перебирается candidates случайных точек: годятся те, что дальше
    SAFE_CURSOR_FACTOR × радиус уклонения от курсора, из них берётся первая (случайная).
    Если не годится ни одна — самая дальняя. Всегда брать самую дальнюю нельзя: пчелу
    прибивает в дальний угол экрана, и на соседний монитор она уже не перелетает.
    """
    tries = max(1, candidates) if cursor is not None else 1
    safe = style.dodge_radius * SAFE_CURSOR_FACTOR
    best: QPoint | None = None
    best_dist = -1.0
    for _ in range(tries):
        angle = rng.uniform(0.0, 2 * math.pi)
        dist = rng.uniform(style.radius * MIN_HOP_FRACTION, style.radius)
        point = _clamp(pos.x() + dist * math.cos(angle), pos.y() + dist * math.sin(angle),
                       area, size)
        if cursor is None:
            return point
        d = _dist_to(cursor, point, size)
        if d >= safe:
            return point
        if d > best_dist:
            best, best_dist = point, d
    assert best is not None
    return best


def dodge_target(pos: QPoint, cursor: QPoint, style: FrenzyStyle, area: Areas,
                 size: int) -> QPoint:
    """Куда отскочить от курсора: на style.radius в сторону от него, внутри area.

    Перебираются направления DODGE_ANGLES вокруг «прямо от курсора» и берётся точка,
    самая дальняя от курсора после прижатия к краям (так пчела выбирается из угла).
    """
    cx, cy = pos.x() + size / 2, pos.y() + size / 2
    dx, dy = cx - cursor.x(), cy - cursor.y()
    base = math.atan2(dy, dx) if math.hypot(dx, dy) > 0.5 else 0.0
    best: QPoint | None = None
    best_dist = -1.0
    for deg in DODGE_ANGLES:
        a = base + math.radians(deg)
        point = _clamp(cx + style.radius * math.cos(a) - size / 2,
                       cy + style.radius * math.sin(a) - size / 2, area, size)
        d = _dist_to(cursor, point, size)
        if d > best_dist + 0.5:  # при равенстве остаётся направление ближе к прямому
            best, best_dist = point, d
    assert best is not None
    return best


class FrenzyController(QObject):
    """Пока пчела злится, она прыгает с места на место; angry high — быстрее и дальше.

    Весь цикл ведёт один таймер, и прыжок никогда не начинается прямо в обработчике
    mood_changed: укус стартует из того же сигнала, и к срабатыванию таймера он уже
    в состоянии FLYING, поэтому его полёт не будет перебит. Перед каждым прыжком
    проверяется: пчелу не тащат, укус в IDLE, пчела не летит. Иначе — повторная
    проверка через POLL_MS, так метания сами продолжаются после укуса и переноса.

    Когда злость проходит, новые прыжки не планируются, а текущий (доли секунды)
    долетает до конца: обрывать его посреди экрана незачем.

    Уклонение от курсора ведёт второй таймер (dodge_poll_ms), он работает только,
    пока пчела злая. Курсор ближе style.dodge_radius — пчела сразу отскакивает,
    даже посреди паузы или своего прыжка (прыжок перенаправляется). Не уклоняется,
    пока на ней зажата кнопка мыши или её тащат, пока идёт укус, открыто меню
    и пока она летит по чужой команде.
    """

    def __init__(self, bee, bite_controller=None, *, rng: random.Random | None = None,
                 angry_style: FrenzyStyle = ANGRY_STYLE,
                 furious_style: FrenzyStyle = FURIOUS_STYLE, poll_ms: int = POLL_MS,
                 area_provider=screen_area,
                 cursor_provider: Callable[[], QPoint] = QCursor.pos,
                 dodge_poll_ms: int = DODGE_POLL_MS,
                 dodge_min_interval_s: float = DODGE_MIN_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(bee)
        self._bee = bee
        self._bite = bite_controller
        self._rng = rng or random.Random()
        self._angry_style, self._furious_style = angry_style, furious_style
        self._poll_ms = poll_ms
        self._area_provider = area_provider
        self._cursor_provider = cursor_provider
        self._dodge_min_interval_s = dodge_min_interval_s
        self._clock = clock
        self.hops = 0  # сколько прыжков сделано (для логов и тестов)
        self.dodges = 0  # сколько раз отскочила от курсора
        self._own_flight = False  # летит ли пчела по нашей команде
        self._last_dodge = -math.inf

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._step)
        self._dodge_timer = QTimer(self)
        self._dodge_timer.setInterval(dodge_poll_ms)
        self._dodge_timer.timeout.connect(self._check_cursor)

        bee.mood_changed.connect(self.on_mood_changed)
        bee.drag_started.connect(self.on_drag_started)
        bee.drag_finished.connect(self.on_drag_finished)
        if self._angry():
            self._timer.start(START_DELAY_MS)
            self._dodge_timer.start()

    # --- состояние ---------------------------------------------------------

    def _angry(self) -> bool:
        return self._bee.engine.mood is Mood.ANGRY

    def _style(self) -> FrenzyStyle:
        return self._furious_style if self._bee.engine.angry_high else self._angry_style

    def _blocked(self) -> str | None:
        """Почему сейчас нельзя прыгать; None, если можно."""
        if self._bee.is_dragging:
            return "пчелу тащат"
        if self._bite is not None and self._bite.state != self._bite.IDLE:
            return "идёт укус"
        if self._bee.is_flying:
            return "пчела уже летит"
        return None

    def _dodge_blocked(self) -> str | None:
        """Почему сейчас нельзя уворачиваться от курсора; None, если можно."""
        bee = self._bee
        if bee.is_dragging or getattr(bee, "is_pressed", False):
            return "пчелу держат"
        if self._bite is not None and self._bite.state != self._bite.IDLE:
            return "идёт укус"
        menu = getattr(bee, "menu", None)
        if menu is not None and menu.isVisible():
            return "открыто меню"
        if bee.is_flying and not self._own_flight:
            return "пчела летит по чужой команде"
        return None

    @property
    def active(self) -> bool:
        """Запланирован ли следующий шаг метаний."""
        return self._timer.isActive()

    @property
    def dodging(self) -> bool:
        """Следит ли пчела за курсором (запущен таймер уклонения)."""
        return self._dodge_timer.isActive()

    # --- события -----------------------------------------------------------

    def on_mood_changed(self, change: MoodChange) -> None:
        """Разозлилась — метания начинаются со следующего оборота цикла; успокоилась — стоп."""
        if change.new is not Mood.ANGRY:
            if self._timer.isActive():
                log.info("злость прошла: больше не мечусь")
            self._timer.stop()
            self._dodge_timer.stop()
            return
        if not self._dodge_timer.isActive():
            self._dodge_timer.start()
        if not self._timer.isActive() or change.became_angry_high:
            # Здесь не летим: проверка _blocked() — уже в _step, после всех обработчиков.
            self._timer.start(START_DELAY_MS)

    def on_drag_started(self) -> None:
        self._own_flight = False  # перенос обрывает любой полёт

    def on_drag_finished(self) -> None:
        if self._angry() and not self._timer.isActive():
            self._timer.start(START_DELAY_MS)

    # --- шаг ---------------------------------------------------------------

    def _step(self) -> None:
        if not self._angry():
            return
        reason = self._blocked()
        if reason:
            log.debug("не мечусь: %s", reason)
            own = self._own_flight and reason == "пчела уже летит"
            self._timer.start(LANDING_POLL_MS if own else self._poll_ms)
            return
        style = self._style()
        bee = self._bee
        center = bee.geometry().center()
        target = hop_target(bee.pos(), style, self._area_provider(center), bee.width(),
                            self._rng, cursor=self._cursor_provider(),
                            candidates=HOP_CANDIDATES)
        self._own_flight = True
        duration = bee.fly_to(target, self._on_arrived, speed=style.speed,
                              min_ms=style.min_ms, easing=style.easing)
        self.hops += 1
        pause = self._rng.uniform(style.pause_min_s, style.pause_max_s)
        # Следующий шаг — после прилёта и паузы; если полёт перебьют (укус, перенос),
        # шаг всё равно наступит и проверит, свободна ли пчела.
        self._timer.start(int(duration or 0) + int(pause * 1000))

    def _check_cursor(self) -> None:
        """Курсор слишком близко — отскок в сторону от него (не чаще dodge_min_interval_s)."""
        if not self._angry():
            self._dodge_timer.stop()
            return
        reason = self._dodge_blocked()
        if reason:
            return
        bee = self._bee
        style = self._style()
        cursor = self._cursor_provider()
        center = bee.geometry().center()
        if math.hypot(center.x() - cursor.x(), center.y() - cursor.y()) > style.dodge_radius:
            return
        now = self._clock()
        if now - self._last_dodge < self._dodge_min_interval_s:
            return
        self._last_dodge = now
        target = dodge_target(bee.pos(), cursor, style, self._area_provider(center), bee.width())
        log.debug("курсор рядом: отскакиваю в (%d, %d)", target.x(), target.y())
        self._own_flight = True
        duration = bee.fly_to(target, self._on_arrived, speed=style.speed * DODGE_SPEED_FACTOR,
                              min_ms=style.min_ms, easing=DODGE_EASING)
        self.dodges += 1
        # Обычные метания продолжатся после отскока и паузы.
        pause = self._rng.uniform(style.pause_min_s, style.pause_max_s)
        self._timer.start(int(duration or 0) + int(pause * 1000))

    def _on_arrived(self) -> None:
        self._own_flight = False
