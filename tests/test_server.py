"""Тесты выбора и запуска встроенного llama-server (без настоящего сервера)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from bee_pet import llm, server
from bee_pet.llm import BeeClient
from bee_pet.server import (DEFAULT_URL, BundledServer, Plan, choose_plan, find_bundled,
                            health_ok, setup_server)

EXE, GGUF = Path("C:/b/llama/llama-server.exe"), Path("C:/b/model/bee.gguf")


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def restore_url():
    old = llm.BEE_URL
    yield
    llm.BEE_URL = old


def _bundled():
    return EXE, GGUF


def _nothing():
    return None


# --- choose_plan ------------------------------------------------------------

def test_env_url_wins():
    probed = []
    plan = choose_plan({"BEE_URL": "http://h:9000/"}, probed.append, _bundled)
    assert plan == Plan("env", "http://h:9000")
    assert probed == []  # чужой сервер не трогаем и 8081 не проверяем


def test_existing_server_on_default_port():
    plan = choose_plan({}, lambda url: url == DEFAULT_URL, _bundled)
    assert plan == Plan("existing", DEFAULT_URL)


def test_bundled_when_nothing_answers():
    plan = choose_plan({}, lambda url: False, _bundled)
    assert plan.kind == "bundled" and (plan.exe, plan.gguf) == (EXE, GGUF)


def test_none_without_bundle():
    assert choose_plan({}, lambda url: False, _nothing) == Plan("none", DEFAULT_URL)


def test_force_bundled_skips_env_and_probe():
    probed = []
    env = {"BEE_URL": "http://h:9000", "BEE_FORCE_BUNDLED": "1"}
    assert choose_plan(env, probed.append, _bundled).kind == "bundled"
    assert probed == []


def test_force_bundled_zero_means_off():
    plan = choose_plan({"BEE_FORCE_BUNDLED": "0"}, lambda url: True, _bundled)
    assert plan.kind == "existing"


# --- поиск файлов и /health -------------------------------------------------

def test_find_bundled(tmp_path):
    assert find_bundled(tmp_path) is None
    (tmp_path / "llama").mkdir()
    exe = tmp_path / "llama" / server.LLAMA_EXE
    exe.write_bytes(b"")
    assert find_bundled(tmp_path) is None  # без модели не годится
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "b.gguf").write_bytes(b"")
    (tmp_path / "model" / "a.gguf").write_bytes(b"")
    assert find_bundled(tmp_path) == (exe, tmp_path / "model" / "a.gguf")


def _serve(status):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_health_ok_states():
    srv, url = _serve(200)
    try:
        assert health_ok(url)
    finally:
        srv.shutdown()
    srv, url = _serve(503)  # модель ещё грузится
    try:
        assert not health_ok(url)
        assert health_ok(url, accept_loading=True)
    finally:
        srv.shutdown()
    assert not health_ok(f"http://127.0.0.1:{server.free_port()}")


# --- BundledServer ------------------------------------------------------------

class FakeProc:
    def __init__(self, hang=False):
        self.pid = 4242
        self.returncode = None
        self.calls = []
        self._hang = hang

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self._hang:
            self.returncode = 0

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("llama-server", timeout)
        return self.returncode


def _make(proc, probe=lambda url: True, **kw):
    launched = []

    def popen(cmd, **kwargs):
        launched.append((cmd, kwargs))
        return proc

    srv = BundledServer(EXE, GGUF, port=5555, popen=popen, probe=probe,
                        poll_interval=0.01, use_job=False, **kw)
    return srv, launched


def _pump_until(qapp, cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        QCoreApplication.processEvents()
        time.sleep(0.01)
    return cond()


def test_start_command_and_ready(qapp):
    proc = FakeProc()
    srv, launched = _make(proc)
    got = []
    srv.ready.connect(lambda: got.append(True))
    srv.start()
    cmd, kwargs = launched[0]
    assert cmd == [str(EXE), "-m", str(GGUF), "-c", "4096",
                   "--host", "127.0.0.1", "--port", "5555"]
    assert kwargs["stdout"] is subprocess.DEVNULL
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert _pump_until(qapp, lambda: got)
    srv.stop()


def test_ready_resets_client_backoff(qapp):
    """Сбой до загрузки модели ставит офлайн-паузу; ready от сервера её снимает."""
    now = [100.0]
    client = BeeClient(ask=lambda m: (_ for _ in ()).throw(OSError()), clock=lambda: now[0])
    client._on_done(None, "бзз", False, "hi", "", True, False)
    assert not client.likely_online()
    srv, _ = _make(FakeProc())
    srv.ready.connect(client.mark_online)
    srv.start()
    assert _pump_until(qapp, client.likely_online)
    srv.stop()


def test_no_ready_if_process_dies(qapp):
    proc = FakeProc()
    proc.returncode = 1
    srv, _ = _make(proc, probe=lambda url: False)
    got = []
    srv.ready.connect(lambda: got.append(True))
    srv.start()
    srv._watcher.join(1)
    QCoreApplication.processEvents()
    assert not srv._watcher.is_alive() and got == []


def test_stop_terminates_then_kills():
    polite = FakeProc()
    srv, _ = _make(polite, probe=lambda url: False)
    srv.start()
    srv.stop()
    assert polite.calls[0] == "terminate" and "kill" not in polite.calls
    stubborn = FakeProc(hang=True)
    srv, _ = _make(stubborn, probe=lambda url: False)
    srv.start()
    srv.stop()
    assert stubborn.calls[:3] == ["terminate", "wait", "kill"]


def test_stop_on_quit_signal(qapp):
    """Как в main(): aboutToQuit -> stop."""
    proc = FakeProc()
    srv, _ = _make(proc, probe=lambda url: False)
    srv.start()
    qapp.aboutToQuit.connect(srv.stop)
    try:
        qapp.aboutToQuit.emit()
        assert "terminate" in proc.calls
    finally:
        qapp.aboutToQuit.disconnect(srv.stop)


# --- setup_server: переключение адреса llm ------------------------------------

def test_setup_env_sets_url():
    assert setup_server({"BEE_URL": "http://h:9000"}, probe=lambda u: False,
                        bundled=_bundled) is None
    assert llm.BEE_URL == "http://h:9000"


def test_setup_existing_uses_default():
    llm.set_base_url("http://elsewhere:1")
    assert setup_server({}, probe=lambda u: True, bundled=_bundled) is None
    assert llm.BEE_URL == DEFAULT_URL


def test_setup_bundled_points_llm_at_own_server():
    proc = FakeProc()
    made = []

    def factory(exe, gguf):
        srv, _ = _make(proc, probe=lambda url: False)
        made.append(srv)
        return srv

    srv = setup_server({}, probe=lambda u: False, bundled=_bundled, server_factory=factory)
    try:
        assert srv is made[0]
        assert llm.BEE_URL == "http://127.0.0.1:5555"
    finally:
        srv.stop()


def test_setup_bundled_start_failure():
    def factory(exe, gguf):
        def popen(cmd, **kw):
            raise FileNotFoundError(cmd[0])
        return BundledServer(exe, gguf, port=5556, popen=popen, use_job=False)

    assert setup_server({}, probe=lambda u: False, bundled=_bundled,
                        server_factory=factory) is None


def test_ask_bee_reads_url_at_call_time(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        raise OSError("нет сервера")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    llm.set_base_url("http://127.0.0.1:7777/")
    with pytest.raises(OSError):
        llm.ask_bee([{"role": "user", "content": "hi"}])
    assert seen == ["http://127.0.0.1:7777/v1/chat/completions"]


@pytest.mark.skipif(os.name != "nt", reason="Job Object есть только в Windows")
def test_job_object_kills_child_on_close():
    """Реальный Job Object на безобидном процессе: создаётся и принимает процесс."""
    import sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assign = server._create_kill_on_close_job()
        assert assign is not None and assign(int(child._handle))
    finally:
        child.kill()
        child.wait(5)
