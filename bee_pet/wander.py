"""Спокойная пчела гуляет между окнами, а пока пользователя нет — переключает их.

Модуль не зависит от платформы: чужие окна видны только через `winapi`, которую
в тестах подменяют заглушкой. Над чужими окнами здесь возможно лишь одно действие —
`winapi.focus_window` (вывести окно на передний план), и только когда пользователь
давно не трогал клавиатуру и мышь. Окна здесь никогда не закрываются.

Если садиться не на что (окон нет, все свёрнуты или закрыты, нет WinAPI), пчела
садится на верхний край панели задач своего экрана. Где именно пчела села, узнаёт
WalkController (сигнал `landed`) — дальше она ходит по кромке пешком.
"""
from __future__ import annotations

import logging
import os
import random
from types import ModuleType
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from bee_pet import winapi
from bee_pet.biting import (BLOCKED_CLASSES, MAX_MOVE_PX, MIN_HEIGHT, MIN_WIDTH, Rect,
                            _intersects, _moved, physical_screen_rects, physical_to_logical,
                            qt_screens)
from bee_pet.mood import Mood, MoodChange
from bee_pet.painter import feet_y
from bee_pet.winapi import WindowInfo

log = logging.getLogger("bee_pet.wander")

# Тайминги по умолчанию (в тестах подменяются через конструктор).
WANDER_MIN_S = 60.0
WANDER_MAX_S = 180.0
IDLE_FOCUS_S = 120.0  # столько секунд без ввода — и пчела начинает переключать окна

FLIGHT_PROMPT = "*flies to another window*"  # BeeLLM отвечает на это спокойным «buzzz zz bzz»
FLIGHT_BUZZ_MS = 2500
WANDER_TAG = "wander"

LANDING_TRIES = 6      # сколько случайных точек на верхней кромке окна пробовать
LANDING_INSET = 0.15   # не садиться у самых краёв окна (доля ширины)

Spot = tuple[int, int]
# Провайдер экрана: точка (логические координаты) -> (availableGeometry, geometry) экрана.
ScreenProvider = Callable[[QPoint], tuple[QRect, QRect]]


# --- политика -------------------------------------------------------------


def headroom_px(screens: Sequence[tuple[QRect, float]], feet: int) -> int:
    """Сколько физических пикселей над кромкой нужно пчеле, чтобы стоять на ней целиком."""
    return max((round(feet * dpr) for _, dpr in screens), default=feet)


def landing_skip_reason(win: WindowInfo, own_pid: int,
                        screens: Sequence[Rect] | None = None,
                        headroom: int = 0) -> str | None:
    """Почему на окно нельзя садиться; None, если можно. Терминалы и IDE разрешены.

    headroom — сколько места (физ. px) нужно над кромкой внутри экрана: у развёрнутого
    окна кромка у самого края экрана, стоять на ней негде, и пчела выбирает другое
    окно или панель задач.
    """
    if win.pid == own_pid:
        return "окно собственного процесса"
    if win.class_name in BLOCKED_CLASSES:
        return f"системный класс {win.class_name}"
    if not win.title:
        return "без заголовка"
    if win.minimized:
        return "окно свёрнуто"
    if win.cloaked:
        return "окно cloaked"
    if win.is_tool:
        return "tool-окно"
    left, top, right, bottom = win.rect
    if right - left < MIN_WIDTH or bottom - top < MIN_HEIGHT:
        return "окно слишком маленькое"
    if screens is not None and not any(_intersects(win.rect, s) for s in screens):
        return "окно вне экранов"
    if screens and headroom > 0 and not any(
            s[0] < right and left < s[2] and s[1] + headroom <= top < s[3] for s in screens):
        return "над кромкой нет места (окно у верхнего края экрана)"
    return None


def _occluders(windows: Sequence[WindowInfo], index: int, own_pid: int) -> list[WindowInfo]:
    """Окна выше по Z-порядку, которые реально видны (свои окна пчелы не в счёт)."""
    return [w for w in windows[:index]
            if w.pid != own_pid and not w.minimized and not w.cloaked]


