"""Пчеле скучно — она таскает ярлыки по рабочему столу (выключатель в меню, по умолчанию выключен).

Раз в 1–3 минуты спокойная пчела выбирает случайный значок на одном из экранов,
подлетает к нему, недолго висит над ним, «берёт» и несёт на 100–300 px в сторону:
в пределах рабочей области того же экрана и не ближе одной ячейки сетки к другим
значкам. Значок едет за пчелой (позиция обновляется раз в ~100 мс), потом пчела
бросает его и садится на ближайшую кромку (WanderController.land_nearest).

Значки двигает только `desktop_icons` — меняются **лишь позиции**: ничего не
удаляется, не переименовывается и не открывается. Перед первым переносом значка его
исходная позиция записывается в QSettings (переживает перезапуск) и больше не
перезаписывается; «↩️ Вернуть ярлыки на место» ставит все записанные значки обратно.

Если на рабочем столе включено «Упорядочить значки автоматически», значки сразу
вернутся в сетку, поэтому пчела их не трогает (в лог пишет один раз).

Значок, закрытый окном. Сначала пчела ищет значок, у которого и ячейка, и место
назначения ничем не закрыты, — его она несёт сразу. Если таких нет, она может
свернуть окна, которые закрывают ячейку значка или место назначения: не больше
MAX_MINIMIZE за попытку и только «сворачиваемые» (не свои, не системные классы,
не от имени администратора: `elevated is not False` значит «не трогать»). Пчела
подлетает к заголовку окна, «толкает» его (короткий выпад, как при укусе) и вежливо
просит свернуться — `winapi.minimize_window` (SC_MINIMIZE, как кнопка в заголовке).
Потом ждёт, пока окно правда свернётся; не свернулось — попытка отменяется, значок
не трогается. Свёрнутые окна обратно не разворачиваются. Это единственное место,
где пчела сворачивает чужие окна, и только при включённой шалости с ярлыками.

Перенос отменяется, если пчелу схватили, она разозлилась или выключатель сняли:
значок остаётся там, где его застала отмена (его исходная позиция уже записана).
Отмена на этапе сворачивания окон останавливает и его: после отмены пчела больше
ничего не сворачивает.
Пока идёт перенос, `bee.occupied_by` занят: прогулки и ходьба пчелу не двигают.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
from types import ModuleType
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QTimer

from bee_pet import desktop_icons, winapi
from bee_pet.biting import BLOCKED_CLASSES, Rect, _intersects, physical_to_logical, qt_screens
from bee_pet.desktop_icons import IconInfo
from bee_pet.mood import Mood, MoodChange
from bee_pet.wander import LANDING_INSET, available_areas, landing_corner, logical_to_physical
from bee_pet.winapi import WindowInfo

log = logging.getLogger("bee_pet.icon_prank")

# Тайминги и расстояния по умолчанию (в тестах подменяются через конструктор).
PRANK_MIN_S = 60.0
PRANK_MAX_S = 180.0
CARRY_PX = (100, 300)      # на сколько унести значок (логические px)
FOLLOW_MS = 100            # как часто значок догоняет пчелу
HOVER_MS = 600             # сколько висеть над значком, прежде чем взять
CARRY_SPEED = 150.0        # px/с: с ношей пчела летит медленнее
LAND_RETRY_MS = 300        # посадка не удалась (пчела занята) — повторить через
DEST_TRIES = 40            # сколько случайных точек назначения перебрать
DEFAULT_CELL = (96, 96)    # ячейка сетки (физ. px), если её не удалось узнать
MAX_MINIMIZE = 2           # больше окон ради одного значка не сворачивать
DEST_COVER_TRIES = 6       # сколько мест назначения перебрать на значок, ища незакрытое
PUSH_MS = 400              # сколько пчела «толкает» окно, прежде чем его свернуть
MINIMIZE_WAIT_MS = 1500    # сколько ждать, пока окно свернётся
MINIMIZE_POLL_MS = 100     # как часто проверять, свернулось ли
OCCUPIED = "таскает ярлык"

SETTINGS_ENABLED = "icon_prank_enabled"
SETTINGS_ORIGINALS = "icon_prank_originals"  # JSON: {ключ значка: [x, y]} физ. px


# --- геометрия ------------------------------------------------------------


def grip_offset(size: int) -> int:
    """На сколько ниже центра окна пчелы висит центр значка, который она несёт."""
    return size // 4


def bee_corner(grip: QPoint, size: int) -> QPoint:
    """Левый верхний угол окна пчелы, чтобы центр значка пришёлся на точку grip."""
    return QPoint(grip.x() - size // 2, grip.y() - size // 2 - grip_offset(size))


def bee_grip(pos: QPoint, size: int) -> QPoint:
    """Где центр значка, который несёт пчела с левым верхним углом окна в pos."""
    return QPoint(pos.x() + size // 2, pos.y() + size // 2 + grip_offset(size))


def icon_center(icon: IconInfo, cell: tuple[int, int]) -> tuple[int, int]:
    """Центр ячейки значка в физических px."""
    return icon.x + cell[0] // 2, icon.y + cell[1] // 2


def logical_cell(cell: tuple[int, int], point: QPoint,
                 screens: Sequence[tuple[QRect, float]]) -> tuple[int, int]:
    """Ячейка сетки (физ. px) в логических px экрана, где точка point."""
    dpr = next((d for g, d in screens if g.contains(point)), screens[0][1] if screens else 1.0)
    return max(1, round(cell[0] / dpr)), max(1, round(cell[1] / dpr))


def too_close(point: QPoint, others: Sequence[QPoint], cell: tuple[int, int]) -> bool:
    """Ближе ли точка к какому-нибудь центру из others, чем на одну ячейку (по обеим осям)."""
    return any(abs(point.x() - o.x()) < cell[0] and abs(point.y() - o.y()) < cell[1]
               for o in others)


def pick_destination(start: QPoint, area: QRect, others: Sequence[QPoint],
                     cell: tuple[int, int], rng: random.Random,
                     dist: tuple[int, int] = CARRY_PX, tries: int = DEST_TRIES) -> QPoint | None:
    """Новый центр значка: в dist от start, ячейка целиком в area, не у других значков.

    Все координаты логические. None — подходящего места не нашлось.
    """
    half_w, half_h = cell[0] // 2, cell[1] // 2
    lo_x, hi_x = area.left() + half_w, area.right() + 1 - (cell[0] - half_w)
    lo_y, hi_y = area.top() + half_h, area.bottom() + 1 - (cell[1] - half_h)
    if hi_x < lo_x or hi_y < lo_y:
        return None
    for _ in range(tries):
        angle = rng.uniform(0.0, 2 * math.pi)
        d = rng.uniform(*dist)
        x = max(lo_x, min(round(start.x() + d * math.cos(angle)), hi_x))
        y = max(lo_y, min(round(start.y() + d * math.sin(angle)), hi_y))
        point = QPoint(x, y)
        if math.hypot(x - start.x(), y - start.y()) < dist[0]:
            continue  # упёрлись в край экрана: вышло слишком близко
        if too_close(point, others, cell):
            continue
        return point
    return None


# --- окна поверх значков ---------------------------------------------------


def icon_rect(x: int, y: int, cell: tuple[int, int]) -> Rect:
    """Прямоугольник ячейки значка (физ. px) по её левому верхнему углу."""
    return x, y, x + cell[0], y + cell[1]


def can_cover(win: WindowInfo, own_pid: int) -> bool:
    """Может ли окно закрывать значки: видимое, не своё, не рабочий стол и не панель задач."""
    return (win.pid != own_pid and not win.minimized and not win.cloaked and not win.is_tool
            and win.class_name not in BLOCKED_CLASSES)


def covering_windows(rect: Rect, windows: Sequence[WindowInfo], own_pid: int) -> list[WindowInfo]:
    """Окна, которые закрывают прямоугольник rect (физ. px), в порядке windows (сверху вниз).

    Значки рабочего стола всегда под окнами, поэтому Z-порядок между окнами не важен:
    закрывает любое пересекающееся окно, кроме своих, свёрнутых, cloaked, tool-окон
    и системных классов (Progman, WorkerW, панель задач).
    """
    return [w for w in windows if can_cover(w, own_pid) and _intersects(w.rect, rect)]


def minimize_skip_reason(win: WindowInfo, own_pid: int) -> str | None:
    """Почему окно нельзя свернуть ради значка; None, если можно."""
    if win.pid == own_pid:
        return "окно собственного процесса"
    if win.class_name in BLOCKED_CLASSES:
        return f"системный класс {win.class_name}"
    if win.elevated is not False:
        return "права процесса администраторские или неизвестны"
    return None


def title_spot(win: WindowInfo, near_x: int) -> tuple[int, int]:
    """Точка на верхней кромке окна (физ. px), ближайшая к near_x (не у самых краёв)."""
    left, top, right, _ = win.rect
    inset = int((right - left) * LANDING_INSET)
    return max(left + inset, min(near_x, right - 1 - inset)), top + 1


# --- контроллер -----------------------------------------------------------


class IconPrankController(QObject):
    """От скуки переносит один значок рабочего стола; умеет вернуть все значки на место."""

    IDLE, TO_ICON, HOVER, CARRYING = "idle", "to_icon", "hover", "carrying"
    # Сворачивание окон, закрывающих значок: летит к заголовку, толкает, ждёт.
    TO_WINDOW, PUSHING, MINIMIZING = "to_window", "pushing", "minimizing"

    def __init__(self, bee, wander=None, bite_controller=None, *,
                 icons_module: ModuleType = desktop_icons,
                 winapi_module: ModuleType = winapi, own_pid: int | None = None,
                 settings: QSettings | None = None,
                 rng: random.Random | None = None,
                 min_s: float = PRANK_MIN_S, max_s: float = PRANK_MAX_S,
                 carry_px: tuple[int, int] = CARRY_PX, follow_ms: int = FOLLOW_MS,
                 hover_ms: int = HOVER_MS, carry_speed: float = CARRY_SPEED,
                 land_retry_ms: int = LAND_RETRY_MS, fly_speed: float | None = None,
                 push_ms: int = PUSH_MS, minimize_wait_ms: int = MINIMIZE_WAIT_MS,
                 minimize_poll_ms: int = MINIMIZE_POLL_MS,
                 screens_provider: Callable[[], list[tuple[QRect, float]]] = qt_screens,
                 areas_provider: Callable[[], list[QRect]] = available_areas,
                 autostart: bool = True) -> None:
        super().__init__(bee)
        self._bee, self._wander, self._bite = bee, wander, bite_controller
        self._icons = icons_module
        self._winapi = winapi_module
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._fly_speed = fly_speed
        self._push_ms = push_ms
        self._minimize_wait_ms = minimize_wait_ms
        self._settings = settings if settings is not None else bee.settings
        self._rng = rng or random.Random()
        self._min_s, self._max_s = min_s, max_s
        self._carry_px = carry_px
        self._hover_ms = hover_ms
        self._carry_speed = carry_speed
        self._land_retry_ms = land_retry_ms
        self._screens_provider = screens_provider
        self._areas_provider = areas_provider

        self.state = self.IDLE
        self.pranks = 0                        # сколько значков перенесено (для логов и тестов)
        self.icon: IconInfo | None = None      # значок, который несём
        self.destination: QPoint | None = None  # куда (логический центр значка)
        self.to_minimize: list[WindowInfo] = []  # окна, которые ещё надо свернуть
        self.minimized: list[WindowInfo] = []    # окна, свёрнутые в этой попытке
        self._start_point: QPoint | None = None  # логический центр значка до переноса
        self._pushing = False                    # пчела делает выпад (bee.stinging — наш)
        self._wait_left = 0
        self._cell = DEFAULT_CELL               # ячейка сетки, физ. px
        self._mover = None
        self._auto_arrange_logged = False
        self._last_refusal: str | None = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._on_hover_done)
        self._follow_timer = QTimer(self)
        self._follow_timer.setInterval(follow_ms)
        self._follow_timer.timeout.connect(self._follow)
        self._land_timer = QTimer(self)
        self._land_timer.setSingleShot(True)
        self._land_timer.timeout.connect(self._land)
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.timeout.connect(self._on_push_done)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(minimize_poll_ms)
        self._poll_timer.timeout.connect(self._poll_minimized)

        bee.mood_changed.connect(self.on_mood_changed)
        bee.drag_started.connect(self.on_drag_started)
        if hasattr(bee, "icon_prank_toggled"):
            bee.icon_prank_toggled.connect(self.on_toggled)
        if hasattr(bee, "icon_restore_requested"):
            bee.icon_restore_requested.connect(self.restore)
        if hasattr(bee, "set_icon_prank_available"):
            bee.set_icon_prank_available(bool(self._icons.AVAILABLE))
        self._update_restorable()
        if autostart:
            self.schedule()

    # --- расписание --------------------------------------------------------

    def schedule(self) -> None:
        """Заводит таймер следующего приступа скуки на случайный срок."""
        delay = self._rng.uniform(self._min_s, self._max_s)
        self._timer.start(max(1, int(delay * 1000)))

    def _on_timer(self) -> None:
        try:
            if self._enabled():
                self.prank_now()
        finally:
            self.schedule()

    # --- состояние ---------------------------------------------------------

    @property
    def active(self) -> bool:
        """Идёт ли перенос (от вылета к значку до броска)."""
        return self.state != self.IDLE

    def _enabled(self) -> bool:
        return bool(getattr(self._bee, "icon_prank_enabled", False))

    def _refusal(self) -> str | None:
        """Почему сейчас нельзя таскать значки; None, если можно."""
        if not self._icons.AVAILABLE:
            return "значки рабочего стола недоступны (не Windows)"
        if not self._enabled():
            return "выключено в меню"
        if self.active:
            return "уже несу значок"
        if self._bee.engine.mood is Mood.ANGRY:
            return "пчела злится"
        if self._bee.is_dragging:
            return "пчелу тащат"
        if self._bee.is_flying:
            return "пчела летит"
        if self._bite is not None and self._bite.state != self._bite.IDLE:
            return "идёт укус"
        if getattr(self._bee, "occupied_by", None):
            return f"пчела занята ({self._bee.occupied_by})"
        arrange = self._icons.auto_arrange()
        if arrange is None:
            return "не удалось узнать режим упорядочения значков"
        if arrange:
            if not self._auto_arrange_logged:
                log.info("на рабочем столе включено автоупорядочение значков: не таскаю "
                         "(значки сразу вернутся в сетку)")
                self._auto_arrange_logged = True
            return "автоупорядочение значков"
        self._auto_arrange_logged = False
        return None

    def _refuse(self, reason: str) -> None:
        if reason != self._last_refusal:
            log.info("не таскаю ярлыки: %s", reason)
        else:
            log.debug("не таскаю ярлыки: %s", reason)
        self._last_refusal = reason

    # --- запись исходных позиций ------------------------------------------

    def originals(self) -> dict[str, tuple[int, int]]:
        """Записанные исходные позиции значков (физ. px)."""
        raw = self._settings.value(SETTINGS_ORIGINALS, "", type=str)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {str(k): (int(v[0]), int(v[1])) for k, v in data.items()}
        except (ValueError, TypeError, IndexError, AttributeError):
            log.warning("запись исходных позиций значков испорчена: начинаю заново")
            return {}

    def _save_originals(self, data: dict[str, tuple[int, int]]) -> None:
        if data:
            self._settings.setValue(SETTINGS_ORIGINALS,
                                    json.dumps({k: list(v) for k, v in data.items()},
                                               ensure_ascii=False))
        else:
            self._settings.remove(SETTINGS_ORIGINALS)
        self._settings.sync()
        self._update_restorable()

    def _remember(self, icon: IconInfo) -> None:
        """Записывает исходную позицию значка, если её ещё нет (первый перенос)."""
        data = self.originals()
        if icon.key in data:
            return
        data[icon.key] = (icon.x, icon.y)
        log.info("запомнила место значка %r: (%d, %d)", icon.name, icon.x, icon.y)
        self._save_originals(data)

    def _update_restorable(self) -> None:
        if hasattr(self._bee, "set_icons_restorable"):
            self._bee.set_icons_restorable(bool(self.originals()))

    # --- события -----------------------------------------------------------

    def on_mood_changed(self, change: MoodChange) -> None:
        """Разозлилась — бросает значок где есть (злостью ведают frenzy и укус)."""
        if change.new is not Mood.ANGRY or not self.active:
            return
        # Angry high стартует укус из того же сигнала: его полёт не обрываем.
        self._abort("пчела разозлилась", cancel_flight=not change.became_angry_high, land=False)

    def on_drag_started(self) -> None:
        if self.active:
            self._abort("пчелу взяли в руки", cancel_flight=False, land=False)

    def on_toggled(self, enabled: bool) -> None:
        if not enabled and self.active:
            self._abort("таскание ярлыков выключено", cancel_flight=True, land=True)

    # --- перенос -----------------------------------------------------------

    def _windows(self) -> list[WindowInfo]:
        return self._winapi.list_windows() if self._winapi.AVAILABLE else []

    def _dest_rect(self, dest: QPoint, screens) -> Rect:
        """Ячейка (физ. px), куда встанет значок с логическим центром dest."""
        return icon_rect(*self._icon_point(dest, screens), self._cell)

    def _blockers(self, src: Rect, dst: Rect, windows: Sequence[WindowInfo]) -> list[WindowInfo]:
        """Окна, закрывающие ячейку значка или место назначения (без повторов)."""
        result: list[WindowInfo] = []
        for w in (covering_windows(src, windows, self._own_pid)
                  + covering_windows(dst, windows, self._own_pid)):
            if all(w.hwnd != r.hwnd for r in result):
                result.append(w)
        return result

    def _minimize_refusal(self, blockers: Sequence[WindowInfo]) -> str | None:
        """Почему ради значка нельзя свернуть эти окна; None, если можно."""
        if len(blockers) > MAX_MINIMIZE:
            return f"закрыт окнами: {len(blockers)} (сворачиваю не больше {MAX_MINIMIZE})"
        for w in blockers:
            reason = minimize_skip_reason(w, self._own_pid)
            if reason:
                return f"окно {w.title!r} ({w.process_name}) не свернуть: {reason}"
        return None

    def prank_now(self) -> bool:
        """Пробует утащить значок прямо сейчас; True, если пчела полетела (к значку или окну)."""
        reason = self._refusal()
        if reason:
            self._refuse(reason)
            return False
        icons = self._icons.list_icons()
        if not icons:
            self._refuse("значков на рабочем столе нет")
            return False
        self._cell = self._icons.icon_spacing() or DEFAULT_CELL
        screens = self._screens_provider()
        areas = self._areas_provider()
        windows = self._windows()
        centers = {i.key: physical_to_logical(icon_center(i, self._cell), screens) for i in icons}
        order = list(icons)
        self._rng.shuffle(order)
        fallback: tuple[IconInfo, QPoint, QPoint, list[WindowInfo]] | None = None
        refusals: list[str] = []
        for icon in order:
            start = centers[icon.key]
            area = next((a for a in areas if a.contains(start)), None)
            if area is None:
                log.debug("значок %r не на экране", icon.name)
                continue
            cell = logical_cell(self._cell, start, screens)
            others = [c for k, c in centers.items() if k != icon.key]
            src = icon_rect(icon.x, icon.y, self._cell)
            found = False
            for _ in range(DEST_COVER_TRIES):
                dest = pick_destination(start, area, others, cell, self._rng, self._carry_px)
                if dest is None:
                    break
                found = True
                blockers = self._blockers(src, self._dest_rect(dest, screens), windows)
                if not blockers:
                    self._start(icon, start, dest)  # ничем не закрыт — несём сразу
                    return True
                if fallback is None:
                    refusal = self._minimize_refusal(blockers)
                    if refusal is None:
                        fallback = (icon, start, dest, blockers)
                    else:
                        refusals.append(f"{icon.name!r}: {refusal}")
            if not found:
                log.debug("значку %r некуда переехать", icon.name)
        if fallback is not None:
            self._start(*fallback)
            return True
        for line in refusals[:5]:
            log.debug("значок закрыт окнами: %s", line)
        if refusals:
            self._refuse("значки закрыты окнами, которые нельзя свернуть "
                         f"(или их больше {MAX_MINIMIZE})")
        else:
            self._refuse("ни один значок некуда унести")
        return False

    def _start(self, icon: IconInfo, start: QPoint, dest: QPoint,
               blockers: Sequence[WindowInfo] = ()) -> None:
        self._last_refusal = None
        self.icon, self.destination, self._start_point = icon, dest, start
        self.minimized = []
        self._bee.occupied_by = OCCUPIED
        if blockers:
            self.to_minimize = list(blockers)
            log.info("скучно: значок %r закрыт окнами (%s) — сначала сверну их", icon.name,
                     ", ".join(f"{w.title!r} ({w.process_name})" for w in blockers))
            self._fly_to_window()
            return
        self._fly_to_icon()

    def _fly_to_icon(self) -> None:
        icon, dest, start = self.icon, self.destination, self._start_point
        assert icon is not None and dest is not None and start is not None
        log.info("скучно: несу значок %r из (%d, %d) в логическую точку (%d, %d)",
                 icon.name, icon.x, icon.y, dest.x(), dest.y())
        self.state = self.TO_ICON
        self._bee.fly_to(bee_corner(start, self._bee.width()), self._on_reached_icon,
                         speed=self._fly_speed)

    # --- сворачивание окон над значком ------------------------------------

    def _fly_to_window(self) -> None:
        """Летит к заголовку следующего окна, которое надо свернуть."""
        win, icon = self.to_minimize[0], self.icon
        assert icon is not None
        near_x = icon_center(icon, self._cell)[0]
        corner = landing_corner(title_spot(win, near_x), self._screens_provider(),
                                self._areas_provider(), self._bee.width())
        log.info("лечу толкать окно %r (%s, hwnd=%s)", win.title, win.process_name, win.hwnd)
        self.state = self.TO_WINDOW
        self._bee.fly_to(corner, self._on_reached_window, speed=self._fly_speed)

    def _on_reached_window(self) -> None:
        if self.state != self.TO_WINDOW:
            return
        self.state = self.PUSHING
        self._pushing = True
        self._bee.stinging = True  # выпад, как при укусе: пчела «толкает» окно
        self._bee.update()
        self._push_timer.start(self._push_ms)

    def _stop_push(self) -> None:
        if self._pushing:
            self._pushing = False
            self._bee.stinging = False
            self._bee.update()

    def _current(self, hwnd: int) -> WindowInfo | None:
        return next((w for w in self._windows() if w.hwnd == hwnd), None)

    def _still_ok(self) -> str | None:
        """Причина прервать попытку (проверка прямо перед сворачиванием) или None."""
        if not self._enabled():
            return "таскание ярлыков выключено"
        if self._bee.engine.mood is Mood.ANGRY:
            return "пчела злится"
        if self._bee.is_dragging:
            return "пчелу взяли в руки"
        return None

    def _on_push_done(self) -> None:
        if self.state != self.PUSHING:
            return
        self._stop_push()
        reason = self._still_ok()
        if reason:
            self._abort(reason, cancel_flight=True, land=True)
            return
        win = self.to_minimize[0]
        current = self._current(win.hwnd)
        if current is None or current.minimized:
            log.info("окно %r уже свёрнуто или исчезло", win.title)
            self._next_window()
            return
        refusal = minimize_skip_reason(current, self._own_pid)
        if refusal:
            self._abort(f"окно {win.title!r} нельзя свернуть: {refusal}",
                        cancel_flight=True, land=True)
            return
        ok = self._winapi.minimize_window(win.hwnd)
        log.info("сворачиваю окно %r (%s, hwnd=%s): %s", win.title, win.process_name,
                 win.hwnd, "SC_MINIMIZE отправлен" if ok else "не удалось отправить")
        if not ok:
            self._abort(f"окно {win.title!r} не удалось свернуть", cancel_flight=True, land=True)
            return
        self.state = self.MINIMIZING
        self._wait_left = self._minimize_wait_ms
        self._poll_timer.start()

    def _poll_minimized(self) -> None:
        if self.state != self.MINIMIZING:
            self._poll_timer.stop()
            return
        win = self.to_minimize[0]
        current = self._current(win.hwnd)
        if current is None or current.minimized:
            self._poll_timer.stop()
            log.info("окно %r (%s) свернулось", win.title, win.process_name)
            self.minimized.append(win)
            self._next_window()
            return
        self._wait_left -= self._poll_timer.interval()
        if self._wait_left <= 0:
            self._poll_timer.stop()
            self._abort(f"окно {win.title!r} ({win.process_name}) не свернулось",
                        cancel_flight=True, land=True)

    def _next_window(self) -> None:
        """Следующее окно, а когда все свёрнуты — перепроверка и полёт к значку."""
        self.to_minimize.pop(0)
        if self.to_minimize:
            self._fly_to_window()
            return
        icon, dest = self.icon, self.destination
        assert icon is not None and dest is not None
        current = next((i for i in self._icons.list_icons() if i.key == icon.key), None)
        if current is None or (current.x, current.y) != (icon.x, icon.y):
            self._abort("значок исчез или сдвинулся", cancel_flight=True, land=True)
            return
        blockers = self._blockers(icon_rect(icon.x, icon.y, self._cell),
                                  self._dest_rect(dest, self._screens_provider()),
                                  self._windows())
        if blockers:
            self._abort("значок всё ещё закрыт: " + ", ".join(
                f"{w.title!r} ({w.process_name})" for w in blockers),
                cancel_flight=True, land=True)
            return
        self._fly_to_icon()

    def _on_reached_icon(self) -> None:
        if self.state != self.TO_ICON:
            return
        self.state = self.HOVER
        self._hover_timer.start(self._hover_ms)

    def _on_hover_done(self) -> None:
        if self.state != self.HOVER:
            return
        icon, dest = self.icon, self.destination
        assert icon is not None and dest is not None
        mover = self._icons.open_mover(icon.key)
        if mover is None:
            self._finish("значок не удалось взять", land=True)
            return
        self._remember(icon)  # до первого переноса
        self._mover = mover
        self.state = self.CARRYING
        self._follow_timer.start()
        self._bee.fly_to(bee_corner(dest, self._bee.width()), self._on_carried,
                         speed=self._carry_speed)

    def _icon_point(self, grip: QPoint, screens=None) -> tuple[int, int]:
        """Левый верхний угол ячейки значка (физ. px), чей центр — логическая точка grip."""
        px, py = logical_to_physical(grip, screens if screens is not None
                                     else self._screens_provider())
        return px - self._cell[0] // 2, py - self._cell[1] // 2

    def _follow(self) -> None:
        """Значок догоняет пчелу."""
        if self.state != self.CARRYING or self._mover is None:
            self._follow_timer.stop()
            return
        grip = bee_grip(self._bee.pos(), self._bee.width())
        if not self._mover.move(*self._icon_point(grip)):
            self._abort("значок перестал двигаться", cancel_flight=True, land=True)

    def _on_carried(self) -> None:
        if self.state != self.CARRYING or self._mover is None:
            return
        assert self.destination is not None
        ok = self._mover.move(*self._icon_point(self.destination))
        self.pranks += 1
        self._finish("значок брошен" if ok else "значок не удалось поставить", land=True)

    def _abort(self, reason: str, *, cancel_flight: bool, land: bool) -> None:
        if cancel_flight and self._bee.is_flying:
            self._bee.cancel_flight()
        self._finish(f"перенос отменён: {reason}", land=land)

    def _finish(self, reason: str, *, land: bool) -> None:
        """Конец переноса: значок остаётся, где есть; пчела садится (если land)."""
        log.info("%s", reason)
        self._hover_timer.stop()
        self._follow_timer.stop()
        self._push_timer.stop()
        self._poll_timer.stop()
        self._stop_push()
        self.to_minimize = []
        self._start_point = None
        if self._mover is not None:
            try:
                self._mover.close()
            finally:
                self._mover = None
        self.state = self.IDLE
        self.icon = self.destination = None
        if getattr(self._bee, "occupied_by", None) == OCCUPIED:
            self._bee.occupied_by = None
        if land:
            self._land()

    def _land(self) -> None:
        """Садится на ближайшую кромку; пчела занята — попробует чуть позже."""
        if self._wander is None or self.active:
            return
        if self._bee.engine.mood is Mood.ANGRY or self._bee.is_dragging:
            return  # злостью ведает frenzy, после переноса посадит WalkController
        if not self._wander.land_nearest():
            self._land_timer.start(self._land_retry_ms)

    # --- возврат на место --------------------------------------------------

    def restore(self) -> int:
        """Ставит все записанные значки на исходные места; сколько поставлено."""
        data = self.originals()
        if not data:
            return 0
        if self.active:
            self._abort("возвращаю ярлыки на место", cancel_flight=True, land=True)
        current = {i.key for i in self._icons.list_icons()}
        if not current:
            log.warning("значки рабочего стола не прочитались: запись не трогаю")
            return 0
        left: dict[str, tuple[int, int]] = {}
        moved = 0
        for key, (x, y) in data.items():
            if key not in current:
                log.info("значка %r больше нет: пропускаю", key)
                continue
            if self._icons.move_icon(key, x, y):
                moved += 1
            else:
                left[key] = (x, y)  # не вышло — попробуем в следующий раз
        log.info("вернула на место значков: %d", moved)
        self._save_originals(left)
        return moved
