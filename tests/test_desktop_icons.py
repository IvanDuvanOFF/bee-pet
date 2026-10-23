"""Тесты модуля значков рабочего стола: заглушки вне Windows и живое ЧТЕНИЕ на Windows.

Настоящие значки здесь никогда не двигаются: только list_icons/auto_arrange/icon_spacing.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib.util
import sys
from pathlib import Path

import pytest

from bee_pet import desktop_icons

MODULE_PATH = Path(desktop_icons.__file__)


def test_import_has_api():
    for name in ("AVAILABLE", "IconInfo", "list_icons", "move_icon", "open_mover",
                 "auto_arrange", "icon_spacing"):
        assert hasattr(desktop_icons, name)
    assert desktop_icons.AVAILABLE == (sys.platform == "win32")


def test_stub_path_off_windows(monkeypatch):
    """Модуль, загруженный «как на Linux», отдаёт безопасные заглушки."""
    monkeypatch.setattr(sys, "platform", "linux")
    spec = importlib.util.spec_from_file_location("desktop_icons_stub", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)  # dataclass ищет модуль по имени
    spec.loader.exec_module(mod)
    assert mod.AVAILABLE is False
    assert mod.list_icons() == []
    assert mod.move_icon("x", 1, 2) is False
    assert mod.open_mover("x") is None
    assert mod.auto_arrange() is None
    assert mod.icon_spacing() is None


@pytest.mark.skipif(not desktop_icons.AVAILABLE, reason="только Windows")
def test_live_read_only():
    """Только чтение: значки с позициями, режим упорядочения, шаг сетки."""
    icons = desktop_icons.list_icons()
    if not icons:
        pytest.skip("рабочий стол недоступен или на нём нет значков")
    keys = [i.key for i in icons]
    assert len(set(keys)) == len(keys)
    for i in icons:
        assert i.key and isinstance(i.x, int) and isinstance(i.y, int)
    assert desktop_icons.auto_arrange() in (True, False)
    spacing = desktop_icons.icon_spacing()
    assert spacing is not None and spacing[0] > 0 and spacing[1] > 0