def spot_covered(spot: Spot, higher: Sequence[WindowInfo]) -> bool:
    """Закрыта ли точка каким-нибудь окном выше по Z-порядку."""
    x, y = spot
    return any(w.rect[0] <= x < w.rect[2] and w.rect[1] <= y < w.rect[3] for w in higher)


def _on_screen(spot: Spot, screens: Sequence[Rect] | None) -> bool:
    if not screens:
        return True
    x, y = spot
    return any(s[0] <= x < s[2] and s[1] <= y < s[3] for s in screens)


def landing_spot(win: WindowInfo, higher: Sequence[WindowInfo], rng: random.Random,
                 screens: Sequence[Rect] | None = None,
                 near_x: int | None = None) -> Spot | None:
    """Случайная открытая точка на верхней кромке окна (физические пиксели) или None.

    near_x — сначала пробуется точка кромки, ближайшая к этому x (посадка «поближе»).
    """
    left, top, right, _ = win.rect
    inset = int((right - left) * LANDING_INSET)
    lo, hi = left + inset, right - 1 - inset
    if near_x is not None:
        spot = (max(lo, min(near_x, hi)), top + 1)
        if _on_screen(spot, screens) and not spot_covered(spot, higher):
            return spot
    for _ in range(LANDING_TRIES):
        spot = (rng.randint(lo, hi), top + 1)
        if _on_screen(spot, screens) and not spot_covered(spot, higher):
            return spot
    return None


def pick_landing(windows: Sequence[WindowInfo], own_pid: int, rng: random.Random | None = None,
                 screens: Sequence[Rect] | None = None, current_hwnd: int | None = None,
                 exclude: Callable[[WindowInfo], str | None] | None = None,
                 near: Spot | None = None, headroom: int = 0,
                 ) -> tuple[WindowInfo, Spot] | None:
    """Случайное окно для посадки и точка на его кромке; окна идут сверху вниз.

    exclude — дополнительный фильтр (причина отказа или None). Окно, на котором пчела
    уже сидит, выбирается, только если других нет. near — точка (физические пиксели):
    тогда берётся не случайное окно, а точка посадки, ближайшая к ней.
    """
    rng = rng or random.Random()
    options: list[tuple[WindowInfo, Spot]] = []
    for i, win in enumerate(windows):
        reason = landing_skip_reason(win, own_pid, screens, headroom)
        if reason is None and exclude is not None:
            reason = exclude(win)
        if reason is None:
            spot = landing_spot(win, _occluders(windows, i, own_pid), rng, screens,
                                near[0] if near is not None else None)
            if spot is None:
                reason = "кромка закрыта другими окнами"
            else:
                options.append((win, spot))
        if reason is not None:
            log.debug("не сажусь на %r (%s): %s", win.title, win.process_name, reason)
    if not options:
        return None
    if near is not None:
        return min(options, key=lambda o: (o[1][0] - near[0]) ** 2 + (o[1][1] - near[1]) ** 2)
    fresh = [o for o in options if o[0].hwnd != current_hwnd]
    return rng.choice(fresh or options)


# --- координаты -----------------------------------------------------------


def available_areas() -> list[QRect]:
    """Рабочие области экранов (без панели задач), основной экран первым."""
    primary = QGuiApplication.primaryScreen()
    others = [s for s in QGuiApplication.screens() if s is not primary]
    return [s.availableGeometry() for s in ([primary] if primary else []) + others]


def screen_at(point: QPoint) -> tuple[QRect, QRect]:
    """(availableGeometry, geometry) экрана, где точка; если она вне экранов — основного."""
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry(), screen.geometry()


def logical_to_physical(point: QPoint, screens: Sequence[tuple[QRect, float]]) -> Spot:
    """Обратное к physical_to_logical (то же допущение о расположении экранов)."""
    if not screens:
        return point.x(), point.y()
    geom, dpr = next(((g, d) for g, d in screens if g.contains(point)), screens[0])
    return (round(geom.left() * dpr + (point.x() - geom.left()) * dpr),
            round(geom.top() * dpr + (point.y() - geom.top()) * dpr))


