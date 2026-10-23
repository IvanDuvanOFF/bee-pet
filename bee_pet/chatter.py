"""Пчела жужжит сама по себе: время от времени пингует BeeLLM и показывает ответ в пузыре.

Пинг — нейтральная реплика PING_PROMPT. Модель получает историю диалога и отвечает
в тон настроению (спокойная — «zzz buzzz», злая — «BZZZZ!!!»), но сам обмен
в историю не попадает, а тег настроения из ответа игнорируется: настроение от
жужжания не меняется (теги у модели «гуляют», пинг мог бы разозлить или успокоить).

Чем злее пчела, тем чаще она жужжит: спокойная — раз в несколько минут, злая —
раз в несколько секунд, angry high — почти непрерывно.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from bee_pet.mood import Mood, MoodChange

log = logging.getLogger("bee_pet.chatter")

PING_PROMPT = "hmm"
CHATTER_TAG = "chatter"

# Категории настроения, от которых зависит частота жужжания.
CALM, ANGRY, FURIOUS = "calm", "angry", "furious"

# Интервалы между пингами, секунды (равномерно случайные в диапазоне).
CALM_INTERVAL_S = (90.0, 240.0)
ANGRY_INTERVAL_S = (6.0, 15.0)
FURIOUS_INTERVAL_S = (1.5, 4.0)

CHATTER_BUBBLE_MS = 2500
# У angry high пузырь висит дольше максимального интервала, чтобы ругань шла
# без пауз: следующий ответ просто заменяет текст в ещё видимом пузыре.
FURIOUS_BUBBLE_MS = 4500

# Если ответа на пинг нет так долго, считаем его потерянным и пингуем снова.
PENDING_TIMEOUT_S = 30.0


class ChatterController(QObject):
    """Планирует пинги модели и показывает ответы в пузыре, не трогая настроение.

    Пинг пропускается (и просто планируется следующий), если:
    - предыдущий пинг ещё ждёт ответа (одновременно только один);
    - идёт укус (BiteController не в IDLE): пузырь с обратным отсчётом не затираем;
    - в пузыре сейчас чужой текст (ответ меню, чата, удержания или жужжание в полёте).
    Те же проверки повторяются, когда ответ пришёл. Перенос мышкой не мешает.
    """

    def __init__(self, bee, client, bite_controller=None, *,
                 rng: random.Random | None = None,
                 calm_interval_s: tuple[float, float] = CALM_INTERVAL_S,
                 angry_interval_s: tuple[float, float] = ANGRY_INTERVAL_S,
                 furious_interval_s: tuple[float, float] = FURIOUS_INTERVAL_S,
                 bubble_ms: int = CHATTER_BUBBLE_MS,
                 furious_bubble_ms: int = FURIOUS_BUBBLE_MS,
                 pending_timeout_s: float = PENDING_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 autostart: bool = True) -> None:
        super().__init__(bee)
        self._bee, self._client, self._bite = bee, client, bite_controller
        self._rng = rng or random.Random()
        self._intervals = {CALM: calm_interval_s, ANGRY: angry_interval_s,
                           FURIOUS: furious_interval_s}
        self._bubble_ms = {CALM: bubble_ms, ANGRY: bubble_ms, FURIOUS: furious_bubble_ms}
        self._pending_timeout_s = pending_timeout_s
        self._clock = clock
        self._seq = 0
        self._pending_since: float | None = None  # время пинга, ждущего ответа
        self._last_text: str | None = None  # что мы последним показали в пузыре
        self._category = self._current_category()
        self.pings = 0  # сколько пингов отправлено (для логов и тестов)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._ping)

        bee.mood_changed.connect(self.on_mood_changed)
        client.reply_ready.connect(self.on_reply)
        if autostart:
            self.schedule()

    # --- состояние ---------------------------------------------------------

    def _current_category(self) -> str:
        engine = self._bee.engine
        if engine.mood is not Mood.ANGRY:
            return CALM
        return FURIOUS if engine.angry_high else ANGRY

    @property
    def category(self) -> str:
        """Текущая категория частоты: calm, angry или furious."""
        return self._category

    @property
    def pending(self) -> bool:
        """Ждёт ли пинг ответа."""
        return self._pending_since is not None

    def next_interval_s(self) -> float:
        """Случайный интервал до следующего пинга для текущей категории."""
        lo, hi = self._intervals[self._category]
        return self._rng.uniform(lo, hi)

    def _blocked(self) -> str | None:
        """Почему сейчас нельзя показывать жужжание; None, если можно."""
        if self._bite is not None and self._bite.state != self._bite.IDLE:
            return "идёт укус"
        bubble = self._bee.bubble
        if bubble.isVisible() and bubble.text != self._last_text:
            return "в пузыре чужой текст"
        return None

    # --- события -----------------------------------------------------------

    def schedule(self) -> None:
        """Планирует следующий пинг (перезапускает таймер)."""
        self._timer.start(int(self.next_interval_s() * 1000))

    def on_mood_changed(self, change: MoodChange) -> None:  # noqa: ARG002
        """Сменилась категория настроения — новый интервал: злость начинает жужжать скоро."""
        category = self._current_category()
        if category != self._category:
            log.debug("категория настроения %s -> %s", self._category, category)
            self._category = category
            self.schedule()

    def _ping(self) -> None:
        self.schedule()
        if self._pending_since is not None:
            if self._clock() - self._pending_since < self._pending_timeout_s:
                log.debug("не жужжу: прошлый пинг ещё ждёт ответа")
                return
            log.info("ответ на пинг потерялся, пингую заново")
        reason = self._blocked()
        if reason:
            log.debug("не жужжу: %s", reason)
            return
        self._seq += 1
        self._pending_since = self._clock()
        self.pings += 1
        self._client.request(PING_PROMPT, tag=(CHATTER_TAG, self._seq),
                             remember=False, use_history=True)

    def on_reply(self, tag: object, text: str, from_model: bool) -> None:  # noqa: ARG002
        """Ответ на пинг (или заготовка без сервера) — в пузырь; настроение не трогаем."""
        if tag != (CHATTER_TAG, self._seq):
            return
        self._pending_since = None
        reason = self._blocked()
        if reason:
            log.debug("ответ на пинг не показываю: %s", reason)
            return
        shown = f"🐝 {text}"
        self._last_text = shown
        self._bee.say(shown, self._bubble_ms[self._category])
