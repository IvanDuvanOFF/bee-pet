"""Клиент BeeLLM: запрос к llama-server, заготовки «бзз» и асинхронный воркер."""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.request
from collections import deque
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from bee_pet.mood import parse_mood_tag

# Адрес llama-server; ask_bee читает его при каждом запросе, так что при запуске
# его можно поменять (server.py направляет пчелу на встроенный сервер).
BEE_URL = os.environ.get("BEE_URL", "http://127.0.0.1:8081")

MAX_REPLY_CHARS = 200
OFFLINE_BACKOFF = 30.0  # секунд без сетевых попыток после сбоя
HISTORY_TURNS = 4  # сколько последних пар «реплика — ответ» помнит пчела

# Сообщение для chat-эндпоинта: {"role": "user" | "assistant", "content": str}.
Messages = list[dict[str, str]]

# Ведущий тег настроения в ответе модели: «[happy:low] ...» или «[angry] ...».
_MOOD_TAG_RE = re.compile(r"^\s*\[\s*\w+(?:\s*:\s*\w+)?\s*\]\s*")

# Заготовки на случай, если пчела улетела (сервер не запущен).
FALLBACK_BUZZ = [
    "бззз... 🐝",
    "бзз-бзз! *садится на цветок*",
    "ббббззззз 🌼",
    "*пчела молча опыляет ромашку*",
    "бз. бзззз? бзз! 🍯",
    "бззз? *задумчиво кружит*",
    "*пчела жужжит что-то своё* 🐝",
    "бзз-бзз-бзз 🌸",
]


def set_base_url(url: str) -> None:
    """Меняет адрес llama-server для следующих запросов."""
    global BEE_URL
    BEE_URL = url.rstrip("/")


def ask_bee(messages: Messages, max_tokens: int = 64, timeout: float = 10.0) -> str:
    """Шлёт диалог в llama-server (/v1/chat/completions); при сбоях бросает исключение.

    Chat-эндпоинт сам накладывает шаблон BeeLLM (<|user|>…<|bee|>…</s>): на сыром
    /v1/completions модель без шаблона отвечает мусором или пустой строкой.
    """
    body = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 1.0,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BEE_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def strip_mood_tag(text: str) -> str:
    """Убирает ведущий тег настроения вида «[happy:low]» или «[angry]» и пробелы вокруг."""
    return _MOOD_TAG_RE.sub("", text or "", count=1).strip()


def random_fallback() -> str:
    """Случайная заготовка «бзз»."""
    return random.choice(FALLBACK_BUZZ)


