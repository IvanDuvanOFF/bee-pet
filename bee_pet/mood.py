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
    """Детерминированный автомат настроения."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.mood = Mood.NEUTRAL
        self.angry_high = False
        self.changed_at = clock()

    @property
    def is_angry_high(self) -> bool:
        """Сильно ли злится пчела."""
        return self.angry_high

    def _set(self, mood: Mood, angry_high: bool) -> MoodChange:
        """Применяет состояние; время сбрасывается, только если что-то изменилось."""
        old, was_high = self.mood, self.angry_high
        if (mood, angry_high) != (old, was_high):
            self.changed_at = self._clock()
        self.mood, self.angry_high = mood, angry_high
        return MoodChange(old, mood, angry_high, angry_high and not was_high)

    def apply(self, action: Action) -> MoodChange:
        """Применяет действие по таблице переходов."""
        if self.mood is Mood.ANGRY:
            if action is Action.SORRY:
                return self._set(Mood.NEUTRAL, False)
            if action in _ANGRY_ESCALATORS:
                return self._set(Mood.ANGRY, True)
            return self._set(self.mood, self.angry_high)
        new = _CALM_TRANSITIONS.get(action)
        if new is None:
            return self._set(self.mood, self.angry_high)
        return self._set(new, False)

    def tick(self) -> MoodChange | None:
        """Затухание настроения; вызывается GUI периодически."""
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
