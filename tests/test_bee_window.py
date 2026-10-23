"""Тесты окна пчелы, рисования и пузыря (offscreen)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEasingCurve, QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.bubble import SpeechBubble
from bee_pet.mood import Action, Mood
from bee_pet.painter import PaintState, paint_bee
from PySide6.QtCore import QRectF


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "bee.ini"), QSettings.IniFormat)


@pytest.fixture
def bee(qapp, settings):
    w = BeeWindow(settings=settings)
    w.show()
    yield w
    w.bubble.hide()
    w.close()


def test_window_shows(bee):
    assert bee.isVisible()
    assert (bee.width(), bee.height()) == (120, 120)


def test_apply_action_changes_mood_and_emits(bee):
    changes = []
    bee.mood_changed.connect(changes.append)
    bee.apply_action(Action.FLOWER)
    assert bee.engine.mood is Mood.HAPPY
    assert changes and changes[-1].new is Mood.HAPPY


def test_apply_model_mood_changes_mood_and_emits(bee):
    changes = []
    bee.mood_changed.connect(changes.append)
    bee.apply_model_mood(Mood.ANGRY, True)
    assert bee.engine.mood is Mood.ANGRY and bee.engine.angry_high
    assert changes[-1].became_angry_high


def test_menu_action_deferred_mood_untouched(bee):
    performed = []
    bee.action_performed.connect(performed.append)
    bee.mood_deferred = True  # настроение выставит ответ модели
    bee.menu_actions[Action.SWAT].trigger()
    assert performed == [Action.SWAT] and bee.engine.mood is Mood.NEUTRAL


def test_menu_action_emits_without_canned_line(bee):
    performed = []
    bee.action_performed.connect(performed.append)
    bee.menu_actions[Action.SWAT].trigger()
    assert performed == [Action.SWAT]
    assert bee.engine.mood is Mood.ANGRY
    assert not bee.bubble.isVisible()  # реплику покажет ответ модели, не заготовка
    bee.menu_actions[Action.FLOWER].trigger()
    assert bee.engine.angry_high
    bee.menu_actions[Action.PET].trigger()
    assert bee.engine.mood is Mood.ANGRY and bee.engine.angry_high
    bee.menu_actions[Action.SORRY].trigger()
    assert bee.engine.mood is Mood.NEUTRAL
    assert performed == [Action.SWAT, Action.FLOWER, Action.PET, Action.SORRY]
    assert not bee.bubble.isVisible()


def test_mood_decay_emits_mood_decayed(qapp, settings):
    from bee_pet.mood import ANGRY_DECAY, MoodEngine
    clock = _Clock()
    w = BeeWindow(settings=settings, engine=MoodEngine(clock), clock=clock)
    decayed, changed = [], []
    w.mood_decayed.connect(decayed.append)
    w.mood_changed.connect(changed.append)
    w.apply_action(Action.SWAT)
    assert decayed == []  # действие — не затухание
    w._on_mood_tick()
    assert decayed == []
    clock.t += ANGRY_DECAY
    w._on_mood_tick()
    assert len(decayed) == 1 and decayed[0].new is Mood.NEUTRAL
    assert changed[-1] is decayed[0]


def test_biting_default_and_persist(qapp, settings):
    w = BeeWindow(settings=settings)
    assert w.biting_enabled is True
    assert w.biting_action.isChecked()
    got = []
    w.biting_toggled.connect(got.append)
    w.biting_action.setChecked(False)
    assert got == [False] and not w.biting_enabled
    assert BeeWindow(settings=settings).biting_enabled is False


def test_fly_to_calls_on_arrived(bee):
    target = bee.pos() - QPoint(100, 50)
    arrived = []
    bee.fly_to(target, lambda: arrived.append(True))
    assert bee.is_flying
    QTest.qWait(1200)
    assert arrived == [True]
    assert bee.pos() == target


def test_cancel_flight_skips_callback(bee):
    arrived = []
    bee.fly_to(bee.pos() - QPoint(150, 0), lambda: arrived.append(True))
    bee.cancel_flight()
    QTest.qWait(500)
    assert arrived == [] and not bee.is_flying


def test_fly_to_default_duration_unchanged(bee):
    start = bee.pos()
    assert bee.fly_to(start - QPoint(400, 0)) == 1000   # 400 px при 400 px/с
    assert bee.fly_to(bee.pos() - QPoint(10, 0)) == 300  # не короче 300 мс
    assert bee.fly_to(bee.pos() - QPoint(5000, 0)) == 3000  # не дольше 3 с
    assert bee._flight.easingCurve().type() == QEasingCurve.InOutSine
    bee.cancel_flight()


def test_fly_to_speed_min_ms_and_easing(bee):
    start = bee.pos()
    assert bee.fly_to(start - QPoint(300, 0), speed=1500.0, min_ms=80,
                      easing=QEasingCurve.Linear) == 200
    assert bee._flight.easingCurve().type() == QEasingCurve.Linear
    assert bee.fly_to(bee.pos() - QPoint(30, 0), speed=1500.0, min_ms=80) == 80
    assert bee.fly_to(bee.pos() - QPoint(30, 0), speed=1500.0, min_ms=150) == 150
    bee.cancel_flight()


def test_drag_finished_only_after_real_drag(bee):
    started, finished = [], []
    bee.drag_started.connect(lambda: started.append(1))
    bee.drag_finished.connect(lambda: finished.append(1))
    QTest.mouseClick(bee, Qt.LeftButton, pos=QPoint(60, 60))  # клик без переноса
    assert (started, finished) == ([], [])
    QTest.mousePress(bee, Qt.LeftButton, pos=QPoint(60, 60))
    QTest.mouseMove(bee, QPoint(90, 90))
    QTest.mouseMove(bee, QPoint(100, 100))
    assert bee.is_dragging and started == [1] and finished == []
    QTest.mouseRelease(bee, Qt.LeftButton, pos=QPoint(100, 100))
    assert not bee.is_dragging and finished == [1]


@pytest.mark.parametrize("mood", list(Mood))
@pytest.mark.parametrize("extra", [dict(), dict(dragging=True, tilt=15.0, wiggle=(3.0, 1.0))])
def test_painter_renders_every_mood(qapp, mood, extra):
    img = QImage(120, 120, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    paint_bee(p, QRectF(0, 0, 120, 120),
              PaintState(phase=0.7, mood=mood, angry_high=mood is Mood.ANGRY, **extra))
    p.end()
    assert any(img.pixelColor(x, 60).alpha() > 0 for x in range(120))


def test_bubble_show_and_hide(qapp):
    b = SpeechBubble()
    b.show_text("бзз", QRect(500, 500, 80, 80), duration_ms=100)
    assert b.isVisible() and b.text == "бзз"
    b.show_text("новый текст", QRect(500, 500, 80, 80), duration_ms=100)
    assert b.text == "новый текст"
    QTest.qWait(300)
    assert not b.isVisible()


@pytest.mark.parametrize("text", ["бзз", "🐝 bzz bz zz bzz",
                                  "🐝 *STINGS YOU* *screams in bee* *knocks over the flower* BZZT!!"])
def test_bubble_text_fits(qapp, text):
    """Текст, нарисованный в области пузыря, помещается в неё без обрезки."""
    from bee_pet.bubble import BORDER, PAD, TAIL_H, TEXT_FLAGS
    b = SpeechBubble()
    b.show_text(text, QRect(500, 500, 80, 80), duration_ms=100)
    inner = QRectF(0, 0, b.width() - 2 * (PAD + BORDER),
                   b.height() - TAIL_H - 2 * (PAD + BORDER))
    img = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setFont(b._font)
    needed = p.boundingRect(inner, TEXT_FLAGS, text)
    p.end()
    assert needed.width() <= inner.width() + 0.5
    assert needed.height() <= inner.height() + 0.5
    b.hide()


# --- мельтешение курсором и переключатель фокуса ------------------------------

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from bee_pet.bee_window import JITTER_COOLDOWN_S, JitterDetector


def _feed(det, points, dt):
    """Скармливает детектору точки с шагом dt; возвращает список результатов."""
    return [det.add(i * dt, x, y) for i, (x, y) in enumerate(points)]


def _zigzag(n, amp=60.0, axis=0):
    """Курсор мечется туда-сюда по одной оси с размахом amp."""
    pts = []
    for i in range(n):
        v = 500.0 + (amp if i % 2 else 0.0)
        pts.append((v, 300.0) if axis == 0 else (300.0, v))
    return pts


@pytest.mark.parametrize("axis", [0, 1])
def test_jitter_fast_back_and_forth(axis):
    assert any(_feed(JitterDetector(), _zigzag(10, axis=axis), 0.05))


def test_jitter_slow_movement_ignored():
    # те же развороты, но по одному в секунду — окно успевает забыть
    assert not any(_feed(JitterDetector(), _zigzag(10), 0.6))


def test_jitter_one_direction_ignored():
    pts = [(100.0 + 20 * i, 300.0) for i in range(40)]
    assert not any(_feed(JitterDetector(), pts, 0.02))


def test_jitter_small_noise_ignored():
    # дрожание на 2 px: много разворотов, но шаги короче порога и путь мал
    assert not any(_feed(JitterDetector(), _zigzag(40, amp=2.0), 0.02))


def test_jitter_samples_expire():
    # три разворота, затем ещё три точки: подряд это мельтешение...
    first, second = _zigzag(5), _zigzag(4)[1:]
    det = JitterDetector()
    assert any(_feed(det, first + second, 0.05))
    # ...а с паузой дольше окна старые точки забыты
    det = JitterDetector()
    assert not any(_feed(det, first, 0.05))
    assert not any(det.add(5.0 + i * 0.05, x, y) for i, (x, y) in enumerate(second))


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _hover(w, gx, gy):
    local = QPointF(gx - w.x(), gy - w.y())
    ev = QMouseEvent(QMouseEvent.MouseMove, local, QPointF(gx, gy),
                     Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    w.mouseMoveEvent(ev)


def _rub(w, clock, n=10):
    for x, y in _zigzag(n, amp=60.0):
        clock.t += 0.03
        _hover(w, w.x() + x - 470, w.y() + 60)


def test_jitter_over_bee_is_swat(qapp, settings):
    clock = _Clock()
    w = BeeWindow(settings=settings, clock=clock)
    w.show()
    assert w.hasMouseTracking()
    performed = []
    w.action_performed.connect(performed.append)
    _rub(w, clock)
    assert performed == [Action.SWAT]
    assert w.engine.mood is Mood.ANGRY
    # пауза: продолжение мельтешения сразу не даёт второго шлепка
    _rub(w, clock)
    assert performed == [Action.SWAT]
    # после паузы — снова шлепок
    clock.t += JITTER_COOLDOWN_S
    _rub(w, clock)
    assert performed == [Action.SWAT, Action.SWAT]
    w.bubble.hide()
    w.close()


def test_jitter_ignored_while_pressed(qapp, settings):
    clock = _Clock()
    w = BeeWindow(settings=settings, clock=clock)
    performed = []
    w.action_performed.connect(performed.append)
    w._press_global = QPointF(0, 0)  # левая кнопка зажата
    _rub(w, clock)
    assert performed == []


def test_focus_switch_default_and_persist(qapp, settings):
    w = BeeWindow(settings=settings)
    assert w.focus_switch_enabled is True
    assert w.focus_switch_action.isChecked()
    acts = w.menu.actions()
    assert acts.index(w.focus_switch_action) == acts.index(w.biting_action) + 1
    got = []
    w.focus_switch_toggled.connect(got.append)
    w.focus_switch_action.setChecked(False)
    assert got == [False] and not w.focus_switch_enabled
    assert BeeWindow(settings=settings).focus_switch_enabled is False
