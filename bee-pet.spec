# -*- mode: python ; coding: utf-8 -*-
# Сборка bee-pet.exe (PyInstaller).
#
#   python tools\fetch_vendor.py     # один раз: llama-server и модель в vendor/
#   pyinstaller bee-pet.spec         # один файл: dist\bee-pet.exe
#
# Вариант папкой (быстрее стартует, не распаковывается во временную папку):
#   $env:BEE_ONEDIR="1"; pyinstaller bee-pet.spec    # dist\bee-pet\bee-pet.exe
import os
from pathlib import Path

ROOT = Path(SPECPATH)
VENDOR = ROOT / "vendor"
ONEDIR = os.environ.get("BEE_ONEDIR", "") not in ("", "0")

# llama-server с DLL и модель кладём как данные (без анализа зависимостей), сохраняя
# структуру vendor/: bee_pet/server.py ищет их в sys._MEIPASS/vendor/.
datas = []
llama = sorted(p for p in (VENDOR / "llama").glob("*") if p.is_file() and not p.name.startswith("."))
models = sorted((VENDOR / "model").glob("*.gguf"))
if not any(p.name == "llama-server.exe" for p in llama) or not models:
    print("ВНИМАНИЕ: vendor/ пуст — exe соберётся без модели (чат только заготовками).\n"
          "Сначала выполните: python tools\\fetch_vendor.py")
datas += [(str(p), "vendor/llama") for p in llama]
datas += [(str(p), "vendor/model") for p in models]

# Qt-модули, которые пчеле не нужны: без них exe в разы меньше.
EXCLUDES = [
    "tkinter", "unittest", "pydoc", "test", "ssl", "_ssl",  # HTTP только к localhost
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtDBus",
    "PySide6.QtPrintSupport", "PySide6.QtHttpServer", "PySide6.QtAxContainer",
]

a = Analysis(
    [str(ROOT / "tools" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

# Хуки PySide6 тянут плагины (виртуальная клавиатура -> QtQuick/Qml, PDF, SVG, форматы
# картинок) и программный OpenGL на 20 МБ — пчеле всё это не нужно.
DROP_BINARIES = (
    "opengl32sw.dll", "qt6quick", "qt6qml", "qt6opengl", "qt6pdf", "qt6svg",
    "qt6network", "qt6virtualkeyboard", "qtvirtualkeyboardplugin", "qpdf.dll",
    "qsvg.dll", "qsvgicon.dll", "qdirect2d.dll", "qjpeg.dll", "qtiff.dll", "qwebp.dll",
    "qicns.dll", "qtga.dll", "qwbmp.dll", "libssl", "libcrypto",
)
# DLL llama.cpp анализатор зависимостей копирует ещё и в корень — там они не нужны:
# llama-server грузит их из своей папки vendor/llama.
LLAMA_NAMES = {p.name.lower() for p in llama}


def keep_binary(entry):
    dest = entry[0].replace("\\", "/").lower()
    name = dest.rsplit("/", 1)[-1]
    if "/" not in dest and name in LLAMA_NAMES:
        return False
    return not any(name.startswith(d) or name == d for d in DROP_BINARIES)


def keep_data(entry):
    dest = entry[0].replace("\\", "/").lower()
    # Переводы Qt: оставляем только русские.
    if "/translations/" in dest and "_ru" not in dest:
        return False
    return True


a.binaries = [b for b in a.binaries if keep_binary(b)]
a.datas = [d for d in a.datas if keep_data(d)]

pyz = PYZ(a.pure)

# UPX выключен: сжатые exe чаще ловят ложные срабатывания антивирусов.
if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="bee-pet",
        console=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="bee-pet", upx=False)
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="bee-pet",
        console=False,
        upx=False,
        runtime_tmpdir=None,
    )
