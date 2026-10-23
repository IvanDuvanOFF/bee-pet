"""Тесты метаний злой пчелы (FrenzyController) на заглушке окна."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math
import random
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bee_pet.frenzy import (ANGRY_STYLE, FURIOUS_STYLE, FrenzyController, FrenzyStyle,
                             dodge_target, hop_target)
from bee_pet.mood import Mood, MoodChange

AREA = QRect(0, 0, 1920, 1040)
SIZE = 120
POLL_MS = 20
FAR = QPoint(-5000, -5000)  # курсор далеко: уклоняться не от чего


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBee(QObject):
    """Заглушка BeeWindow: только то, что нужно метаниям."""
    mood_changed = Signal(object)
    drag_started = Signal()
    drag_finished = Signal()

    def __init__(self):
        super().__init__()
        self.engine = SimpleNamespace(mood=Mood.NEUTRAL, angry_high=False)
        self.is_dragging = False
        self.is_pressed = False
        self.is_flying = False
        self.menu_open = False
        self.menu = SimpleNamespace(isVisible=lambda: self.menu_open)
        self._pos = QPoint(900, 500)
        self.flights = []  # (кто, цель, kwargs)
        self._on_arrived = None

    def pos(self):
        return self._pos

    def width(self):
        return SIZE

    def geometry(self):
        return QRect(self._pos, self._pos + QPoint(SIZE - 1, SIZE - 1))

    def fly_to(self, target, on_arrived=None, *, who="frenzy", **kw):
        self.flights.append((who, target, kw))
        self.is_flying = True
        self._on_arrived = on_arrived
        return 50

    def land(self):
        """Полёт закончился: пчела на месте, зовём on_arrived."""
        if self.flights:
            self._pos = self.flights[-1][1]
        self.is_flying = False
        cb, self._on_arrived = self._on_arrived, None
        if cb:
            cb()

    def set_mood(self, mood, high=False):
        old, was_high = self.engine.mood, self.engine.angry_high
        self.engine.mood, self.engine.angry_high = mood, high
        self.mood_changed.emit(MoodChange(old, mood, high, high and not was_high))


class FakeBite(QObject):
    IDLE, FLYING = "idle", "flying"

    def __init__(self):
        super().__init__()
        self.state = self.IDLE


@pytest.fixture
def env(qapp):
    bee, bite = FakeBee(), FakeBite()
    frenzy = FrenzyController(bee, bite, rng=random.Random(1), poll_ms=POLL_MS,
                              area_provider=lambda center: AREA, cursor_provider=lambda: FAR)
    yield bee, bite, frenzy


def frenzy_hops(bee):
    return [f for f in bee.flights if f[0] == "frenzy"]


def dist(a, b):
    return math.hypot(a.x() - b.x(), a.y() - b.y())


def test_no_hops_when_calm(env):
    bee, bite, frenzy = env
    bee.set_mood(Mood.HAPPY)
    QTest.qWait(80)
    assert bee.flights == [] and not frenzy.active


def test_angry_hop_uses_angry_style(env):
    bee, bite, frenzy = env
    start = bee.pos()
    bee.set_mood(Mood.ANGRY)
    assert bee.flights == []  # прямо в обработчике mood_changed не летим
    QTest.qWait(30)
    [(_, target, kw)] = bee.flights
    assert kw == dict(speed=600.0, min_ms=150, easing=ANGRY_STYLE.easing)
    assert ANGRY_STYLE.radius * 0.25 - 2 <= dist(start, target) <= ANGRY_STYLE.radius + 2
    assert frenzy.active  # следующий шаг запланирован


def test_angry_high_hop_uses_fast_style(env):
    bee, bite, frenzy = env
    start = bee.pos()
    bee.set_mood(Mood.ANGRY, high=True)
    QTest.qWait(30)
    [(_, target, kw)] = bee.flights
    assert kw == dict(speed=1500.0, min_ms=80, easing=FURIOUS_STYLE.easing)
    assert dist(start, target) <= FURIOUS_STYLE.radius + 2


def test_styles_constants():
    assert (ANGRY_STYLE.radius, ANGRY_STYLE.speed) == (250.0, 600.0)
    assert (ANGRY_STYLE.pause_min_s, ANGRY_STYLE.pause_max_s) == (0.4, 1.2)
    assert (FURIOUS_STYLE.radius, FURIOUS_STYLE.speed) == (600.0, 1500.0)
    assert (FURIOUS_STYLE.pause_min_s, FURIOUS_STYLE.pause_max_s) == (0.0, 0.25)


def test_keeps_hopping_after_landing(qapp):
    bee, bite = FakeBee(), FakeBite()
    fast = FrenzyStyle(250.0, 600.0, 0.0, 0.01, 50, QEasingCurve.OutQuad)
    FrenzyController(bee, bite, rng=random.Random(2), angry_style=fast, poll_ms=POLL_MS,
                     area_provider=lambda c: AREA, cursor_provider=lambda: FAR)
    bee.set_mood(Mood.ANGRY)
    for n in range(1, 4):
        QTest.qWait(90)
        assert len(bee.flights) == n
        bee.land()


def test_hop_targets_stay_in_area():
    area = QRect(0, 0, 400, 300)
    rng = random.Random(3)
    for _ in range(200):
        t = hop_target(QPoint(10, 10), FURIOUS_STYLE, area, SIZE, rng)
        assert area.contains(QRect(t, t + QPoint(SIZE - 1, SIZE - 1)))


def test_no_hops_while_dragging_resumes_after(env):
    bee, bite, frenzy = env
    bee.is_dragging = True
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(60)
    assert bee.flights == []
    bee.is_dragging = False
    bee.drag_finished.emit()
    QTest.qWait(30)
    assert len(frenzy_hops(bee)) == 1


def test_no_hops_while_bite_busy_resumes_when_idle(env):
    bee, bite, frenzy = env
    bite.state = bite.FLYING
    bee.set_mood(Mood.ANGRY, high=True)
    QTest.qWait(80)
    assert bee.flights == []
    bite.state = "warning"
    QTest.qWait(60)
    assert bee.flights == []  # пчела висит у окна во время отсчёта
    bite.state = bite.IDLE
    QTest.qWait(60)
    assert len(frenzy_hops(bee)) == 1


def test_no_hops_during_foreign_flight(env):
    bee, bite, frenzy = env
    bee.fly_to(QPoint(0, 0), who="other")
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(60)
    assert frenzy_hops(bee) == []
    bee.land()
    QTest.qWait(60)
    assert len(frenzy_hops(bee)) == 1


def test_bite_starting_from_same_signal_wins(env):
    bee, bite, frenzy = env

    def bite_on_mood(change):  # подключён после метаний: худший порядок
        if change.became_angry_high:
            bite.state = bite.FLYING
            bee.fly_to(QPoint(100, 100), who="bite")

    bee.mood_changed.connect(bite_on_mood)
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(30)
    assert len(frenzy_hops(bee)) == 1
    bee.set_mood(Mood.ANGRY, high=True)  # укус стартует, пока наш прыжок ещё в полёте
    QTest.qWait(100)
    assert bee.flights[-1][0] == "bite"
    assert len(frenzy_hops(bee)) == 1


def test_stops_when_calm(env):
    bee, bite, frenzy = env
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(30)
    assert len(bee.flights) == 1
    bee.set_mood(Mood.NEUTRAL)
    assert not frenzy.active
    bee.land()  # текущий прыжок долетает
    QTest.qWait(int(ANGRY_STYLE.pause_max_s * 1000) + 150)
    assert len(bee.flights) == 1


# --- уклонение от курсора -----------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


DODGE_SPEED = ANGRY_STYLE.speed * 1.5


@pytest.fixture
def dodge_env(qapp):
    """Метания с подвижным курсором и ручными часами; первый прыжок уже долетел."""
    bee, bite = FakeBee(), FakeBite()
    cursor = {"pos": FAR}
    clock = Clock()
    frenzy = FrenzyController(bee, bite, rng=random.Random(5), poll_ms=POLL_MS,
                              area_provider=lambda c: AREA, cursor_provider=lambda: cursor["pos"],
                              dodge_poll_ms=10, clock=clock)
    yield bee, bite, frenzy, cursor, clock


def dodges(bee):
    return [f for f in bee.flights if f[2].get("speed") == DODGE_SPEED]


def angry_and_landed(bee):
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(30)
    assert len(bee.flights) == 1
    bee.land()  # дальше пауза не меньше 0,4 с: в ней прыжков нет


def near(bee, dx=40, dy=0):
    c = bee.geometry().center()
    return QPoint(c.x() + dx, c.y() + dy)


def test_hop_target_prefers_far_from_cursor():
    pos, cursor = QPoint(900, 500), QPoint(1100, 560)
    left = 0
    for seed in range(100):
        single = hop_target(pos, ANGRY_STYLE, AREA, SIZE, random.Random(seed), cursor=cursor)
        best = hop_target(pos, ANGRY_STYLE, AREA, SIZE, random.Random(seed), cursor=cursor,
                          candidates=6)
        d = lambda p: dist(QPoint(p.x() + SIZE // 2, p.y() + SIZE // 2), cursor)  # noqa: E731
        assert d(best) >= d(single)  # первый кандидат тот же, лучший не ближе
        assert AREA.contains(QRect(best, best + QPoint(SIZE - 1, SIZE - 1)))
        left += best.x() < pos.x()
    assert left >= 90  # курсор справа — пчела почти всегда прыгает влево


def test_dodge_target_moves_away_and_escapes_corner():
    pos = QPoint(900, 500)
    cursor = near(SimpleNamespace(geometry=lambda: QRect(pos, pos + QPoint(SIZE - 1, SIZE - 1))))
    t = dodge_target(pos, cursor, ANGRY_STYLE, AREA, SIZE)
    assert t.x() < pos.x() - ANGRY_STYLE.radius + 5 and abs(t.y() - pos.y()) <= 10
    # в левом верхнем углу, курсор слева-сверху от центра: прямо — в угол, берём лучший бок
    corner = QPoint(0, 0)
    cur = QPoint(40, 40)
    t = dodge_target(corner, cur, ANGRY_STYLE, AREA, SIZE)
    assert AREA.contains(QRect(t, t + QPoint(SIZE - 1, SIZE - 1)))
    centre = QPoint(t.x() + SIZE // 2, t.y() + SIZE // 2)
    assert dist(centre, cur) > ANGRY_STYLE.dodge_radius


def test_dodge_when_cursor_near(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    angry_and_landed(bee)
    start = bee.pos()
    cursor["pos"] = near(bee, 40, 0)  # справа от центра, ближе 150 px
    QTest.qWait(40)
    [(_, target, kw)] = dodges(bee)
    assert target.x() < start.x() - 200  # отскочила влево, прочь от курсора
    assert kw["easing"] == QEasingCurve.OutQuad and frenzy.dodges == 1


def test_no_dodge_when_cursor_outside_radius(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    angry_and_landed(bee)
    cursor["pos"] = near(bee, int(ANGRY_STYLE.dodge_radius) + 10, 0)
    QTest.qWait(40)
    assert dodges(bee) == []


def test_furious_dodge_radius_is_larger(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    bee.set_mood(Mood.ANGRY, high=True)
    QTest.qWait(30)
    bee.land()
    bee.flights.clear()
    cursor["pos"] = near(bee, 200, 0)  # дальше 150, но ближе 220
    QTest.qWait(40)
    assert [f for f in bee.flights if f[2].get("speed") == FURIOUS_STYLE.speed * 1.5]


@pytest.mark.parametrize("block", ["dragging", "pressed", "bite", "menu", "foreign"])
def test_no_dodge_when_blocked(dodge_env, block):
    bee, bite, frenzy, cursor, clock = dodge_env
    angry_and_landed(bee)
    if block == "dragging":
        bee.is_dragging = True
    elif block == "pressed":
        bee.is_pressed = True
    elif block == "bite":
        bite.state = bite.FLYING
    elif block == "menu":
        bee.menu_open = True
    else:
        bee.fly_to(QPoint(0, 0), who="other")
    cursor["pos"] = near(bee)
    QTest.qWait(50)
    assert dodges(bee) == [] and frenzy.dodges == 0


def test_dodge_redirects_own_hop(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    bee.set_mood(Mood.ANGRY)
    QTest.qWait(30)
    assert len(bee.flights) == 1 and bee.is_flying  # свой прыжок в полёте
    cursor["pos"] = near(bee)
    QTest.qWait(40)
    assert len(dodges(bee)) == 1


def test_dodge_rate_limited(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    angry_and_landed(bee)
    cursor["pos"] = near(bee)
    QTest.qWait(40)
    assert frenzy.dodges == 1
    bee.land()
    cursor["pos"] = near(bee)  # курсор догнал пчелу, но часы не сдвинулись
    QTest.qWait(40)
    assert frenzy.dodges == 1
    clock.t += 0.2
    QTest.qWait(40)
    assert frenzy.dodges == 2


def test_dodge_poll_only_while_angry(dodge_env):
    bee, bite, frenzy, cursor, clock = dodge_env
    assert not frenzy.dodging
    bee.set_mood(Mood.HAPPY)
    assert not frenzy.dodging
    bee.set_mood(Mood.ANGRY)
    assert frenzy.dodging
    bee.set_mood(Mood.NEUTRAL)
    assert not frenzy.dodging
    cursor["pos"] = near(bee)
    QTest.qWait(40)
    assert dodges(bee) == []


def test_hop_can_cross_to_neighbour_screen():
    """Злая пчела перелетает на соседний монитор, но не в «дыру» между экранами разной высоты."""
    left, right = QRect(-1920, 8, 1920, 1080), QRect(0, 0, 1536, 960)
    rng = random.Random(1)
    pos = QPoint(-SIZE // 2, 300)  # пчела на стыке мониторов
    seen_left = seen_right = False
    for _ in range(300):
        t = hop_target(pos, FURIOUS_STYLE, [left, right], SIZE, rng)
        bee_rect = QRect(t, t + QPoint(SIZE - 1, SIZE - 1))
        assert left.contains(bee_rect) or right.contains(bee_rect)
        seen_left |= left.contains(bee_rect)
        seen_right |= right.contains(bee_rect)
    assert seen_left and seen_right
