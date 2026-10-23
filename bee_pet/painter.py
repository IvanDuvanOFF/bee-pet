"""Векторная отрисовка пчелы и значков настроения (чистый QPainter, без картинок)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF

from bee_pet.mood import Mood

# Логический размер, под который нарисована пчела (всё масштабируется под rect).
BASE = 120.0

YELLOW = QColor("#FFC82E")
DARK = QColor("#3B2A1A")
WING = QColor(255, 255, 255, 150)
WING_EDGE = QColor(140, 170, 200, 200)

# Геометрия рисунка (в единицах BASE): от неё зависит, где у пчелы лапки.
BODY_DROP = 8.0    # рисунок опущен на столько ниже центра окна
LEG_TOP = 24.0     # откуда растут лапки (от центра тела)
LEG_LEN = 9.0      # длина лапки
LEG_PEN = 2.4      # толщина лапки (скруглённый конец добавляет половину)
# Нижний край лапок в позе «сидит/идёт» (y от верха окна размером BASE).
FEET_Y = BASE / 2 + BODY_DROP + LEG_TOP + LEG_LEN + LEG_PEN / 2

STEP_HZ = 2.5          # шагов в секунду каждой парой лапок при ходьбе
STEP_SWING = 24.0      # размах шага, градусы
STEP_BOB = 1.5         # подпрыгивание тела на каждом шаге, px
TWITCH_PERIOD_S = 4.0  # сидящая пчела изредка вздрагивает крыльями
TWITCH_S = 0.3


@dataclass
class PaintState:
    """Всё, что нужно для кадра."""
    phase: float = 0.0          # время анимации, секунды
    mood: Mood = Mood.NEUTRAL
    angry_high: bool = False
    dragging: bool = False
    tilt: float = 0.0           # наклон, градусы (по часовой)
    wiggle: tuple[float, float] = (0.0, 0.0)  # смещение рисунка внутри окна, px
    stinging: bool = False      # выпад жалом (кусает окно)
    perched: bool = False       # сидит на кромке: крылья сложены, лапки стоят, без покачивания
    walking: bool = False       # идёт по кромке: лапки шагают, крылья сложены
    facing: int = -1            # куда смотрит голова: -1 влево (как нарисовано), +1 вправо


def feet_y(height: float) -> int:
    """y нижнего края лапок в окне высотой height (поза «сидит»): им пчела стоит на кромке."""
    return round(FEET_Y * height / BASE)


def _standing(st: PaintState) -> bool:
    """Стоит ли пчела на лапках (сидит или идёт), а не летает."""
    return (st.perched or st.walking) and not st.dragging


def _star(cx: float, cy: float, r: float) -> QPolygonF:
    """Четырёхконечная искра."""
    k = r * 0.28
    return QPolygonF([QPointF(cx, cy - r), QPointF(cx + k, cy - k), QPointF(cx + r, cy),
                      QPointF(cx + k, cy + k), QPointF(cx, cy + r), QPointF(cx - k, cy + k),
                      QPointF(cx - r, cy), QPointF(cx - k, cy - k)])


def _heart(cx: float, cy: float, s: float) -> QPainterPath:
    """Сердечко размером около 2*s."""
    path = QPainterPath()
    path.moveTo(cx, cy + s)
    path.cubicTo(cx - 2 * s, cy - 0.2 * s, cx - 0.9 * s, cy - 1.4 * s, cx, cy - 0.45 * s)
    path.cubicTo(cx + 0.9 * s, cy - 1.4 * s, cx + 2 * s, cy - 0.2 * s, cx, cy + s)
    return path


def _draw_wings(p: QPainter, st: PaintState) -> None:
    if _standing(st):
        _draw_folded_wings(p, st)
        return
    fast = st.dragging or st.angry_high
    speed = 2 * math.pi * (13.0 if fast else 7.0)
    amp = 38.0 if fast else 24.0
    flap = math.sin(st.phase * speed)
    p.setPen(QPen(WING_EDGE, 1.2))
    p.setBrush(WING)
    for side, base_angle in ((0, -35.0), (1, -12.0)):
        p.save()
        p.translate(8 + side * 8, -10)
        p.rotate(base_angle + flap * amp * (1 if side == 0 else 0.8))
        p.drawEllipse(QRectF(-6, -30, 16, 32))
        p.restore()


def _draw_folded_wings(p: QPainter, st: PaintState) -> None:
    """Крылья сложены вдоль спины к жалу; сидящая пчела изредка ими вздрагивает."""
    twitch = 0.0
    if st.perched and not st.walking:
        t = st.phase % TWITCH_PERIOD_S
        if t < TWITCH_S:
            twitch = math.sin(t / TWITCH_S * 2 * math.pi * 2) * 10
    p.setPen(QPen(WING_EDGE, 1.2))
    p.setBrush(WING)
    for side, base_angle in ((0, 62.0), (1, 76.0)):
        p.save()
        p.translate(8 + side * 8, -10)
        p.rotate(base_angle - twitch * (1 if side == 0 else 0.6))
        p.drawEllipse(QRectF(-6, -30, 16, 32))
        p.restore()


def _draw_legs(p: QPainter, st: PaintState) -> None:
    p.setPen(QPen(DARK, LEG_PEN, Qt.SolidLine, Qt.RoundCap))
    walking = st.walking and not st.dragging
    for i, x in enumerate((-2, 10, 22)):
        if st.dragging:
            kick = math.sin(st.phase * 2 * math.pi * 6 + i * 2.1) * 25
        elif walking:
            # соседние лапки шагают в противофазе
            kick = math.sin(st.phase * 2 * math.pi * STEP_HZ + (i % 2) * math.pi) * STEP_SWING
        else:
            kick = 0.0
        p.save()
        p.translate(x, LEG_TOP)
        p.rotate(kick)
        p.drawLine(QPointF(0, 0), QPointF(0, LEG_LEN))
        p.restore()


def _draw_body(p: QPainter, st: PaintState) -> None:
    body = QPainterPath()
    body.addEllipse(QRectF(-20, -12, 56, 40))
    p.setPen(Qt.NoPen)
    p.setBrush(YELLOW)
    p.drawPath(body)
    p.save()
    p.setClipPath(body)
    p.setBrush(DARK)
    for x in (2, 13, 24):
        p.drawRect(QRectF(x, -14, 5.5, 44))
    p.restore()
    p.setPen(QPen(DARK, 1.6))
    p.setBrush(Qt.NoBrush)
    p.drawPath(body)
    # жало
    p.setPen(Qt.NoPen)
    p.setBrush(DARK)
    p.drawPolygon(QPolygonF([QPointF(35, 11), QPointF(46, 16), QPointF(35, 21)]))


def _draw_head(p: QPainter, st: PaintState) -> None:
    mood = st.mood
    cx, cy = -26.0, 8.0
    # усики
    droop = mood is Mood.SAD
    p.setPen(QPen(DARK, 2.2, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    for dx, tx in ((-5, -14), (4, 2)):
        path = QPainterPath(QPointF(cx + dx, cy - 14))
        if droop:
            path.cubicTo(cx + dx + tx * 0.4, cy - 30, cx + dx + tx * 1.2, cy - 28, cx + dx + tx * 1.4, cy - 16)
        else:
            path.cubicTo(cx + dx + tx * 0.2, cy - 24, cx + dx + tx * 0.6, cy - 30, cx + dx + tx, cy - 32)
        p.drawPath(path)
        end = path.currentPosition()
        p.setBrush(DARK)
        p.drawEllipse(end, 2.4, 2.4)
        p.setBrush(Qt.NoBrush)
    # голова
    p.setPen(QPen(DARK, 1.6))
    p.setBrush(YELLOW)
    p.drawEllipse(QPointF(cx, cy), 17, 16)
    if mood is Mood.ANGRY:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(235, 40, 30, 150 if st.angry_high else 110))
        p.drawEllipse(QPointF(cx, cy), 17, 16)
    # щёчки
    if mood in (Mood.HAPPY, Mood.LOVING):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 120, 120, 150))
        for ex in (-11, 11):
            p.drawEllipse(QPointF(cx + ex, cy + 6), 3.6, 2.4)
    # глаза
    p.setPen(Qt.NoPen)
    for ex in (-7, 7):
        if mood is Mood.LOVING:
            p.setBrush(QColor("#E0245E"))
            p.drawPath(_heart(cx + ex, cy - 2, 3.2))
            continue
        p.setBrush(DARK)
        p.drawEllipse(QPointF(cx + ex, cy - 2), 3.4, 4.2)
        p.setBrush(Qt.white)
        p.drawEllipse(QPointF(cx + ex + 1, cy - 3.4), 1.2, 1.2)
    # брови
    p.setPen(QPen(DARK, 2.2, Qt.SolidLine, Qt.RoundCap))
    if mood is Mood.ANGRY:
        p.drawLine(QPointF(cx - 12, cy - 12), QPointF(cx - 3, cy - 7))
        p.drawLine(QPointF(cx + 12, cy - 12), QPointF(cx + 3, cy - 7))
    elif mood is Mood.SAD:
        p.drawLine(QPointF(cx - 12, cy - 7), QPointF(cx - 3, cy - 11))
        p.drawLine(QPointF(cx + 12, cy - 7), QPointF(cx + 3, cy - 11))
    # рот
    p.setBrush(Qt.NoBrush)
    mouth = QRectF(cx - 6, cy + 3, 12, 8)
    if mood in (Mood.HAPPY, Mood.LOVING):
        p.drawArc(mouth, 200 * 16, 140 * 16)
    elif mood is Mood.SAD:
        p.drawArc(QRectF(cx - 5, cy + 7, 10, 7), 20 * 16, 140 * 16)
    elif mood is Mood.ANGRY:
        p.drawLine(QPointF(cx - 5, cy + 9), QPointF(cx + 5, cy + 9))
    elif mood is Mood.CONFUSED:
        p.drawArc(QRectF(cx - 4, cy + 7, 8, 5), 0, 360 * 16)
    else:
        p.drawArc(QRectF(cx - 4, cy + 5, 8, 6), 210 * 16, 120 * 16)


def _draw_extras(p: QPainter, st: PaintState) -> None:
    """Значки настроения над и вокруг головы."""
    t = st.phase
    mood = st.mood
    if mood is Mood.HAPPY:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFE066"))
        for i, (x, y) in enumerate(((-4, -34), (14, -26), (-46, -22))):
            r = 4.5 + 2.0 * math.sin(t * 5 + i * 1.7)
            p.drawPolygon(_star(x, y, max(r, 2.0)))
    elif mood is Mood.LOVING:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FF4D7D"))
        for i in range(3):
            f = (t * 0.6 + i / 3) % 1.0
            x = -34 + i * 16 + math.sin(t * 3 + i) * 3
            y = -22 - f * 26
            p.setOpacity(1.0 - f * 0.7)
            p.drawPath(_heart(x, y, 3.2 + i * 0.6))
        p.setOpacity(1.0)
    elif mood is Mood.SAD:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(90, 170, 255, 220))
        for i, ex in enumerate((-33, -19)):
            f = (t * 0.9 + i * 0.5) % 1.0
            y = 12 + f * 22
            drop = QPainterPath(QPointF(ex, y - 4))
            drop.cubicTo(ex + 4, y + 1, ex + 3, y + 5, ex, y + 5)
            drop.cubicTo(ex - 3, y + 5, ex - 4, y + 1, ex, y - 4)
            p.setOpacity(1.0 - f * 0.6)
            p.drawPath(drop)
        p.setOpacity(1.0)
    elif mood is Mood.ANGRY:
        count = 6 if st.angry_high else 3
        p.setPen(QPen(QColor("#FF6A00"), 2.2, Qt.SolidLine, Qt.RoundCap))
        for i in range(count):
            a = math.radians(-160 + i * (140 / max(count - 1, 1)))
            pulse = 0.5 + 0.5 * math.sin(t * (14 if st.angry_high else 8) + i * 1.3)
            r0, r1 = 22 + 3 * pulse, 30 + 8 * pulse
            c = QPointF(-26, 8)
            p.drawLine(c + QPointF(math.cos(a) * r0, math.sin(a) * r0),
                       c + QPointF(math.cos(a) * r1, math.sin(a) * r1))
    elif mood is Mood.CONFUSED:
        font = QFont()
        font.setBold(True)
        font.setPixelSize(26)
        p.setFont(font)
        p.setPen(QColor("#6C4AB6"))
        p.save()
        p.translate(-26, -34)
        if st.facing > 0:
            p.scale(-1, 1)  # рисунок отзеркален, а «?» должен читаться
        p.rotate(math.sin(t * 3) * 10)
        p.drawText(QRectF(-14, -16, 28, 32), Qt.AlignCenter, "?")
        p.restore()


def paint_bee(p: QPainter, rect: QRectF, state: PaintState) -> None:
    """Рисует пчелу в прямоугольнике rect (логические координаты окна)."""
    p.save()
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                     | QPainter.SmoothPixmapTransform)
    scale = rect.width() / BASE
    p.translate(rect.center())
    p.scale(scale, scale)
    # лёгкое покачивание вверх-вниз (в полёте) и смещения (барахтанье, дрожь)
    if state.walking and not state.dragging:
        bob = -abs(math.sin(state.phase * 2 * math.pi * STEP_HZ)) * STEP_BOB
    elif _standing(state):
        bob = 0.0
    else:
        bob = math.sin(state.phase * 2 * math.pi * 0.8) * 2.5
    p.translate(state.wiggle[0], bob + state.wiggle[1] + BODY_DROP)
    p.rotate(state.tilt)
    if state.facing > 0:
        p.scale(-1, 1)  # голова справа: зеркалим весь рисунок
    if state.stinging:
        p.translate(12, 0)  # короткий выпад вперёд жалом
        p.rotate(8)
    _draw_wings(p, state)
    _draw_legs(p, state)
    _draw_body(p, state)
    _draw_head(p, state)
    _draw_extras(p, state)
    p.restore()
