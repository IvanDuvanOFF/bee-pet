"""Речевой пузырь: отдельное безрамочное окно, не забирающее фокус."""
from __future__ import annotations

import math

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import QWidget

MAX_TEXT_WIDTH = 240
PAD = 10
TAIL_H = 10
TAIL_W = 14
BORDER = 1  # отступ контура от края окна, чтобы обводку не срезало
TEXT_FLAGS = int(Qt.TextWordWrap | Qt.AlignCenter)


class SpeechBubble(QWidget):
    """Скруглённый пузырь с хвостиком, указывающим на пчелу."""

    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self._text = ""
        self._anchor = QRect()
        self._below = False
        self._tail_x = 0.0
        self._font = QFont()
        self._font.setPixelSize(14)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    @property
    def text(self) -> str:
        """Текущий текст пузыря."""
        return self._text

    def show_text(self, text: str, anchor: QRect, duration_ms: int = 4000) -> None:
        """Показывает текст рядом с anchor; повторный вызов заменяет текст и сбрасывает таймер."""
        self._text = text
        self._anchor = QRect(anchor)
        self._layout()
        self.show()
        self.raise_()
        self.update()
        self._timer.start(duration_ms)

    def reposition(self, anchor: QRect) -> None:
        """Переставляет видимый пузырь вслед за пчелой."""
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._layout()

    def _layout(self) -> None:
        """Считает размер по тексту и ставит окно над (или под) якорем в пределах экрана."""
        text_w, text_h = self._measure()
        w = text_w + 2 * (PAD + BORDER)
        h = text_h + 2 * (PAD + BORDER) + TAIL_H
        screen = QGuiApplication.screenAt(self._anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = self._anchor.center().x() - w // 2
        x = max(area.left(), min(x, area.right() - w + 1))
        y = self._anchor.top() - h
        self._below = y < area.top()
        if self._below:
            y = self._anchor.bottom() + 1
        y = max(area.top(), min(y, area.bottom() - h + 1))
        self._tail_x = max(PAD + TAIL_W, min(self._anchor.center().x() - x, w - PAD - TAIL_W))
        self.setGeometry(x, y, w, h)

    def _measure(self) -> tuple[int, int]:
        """Размер текста так, как его нарисует QPainter.

        QFontMetrics не учитывает подмену шрифта для эмодзи (🐝 рисуется шрифтом
        Segoe UI Emoji и шире), и короткая строка при рисовании переносилась и обрезалась.
        """
        img = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        p.setFont(self._font)
        rect = p.boundingRect(QRectF(0, 0, MAX_TEXT_WIDTH, 10000), TEXT_FLAGS, self._text)
        p.end()
        return math.ceil(rect.width()), math.ceil(rect.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        top = TAIL_H if self._below else 0
        body = QRectF(BORDER, top + BORDER, w - 2 * BORDER, h - TAIL_H - 2 * BORDER)
        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        tail = QPainterPath()
        tx = self._tail_x
        if self._below:
            tail.moveTo(tx - TAIL_W / 2, body.top())
            tail.lineTo(tx, 1)
            tail.lineTo(tx + TAIL_W / 2, body.top())
        else:
            tail.moveTo(tx - TAIL_W / 2, body.bottom())
            tail.lineTo(tx, h - 1)
            tail.lineTo(tx + TAIL_W / 2, body.bottom())
        path = path.united(tail)
        p.setPen(QPen(QColor("#3B2A1A"), 1.5))
        p.setBrush(QColor("#FFFBEA"))
        p.drawPath(path)
        p.setPen(QColor("#3B2A1A"))
        p.setFont(self._font)
        # Область текста ровно того размера, что посчитал _measure().
        p.drawText(body.adjusted(PAD, PAD, -PAD, -PAD), TEXT_FLAGS, self._text)
        p.end()
