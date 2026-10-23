"""Встроенный llama-server: где взять модель для чата и как его запустить и остановить.

Порядок выбора (choose_plan):
1. Задана переменная BEE_URL — это сервер пользователя, ничего не запускаем.
2. На http://127.0.0.1:8081 уже кто-то отвечает (например, docker compose) — берём его.
3. Рядом лежат llama-server и GGUF (vendor/ или внутри собранного exe) — запускаем
   свой сервер на свободном порту.
4. Иначе пчела говорит заготовками «бзз».

BEE_FORCE_BUNDLED=1 пропускает пункты 1–2: так проверяют встроенный сервер, когда на 8081
уже работает docker.

Сервер стартует в фоне и не держит GUI: пока модель грузится, BeeClient отвечает
заготовками, а когда /health ответил, сигнал ready сбрасывает офлайн-паузу клиента.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8081"
LLAMA_EXE = "llama-server.exe" if sys.platform == "win32" else "llama-server"
CONTEXT_SIZE = 4096
STARTUP_TIMEOUT = 60.0  # секунд ждём /health от своего сервера
STOP_TIMEOUT = 2.0  # секунд на вежливую остановку, потом kill


def log_dir() -> Path:
    """Папка для логов: %LOCALAPPDATA%\\bee-pet (на других ОС ~/.cache/bee-pet)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".cache")
    path = Path(base) / "bee-pet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_root() -> Path:
    """Где лежат llama-server и модель: внутри собранного exe или в vendor/ репозитория.

    PyInstaller кладёт данные в sys._MEIPASS (во временную папку для onefile,
    в _internal рядом с exe для onedir); spec сохраняет там ту же структуру vendor/.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "vendor"
    return Path(__file__).resolve().parent.parent / "vendor"


def find_bundled(root: Path | None = None) -> tuple[Path, Path] | None:
    """(llama-server, gguf), если оба есть; модель — первая по алфавиту *.gguf."""
    root = root if root is not None else bundle_root()
    exe = root / "llama" / LLAMA_EXE
    models = sorted((root / "model").glob("*.gguf")) if (root / "model").is_dir() else []
    if exe.is_file() and models:
        return exe, models[0]
    return None


def health_ok(url: str, timeout: float = 0.3, accept_loading: bool = False) -> bool:
    """Отвечает ли llama-server на /health.

    accept_loading=True: ответ 503 («модель ещё грузится») тоже считается живым сервером.
    """
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as err:
        return accept_loading and err.code == 503
    except (OSError, ValueError):
        return False


def free_port() -> int:
    """Свободный TCP-порт на localhost (его выбирает ОС)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class Plan:
    """Что делать с сервером модели.

    kind: "env" (BEE_URL), "existing" (уже отвечает на 8081), "bundled" (запускаем свой),
    "none" (сервера нет, только заготовки).
    """
    kind: str
    url: str
    exe: Path | None = None
    gguf: Path | None = None


def choose_plan(environ: Mapping[str, str],
                probe: Callable[[str], bool],
                bundled: Callable[[], tuple[Path, Path] | None]) -> Plan:
    """Выбирает сервер модели (см. docstring модуля). probe — живой ли сервер по адресу."""
    force = environ.get("BEE_FORCE_BUNDLED", "") not in ("", "0")
    if not force:
        if environ.get("BEE_URL"):
            return Plan("env", environ["BEE_URL"].rstrip("/"))
        if probe(DEFAULT_URL):
            return Plan("existing", DEFAULT_URL)
    found = bundled()
    if found is not None:
        return Plan("bundled", "", *found)
    return Plan("none", DEFAULT_URL)


# --- Job Object: сервер умирает вместе с пчелой, даже если она упала ------------------

