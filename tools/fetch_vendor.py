"""Скачивает в vendor/ всё, что нужно встроенному чату: llama-server и модель BeeLLM.

    python tools\\fetch_vendor.py

- llama.cpp: официальный релиз ggml-org/llama.cpp для Windows x64 (CPU), из архива
  берутся только llama-server.exe и нужные ему DLL;
- BeeLLM: GGUF с Hugging Face, тот же файл, что выбирает `llama-server -hf schizophyllume/BeeLLM`.

Версии и SHA256 зафиксированы ниже; при несовпадении хеша скрипт падает и ничего не
распаковывает. Повторный запуск ничего не качает, если файлы на месте и хеши сходятся.
Скачанное здесь никогда не запускается — только проверяется и распаковывается в vendor/.
Только стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
CACHE = VENDOR / ".download"

# llama.cpp: релиз b11645 (15.10.2026), сборка Windows x64 для CPU.
LLAMA_TAG = "b11645"
LLAMA_ZIP = f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip"
LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/{LLAMA_ZIP}"
LLAMA_SHA256 = "5dd36a16ee76bbf8bd9b650a3b1b8de52d919ad0023e0438176d67993a675d47"
LLAMA_LICENSE_URL = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_TAG}/LICENSE"
LLAMA_LICENSE_SHA256 = "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d"

# BeeLLM: ревизия репозитория schizophyllume/BeeLLM и файл по умолчанию для -hf.
MODEL_REPO = "schizophyllume/BeeLLM"
MODEL_REVISION = "f469ed610f716ae438c04c2bd04921fed7eabd55"
MODEL_FILE = "BeeLLM-12M-Mood.gguf"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"
MODEL_SHA256 = "32d5e894913620beab6b7eda374598a538285217b893ce5f3c7c21d1ef98879b"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expected_sha: str) -> None:
    """Качает url в dest (через .part) с прогрессом и проверяет SHA256."""
    if dest.is_file() and sha256(dest) == expected_sha:
        print(f"  уже скачано: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"  качаю {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "bee-pet-fetch-vendor"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            done += len(chunk)
            percent = done * 100 // total if total else 0
            if total and percent // 10 > shown:  # строка на каждые 10 %
                shown = percent // 10
                print(f"  {percent:3d} %  ({done / 1e6:.1f} из {total / 1e6:.1f} МБ)", flush=True)
    actual = sha256(part)
    if actual != expected_sha:
        part.unlink()
        sys.exit(f"ОШИБКА: SHA256 не совпал для {dest.name}\n"
                 f"  ожидался {expected_sha}\n  получен  {actual}\n"
                 "Файл удалён. Ничего не распаковано.")
    part.replace(dest)
    print(f"  SHA256 совпал: {dest.name}")


def wanted(name: str) -> bool:
    """Что из архива llama.cpp нужно llama-server: сам exe, общие DLL и лицензии.

    DLL других утилит (llama-cli-impl.dll и т. п.) и прочие exe не берём.
    """
    lower = name.lower()
    if lower == "llama-server.exe" or lower.startswith("license"):
        return True
    if not lower.endswith(".dll"):
        return False
    return not (lower.endswith("-impl.dll") and lower != "llama-server-impl.dll")


def fetch_llama() -> None:
    print(f"llama.cpp {LLAMA_TAG} (Windows x64, CPU):")
    dest = VENDOR / "llama"
    stamp = dest / ".source"
    stamp_text = f"{LLAMA_ZIP} {LLAMA_SHA256}\n"
    license_path = dest / "LICENSE-llama.cpp.txt"
    if (stamp.is_file() and stamp.read_text() == stamp_text
            and (dest / "llama-server.exe").is_file() and license_path.is_file()):
        print("  уже на месте: vendor/llama/")
        return
    archive = CACHE / LLAMA_ZIP
    download(LLAMA_URL, archive, LLAMA_SHA256)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name  # без путей из архива: только плоско в vendor/llama
            if info.is_dir() or not name or not wanted(name):
                continue
            with zf.open(info) as src, open(dest / name, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    if not (dest / "llama-server.exe").is_file():
        sys.exit("ОШИБКА: в архиве нет llama-server.exe")
    lic = CACHE / f"LICENSE-llama.cpp-{LLAMA_TAG}.txt"
    download(LLAMA_LICENSE_URL, lic, LLAMA_LICENSE_SHA256)
    shutil.copyfile(lic, license_path)
    stamp.write_text(stamp_text)
    print(f"  распаковано файлов: {count} -> vendor/llama/")


def fetch_model() -> None:
    print(f"Модель {MODEL_REPO} ({MODEL_FILE}, ревизия {MODEL_REVISION[:10]}):")
    dest = VENDOR / "model"
    dest.mkdir(parents=True, exist_ok=True)
    for other in dest.glob("*.gguf"):
        if other.name != MODEL_FILE:
            print(f"  убираю лишнюю модель: {other.name}")
            other.unlink()
    download(MODEL_URL, dest / MODEL_FILE, MODEL_SHA256)


def main() -> None:
    fetch_llama()
    fetch_model()
    print("Готово: vendor/ можно собирать (pyinstaller bee-pet.spec).")


if __name__ == "__main__":
    main()
