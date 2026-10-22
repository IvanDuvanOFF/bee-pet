# bee-pet — common rules for every stage

Read `CLAUDE.md` (project root) first: it is the source of truth for product decisions
(written in Russian). This file and `stage-N.md` are the implementation spec.

## Product in one paragraph

A desktop pet: a vector-drawn bee in a transparent, frameless, always-on-top window.
The user can drag it, use a right-click context menu (give flower, pet, swat, sorry,
spray, open chat, toggle "allow biting windows", exit) and chat with it in a mini-chat.
Replies come from BeeLLM via llama-server (`/v1/completions`), falling back to canned
"бзз" lines. The bee has a deterministic local mood. When very angry (angry-high) it may
close one whitelisted window via `WM_CLOSE` on Windows, with safeguards.

Target platform for the MVP: **Windows 11**. Development happens on Fedora Linux, so all
code except `bee_pet/winapi.py` must be platform-neutral, and every module must be
importable on Linux (guard Windows-only `ctypes` calls behind `sys.platform == "win32"`).

## Layout (create files as stages need them)

```
bee_pet/
  __init__.py
  __main__.py      # entry point: python -m bee_pet
  app.py           # QApplication setup, wiring of components
  mood.py          # pure mood state machine (no Qt)
  painter.py       # QPainter drawing of the bee and mood badges
  bee_window.py    # the bee window: animation, drag, context menu, flight
  bubble.py        # speech bubble window
  llm.py           # BeeLLM client + fallback lines + async worker
  chat.py          # mini-chat window
  winapi.py        # Windows-only window enumeration / closing (ctypes)
  biting.py        # platform-neutral biting policy and sequence logic
tests/
requirements.txt       # PySide6
requirements-dev.txt   # -r requirements.txt, pytest
```

## Environment

- Use the project venv: `python3 -m venv .venv` (create if missing), then
  `.venv/bin/pip install -r requirements-dev.txt`. Never install into system Python.
- Run tests with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`.
- You cannot run Windows here. Windows-only code must be reviewed carefully by reading,
  and its logic split so that the decision-making parts are testable with fakes on Linux.
- A smoke run of the GUI: `QT_QPA_PLATFORM=offscreen timeout 3 .venv/bin/python -m bee_pet`
  must exit by timeout (code 124) without tracebacks.

## Code conventions

- Python 3.11+ syntax, type hints, dataclasses/enums where natural, no extra dependencies
  beyond PySide6 (stdlib `urllib` for HTTP, like `docs/bee_mcp.py`).
- **Comments, docstrings and all UI text in Russian.** Phrases sent to BeeLLM are English
  (the model only understands English).
- Keep modules small and focused; no global mutable state except the QApplication.
- Never block the GUI thread (network calls go to a worker thread).
- Do not modify `docs/bee_mcp.py`, `docs/bee_instructions.md`, `BEE_HANDOFF.md`.
  Update `CLAUDE.md` only where your stage spec says so.
- Do not git commit. Do not add features beyond your stage spec.

## Report back (keep it short, under ~25 lines)

Files created/changed, what was verified (test output summary, smoke run result),
deviations from the spec and why, open questions or risks for the next stage.
