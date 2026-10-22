# Stage 4 — angry bee closes windows (Windows only)

Read `docs/spec/00-common.md` and the section «Злая пчела закрывает окна» in `CLAUDE.md`
first. Stages 1–3 are done; read the code.

This is the only feature that touches other applications. Safety rules are hard
requirements, not suggestions.

## winapi.py (Windows-only, importable everywhere)

On non-Windows: `AVAILABLE = False`, `list_windows()` returns `[]`, `close_window()` is a no-op
returning False. On Windows (`ctypes` + `ctypes.wintypes`, declare `argtypes`/`restype`
for every function used):

```python
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str      # lowercase exe basename, e.g. "notepad.exe"; "" if unknown
    rect: tuple[int, int, int, int]   # physical px: left, top, right, bottom (DWM frame bounds)
    elevated: bool | None  # None if it could not be determined
    is_tool: bool
    cloaked: bool
    minimized: bool
```

- `list_windows() -> list[WindowInfo]` in Z-order (top first) via `EnumWindows`,
  only `IsWindowVisible` top-level windows with a non-empty title.
- rect: `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS=9)`, fallback `GetWindowRect`.
- cloaked: `DwmGetWindowAttribute(DWMWA_CLOAKED=14)` non-zero.
- is_tool: `GetWindowLongPtrW(GWL_EXSTYLE)` has `WS_EX_TOOLWINDOW`.
- process: `GetWindowThreadProcessId`, `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`,
  `QueryFullProcessImageNameW`. **UWP apps** (e.g. Windows 11 Calculator): the top-level
  window belongs to `applicationframehost.exe`; resolve the real app via
  `EnumChildWindows`, picking the child whose pid differs (class `Windows.UI.Core.CoreWindow`),
  and report that child's process name (e.g. `calculatorapp.exe`) while keeping the frame hwnd.
- elevated: `OpenProcessToken(TOKEN_QUERY)` + `GetTokenInformation(TokenElevation)`;
  access denied or any failure → None.
- `close_window(hwnd) -> bool`: **only** `PostMessageW(hwnd, WM_CLOSE, 0, 0)`.
  No `TerminateProcess`, no `DestroyWindow`, no `taskkill`, no `SendMessage` with WM_QUIT.
- `physical_to_logical(rect) -> QRect` helper may live in `biting.py` or `bee_window.py`:
  map physical px to Qt logical coordinates using the `QScreen` that contains the rect center
  (Qt 6 is per-monitor DPI aware; for each screen, logical = screen.geometry().topLeft() +
  (phys - screen_native_top_left) / devicePixelRatio, where native top-left can be computed as
  `screen.geometry().topLeft() * dpr` for the common case). Document the assumption; best
  effort is fine.

## biting.py (platform-neutral, fully unit-tested)

Policy:
- `DEFAULT_WHITELIST = {"notepad.exe", "calc.exe", "calculatorapp.exe", "explorer.exe"}`;
  `explorer.exe` windows are eligible **only** with class `CabinetWClass` (File Explorer).
- Hard blocklist regardless of whitelist: own pid; classes `Progman`, `WorkerW`,
  `Shell_TrayWnd`, `Shell_SecondaryTrayWnd`, `NotifyIconOverflowWindow`; terminals and IDEs
  (`windowsterminal.exe`, `cmd.exe`, `powershell.exe`, `pwsh.exe`, `conhost.exe`,
  `openconsole.exe`, `wt.exe`, `code.exe`, `idea64.exe`, `pycharm64.exe`, `devenv.exe`,
  …); `elevated is True` or `elevated is None` (unknown = unsafe); minimized, cloaked,
  tool windows; windows whose rect is off all screens or smaller than 100×60 px.
- `eligible(win, own_pid, whitelist) -> bool`; `pick_target(windows, own_pid, whitelist,
  rng) -> WindowInfo | None` — prefer the top-most eligible window, with some randomness
  among the top 3 eligible.

Sequence — `BiteController(QObject)`, owned by the app, connected to `BeeWindow`:
- Trigger: a `MoodChange` with `became_angry_high` True.
- Preconditions: `winapi.AVAILABLE`, `bee.biting_enabled`, no bite yet in the current
  angry episode (episode ends when mood leaves ANGRY), cooldown ≥ 5 min since the last bite
  (module constant), not currently dragged.
- Steps: pick target → bee `fly_to` a point on the target's title bar (top edge, ~1/3 from the
  left, converted to logical coords) → bubble warning with countdown, e.g.
  «ВСЁ! это окно моё! 5…4…3…» (5 s, update each second) → re-check: still angry-high,
  the same hwnd still present in `list_windows()`, still eligible, and its rect moved by
  < 50 px → sting animation (short dash/jab drawn by the bee, e.g. a `stinging` flag in
  PaintState for ~400 ms) → `close_window(hwnd)` → smug bubble «бзз. так ему и надо 😤» →
  record bite time, mark episode as bitten.
- Cancel at any point if: mood is no longer angry-high (e.g. user said sorry — show
  «ладно, живи... 🐝»), biting toggled off, user starts dragging the bee, the target vanished
  or moved/minimized. On cancel nothing is closed.
- Never close more than one window per episode. Log every decision with `logging`
  (logger `bee_pet.biting`), including why a window was skipped at debug level.

Do not add idle wandering or landing on windows; flying is only for this sequence.

## Tests

Pure `biting` policy with fake `WindowInfo`s (whitelist, Explorer class rule, blocklists,
elevated None/True, own pid, minimized/cloaked/tool/tiny, UWP calculator name);
`pick_target` determinism with a seeded rng; `BiteController` sequence with a fake
winapi module (monkeypatch `list_windows`/`close_window`/`AVAILABLE`), a fake or
shortened timing (make countdown/cooldown durations constructor parameters), checking:
closes exactly once on the happy path; sorry during countdown cancels; target vanished
cancels; toggle off cancels; second angry-high in the same episode does nothing; cooldown
respected. Verify that no code path outside `winapi.close_window` posts messages.

## Documentation

Add `docs/windows-test.md` (Russian): how to set up on Windows 11 (install Python,
venv, run), and a manual QA checklist covering drag, every menu item, chat online and
offline, every mood visual, and the bite sequence including the sorry-cancel case and a Notepad
window with unsaved text (it must ask «Сохранить?» rather than lose data). Update the
«Запуск» section of `CLAUDE.md` if commands changed.

## Done when

Tests green, smoke run ok on Linux (biting inert there), report per common rules, plus a
list of things that can only be verified on real Windows.
