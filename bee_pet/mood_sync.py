"""Настроение по ответу модели: запросы меню и чата и запасные локальные правила."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject

from bee_pet.mood import Action, Mood


@dataclass(frozen=True, eq=False)
class MoodRequest:
    """Метка запроса, от ответа на который зависит настроение (сравнивается по identity).

    source — кто спросил (метка меню или чата), action — действие для локального правила.
    """
    source: object
    action: Action


class MoodSync(QObject):
    """Выставляет настроение пчелы по тегу «[mood:level]» в ответе модели.

    Если клиент не считается офлайн, настроение до ответа не меняется: пришёл тег —
    берём его, пришла заготовка или тег не разобрался — локальное правило действия.
    Если клиент заведомо офлайн, локальное правило применяется сразу и один раз.
    Чужие запросы (например, жужжание в полёте) настроение не трогают.
    """

    def __init__(self, bee, client) -> None:
        super().__init__(bee)
        self._bee, self._client = bee, client
        # Ждущие ответа запросы: метка -> применено ли уже локальное правило.
        self._pending: dict[MoodRequest, bool] = {}
        self._settled: set[MoodRequest] = set()  # настроение уже взято из тега
        bee.mood_deferred = True
        client.mood_ready.connect(self._on_mood_ready)
        client.reply_ready.connect(self._on_reply)

    def request(self, prompt: str, action: Action, source: object) -> MoodRequest:
        """Отправляет реплику модели; настроение выставится по ответу (или сразу без сети)."""
        tag = MoodRequest(source, action)
        applied = not self._client.likely_online()
        if applied:
            self._bee.apply_action(action)
        self._pending[tag] = applied
        self._client.request(prompt, tag=tag)
        return tag

    def _on_mood_ready(self, tag: object, mood: Mood, angry_high: bool) -> None:
        if isinstance(tag, MoodRequest) and tag in self._pending:
            self._settled.add(tag)
            self._bee.apply_model_mood(mood, angry_high)

    def _on_reply(self, tag: object, text: str, from_model: bool) -> None:  # noqa: ARG002
        if not (isinstance(tag, MoodRequest) and tag in self._pending):
            return
        applied = self._pending.pop(tag)
        if tag in self._settled:
            self._settled.discard(tag)
        elif not applied:
            self._bee.apply_action(tag.action)
