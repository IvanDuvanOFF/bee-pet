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

Перенос отменяется, если пчелу схватили, она разозлилась или выключатель сняли:
значок остаётся там, где его застала отмена (его исходная позиция уже записана).
Пока идёт перенос, `bee.occupied_by` занят: прогулки и ходьба пчелу не двигают.
"""
from __future__ import annotations

import json
import logging
import math
import random
from types import ModuleType
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QTimer

from bee_pet import desktop_icons
from bee_pet.biting import physical_to_logical, qt_screens
from bee_pet.desktop_icons import IconInfo
from bee_pet.mood import Mood, MoodChange
from bee_pet.wander import available_areas, logical_to_physical

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


# --- контроллер -----------------------------------------------------------


class IconPrankController(QObject):
    """От скуки переносит один значок рабочего стола; умеет вернуть все значки на место."""

    IDLE, TO_ICON, HOVER, CARRYING = "idle", "to_icon", "hover", "carrying"

    def __init__(self, bee, wander=None, bite_controller=None, *,
                 icons_module: ModuleType = desktop_icons,
                 settings: QSettings | None = None,
                 rng: random.Random | None = None,
                 min_s: float = PRANK_MIN_S, max_s: float = PRANK_MAX_S,
                 carry_px: tuple[int, int] = CARRY_PX, follow_ms: int = FOLLOW_MS,
                 hover_ms: int = HOVER_MS, carry_speed: float = CARRY_SPEED,
                 land_retry_ms: int = LAND_RETRY_MS,
                 screens_provider: Callable[[], list[tuple[QRect, float]]] = qt_screens,
                 areas_provider: Callable[[], list[QRect]] = available_areas,
                 autostart: bool = True) -> None:
        super().__init__(bee)
        self._bee, self._wander, self._bite = bee, wander, bite_controller
        self._icons = icons_module
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

    def prank_now(self) -> bool:
        """Пробует утащить значок прямо сейчас; True, если пчела полетела к нему."""
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
        centers = {i.key: physical_to_logical(icon_center(i, self._cell), screens) for i in icons}
        order = list(icons)
        self._rng.shuffle(order)
        for icon in order:
            start = centers[icon.key]
            area = next((a for a in areas if a.contains(start)), None)
            if area is None:
                log.debug("значок %r не на экране", icon.name)
                continue
            cell = logical_cell(self._cell, start, screens)
            others = [c for k, c in centers.items() if k != icon.key]
            dest = pick_destination(start, area, others, cell, self._rng, self._carry_px)
            if dest is None:
                log.debug("значку %r некуда переехать", icon.name)
                continue
            self._start(icon, start, dest)
            return True
        self._refuse("ни один значок некуда унести")
        return False

    def _start(self, icon: IconInfo, start: QPoint, dest: QPoint) -> None:
        self._last_refusal = None
        log.info("скучно: несу значок %r из (%d, %d) в логическую точку (%d, %d)",
                 icon.name, icon.x, icon.y, dest.x(), dest.y())
        self.icon, self.destination = icon, dest
        self.state = self.TO_ICON
        self._bee.occupied_by = OCCUPIED
        self._bee.fly_to(bee_corner(start, self._bee.width()), self._on_reached_icon)

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

    def _icon_point(self, grip: QPoint) -> tuple[int, int]:
        """Левый верхний угол ячейки значка (физ. px), чей центр — логическая точка grip."""
        px, py = logical_to_physical(grip, self._screens_provider())
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
