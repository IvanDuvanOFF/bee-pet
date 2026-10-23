"""Пчелу долго держат мышкой — она злится (через модель, как команды меню).

Сами реплики и настроение идут через MoodSync: модель отвечает тегом «[mood:level]»,
а без сервера работают локальные правила для Action.GRAB и Action.RELEASE.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

from bee_pet.mood import ACTION_PHRASES, Action

log = logging.getLogger("bee_pet.hold")

# Тайминги по умолчанию (в тестах подменяются через конструктор), с начала перетаскивания.
HOLD_ANGRY_S = 3.0    # держат столько — пчела злится (angry low)
HOLD_FURIOUS_S = 8.0  # всё ещё держат — злится сильнее (angry high)

GRAB_PROMPT = ACTION_PHRASES[Action.GRAB]        # «*grabs your wings*»
RELEASE_PROMPT = ACTION_PHRASES[Action.RELEASE]  # «*lets you go* sorry»
HOLD_TAG = "hold"


class HoldController(QObject):
    """Следит за перетаскиванием: долго держат — злится, быстро отпустили — успокаивается.

    Стадии считаются для каждого перетаскивания заново:
    0 — держат меньше HOLD_ANGRY_S, ничего не происходит (обычный перенос);
    1 — держат HOLD_ANGRY_S, модели уходит GRAB_PROMPT;
    2 — держат HOLD_FURIOUS_S, GRAB_PROMPT уходит ещё раз (модель злится сильнее).
    Отпустили на стадии 1 — уходит RELEASE_PROMPT, на стадии 2 — ничего (пчела в ярости).
    """

    def __init__(self, bee, sync, *, angry_s: float = HOLD_ANGRY_S,
                 furious_s: float = HOLD_FURIOUS_S) -> None:
        super().__init__(bee)
        self._bee, self._sync = bee, sync
        self.stage = 0

        self._angry_timer = self._make_timer(angry_s, self._on_angry)
        self._furious_timer = self._make_timer(furious_s, self._on_furious)

        bee.drag_started.connect(self.on_drag_started)
        bee.drag_finished.connect(self.on_drag_finished)

    def _make_timer(self, seconds: float, slot) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(1, int(seconds * 1000)))
        timer.timeout.connect(slot)
        return timer

    # --- события -----------------------------------------------------------

    def on_drag_started(self) -> None:
        """Новое перетаскивание: стадии с нуля."""
        self.stage = 0
        self._angry_timer.start()
        self._furious_timer.start()

    def on_drag_finished(self) -> None:
        self._angry_timer.stop()
        self._furious_timer.stop()
        if self.stage == 1:
            log.info("отпустили вскоре после злости: извиняемся")
            self._sync.request(RELEASE_PROMPT, Action.RELEASE, HOLD_TAG)
        self.stage = 0

    def _on_angry(self) -> None:
        if not self._bee.is_dragging or self.stage != 0:
            return
        self.stage = 1
        log.info("пчелу держат уже долго: злится")
        self._sync.request(GRAB_PROMPT, Action.GRAB, HOLD_TAG)

    def _on_furious(self) -> None:
        if not self._bee.is_dragging or self.stage != 1:
            return
        self.stage = 2
        log.info("пчелу всё ещё держат: злится сильнее")
        self._sync.request(GRAB_PROMPT, Action.GRAB, HOLD_TAG)
