"""Речевой пузырь: отдельное безрамочное окно, не забирающее фокус."""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

MAX_TEXT_WIDTH = 240
PAD = 10
TAIL_H = 10
TAIL_W = 14


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
        fm = QFontMetrics(self._font)
        text_rect = fm.boundingRect(QRect(0, 0, MAX_TEXT_WIDTH, 10000),
                                    int(Qt.TextWordWrap), self._text)
        w = text_rect.width() + 2 * PAD
        h = text_rect.height() + 2 * PAD + TAIL_H
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

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        top = TAIL_H if self._below else 0
        body = QRectF(1, top + 1, w - 2, h - TAIL_H - 2)
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
        p.drawText(body.adjusted(PAD, PAD - 1, -PAD, -PAD + 1),
                   int(Qt.TextWordWrap | Qt.AlignCenter), self._text)
        p.end()
