"""Работа с чужими окнами через WinAPI (ctypes). Только Windows; на других ОС — заглушки.

Действий над чужими окнами ровно два:
- вежливое `PostMessageW(WM_CLOSE)` в `close_window` (злая пчела кусает окно);
- вывод окна на передний план в `focus_window` (пчела переключает окна, пока
  пользователя нет за компьютером).
Никаких TerminateProcess, DestroyWindow, taskkill, WM_QUIT и эмуляции нажатий
клавиш (keybd_event, SendInput) здесь нет и быть не должно.

Третий (и последний) вид действий над чужим интерфейсом живёт отдельно, в
`desktop_icons`: пчела от скуки меняет позиции значков на рабочем столе (только
позиции; ничего не удаляет, не переименовывает и не открывает).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

log = logging.getLogger("bee_pet.winapi")

AVAILABLE = sys.platform == "win32"

WM_CLOSE = 0x0010
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation
UWP_HOST = "applicationframehost.exe"


@dataclass(frozen=True)
class WindowInfo:
    """Снимок окна верхнего уровня."""
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str   # имя exe строчными буквами, "" если неизвестно
    rect: tuple[int, int, int, int]  # физические пиксели: left, top, right, bottom
    elevated: bool | None  # None — определить не удалось
    is_tool: bool
    cloaked: bool
    minimized: bool


if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    class _TokenElevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    class _LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class _Api:
        """Объявленные прототипы WinAPI (argtypes/restype обязательны для 64-битных HWND)."""

        def __init__(self) -> None:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            a32 = ctypes.WinDLL("advapi32", use_last_error=True)
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

            self.EnumWindows = u32.EnumWindows
            self.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
            self.EnumWindows.restype = wintypes.BOOL
            self.EnumChildWindows = u32.EnumChildWindows
            self.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
            self.EnumChildWindows.restype = wintypes.BOOL
            self.IsWindowVisible = u32.IsWindowVisible
            self.IsWindowVisible.argtypes = [wintypes.HWND]
            self.IsWindowVisible.restype = wintypes.BOOL
            self.IsIconic = u32.IsIconic
            self.IsIconic.argtypes = [wintypes.HWND]
            self.IsIconic.restype = wintypes.BOOL
            self.GetWindowTextLengthW = u32.GetWindowTextLengthW
            self.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            self.GetWindowTextLengthW.restype = ctypes.c_int
            self.GetWindowTextW = u32.GetWindowTextW
            self.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.GetWindowTextW.restype = ctypes.c_int
            self.GetClassNameW = u32.GetClassNameW
            self.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.GetClassNameW.restype = ctypes.c_int
            self.GetWindowThreadProcessId = u32.GetWindowThreadProcessId
            self.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            self.GetWindowThreadProcessId.restype = wintypes.DWORD
            self.GetWindowRect = u32.GetWindowRect
            self.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            self.GetWindowRect.restype = wintypes.BOOL
            # На 32-битной Windows GetWindowLongPtrW не экспортируется.
            get_long = getattr(u32, "GetWindowLongPtrW", None) or u32.GetWindowLongW
            self.GetWindowLongPtrW = get_long
            self.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            self.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.PostMessageW = u32.PostMessageW
            self.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
            self.PostMessageW.restype = wintypes.BOOL
            self.GetLastInputInfo = u32.GetLastInputInfo
            self.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
            self.GetLastInputInfo.restype = wintypes.BOOL
            self.GetForegroundWindow = u32.GetForegroundWindow
            self.GetForegroundWindow.argtypes = []
            self.GetForegroundWindow.restype = wintypes.HWND
            self.SetForegroundWindow = u32.SetForegroundWindow
            self.SetForegroundWindow.argtypes = [wintypes.HWND]
            self.SetForegroundWindow.restype = wintypes.BOOL
            self.BringWindowToTop = u32.BringWindowToTop
            self.BringWindowToTop.argtypes = [wintypes.HWND]
            self.BringWindowToTop.restype = wintypes.BOOL
            self.AttachThreadInput = u32.AttachThreadInput
            self.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            self.AttachThreadInput.restype = wintypes.BOOL
            self.IsWindow = u32.IsWindow
            self.IsWindow.argtypes = [wintypes.HWND]
            self.IsWindow.restype = wintypes.BOOL

            self.OpenProcess = k32.OpenProcess
            self.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.OpenProcess.restype = wintypes.HANDLE
            self.QueryFullProcessImageNameW = k32.QueryFullProcessImageNameW
            self.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            self.QueryFullProcessImageNameW.restype = wintypes.BOOL
            self.CloseHandle = k32.CloseHandle
            self.CloseHandle.argtypes = [wintypes.HANDLE]
            self.CloseHandle.restype = wintypes.BOOL
            self.GetTickCount = k32.GetTickCount
            self.GetTickCount.argtypes = []
            self.GetTickCount.restype = wintypes.DWORD
            self.GetCurrentThreadId = k32.GetCurrentThreadId
            self.GetCurrentThreadId.argtypes = []
            self.GetCurrentThreadId.restype = wintypes.DWORD

            self.OpenProcessToken = a32.OpenProcessToken
            self.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
            self.OpenProcessToken.restype = wintypes.BOOL
            self.GetTokenInformation = a32.GetTokenInformation
            self.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            self.GetTokenInformation.restype = wintypes.BOOL

            self.DwmGetWindowAttribute = dwm.DwmGetWindowAttribute
            self.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                   wintypes.LPVOID, wintypes.DWORD]
            self.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT

    _api = _Api()

    def _text(fn, hwnd: int, size: int = 512) -> str:
        buf = ctypes.create_unicode_buffer(size)
        fn(hwnd, buf, size)
        return buf.value

    def _pid_of(hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        _api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(pid: int) -> str:
        """Имя exe строчными буквами или пустая строка."""
        handle = _api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not _api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        finally:
            _api.CloseHandle(handle)

    def _is_elevated(pid: int) -> bool | None:
        """True/False, а при любой ошибке (в том числе отказе в доступе) — None."""
        process = _api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            token = wintypes.HANDLE()
            if not _api.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
                return None
            try:
                info = _TokenElevation()
                returned = wintypes.DWORD(0)
                ok = _api.GetTokenInformation(token, TOKEN_ELEVATION_CLASS, ctypes.byref(info),
                                              ctypes.sizeof(info), ctypes.byref(returned))
                return bool(info.TokenIsElevated) if ok else None
            finally:
                _api.CloseHandle(token)
        finally:
            _api.CloseHandle(process)

    def _real_pid(hwnd: int, frame_pid: int) -> int:
        """Для UWP-окна (ApplicationFrameHost) ищет pid настоящего приложения среди дочерних."""
        found: list[int] = []

        def on_child(child, _lparam):
            try:
                pid = _pid_of(child)
                if pid and pid != frame_pid:
                    found.append(pid)
                    return False
            except Exception:  # noqa: BLE001 — перечисление не должно падать
                pass
            return True

        cb = _WNDENUMPROC(on_child)
        _api.EnumChildWindows(hwnd, cb, 0)
        return found[0] if found else frame_pid

    def _frame_rect(hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        hr = _api.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect),
                                        ctypes.sizeof(rect))
        if hr != 0:
            _api.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def _is_cloaked(hwnd: int) -> bool:
        value = wintypes.DWORD(0)
        hr = _api.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value),
                                        ctypes.sizeof(value))
        return hr == 0 and value.value != 0

    def _describe(hwnd: int) -> WindowInfo | None:
        if not _api.IsWindowVisible(hwnd):
            return None
        title = _text(_api.GetWindowTextW, hwnd)
        if not title:
            return None
        pid = _pid_of(hwnd)
        name = _process_name(pid)
        real_pid = pid
        if name == UWP_HOST:
            real_pid = _real_pid(hwnd, pid)
            if real_pid != pid:
                name = _process_name(real_pid) or name
        exstyle = int(_api.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        return WindowInfo(
            hwnd=int(hwnd), title=title, class_name=_text(_api.GetClassNameW, hwnd, 256),
            pid=pid, process_name=name, rect=_frame_rect(hwnd),
            elevated=_is_elevated(real_pid), is_tool=bool(exstyle & WS_EX_TOOLWINDOW),
            cloaked=_is_cloaked(hwnd), minimized=bool(_api.IsIconic(hwnd)))

    def list_windows() -> list[WindowInfo]:
        """Видимые окна верхнего уровня с заголовком, сверху вниз по Z-порядку."""
        result: list[WindowInfo] = []

        def on_window(hwnd, _lparam):
            try:
                info = _describe(hwnd)
                if info is not None:
                    result.append(info)
            except Exception:  # noqa: BLE001 — одно кривое окно не должно ронять перечисление
                log.debug("не удалось описать окно %s", hwnd, exc_info=True)
            return True

        cb = _WNDENUMPROC(on_window)
        _api.EnumWindows(cb, 0)
        return result

    def close_window(hwnd: int) -> bool:
        """Вежливо просит окно закрыться (WM_CLOSE). Никаких убийств процессов."""
        return bool(_api.PostMessageW(hwnd, WM_CLOSE, 0, 0))

    def idle_seconds() -> float:
        """Сколько секунд не было ввода с клавиатуры и мыши во всей системе."""
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not _api.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # Оба счётчика 32-битные и переполняются раз в ~49 дней: считаем по модулю 2**32.
        elapsed_ms = (int(_api.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed_ms / 1000.0

    def foreground_window() -> int:
        """hwnd активного окна (0, если его нет)."""
        return int(_api.GetForegroundWindow() or 0)

    def focus_window(hwnd: int) -> bool:
        """Выводит окно на передний план. Второе и последнее действие над чужим окном.

        Фоновому процессу SetForegroundWindow обычно запрещён, поэтому на время вызова
        поток пчелы подключается к очереди ввода потока текущего активного окна
        (AttachThreadInput) и обязательно отключается в finally. Нажатия клавиш не эмулируются.
        Свёрнутые окна не трогаем (не разворачиваем).
        """
        if not hwnd or not _api.IsWindow(hwnd) or _api.IsIconic(hwnd):
            return False
        ours = int(_api.GetCurrentThreadId())
        foreground = _api.GetForegroundWindow()
        theirs = int(_api.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        attached = False
        try:
            if theirs and theirs != ours:
                attached = bool(_api.AttachThreadInput(ours, theirs, True))
            _api.SetForegroundWindow(hwnd)
            _api.BringWindowToTop(hwnd)
            # Код возврата SetForegroundWindow ненадёжен (для UWP-окон бывает FALSE при
            # успехе), поэтому проверяем, какое окно стало активным на самом деле.
            return int(_api.GetForegroundWindow() or 0) == int(hwnd)
        finally:
            if attached:
                _api.AttachThreadInput(ours, theirs, False)

else:
    def list_windows() -> list[WindowInfo]:
        """Вне Windows окон нет."""
        return []

    def close_window(hwnd: int) -> bool:  # noqa: ARG001
        """Вне Windows ничего не делает."""
        return False

    def idle_seconds() -> float:
        """Вне Windows простой не отслеживается."""
        return 0.0

    def foreground_window() -> int:
        """Вне Windows активного окна нет."""
        return 0

    def focus_window(hwnd: int) -> bool:  # noqa: ARG001
        """Вне Windows ничего не делает."""
        return False