def _create_kill_on_close_job():
    """Job Object с JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; None, если не вышло или не Windows.

    Дескриптор держим открытым всю жизнь процесса: когда пчела завершается (как угодно,
    хоть через диспетчер задач), ОС закрывает его и убивает все процессы задания.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(job, 9,  # JobObjectExtendedLimitInformation
                                          ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        kernel32.CloseHandle(job)
        return None

    def assign(process_handle: int) -> bool:
        return bool(kernel32.AssignProcessToJobObject(job, process_handle))

    return assign


class BundledServer(QObject):
    """Свой llama-server: запуск в фоне, ожидание /health, остановка при выходе."""

    ready = Signal()  # /health ответил 200 — модель загружена

    def __init__(self, exe: Path, gguf: Path, port: int | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 probe: Callable[[str], bool] = health_ok,
                 poll_interval: float = 0.25, startup_timeout: float = STARTUP_TIMEOUT,
                 log_path: Path | None = None, use_job: bool = True,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.exe, self.gguf = exe, gguf
        self.port = port if port is not None else free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._popen, self._probe = popen, probe
        self._poll_interval, self._startup_timeout = poll_interval, startup_timeout
        self._log_path, self._use_job = log_path, use_job
        self._log_file = None
        self.process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def command(self) -> list[str]:
        """Командная строка llama-server."""
        return [str(self.exe), "-m", str(self.gguf), "-c", str(CONTEXT_SIZE),
                "--host", "127.0.0.1", "--port", str(self.port)]

    def start(self) -> None:
        """Запускает процесс и фоновое ожидание /health; GUI не ждёт."""
        kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": str(self.exe.parent)}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if self._log_path is not None:
            self._log_file = open(self._log_path, "wb")
            kwargs["stdout"] = kwargs["stderr"] = self._log_file
        else:
            kwargs["stdout"] = kwargs["stderr"] = subprocess.DEVNULL
        self.process = self._popen(self.command(), **kwargs)
        log.info("запущен встроенный llama-server (pid %s) на %s, модель %s",
                 getattr(self.process, "pid", "?"), self.url, self.gguf.name)
        if self._use_job:
            self._attach_job()
        self._watcher = threading.Thread(target=self._wait_ready, name="bee-llama-health",
                                         daemon=True)
        self._watcher.start()

    def _attach_job(self) -> None:
        """Привязывает процесс к Job Object, чтобы он не пережил пчелу."""
        handle = getattr(self.process, "_handle", None)
        assign = _create_kill_on_close_job() if handle is not None else None
        if assign is None or not assign(int(handle)):
            log.warning("не удалось привязать llama-server к Job Object: "
                        "если пчела упадёт, сервер может остаться висеть")
            return
        # Ссылка на замыкание держит дескриптор задания открытым до конца процесса.
        self._job_assign = assign

    def _wait_ready(self) -> None:
        """Фоновый поток: опрашивает /health, пока сервер не ответит, не умрёт или не истечёт время."""
        started = time.monotonic()
        while not self._stop.is_set() and time.monotonic() - started < self._startup_timeout:
            if self.process is not None and self.process.poll() is not None:
                log.warning("llama-server завершился при запуске (код %s), чат без модели",
                            self.process.returncode)
                return
            if self._probe(self.url):
                log.info("встроенный llama-server готов за %.1f с", time.monotonic() - started)
                try:
                    self.ready.emit()  # уходит в GUI-поток очередью сигналов
                except RuntimeError:
                    pass  # объект уже удалён при выходе
                return
            self._stop.wait(self._poll_interval)
        if not self._stop.is_set():
            log.warning("llama-server не ответил за %.0f с", self._startup_timeout)

    def stop(self) -> None:
        """Останавливает сервер: terminate, ждём STOP_TIMEOUT, затем kill."""
        self._stop.set()
        proc = self.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(STOP_TIMEOUT)
            log.info("встроенный llama-server остановлен")
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


def setup_server(environ: Mapping[str, str] | None = None,
                 probe: Callable[[str], bool] | None = None,
                 bundled: Callable[[], tuple[Path, Path] | None] = find_bundled,
                 server_factory: Callable[[Path, Path], BundledServer] | None = None,
                 ) -> BundledServer | None:
    """Выбирает сервер модели, направляет на него llm и при надобности запускает свой.

    Возвращает запущенный BundledServer (его надо остановить при выходе) или None.
    """
    from bee_pet import llm

    environ = os.environ if environ is None else environ
    probe = probe if probe is not None else (lambda url: health_ok(url, accept_loading=True))
    plan = choose_plan(environ, probe, bundled)
    if plan.kind == "env":
        log.info("сервер модели из BEE_URL: %s", plan.url)
    elif plan.kind == "existing":
        log.info("на %s уже работает llama-server — пчела говорит через него", plan.url)
    elif plan.kind == "none":
        log.info("сервера модели нет и встроенного тоже — пчела говорит заготовками")
    if plan.kind != "bundled":
        llm.set_base_url(plan.url)
        return None
    if server_factory is None:
        def server_factory(exe: Path, gguf: Path) -> BundledServer:
            return BundledServer(exe, gguf, log_path=log_dir() / "llama-server.log")
    server = server_factory(plan.exe, plan.gguf)
    llm.set_base_url(server.url)
    try:
        server.start()
    except OSError as err:
        log.warning("не удалось запустить llama-server: %s", err)
        return None
    return server
