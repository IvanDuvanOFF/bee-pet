# Stage 2 — the bee window: drawing, animation, drag, context menu, bubble

Read `docs/spec/00-common.md` first. Stage 1 (skeleton, `mood.py`) is done; read it.

## Deliverables

`bee_pet/painter.py`, `bee_pet/bubble.py`, `bee_pet/bee_window.py`, wiring in
`bee_pet/app.py`, tests in `tests/test_bee_window.py` (offscreen).

## Bee window (`BeeWindow(QWidget)`)

- Flags: `Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool`,
  attribute `WA_TranslucentBackground`; `WA_ShowWithoutActivating` so it does not steal focus.
- Fixed logical size ~120×120 (bee ~80 px, room above for the mood badge). Initial
  position: bottom-right of the primary screen's `availableGeometry()` with a margin.
- Animation: one `QTimer` at ~33 ms drives a phase value; `update()` repaints.
  Idle: gentle vertical bobbing of the drawing inside the window (not moving the window)
  and wing flapping.
- Mood visuals (drawn by painter.py): HAPPY — small smile + 😊-style sparkle;
  LOVING — floating hearts; SAD — tear drops, droopy antennae; ANGRY — red tint on
  face, angry brows, sparks; angry-high — more sparks, faster jitter (random offsets of a few px)
  and faster wings; CONFUSED — "?" above the head; NEUTRAL — nothing extra.
- Drag: left-button press + move moves the window (use `globalPosition()` deltas;
  keep the window within the virtual desktop). While dragged the bee "struggles":
  fast wings, side-to-side wiggle, legs kicking. Release drops it in place.
  A plain click without movement (< 4 px) does nothing for now.
- Flight API for later stages: `fly_to(target: QPoint, on_arrived: Callable | None)`
  that moves the window's top-left smoothly (e.g. `QPropertyAnimation` on `pos`,
  ~400 px/s, `QEasingCurve.InOutSine`, duration clamped 300–3000 ms); the bee tilts in the flight
  direction while flying. Starting a drag cancels a flight.
- Mood: holds a `MoodEngine`; a 1 s `QTimer` calls `tick()`. Signal
  `mood_changed(MoodChange)` (or a Python object signal). Method
  `apply_action(action: Action)` applies to the engine, repaints, emits signals.
  Signal `action_performed(Action)` emitted for every menu action (stage 3 sends the
  phrase to BeeLLM).

## painter.py

Pure drawing function(s), e.g. `paint_bee(p: QPainter, rect: QRectF, state: PaintState)`
where `PaintState` is a dataclass (phase, mood, angry_high, dragging, tilt, wiggle).
Antialiased, cute vector bee: yellow body with 2–3 dark stripes, translucent white
wings (flap via phase), head with eyes, antennae, small stinger. No images.

## Context menu (right click, `QMenu`, Russian labels)

- «🌸 Дать цветок» → FLOWER
- «🤚 Погладить» → PET
- «👋 Шлёпнуть» → SWAT
- «🙏 Извиниться» → SORRY
- «💨 Побрызгать дихлофосом» → SPRAY
- separator
- «💬 Чат» → emits `chat_requested` signal (stage 3 implements the chat window; for now
  connect it to nothing or a placeholder bubble «чат скоро будет»)
- «🦷 Разрешить кусать окна» — checkable, default **checked**, persisted with
  `QSettings("bee-pet", "bee-pet")` key `biting_enabled`; expose a property
  `biting_enabled` and a signal on toggle (stage 4 uses it)
- separator
- «❌ Выход» → `QApplication.quit()`

## Speech bubble (`bubble.py`)

`SpeechBubble` — separate frameless, translucent, always-on-top tool window that does not
take focus. `show_text(text: str, anchor: QRect, duration_ms: int = 4000)`: rounded
rectangle with a little tail pointing at the bee, word-wrapped text (max width ~240 px),
positioned above the anchor (below if no room; clamped to the screen), auto-hides after
duration; a new call replaces the text and restarts the timer. The bee window owns one
bubble and repositions it when the bee moves (drag/flight) while it is visible.
`BeeWindow.say(text, duration_ms=4000)` is the public helper.

After each menu action, show a short canned Russian reaction in the bubble (local, until
stage 3 adds the model), e.g. flower → «ура, цветочек! 🌼», swat → «БЗЗ!! 😠»,
lovebomb while angry → «не подлизывайся! 😡», sorry while angry → «ладно... бзз».
Keep these in one dict in `bee_window.py` or a small `lines.py`.

## Tests (offscreen)

Window creates and shows; `apply_action` changes mood and emits signals; biting toggle
default True and persists (use a temporary `QSettings` scope/organization in tests or
call `QSettings.setPath` with a tmp dir); `fly_to` finishes and calls `on_arrived` (use
`QTest.qWait` or process events, keep under ~2 s); painter renders every mood into a
`QImage` without errors; bubble shows and hides.

## Done when

Tests green, smoke run ok (exit 124, no traceback), report per common rules. Also, if you
can, render each mood to PNGs in the scratch dir `/tmp/claude-1000/bee-preview/` (offscreen
`QImage.save`) and list the paths in the report so the result can be eyeballed.
