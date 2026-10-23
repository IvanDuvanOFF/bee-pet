"""Окно пчелы: анимация, перетаскивание, контекстное меню, полёт, настроение."""
from __future__ import annotations

import math
import random
import time
from collections import deque
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF,
                            QSettings, Qt, QTimer, Signal)
from PySide6.QtGui import QAction, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from bee_pet.bubble import SpeechBubble
from bee_pet.lines import reaction
from bee_pet.mood import Action, Mood, MoodChange, MoodEngine
from bee_pet.painter import PaintState, paint_bee

WINDOW_SIZE = 120
SCREEN_MARGIN = 24
FRAME_MS = 33
DRAG_THRESHOLD = 4
FLIGHT_SPEED = 400.0  # px/с

# «Мельтешение» курсором над пчелой считается шлепком.
JITTER_WINDOW_S = 1.0      # скользящее окно наблюдения, с
JITTER_MIN_REVERSALS = 4   # сколько смен направления по одной оси нужно
JITTER_MIN_PATH = 250.0    # суммарный путь курсора за окно, px
JITTER_MIN_STEP = 3.0      # шаги по оси короче этого не меняют направление (шум)
JITTER_COOLDOWN_S = 3.0    # пауза после срабатывания: одно мельтешение = один шлепок

# Пункты меню действий: (подпись, действие).
MENU_ACTIONS: list[tuple[str, Action]] = [
    ("🌸 Дать цветок", Action.FLOWER),
    ("🤚 Погладить", Action.PET),
    ("👋 Шлёпнуть", Action.SWAT),
    ("🙏 Извиниться", Action.SORRY),
    ("💨 Побрызгать дихлофосом", Action.SPRAY),
]


class JitterDetector:
    """Распознаёт мельтешение курсора: частые развороты по одной оси на большом пути.

    Точки старше окна наблюдения выбрасываются, поэтому курсор может ненадолго
    выйти за пределы пчелы и вернуться — мельтешение всё равно засчитается.
    """

    def __init__(self, window_s: float = JITTER_WINDOW_S,
                 min_reversals: int = JITTER_MIN_REVERSALS,
                 min_path: float = JITTER_MIN_PATH,
                 min_step: float = JITTER_MIN_STEP) -> None:
        self.window_s = window_s
        self.min_reversals = min_reversals
        self.min_path = min_path
        self.min_step = min_step
        self._samples: deque[tuple[float, float, float]] = deque()

    def clear(self) -> None:
        """Забывает все накопленные точки."""
        self._samples.clear()

    def add(self, t: float, x: float, y: float) -> bool:
        """Добавляет точку курсора; True, если в окне наблюдения есть мельтешение."""
        self._samples.append((t, x, y))
        while self._samples and t - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        if len(self._samples) < 3:
            return False
        path = 0.0
        reversals = [0, 0]
        last_sign = [0, 0]
        prev = self._samples[0]
        for cur in list(self._samples)[1:]:
            deltas = (cur[1] - prev[1], cur[2] - prev[2])
            path += math.hypot(*deltas)
            for axis, d in enumerate(deltas):
                if abs(d) < self.min_step:
                    continue
                sign = 1 if d > 0 else -1
                if last_sign[axis] and sign != last_sign[axis]:
                    reversals[axis] += 1
                last_sign[axis] = sign
            prev = cur
        return path >= self.min_path and max(reversals) >= self.min_reversals


