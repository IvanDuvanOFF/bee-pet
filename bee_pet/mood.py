"""Настроение пчелы: чистая логика без Qt."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Mood(Enum):
    """Настроение пчелы."""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    LOVING = "loving"
    SAD = "sad"
    ANGRY = "angry"
    CONFUSED = "confused"


class Action(Enum):
    """Действие пользователя или тип реплики."""
    HI = "hi"
    FLOWER = "flower"
    LOVE = "love"
    PET = "pet"
    SPRAY = "spray"
    SWAT = "swat"
    SORRY = "sorry"
    GIBBERISH = "gibberish"
    OTHER = "other"


# Английские фразы, которые уходят в BeeLLM при командах меню.
ACTION_PHRASES: dict[Action, str] = {
    Action.HI: "hi bee!",
    Action.FLOWER: "*gives you a flower*",
    Action.LOVE: "i love you",
    Action.PET: "*pets you*",
    Action.SPRAY: "*sprays you with bug spray*",
    Action.SWAT: "*swats you*",
    Action.SORRY: "sorry",
}

# Длительности затухания, секунды.
ANGRY_HIGH_DECAY = 60.0
ANGRY_DECAY = 180.0
OTHER_MOOD_DECAY = 120.0

# Ключевые слова в порядке приоритета. Формат: (действие, основы-префиксы,
# целые слова, подстроки без границ слова).
_KEYWORDS: list[tuple[Action, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = [
    (Action.SORRY, ("sorry", "apolog", "прости", "извини"), ("sry",), ()),
    (Action.SWAT, ("swat", "шлёп", "шлеп", "ударить"), ("slap", "hit"), ()),
    (Action.SPRAY, ("spray", "дихлофос", "брызг"), (), ()),
    (Action.FLOWER, ("flower", "цвет"), (), ("🌸", "🌼")),
    (Action.LOVE, ("love", "люблю"), (), ("❤",)),
    (Action.PET, ("погладить", "глажу"), ("pet", "pat"), ()),
    (Action.HI, ("hello", "привет"), ("hi", "hey"), ()),
]


def _compile(prefixes: tuple[str, ...], words: tuple[str, ...],
             substrings: tuple[str, ...]) -> re.Pattern[str]:
    """Собирает регулярку: основы — по префиксу слова, короткие слова — целиком."""
    parts = [r"(?<!\w)" + re.escape(p) for p in prefixes]
    parts += [r"(?<!\w)" + re.escape(w) + r"s?(?!\w)" for w in words]
    parts += [re.escape(s) for s in substrings]
    return re.compile("|".join(parts))


_PATTERNS = [(a, _compile(p, w, s)) for a, p, w, s in _KEYWORDS]

_VOWELS = set("aeiouyаеёиоуыэюя")
# Ряды клавиатуры: фрагмент такого ряда считается клавиатурной кашей.
_KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm",
             "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")


def _is_gibberish(text: str) -> bool:
    """Похоже ли на кашу: один токен ≥5 букв с малой долей гласных или кусок ряда клавиатуры."""
    if len(text.split()) != 1 or len(text) < 5 or not text.isalpha():
        return False
    vowels = sum(ch in _VOWELS for ch in text)
    if vowels / len(text) < 0.25:
        return True
    return any(text in row for row in _KEY_ROWS)


def classify(text: str) -> Action:
    """Определяет действие по свободному тексту чата."""
    t = text.strip().lower()
    if not t:
        return Action.OTHER
    for action, phrase in ACTION_PHRASES.items():
        if t == phrase:
            return action
    for action, pattern in _PATTERNS:
        if pattern.search(t):
            return action
    if _is_gibberish(t):
        return Action.GIBBERISH
    return Action.OTHER


# Ведущий тег настроения в ответе BeeLLM: «[angry:high] ...», «[ happy ] ...».
_MOOD_TAG_RE = re.compile(r"^\s*\[\s*(\w+)\s*(?::\s*(\w+)\s*)?\]")


def parse_mood_tag(raw: str) -> tuple[Mood, bool] | None:
    """Разбирает ведущий тег «[mood:level]» ответа модели.

    Возвращает (настроение, angry_high), где angry_high — это angry с уровнем high;
    уровень остальных настроений не важен. Без тега или с незнакомым настроением — None.
    """
    m = _MOOD_TAG_RE.match(raw or "")
    if m is None:
        return None
    try:
        mood = Mood(m.group(1).lower())
    except ValueError:
        return None
    level = (m.group(2) or "").lower()
    return mood, mood is Mood.ANGRY and level == "high"


@dataclass(frozen=True)
class MoodChange:
    """Результат смены настроения."""
    old: Mood
    new: Mood
    angry_high: bool
    became_angry_high: bool


_CALM_TRANSITIONS: dict[Action, Mood] = {
    Action.HI: Mood.NEUTRAL,
    Action.FLOWER: Mood.HAPPY,
    Action.LOVE: Mood.LOVING,
    Action.PET: Mood.HAPPY,
    Action.SPRAY: Mood.SAD,
    Action.SWAT: Mood.ANGRY,
    Action.SORRY: Mood.NEUTRAL,
    Action.GIBBERISH: Mood.CONFUSED,
}

_ANGRY_ESCALATORS = {Action.FLOWER, Action.LOVE, Action.PET, Action.SWAT, Action.SPRAY}


class MoodEngine:
    """Автомат настроения: вердикт модели или локальные правила без неё."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.mood = Mood.NEUTRAL
        self.angry_high = False
        self.changed_at = clock()

    @property
    def is_angry_high(self) -> bool:
        """Сильно ли злится пчела."""
        return self.angry_high

    def _set(self, mood: Mood, angry_high: bool, refresh: bool = False) -> MoodChange:
        """Применяет состояние; время сбрасывается, если что-то изменилось или refresh=True."""
        old, was_high = self.mood, self.angry_high
        if refresh or (mood, angry_high) != (old, was_high):
            self.changed_at = self._clock()
        self.mood, self.angry_high = mood, angry_high
        return MoodChange(old, mood, angry_high, angry_high and not was_high)

    def apply(self, action: Action) -> MoodChange:
        """Применяет действие по таблице переходов (запасной путь, когда модели нет).

        Злая пчела успокаивается только от SORRY (-> NEUTRAL). Цветок, любовь,
        поглаживание, шлепок и дихлофос злят сильнее (angry high), а всё остальное
        оставляет злость как есть. Любое действие, после которого пчела всё ещё злится,
        перезапускает таймеры затухания злости: они отсчитываются от последней провокации.
        """
        if self.mood is Mood.ANGRY:
            if action is Action.SORRY:
                return self._set(Mood.NEUTRAL, False)
            if action in _ANGRY_ESCALATORS:
                return self._set(Mood.ANGRY, True, refresh=True)
            return self._set(Mood.ANGRY, self.angry_high, refresh=True)
        new = _CALM_TRANSITIONS.get(action)
        if new is None:
            return self._set(self.mood, self.angry_high)
        return self._set(new, False)

    def set_from_model(self, mood: Mood, angry_high: bool) -> MoodChange:
        """Ставит настроение, которое назвала модель в теге ответа.

        Таймеры затухания всегда отсчитываются заново, от последнего вердикта модели.
        """
        return self._set(mood, mood is Mood.ANGRY and angry_high, refresh=True)

    def tick(self) -> MoodChange | None:
        """Затухание настроения; вызывается GUI периодически.

        angry high -> angry через ANGRY_HIGH_DECAY, angry -> neutral через ANGRY_DECAY,
        прочие настроения -> neutral через OTHER_MOOD_DECAY (от последней смены или провокации).
        """
        if self.mood is Mood.NEUTRAL:
            return None
        elapsed = self._clock() - self.changed_at
        if self.angry_high:
            if elapsed >= ANGRY_HIGH_DECAY:
                return self._set(Mood.ANGRY, False)
        elif self.mood is Mood.ANGRY:
            if elapsed >= ANGRY_DECAY:
                return self._set(Mood.NEUTRAL, False)
        elif elapsed >= OTHER_MOOD_DECAY:
            return self._set(Mood.NEUTRAL, False)
        return None
