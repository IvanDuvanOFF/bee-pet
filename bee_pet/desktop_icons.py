"""Ярлыки на рабочем столе: чтение позиций и перенос (ctypes + COM). Только Windows.

Четвёртый и последний вид действий над чужим интерфейсом (после WM_CLOSE, переключения
фокуса и SC_MINIMIZE в `winapi`): пчела **только меняет позиции** значков на рабочем
столе. Ничего не удаляется, не переименовывается, не открывается и не запускается;
выделение не меняется (флаг SVSI_POSITIONITEM без SVSI_SELECT).

Путь документированный (Raymond Chen, «Manipulating the positions of desktop icons»),
COM без comtypes/pywin32 — сырыми таблицами виртуальных методов:

    CoCreateInstance(CLSID_ShellWindows) -> IShellWindows
    IShellWindows::FindWindowSW(CSIDL_DESKTOP, SWC_DESKTOP, SWFO_NEEDDISPATCH) -> IDispatch
    IServiceProvider::QueryService(SID_STopLevelBrowser, IID_IShellBrowser) -> IShellBrowser
    IShellBrowser::QueryActiveShellView -> IShellView -> QueryInterface(IFolderView)

Координаты. IFolderView::GetItemPosition отдаёт левый верхний угол ячейки значка
в клиентских координатах списка значков (SysListView32 внутри SHELLDLL_DefView),
а этот список растянут на весь виртуальный экран. Проводник работает с учётом DPI
каждого монитора, поэтому единицы — физические пиксели. Наружу модуль отдаёт и
принимает **физические экранные пиксели**: к позиции прибавляется начало списка
(GetWindowRect окна SysListView32, у трёх мониторов это левый верхний угол
виртуального экрана, x бывает отрицательным). Наш процесс тоже должен быть
DPI-aware (Qt 6 включает per-monitor v2), иначе GetWindowRect вернёт
масштабированные числа. В логические координаты Qt переводит вызывающий код
(`biting.physical_to_logical`).

Ключ значка — имя для разбора (SHGDN_FORPARSING): полный путь файла или ярлыка
(`C:\\Users\\…\\Desktop\\x.lnk`), а у виртуальных объектов — `::{CLSID}` (Корзина).
Он не меняется между запусками, поэтому годится для записи исходных позиций.

Все вызовы — из потока GUI (Qt уже инициализировал там OLE). CoInitializeEx
вызывается на всякий случай: RPC_E_CHANGED_MODE не ошибка, а при успехе вызов
уравновешивается CoUninitialize. Каждый COM-указатель освобождается (Release),
каждый PIDL — CoTaskMemFree. Любая ошибка COM лишь отменяет эту попытку и пишется в лог.
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger("bee_pet.desktop_icons")

AVAILABLE = sys.platform == "win32"

# Константы Shell API.
CSIDL_DESKTOP = 0x0000
SWC_DESKTOP = 0x00000008
SWFO_NEEDDISPATCH = 0x00000001
SVGIO_ALLVIEW = 0x00000002
SVSI_POSITIONITEM = 0x00000080   # только позиция: без SVSI_SELECT / SVSI_DESELECTOTHERS
SHGDN_NORMAL = 0x0000
SHGDN_FORPARSING = 0x8000
CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
VT_EMPTY, VT_I4 = 0, 3
S_OK, S_FALSE = 0, 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: поток уже в другой модели — это нормально


@dataclass(frozen=True)
class IconInfo:
    """Значок рабочего стола.

    key — имя для разбора (стабильный ключ), name — видимая подпись,
    x, y — левый верхний угол ячейки значка в физических экранных пикселях.
    """
    key: str
    name: str
    x: int
    y: int


if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    HRESULT = ctypes.c_long

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, text: str) -> "GUID":
            guid = cls()
            hr = _ole32.CLSIDFromString(text, ctypes.byref(guid))
            if hr != S_OK:
                raise OSError(f"CLSIDFromString({text}) = {hr:#x}")
            return guid

    class _VariantValue(ctypes.Union):
        _fields_ = [("lVal", ctypes.c_long), ("ptrs", ctypes.c_void_p * 2)]

    class VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("value", _VariantValue)]

    class STRRET(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("pOleStr", ctypes.c_void_p), ("uOffset", ctypes.c_uint),
                        ("cStr", ctypes.c_char * 260)]
        _fields_ = [("uType", ctypes.c_uint), ("u", _U)]

    _ole32 = ctypes.WinDLL("ole32")
    _shlwapi = ctypes.WinDLL("shlwapi")
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = HRESULT
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None
    _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    _ole32.CoCreateInstance.restype = HRESULT
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None
    _ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    _ole32.CLSIDFromString.restype = HRESULT
    # StrRetToStrW сама освобождает STRRET.pOleStr; результат освобождаем CoTaskMemFree.
    _shlwapi.StrRetToStrW.argtypes = [ctypes.POINTER(STRRET), ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_void_p)]
    _shlwapi.StrRetToStrW.restype = HRESULT
    _user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR]
    _user32.FindWindowExW.restype = wintypes.HWND
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL

    CLSID_ShellWindows = GUID.parse("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}")
    IID_IShellWindows = GUID.parse("{85CB6900-4D95-11CF-960C-0080C7F4EE85}")
    IID_IServiceProvider = GUID.parse("{6D5140C1-7436-11CE-8034-00AA006009FA}")
    SID_STopLevelBrowser = GUID.parse("{4C96BE40-915C-11CF-99D3-00AA004AE837}")
    IID_IShellBrowser = GUID.parse("{000214E2-0000-0000-C000-000000000046}")
    IID_IFolderView = GUID.parse("{CDE725B0-CCC9-4519-917E-325D72FAB4CE}")
    IID_IShellFolder = GUID.parse("{000214E6-0000-0000-C000-000000000046}")

    _P = ctypes.c_void_p
    _PP = ctypes.POINTER(ctypes.c_void_p)
    _PGUID = ctypes.POINTER(GUID)
    _PPOINT = ctypes.POINTER(wintypes.POINT)

    def _slot(index: int, *argtypes, restype=HRESULT):
        """Слот таблицы виртуальных методов: (номер, прототип с this первым аргументом)."""
        return index, ctypes.WINFUNCTYPE(restype, _P, *argtypes)

    # IUnknown
    _QueryInterface = _slot(0, _PGUID, _PP)
    _Release = _slot(2, restype=wintypes.ULONG)
    # IShellWindows : IDispatch (3–6) — 7 get_Count, 8 Item, 9 _NewEnum, 10 Register,
    # 11 RegisterPending, 12 Revoke, 13 OnNavigate, 14 OnActivated, 15 FindWindowSW
    _FindWindowSW = _slot(15, ctypes.POINTER(VARIANT), ctypes.POINTER(VARIANT), ctypes.c_int,
                          ctypes.POINTER(ctypes.c_long), ctypes.c_int, _PP)
    # IServiceProvider — 3 QueryService
    _QueryService = _slot(3, _PGUID, _PGUID, _PP)
    # IShellBrowser : IOleWindow (3 GetWindow, 4 ContextSensitiveHelp) — 5 InsertMenusSB,
    # 6 SetMenuSB, 7 RemoveMenusSB, 8 SetStatusTextSB, 9 EnableModelessSB,
    # 10 TranslateAcceleratorSB, 11 BrowseObject, 12 GetViewStateStream,
    # 13 GetControlWindow, 14 SendControlMsg, 15 QueryActiveShellView
    _QueryActiveShellView = _slot(15, _PP)
    # IShellView : IOleWindow — 3 GetWindow (окно SHELLDLL_DefView)
    _GetWindow = _slot(3, ctypes.POINTER(wintypes.HWND))
    # IFolderView — 3 GetCurrentViewMode, 4 SetCurrentViewMode, 5 GetFolder, 6 Item,
    # 7 ItemCount, 8 Items, 9 GetSelectionMarkedItem, 10 GetFocusedItem,
    # 11 GetItemPosition, 12 GetSpacing, 13 GetDefaultSpacing, 14 GetAutoArrange,
    # 15 SelectItem, 16 SelectAndPositionItems
    _GetFolder = _slot(5, _PGUID, _PP)
    _Item = _slot(6, ctypes.c_int, _PP)
    _ItemCount = _slot(7, ctypes.c_uint, ctypes.POINTER(ctypes.c_int))
    _GetItemPosition = _slot(11, _P, _PPOINT)
    _GetSpacing = _slot(12, _PPOINT)
    _GetAutoArrange = _slot(14)  # S_OK — автоупорядочение включено, S_FALSE — нет
    _SelectAndPositionItems = _slot(16, ctypes.c_uint, _PP, _PPOINT, wintypes.DWORD)
    # IShellFolder — 3 ParseDisplayName, 4 EnumObjects, 5 BindToObject, 6 BindToStorage,
    # 7 CompareIDs, 8 CreateViewObject, 9 GetAttributesOf, 10 GetUIObjectOf,
    # 11 GetDisplayNameOf
    _GetDisplayNameOf = _slot(11, _P, wintypes.DWORD, ctypes.POINTER(STRRET))

    def _call(obj: ctypes.c_void_p, slot, *args):
        index, proto = slot
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return proto(vtbl[index])(obj, *args)

    def _check(hr: int, what: str) -> None:
        if hr < 0:
            raise OSError(f"{what}: HRESULT {hr & 0xFFFFFFFF:#010x}")

    class _Desktop:
        """Открытые интерфейсы рабочего стола на время одной операции (см. _desktop)."""

        def __init__(self) -> None:
            self._owned: list[ctypes.c_void_p] = []
            self.view = ctypes.c_void_p()     # IFolderView
            self.folder = ctypes.c_void_p()   # IShellFolder
            self.origin = (0, 0)              # начало SysListView32, физические px

        def own(self, ptr: ctypes.c_void_p) -> ctypes.c_void_p:
            if ptr:
                self._owned.append(ptr)
            return ptr

        def release(self) -> None:
            while self._owned:
                ptr = self._owned.pop()
                try:
                    _call(ptr, _Release)
                except Exception:  # noqa: BLE001
                    log.debug("Release не удался", exc_info=True)

        def open(self) -> None:
            windows = ctypes.c_void_p()
            hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_ShellWindows), None, CLSCTX_ALL,
                                         ctypes.byref(IID_IShellWindows), ctypes.byref(windows))
            _check(hr, "CoCreateInstance(ShellWindows)")
            self.own(windows)
            loc, root = VARIANT(), VARIANT()
            loc.vt, loc.value.lVal = VT_I4, CSIDL_DESKTOP
            root.vt = VT_EMPTY
            hwnd = ctypes.c_long(0)
            disp = ctypes.c_void_p()
            hr = _call(windows, _FindWindowSW, ctypes.byref(loc), ctypes.byref(root),
                       SWC_DESKTOP, ctypes.byref(hwnd), SWFO_NEEDDISPATCH, ctypes.byref(disp))
            self.own(disp)
            if hr != S_OK or not disp:  # S_FALSE — окно рабочего стола не найдено
                raise OSError(f"FindWindowSW: HRESULT {hr & 0xFFFFFFFF:#010x}")
            provider = ctypes.c_void_p()
            _check(_call(disp, _QueryInterface, ctypes.byref(IID_IServiceProvider),
                         ctypes.byref(provider)), "QueryInterface(IServiceProvider)")
            self.own(provider)
            browser = ctypes.c_void_p()
            _check(_call(provider, _QueryService, ctypes.byref(SID_STopLevelBrowser),
                         ctypes.byref(IID_IShellBrowser), ctypes.byref(browser)),
                   "QueryService(IShellBrowser)")
            self.own(browser)
            shell_view = ctypes.c_void_p()
            _check(_call(browser, _QueryActiveShellView, ctypes.byref(shell_view)),
                   "QueryActiveShellView")
            self.own(shell_view)
            _check(_call(shell_view, _QueryInterface, ctypes.byref(IID_IFolderView),
                         ctypes.byref(self.view)), "QueryInterface(IFolderView)")
            self.own(self.view)
            _check(_call(self.view, _GetFolder, ctypes.byref(IID_IShellFolder),
                         ctypes.byref(self.folder)), "IFolderView::GetFolder")
            self.own(self.folder)
            defview = wintypes.HWND()
            _check(_call(shell_view, _GetWindow, ctypes.byref(defview)), "IShellView::GetWindow")
            self.origin = _list_origin(defview)

        def items(self):
            """Пары (pidl, ключ, подпись); pidl освобождает вызывающий (CoTaskMemFree)."""
            count = ctypes.c_int(0)
            _check(_call(self.view, _ItemCount, SVGIO_ALLVIEW, ctypes.byref(count)),
                   "IFolderView::ItemCount")
            for i in range(count.value):
                pidl = ctypes.c_void_p()
                if _call(self.view, _Item, i, ctypes.byref(pidl)) != S_OK or not pidl:
                    continue
                try:
                    key = self.display_name(pidl, SHGDN_FORPARSING)
                    name = self.display_name(pidl, SHGDN_NORMAL)
                except Exception:  # noqa: BLE001 — один кривой значок не роняет перечисление
                    log.debug("не удалось прочитать имя значка %d", i, exc_info=True)
                    _ole32.CoTaskMemFree(pidl)
                    continue
                yield pidl, key, name

        def display_name(self, pidl: ctypes.c_void_p, flags: int) -> str:
            ret = STRRET()
            _check(_call(self.folder, _GetDisplayNameOf, pidl, flags, ctypes.byref(ret)),
                   "IShellFolder::GetDisplayNameOf")
            out = ctypes.c_void_p()
            _check(_shlwapi.StrRetToStrW(ctypes.byref(ret), pidl, ctypes.byref(out)),
                   "StrRetToStrW")
            try:
                return ctypes.wstring_at(out.value) if out.value else ""
            finally:
                _ole32.CoTaskMemFree(out)

        def position(self, pidl: ctypes.c_void_p) -> tuple[int, int]:
            pt = wintypes.POINT()
            _check(_call(self.view, _GetItemPosition, pidl, ctypes.byref(pt)),
                   "IFolderView::GetItemPosition")
            return pt.x + self.origin[0], pt.y + self.origin[1]

    def _list_origin(defview: wintypes.HWND) -> tuple[int, int]:
        """Левый верхний угол списка значков (SysListView32) в физических экранных px."""
        target = _user32.FindWindowExW(defview, None, "SysListView32", None) or defview
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(target, ctypes.byref(rect)):
            raise OSError("GetWindowRect(SysListView32) не удался")
        return rect.left, rect.top

    @contextmanager
    def _desktop():
        """Открывает интерфейсы рабочего стола и гарантированно всё освобождает."""
        hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        initialized = hr in (S_OK, S_FALSE)
        if not initialized and hr != RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx: HRESULT {hr & 0xFFFFFFFF:#010x}")
        desk = _Desktop()
        try:
            desk.open()
            yield desk
        finally:
            desk.release()
            if initialized:
                _ole32.CoUninitialize()

    def list_icons() -> list[IconInfo]:
        """Значки рабочего стола с позициями (физические экранные px); [] при ошибке."""
        try:
            with _desktop() as desk:
                result: list[IconInfo] = []
                for pidl, key, name in desk.items():
                    try:
                        x, y = desk.position(pidl)
                        result.append(IconInfo(key, name, x, y))
                    except Exception:  # noqa: BLE001
                        log.debug("нет позиции у значка %r", name, exc_info=True)
                    finally:
                        _ole32.CoTaskMemFree(pidl)
                return result
        except Exception:  # noqa: BLE001 — сбой COM отменяет только эту попытку
            log.warning("не удалось прочитать значки рабочего стола", exc_info=True)
            return []

    class IconMover:
        """Держит рабочий стол открытым, пока пчела несёт один значок (переносы раз в ~100 мс).

        Открытие интерфейсов стоит ~5–25 мс, перенос через открытый — ~1 мс. Создаётся
        через open_mover, закрывается close() (освобождает PIDL, интерфейсы и COM).
        """

        def __init__(self, key: str) -> None:
            self.key = key
            self._pidl = ctypes.c_void_p()
            self._initialized = False
            self._desk: _Desktop | None = None
            hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            self._initialized = hr in (S_OK, S_FALSE)
            if not self._initialized and hr != RPC_E_CHANGED_MODE:
                raise OSError(f"CoInitializeEx: HRESULT {hr & 0xFFFFFFFF:#010x}")
            try:
                self._desk = _Desktop()
                self._desk.open()
                for pidl, item_key, _name in self._desk.items():
                    if not self._pidl and item_key == key:
                        self._pidl = pidl  # этот PIDL освободит close()
                    else:
                        _ole32.CoTaskMemFree(pidl)
                if not self._pidl:
                    raise LookupError(f"значка {key!r} нет на рабочем столе")
            except BaseException:
                self.close()
                raise

        def move(self, x: int, y: int) -> bool:
            """Ставит значок в точку (физические экранные px); только позиция, без выделения."""
            desk = self._desk
            if desk is None or not self._pidl:
                return False
            try:
                pidls = (ctypes.c_void_p * 1)(self._pidl.value)
                pt = wintypes.POINT(int(x) - desk.origin[0], int(y) - desk.origin[1])
                _check(_call(desk.view, _SelectAndPositionItems, 1, ctypes.cast(pidls, _PP),
                             ctypes.byref(pt), SVSI_POSITIONITEM),
                       "IFolderView::SelectAndPositionItems")
                return True
            except Exception:  # noqa: BLE001
                log.warning("не удалось передвинуть значок %r", self.key, exc_info=True)
                return False

        def close(self) -> None:
            """Освобождает PIDL и интерфейсы; повторный вызов ничего не делает."""
            if self._pidl:
                _ole32.CoTaskMemFree(self._pidl)
                self._pidl = ctypes.c_void_p()
            if self._desk is not None:
                self._desk.release()
                self._desk = None
            if self._initialized:
                self._initialized = False
                _ole32.CoUninitialize()

    def open_mover(key: str) -> IconMover | None:
        """Открывает перенос значка key; None, если значка нет или COM не отвечает."""
        try:
            return IconMover(key)
        except LookupError as exc:
            log.info("%s", exc)
        except Exception:  # noqa: BLE001
            log.warning("не удалось открыть рабочий стол для переноса %r", key, exc_info=True)
        return None

    def move_icon(key: str, x: int, y: int) -> bool:
        """Ставит значок key в точку (физические экранные px). Только позиция, без выделения."""
        mover = open_mover(key)
        if mover is None:
            return False
        try:
            return mover.move(x, y)
        finally:
            mover.close()

    def auto_arrange() -> bool | None:
        """Включено ли «Упорядочить значки автоматически»; None — узнать не удалось."""
        try:
            with _desktop() as desk:
                hr = _call(desk.view, _GetAutoArrange)
                _check(hr, "IFolderView::GetAutoArrange")
                return hr == S_OK
        except Exception:  # noqa: BLE001
            log.warning("не удалось узнать режим упорядочения значков", exc_info=True)
            return None

    def icon_spacing() -> tuple[int, int] | None:
        """Размер ячейки сетки значков (физические px); None — узнать не удалось."""
        try:
            with _desktop() as desk:
                pt = wintypes.POINT()
                _check(_call(desk.view, _GetSpacing, ctypes.byref(pt)), "IFolderView::GetSpacing")
                return pt.x, pt.y
        except Exception:  # noqa: BLE001
            log.warning("не удалось узнать шаг сетки значков", exc_info=True)
            return None

else:
    def list_icons() -> list[IconInfo]:
        """Вне Windows значков нет."""
        return []

    class IconMover:
        """Вне Windows переносить нечего (open_mover всегда отдаёт None)."""

        def move(self, x: int, y: int) -> bool:  # noqa: ARG002
            return False

        def close(self) -> None:
            pass

    def open_mover(key: str) -> IconMover | None:  # noqa: ARG001
        """Вне Windows значков нет."""
        return None

    def move_icon(key: str, x: int, y: int) -> bool:  # noqa: ARG001
        """Вне Windows ничего не делает."""
        return False

    def auto_arrange() -> bool | None:
        """Вне Windows неизвестно."""
        return None

    def icon_spacing() -> tuple[int, int] | None:
        """Вне Windows неизвестно."""
        return None