class BeeWindow(QWidget):
    """Прозрачное безрамочное окно с пчелой."""

    mood_changed = Signal(object)       # MoodChange
    action_performed = Signal(object)   # Action из меню
    chat_requested = Signal()
    biting_toggled = Signal(bool)
    focus_switch_toggled = Signal(bool)
    drag_started = Signal()

    def __init__(self, settings: QSettings | None = None,
                 engine: MoodEngine | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(WINDOW_SIZE, WINDOW_SIZE)
        self.setMouseTracking(True)  # движения без кнопок — для распознавания мельтешения

        self._settings = settings if settings is not None else QSettings("bee-pet", "bee-pet")
        self._biting = self._settings.value("biting_enabled", True, type=bool)
        self._focus_switch = self._settings.value("focus_switch_enabled", True, type=bool)
        self.engine = engine if engine is not None else MoodEngine()
        self.bubble = SpeechBubble()

        self._phase = 0.0
        self._tilt = 0.0
        self._wiggle = (0.0, 0.0)
        self._dragging = False
        self.stinging = False  # выпад жалом при укусе окна
        self._press_global = None
        self._press_pos = QPoint()
        self._flight: QPropertyAnimation | None = None
        self._on_arrived: Callable[[], None] | None = None
        self._clock = clock
        self._jitter = JitterDetector()
        self._jitter_cooldown_until = -math.inf

        self._move_to_initial_position()

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_frame)
        self._anim_timer.start(FRAME_MS)
        self._mood_timer = QTimer(self)
        self._mood_timer.timeout.connect(self._on_mood_tick)
        self._mood_timer.start(1000)

        self._build_menu()

    # --- настройки ---------------------------------------------------------

    @property
    def biting_enabled(self) -> bool:
        """Разрешено ли пчеле кусать окна."""
        return self._biting

    def _set_biting(self, value: bool) -> None:
        self._biting = value
        self._settings.setValue("biting_enabled", value)
        self._settings.sync()
        self.biting_toggled.emit(value)

    @property
    def focus_switch_enabled(self) -> bool:
        """Разрешено ли переключать окна, пока пчела отлучилась."""
        return self._focus_switch

    def _set_focus_switch(self, value: bool) -> None:
        self._focus_switch = value
        self._settings.setValue("focus_switch_enabled", value)
        self._settings.sync()
        self.focus_switch_toggled.emit(value)

    # --- меню --------------------------------------------------------------

    def _build_menu(self) -> None:
        self.menu = QMenu(self)
        self.menu_actions: dict[Action, QAction] = {}
        for label, action in MENU_ACTIONS:
            qa = self.menu.addAction(label)
            qa.triggered.connect(lambda _=False, a=action: self._on_menu_action(a))
            self.menu_actions[action] = qa
        self.menu.addSeparator()
        self.chat_action = self.menu.addAction("💬 Чат")
        self.chat_action.triggered.connect(self.chat_requested.emit)
        self.biting_action = self.menu.addAction("🦷 Разрешить кусать окна")
        self.biting_action.setCheckable(True)
        self.biting_action.setChecked(self._biting)
        self.biting_action.toggled.connect(self._set_biting)
        self.focus_switch_action = self.menu.addAction("🪟 Переключать окна, пока меня нет")
        self.focus_switch_action.setCheckable(True)
        self.focus_switch_action.setChecked(self._focus_switch)
        self.focus_switch_action.toggled.connect(self._set_focus_switch)
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction("❌ Выход")
        self.exit_action.triggered.connect(QApplication.quit)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu.exec(event.globalPos())

    def _on_menu_action(self, action: Action) -> None:
        was_angry = self.engine.mood is Mood.ANGRY
        self.apply_action(action)
        self.action_performed.emit(action)
        self.say(reaction(action, was_angry))

    # --- настроение --------------------------------------------------------

    def apply_action(self, action: Action) -> MoodChange:
        """Применяет действие к автомату, перерисовывает и сообщает об изменении."""
        change = self.engine.apply(action)
        self.update()
        self.mood_changed.emit(change)
        return change

    def _on_mood_tick(self) -> None:
        change = self.engine.tick()
        if change is not None:
            self.update()
            self.mood_changed.emit(change)

    # --- пузырь ------------------------------------------------------------

    def _bubble_anchor(self) -> QRect:
        """Область самой пчелы в глобальных координатах."""
        return self.geometry().adjusted(20, 28, -20, -20)

    def say(self, text: str, duration_ms: int = 4000) -> None:
        """Показывает реплику в пузыре над пчелой."""
        self.bubble.show_text(text, self._bubble_anchor(), duration_ms)

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self.bubble.reposition(self._bubble_anchor())

    # --- позиция -----------------------------------------------------------

    def _move_to_initial_position(self) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.right() - WINDOW_SIZE - SCREEN_MARGIN + 1,
                  area.bottom() - WINDOW_SIZE - SCREEN_MARGIN + 1)

    def _clamp_to_desktop(self, pos: QPoint) -> QPoint:
        """Держит окно внутри виртуального рабочего стола."""
        desktop = QGuiApplication.primaryScreen().virtualGeometry()
        x = max(desktop.left(), min(pos.x(), desktop.right() - WINDOW_SIZE + 1))
        y = max(desktop.top(), min(pos.y(), desktop.bottom() - WINDOW_SIZE + 1))
        return QPoint(x, y)

    # --- анимация ----------------------------------------------------------

    def _on_frame(self) -> None:
        self._phase += FRAME_MS / 1000.0
        if self._dragging:
            self._wiggle = (math.sin(self._phase * 2 * math.pi * 5) * 6, 0.0)
        elif self.engine.angry_high:
            self._wiggle = (random.uniform(-3, 3), random.uniform(-3, 3))
        else:
            self._wiggle = (0.0, 0.0)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        state = PaintState(phase=self._phase, mood=self.engine.mood,
                           angry_high=self.engine.angry_high, dragging=self._dragging,
                           tilt=self._tilt, wiggle=self._wiggle, stinging=self.stinging)
        paint_bee(p, QRectF(0, 0, self.width(), self.height()), state)
        p.end()

    # --- перетаскивание ----------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition()
            self._press_pos = self.pos()
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.LeftButton):
            if not event.buttons():
                pos = event.globalPosition()
                self._on_hover_move(pos.x(), pos.y())
            return
        if self._press_global is None:
            return
        delta = event.globalPosition() - self._press_global
        if not self._dragging:
            if math.hypot(delta.x(), delta.y()) < DRAG_THRESHOLD:
                return
            self.cancel_flight()
            self._dragging = True
            self.drag_started.emit()
        self.move(self._clamp_to_desktop(self._press_pos + delta.toPoint()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_global = None
            self._dragging = False
            self._wiggle = (0.0, 0.0)
            self.update()

    # --- мельтешение курсором ----------------------------------------------

    def _on_hover_move(self, x: float, y: float) -> None:
        """Копит точки курсора без нажатых кнопок; мельтешение = шлепок."""
        if self._dragging or self._press_global is not None or self.menu.isVisible():
            return
        now = self._clock()
        if now < self._jitter_cooldown_until:
            return
        if self._jitter.add(now, x, y):
            self._jitter.clear()
            self._jitter_cooldown_until = now + JITTER_COOLDOWN_S
            self._on_menu_action(Action.SWAT)

    # --- полёт -------------------------------------------------------------

    def fly_to(self, target: QPoint, on_arrived: Callable[[], None] | None = None) -> None:
        """Плавно переносит левый верхний угол окна в target; по прилёту зовёт on_arrived."""
        self.cancel_flight()
        start = self.pos()
        dx, dy = target.x() - start.x(), target.y() - start.y()
        dist = math.hypot(dx, dy)
        duration = int(max(300, min(3000, dist / FLIGHT_SPEED * 1000)))
        self._tilt = 20.0 * dx / dist if dist > 1 else 0.0
        self._on_arrived = on_arrived
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setStartValue(start)
        anim.setEndValue(target)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.InOutSine)
        anim.finished.connect(self._on_flight_finished)
        self._flight = anim
        anim.start()

    def cancel_flight(self) -> None:
        """Прерывает полёт без вызова on_arrived."""
        anim, self._flight, self._on_arrived = self._flight, None, None
        self._tilt = 0.0
        if anim is not None:
            anim.stop()
            anim.deleteLater()

    @property
    def is_dragging(self) -> bool:
        """Тащит ли пользователь пчелу прямо сейчас."""
        return self._dragging

    @property
    def is_flying(self) -> bool:
        """Летит ли пчела прямо сейчас."""
        return self._flight is not None

    def _on_flight_finished(self) -> None:
        callback, self._on_arrived = self._on_arrived, None
        anim, self._flight = self._flight, None
        self._tilt = 0.0
        if anim is not None:
            anim.deleteLater()
        self.update()
        if callback is not None:
            callback()