def clean_reply(text: str) -> str:
    """Схлопывает пробелы, режет до 200 символов по границе слова; пустое -> заготовка."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return random_fallback()
    if len(text) <= MAX_REPLY_CHARS:
        return text
    cut = text[:MAX_REPLY_CHARS - 1]
    # Если обрезали посреди слова, откатываемся к последнему пробелу.
    if text[MAX_REPLY_CHARS - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


class _WorkerSignals(QObject):
    """Сигналы воркера (QRunnable сам не QObject)."""
    # tag, text, from_model, prompt, raw (сырой ответ модели; пустой при сбое), remember,
    # reachable (сервер ответил, пусть даже пустой строкой)
    done = Signal(object, str, bool, str, str, bool, bool)


class _AskRunnable(QRunnable):
    """Выполняет запрос к модели в пуле потоков."""

    def __init__(self, ask: Callable[[Messages], str], messages: Messages, prompt: str,
                 tag: object, signals: _WorkerSignals, remember: bool = True) -> None:
        super().__init__()
        self._ask, self._messages, self._prompt = ask, messages, prompt
        self._tag, self._signals, self._remember = tag, signals, remember

    def run(self) -> None:
        try:
            raw = self._ask(self._messages)
        except Exception:  # любой сбой сети или формата -> заготовка
            result = (random_fallback(), False, self._prompt, "", False)
        else:
            text = strip_mood_tag(raw)
            if text:
                result = (clean_reply(text), True, self._prompt, raw.strip(), True)
            else:
                # Модель иногда молчит (например, после «*stings you and then dies*»):
                # сервер жив, поэтому заготовка — но без офлайн-паузы.
                result = (random_fallback(), False, self._prompt, "", True)
        try:
            self._signals.done.emit(self._tag, *result[:4], self._remember, result[4])
        except RuntimeError:
            pass  # объект сигналов уже удалён при выходе из приложения


class BeeClient(QObject):
    """Асинхронный клиент: запрос в воркере, ответ сигналом в GUI-потоке.

    Хранит короткую общую историю диалога (меню и чат вместе), чтобы пчела помнила,
    что её только что шлёпнули, и «sorry» имело смысл. История трогается только
    в GUI-потоке: список сообщений собирается в request(), пополняется в _on_done().
    """

    reply_ready = Signal(object, str, bool)  # tag, text, from_model
    # tag, Mood, angry_high: настроение из тега ответа модели, приходит перед reply_ready
    mood_ready = Signal(object, object, bool)

    def __init__(self, ask: Callable[[Messages], str] = ask_bee,
                 clock: Callable[[], float] = time.monotonic,
                 backoff: float = OFFLINE_BACKOFF, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ask = ask
        self._clock = clock
        self._backoff = backoff
        self._offline_until = 0.0
        self.online: bool | None = None
        # Пары (реплика пользователя, ответ модели). Ответ храним сырым, с тегом
        # настроения: так история ближе к формату, на котором обучалась BeeLLM.
        self._history: deque[tuple[str, str]] = deque(maxlen=HISTORY_TURNS)
        self._pool = QThreadPool(self)
        self._signals = _WorkerSignals(self)
        self._signals.done.connect(self._on_done)

    def _build_messages(self, prompt: str) -> Messages:
        """Свежий список сообщений: история + новая реплика (воркер получает копию)."""
        messages: Messages = []
        for user, bee in self._history:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": bee})
        messages.append({"role": "user", "content": prompt})
        return messages

    def likely_online(self) -> bool:
        """False, только если недавний сбой и пауза перед новой попыткой ещё идёт."""
        return not (self.online is False and self._clock() < self._offline_until)

    def request(self, prompt: str, tag: object = None, remember: bool = True,
                use_history: bool | None = None) -> None:
        """Запрашивает ответ; при недавнем сбое сразу отвечает заготовкой.

        remember=False — ответ в историю не попадает. use_history — отправлять ли
        модели историю диалога (по умолчанию так же, как remember):
        - remember=False — разовая реплика без истории (жужжание в полёте);
        - remember=False, use_history=True — модель видит диалог и отвечает в тон,
          но сам обмен не запоминается (жужжание само по себе, chatter.py).
        """
        if use_history is None:
            use_history = remember
        if not self.likely_online():
            text = random_fallback()
            QTimer.singleShot(0, lambda: self.reply_ready.emit(tag, text, False))
            return
        if use_history:
            messages = self._build_messages(prompt)
        else:
            messages = [{"role": "user", "content": prompt}]
        self._pool.start(_AskRunnable(self._ask, messages, prompt, tag, self._signals, remember))

    @Slot(object, str, bool, str, str, bool, bool)
    def _on_done(self, tag: object, text: str, from_model: bool,
                 prompt: str, raw: str, remember: bool = True,
                 reachable: bool | None = None) -> None:
        if reachable is None:
            reachable = from_model
        self.online = reachable
        if from_model and remember:
            self._history.append((prompt, raw))
        if not reachable:
            # Офлайн-пауза только при сетевом сбое; заготовки в историю не попадают.
            self._offline_until = self._clock() + self._backoff
        parsed = parse_mood_tag(raw) if from_model else None
        if parsed is not None:
            self.mood_ready.emit(tag, *parsed)
        self.reply_ready.emit(tag, text, from_model)

    @Slot()
    def mark_online(self) -> None:
        """Сервер точно ожил (ответил /health): отменяет офлайн-паузу после сбоев."""
        self.online = True
        self._offline_until = 0.0

    def clear_history(self) -> None:
        """Забывает диалог: пчела успокоилась и не помнит прошлых обид."""
        self._history.clear()

    def shutdown(self) -> None:
        """Дожидается воркеров (для выхода из приложения и тестов)."""
        self._pool.waitForDone(2000)