def _clamp(pos: QPoint, area: QRect, size: int) -> QPoint:
    x = max(area.left(), min(pos.x(), area.right() - size + 1))
    y = max(area.top(), min(pos.y(), area.bottom() - size + 1))
    return QPoint(x, y)


def landing_corner(spot: Spot, screens: Sequence[tuple[QRect, float]],
                   areas: Sequence[QRect], size: int) -> QPoint:
    """Левый верхний угол окна пчелы, чтобы она сидела на кромке в точке spot."""
    point = physical_to_logical(spot, screens)
    pos = QPoint(point.x() - size // 2, point.y() - feet_y(size))
    if not areas:
        return pos
    area = next((a for a in areas if a.contains(point)), areas[0])
    return _clamp(pos, area, size)


def taskbar_y(area: QRect, geom: QRect, size: int) -> int:
    """y левого верхнего угла окна пчелы, стоящей лапками на верхнем краю панели задач.

    Край панели — первая строка под рабочей областью (area.bottom() + 1). Если панели
    снизу нет (скрыта или сбоку), area совпадает с экраном и пчела стоит у его низа.
    """
    y = area.bottom() + 1 - feet_y(size)
    return max(geom.top(), min(y, geom.bottom() - size + 1))


def taskbar_corner(area: QRect, geom: QRect, size: int, rng: random.Random,
                   x: int | None = None) -> QPoint:
    """Точка посадки на панель задач: x случайный (или заданный), лапки на её кромке."""
    lo, hi = area.left(), max(area.left(), area.right() - size + 1)
    x = rng.randint(lo, hi) if x is None else max(lo, min(x, hi))
    return QPoint(x, taskbar_y(area, geom, size))


# --- контроллер -----------------------------------------------------------


class WanderController(QObject):
    """Время от времени перелетает на верхнюю кромку случайного окна и жужжит в полёте.

    Сигнал landed(WindowInfo | None) — пчела долетела и села: на окно или (None)
    на панель задач.
    """

    landed = Signal(object)

    def __init__(self, bee, client=None, *, winapi_module: ModuleType = winapi,
                 own_pid: int | None = None, rng: random.Random | None = None,
                 min_s: float = WANDER_MIN_S, max_s: float = WANDER_MAX_S,
                 idle_focus_s: float = IDLE_FOCUS_S,
                 screens_provider: Callable[[], list[tuple[QRect, float]]] = qt_screens,
                 areas_provider: Callable[[], list[QRect]] = available_areas,
                 screen_provider: ScreenProvider = screen_at,
                 autostart: bool = True) -> None:
        super().__init__(bee)
        self._bee = bee
        self._client = client
        self._winapi = winapi_module
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._rng = rng or random.Random()
        self._min_s, self._max_s = min_s, max_s
        self._idle_focus_s = idle_focus_s
        self._screens_provider = screens_provider
        self._areas_provider = areas_provider
        self._screen_provider = screen_provider

        self.flying = False                      # летит ли пчела именно по нашей команде
        self.target: WindowInfo | None = None    # окно, к которому летим
        self.focus_planned = False               # переключить ли на него фокус по прилёту
        self.current_hwnd: int | None = None     # окно, на котором пчела сидит сейчас
        self.current_window: WindowInfo | None = None  # его снимок на момент посадки
        self._buzz_seq = 0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)

        bee.mood_changed.connect(self.on_mood_changed)
        bee.drag_started.connect(self.on_drag_started)
        if hasattr(bee, "focus_switch_toggled"):
            bee.focus_switch_toggled.connect(self.on_focus_switch_toggled)
        if client is not None:
            client.reply_ready.connect(self.on_reply)
        if autostart:
            self.schedule()

    # --- расписание --------------------------------------------------------

    def schedule(self) -> None:
        """Заводит таймер следующей прогулки на случайный срок."""
        delay = self._rng.uniform(self._min_s, self._max_s)
        self._timer.start(max(1, int(delay * 1000)))

    def _on_timer(self) -> None:
        try:
            self.wander_now()
        finally:
            self.schedule()

    # --- события -----------------------------------------------------------

    def _focus_switch_enabled(self) -> bool:
        return bool(getattr(self._bee, "focus_switch_enabled", True))

    def on_mood_changed(self, change: MoodChange) -> None:
        """Разозлилась посреди прогулки — прогулка отменяется (злостью ведает укус)."""
        if change.new is not Mood.ANGRY or not self.flying:
            return
        # Angry-high достижим только из ANGRY, а первое ANGRY нашу прогулку уже отменило;
        # на всякий случай чужой полёт (укус) не трогаем.
        self._abort("пчела разозлилась", cancel=not change.became_angry_high)

    def on_drag_started(self) -> None:
        self.current_hwnd = None
        self.current_window = None
        if self.flying:
            self._abort("пчелу взяли в руки", cancel=True)

    def on_focus_switch_toggled(self, enabled: bool) -> None:
        if not enabled and self.focus_planned:
            log.info("переключение окон выключено: просто сяду на окно")
            self.focus_planned = False

    def _abort(self, reason: str, cancel: bool) -> None:
        log.info("прогулка отменена: %s", reason)
        if cancel:
            self._bee.cancel_flight()
        self.flying = False
        self.target = None
        self.focus_planned = False

    # --- прогулка ----------------------------------------------------------

    def _user_idle(self) -> bool:
        return (self._winapi.AVAILABLE and self._focus_switch_enabled()
                and self._winapi.idle_seconds() >= self._idle_focus_s)

    def _refusal(self) -> str | None:
        if self._bee.engine.mood is Mood.ANGRY:
            return "пчела злится"
        if self._bee.is_dragging:
            return "пчелу тащат"
        if self._bee.is_flying:
            return "пчела уже летит"
        occupied = getattr(self._bee, "occupied_by", None)
        if occupied:
            return f"пчела занята ({occupied})"
        return None

    def _taskbar_corner(self, x: int | None = None) -> QPoint:
        """Посадка на панель задач экрана, где сейчас пчела."""
        area, geom = self._screen_provider(self._bee.geometry().center())
        return taskbar_corner(area, geom, self._bee.width(), self._rng, x)

    def land_nearest(self) -> bool:
        """Садится на ближайшую кромку окна, а если таких нет — на панель задач под собой.

        Нужна после того, как пчелу отпустили или она успокоилась. Окна не переключает
        и в полёте не жужжит (перелёт короткий). True, если полёт начат.
        """
        refuse = self._refusal()
        if refuse:
            log.debug("не сажусь: %s", refuse)
            return False
        screens = self._screens_provider()
        size = self._bee.width()
        center = self._bee.geometry().center()
        feet = QPoint(center.x(), self._bee.y() + feet_y(size))
        windows = self._winapi.list_windows() if self._winapi.AVAILABLE else []
        picked = pick_landing(windows, self._own_pid, self._rng,
                              physical_screen_rects(screens),
                              near=logical_to_physical(feet, screens),
                              headroom=headroom_px(screens, feet_y(size)))
        if picked is not None:
            target, spot = picked
            corner = landing_corner(spot, screens, self._areas_provider(), size)
            log.info("сажусь на ближайшее окно %r (%s, hwnd=%s)", target.title,
                     target.process_name, target.hwnd)
        else:
            target = None
            corner = self._taskbar_corner(self._bee.x())
            log.info("окон для посадки нет: сажусь на панель задач")
        self.target = target
        self.focus_planned = False
        self.flying = True
        self._bee.fly_to(corner, self._on_arrived)
        return True

    def wander_now(self, allow_focus: bool = True) -> bool:
        """Пробует улететь прямо сейчас; True, если полёт начат.

        allow_focus=False — не переключать окно даже в простое (пчела просто перелетает,
        например, потому что окно под ней исчезло).
        """
        refuse = self._refusal()
        if refuse:
            log.debug("не гуляю: %s", refuse)
            return False

        focus = allow_focus and self._user_idle()
        screens = self._screens_provider()
        areas = self._areas_provider()
        size = self._bee.width()
        exclude = None
        if focus:
            foreground = self._winapi.foreground_window()

            def exclude(win: WindowInfo) -> str | None:
                if win.hwnd == foreground:
                    return "уже активное окно"
                if win.elevated is not False:
                    return "права процесса администраторские или неизвестны"
                return None

        windows = self._winapi.list_windows() if self._winapi.AVAILABLE else []
        picked = pick_landing(windows, self._own_pid, self._rng,
                              physical_screen_rects(screens), self.current_hwnd, exclude,
                              headroom=headroom_px(screens, feet_y(size)))
        if picked is not None:
            target, spot = picked
            corner = landing_corner(spot, screens, areas, size)
            self.focus_planned = focus
            log.info("лечу на %r (%s, hwnd=%s)%s", target.title, target.process_name,
                     target.hwnd, ", потом переключу на него" if focus else "")
        else:
            target = None
            corner = self._taskbar_corner()
            self.focus_planned = False
            log.info("подходящих окон нет: лечу на панель задач")

        self.target = target
        self.flying = True
        self._bee.fly_to(corner, self._on_arrived)
        self._request_buzz()
        return True

    def _on_arrived(self) -> None:
        if not self.flying:
            return
        target, focus = self.target, self.focus_planned
        self.flying = False
        self.target = None
        self.focus_planned = False
        self.current_hwnd = target.hwnd if target is not None else None
        self.current_window = target
        if target is not None and focus:
            self._maybe_focus(target)
        self.landed.emit(target)

    def _focus_refusal(self, target: WindowInfo) -> str | None:
        """Причина не переключать окно (последняя проверка прямо перед действием)."""
        if not self._focus_switch_enabled():
            return "переключение окон выключено"
        if self._bee.engine.mood is Mood.ANGRY:
            return "пчела злится"
        if self._bee.is_dragging:
            return "пчелу взяли в руки"
        if self._winapi.idle_seconds() < self._idle_focus_s:
            return "пользователь вернулся"
        current = next((w for w in self._winapi.list_windows() if w.hwnd == target.hwnd), None)
        if current is None:
            return "окно исчезло"
        if current.minimized or current.cloaked:
            return "окно свёрнуто или скрыто"
        if current.elevated is not False:
            return "права процесса администраторские или неизвестны"
        if _moved(current.rect, target.rect) >= MAX_MOVE_PX:
            return "окно сдвинулось"
        if self._winapi.foreground_window() == target.hwnd:
            return "окно уже активно"
        return None

    def _maybe_focus(self, target: WindowInfo) -> None:
        reason = self._focus_refusal(target)
        if reason:
            log.info("не переключаю окно %r: %s", target.title, reason)
            return
        ok = self._winapi.focus_window(target.hwnd)
        log.info("переключила на окно %r (hwnd=%s): %s", target.title, target.hwnd,
                 "ok" if ok else "не удалось")

    # --- жужжание в полёте -------------------------------------------------

    def _request_buzz(self) -> None:
        if self._client is None:
            return
        self._buzz_seq += 1
        self._client.request(FLIGHT_PROMPT, tag=(WANDER_TAG, self._buzz_seq), remember=False)

    def on_reply(self, tag: object, text: str, from_model: bool) -> None:  # noqa: ARG002
        """Ответ на жужжание: показываем пузырь (и заготовку тоже), настроение не трогаем."""
        if tag != (WANDER_TAG, self._buzz_seq):
            return
        if self._bee.engine.mood is Mood.ANGRY:
            return
        self._bee.say(f"🐝 {text}", FLIGHT_BUZZ_MS)
