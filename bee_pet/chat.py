"""Мини-чат с пчелой."""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QPushButton, QTextBrowser,
                               QVBoxLayout, QWidget)

from bee_pet.bee_window import BeeWindow
from bee_pet.llm import BeeClient
from bee_pet.mood import classify

CHAT_SIZE = (300, 360)
OFFLINE_NOTE = "(пчела далеко, отвечает по памяти)"
TYPING_TEXT = "бзз…"


class ChatWindow(QWidget):
    """Небольшое окно чата: история, поле ввода и кнопка отправки."""

    def __init__(self, bee: BeeWindow, client: BeeClient) -> None:
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Чат с пчелой 🐝")
        self.resize(*CHAT_SIZE)
        self._bee, self._client = bee, client
        self._tag = object()  # метка наших запросов среди ответов клиента
        self._entries: list[tuple[str, str]] = []  # (вид, текст)
        self._waiting = False
        self._note_shown = False

        self.history = QTextBrowser()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Скажи что-нибудь пчеле…")
        self.send_button = QPushButton("Отправить")
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.history, 1)
        layout.addLayout(row)

        self.input.returnPressed.connect(self.send)
        self.send_button.clicked.connect(self.send)
        client.reply_ready.connect(self._on_reply)

    # --- показ -------------------------------------------------------------

    def open_chat(self) -> None:
        """Показывает чат рядом с пчелой; если уже открыт, просто поднимает."""
        if not self.isVisible():
            self._place_near_bee()
            self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def _place_near_bee(self) -> None:
        """Слева от пчелы, если помещается, иначе справа; в пределах экрана."""
        geo = self._bee.geometry()
        screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = geo.left() - w - 8
        if x < area.left():
            x = geo.right() + 8
        x = max(area.left(), min(x, area.right() - w + 1))
        y = max(area.top(), min(geo.center().y() - h // 2, area.bottom() - h + 1))
        self.move(QPoint(x, y))

    def closeEvent(self, event) -> None:  # noqa: N802
        """Закрытие только прячет окно, история остаётся."""
        event.ignore()
        self.hide()

    # --- отправка и ответ --------------------------------------------------

    def send(self) -> None:
        """Отправляет введённый текст: меняет настроение и спрашивает модель."""
        text = self.input.text().strip()
        if not text or self._waiting:
            return
        self.input.clear()
        self._entries.append(("user", text))
        self._bee.apply_action(classify(text))  # без запроса к модели по меню-пути
        self._set_waiting(True)
        self._render()
        self._client.request(text, tag=self._tag)

    def _on_reply(self, tag: object, text: str, from_model: bool) -> None:
        if tag is not self._tag:
            return
        self._set_waiting(False)
        self._entries.append(("bee", text))
        if from_model:
            self._note_shown = False
        elif not self._note_shown:
            self._note_shown = True
            self._entries.append(("note", OFFLINE_NOTE))
        self._render()
        self._bee.say(f"🐝 {text}")
        self.input.setFocus()

    def _set_waiting(self, waiting: bool) -> None:
        self._waiting = waiting
        self.input.setEnabled(not waiting)
        self.send_button.setEnabled(not waiting)

    def _render(self) -> None:
        """Перерисовывает историю; в конце — индикатор набора, пока ждём ответ."""
        parts = []
        for kind, text in self._entries:
            t = escape(text)
            if kind == "user":
                parts.append(f"<p align='right'>Ты: {t}</p>")
            elif kind == "bee":
                parts.append(f"<p>🐝 {t}</p>")
            else:
                parts.append(f"<p><small><font color='#888888'>{t}</font></small></p>")
        if self._waiting:
            parts.append(f"<p><i>🐝 {TYPING_TEXT}</i></p>")
        self.history.setHtml("".join(parts))
        bar = self.history.verticalScrollBar()
        bar.setValue(bar.maximum())
