"""Тесты окна пчелы, рисования и пузыря (offscreen)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings
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


def test_menu_action_emits_and_says(bee):
    performed = []
    bee.action_performed.connect(performed.append)
    bee.menu_actions[Action.SWAT].trigger()
    assert performed == [Action.SWAT]
    assert bee.engine.mood is Mood.ANGRY
    assert bee.bubble.isVisible() and bee.bubble.text == "БЗЗ!! 😠"
    bee.menu_actions[Action.FLOWER].trigger()
    assert bee.engine.angry_high
    assert bee.bubble.text == "не подлизывайся! 😡"
    bee.menu_actions[Action.SORRY].trigger()
    assert bee.engine.mood is Mood.NEUTRAL


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
