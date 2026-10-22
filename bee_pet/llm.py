"""Клиент BeeLLM: запрос к llama-server, заготовки «бзз» и асинхронный воркер."""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.request
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

BEE_URL = os.environ.get("BEE_URL", "http://127.0.0.1:8081")

MAX_REPLY_CHARS = 200
OFFLINE_BACKOFF = 30.0  # секунд без сетевых попыток после сбоя

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


def ask_bee(prompt: str, max_tokens: int = 64, timeout: float = 10.0) -> str:
    """Шлёт реплику в llama-server (/v1/completions); при сбоях бросает исключение."""
    body = json.dumps({
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 1.0,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BEE_URL}/v1/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["text"].strip()


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
    done = Signal(object, str, bool)  # tag, text, from_model


class _AskRunnable(QRunnable):
    """Выполняет запрос к модели в пуле потоков."""

    def __init__(self, ask: Callable[[str], str], prompt: str, tag: object,
                 signals: _WorkerSignals) -> None:
        super().__init__()
        self._ask, self._prompt, self._tag, self._signals = ask, prompt, tag, signals

    def run(self) -> None:
        try:
            text = self._ask(self._prompt)
            if not text.strip():
                raise ValueError("пустой ответ")
            result = (clean_reply(text), True)
        except Exception:  # любой сбой сети или формата -> заготовка
            result = (random_fallback(), False)
        try:
            self._signals.done.emit(self._tag, *result)
        except RuntimeError:
            pass  # объект сигналов уже удалён при выходе из приложения


class BeeClient(QObject):
    """Асинхронный клиент: запрос в воркере, ответ сигналом в GUI-потоке."""

    reply_ready = Signal(object, str, bool)  # tag, text, from_model

    def __init__(self, ask: Callable[[str], str] = ask_bee,
                 clock: Callable[[], float] = time.monotonic,
                 backoff: float = OFFLINE_BACKOFF, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ask = ask
        self._clock = clock
        self._backoff = backoff
        self._offline_until = 0.0
        self.online: bool | None = None
        self._pool = QThreadPool(self)
        self._signals = _WorkerSignals(self)
        self._signals.done.connect(self._on_done)

    def request(self, prompt: str, tag: object = None) -> None:
        """Запрашивает ответ; при недавнем сбое сразу отвечает заготовкой."""
        if self.online is False and self._clock() < self._offline_until:
            text = random_fallback()
            QTimer.singleShot(0, lambda: self.reply_ready.emit(tag, text, False))
            return
        self._pool.start(_AskRunnable(self._ask, prompt, tag, self._signals))

    @Slot(object, str, bool)
    def _on_done(self, tag: object, text: str, from_model: bool) -> None:
        self.online = from_model
        if not from_model:
            self._offline_until = self._clock() + self._backoff
        self.reply_ready.emit(tag, text, from_model)

    def shutdown(self) -> None:
        """Дожидается воркеров (для выхода из приложения и тестов)."""
        self._pool.waitForDone(2000)
