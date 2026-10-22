# Stage 1 — project skeleton and mood engine

Read `docs/spec/00-common.md` first.

## Deliverables

1. `requirements.txt` (`PySide6`), `requirements-dev.txt` (`-r requirements.txt`, `pytest`),
   `.gitignore` (`.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`).
2. `bee_pet/__init__.py`, `bee_pet/__main__.py` (calls `bee_pet.app.main()`),
   `bee_pet/app.py` with a minimal `main()` that creates a `QApplication`, sets
   `setQuitOnLastWindowClosed(False)` and runs the event loop (a placeholder; stage 2
   adds the bee window). Use `sys.exit(app.exec())`.
3. `bee_pet/mood.py` — pure Python, no Qt imports.
4. `tests/test_mood.py`.
5. Create the venv and install deps; make sure tests pass and the smoke run works.

## mood.py spec

```python
class Mood(Enum): NEUTRAL, HAPPY, LOVING, SAD, ANGRY, CONFUSED
class Action(Enum): HI, FLOWER, LOVE, PET, SPRAY, SWAT, SORRY, GIBBERISH, OTHER
```

`ACTION_PHRASES: dict[Action, str]` — the English phrase sent to BeeLLM for menu actions:
HI `hi bee!`, FLOWER `*gives you a flower*`, LOVE `i love you`, PET `*pets you*`,
SPRAY `*sprays you with bug spray*`, SWAT `*swats you*`, SORRY `sorry`.

`classify(text: str) -> Action` — maps free chat text to an action:
- case-insensitive, strip whitespace; an exact match with a phrase above wins;
- otherwise keyword search (word-ish matching, English + Russian):
  sorry/sry/apolog/прости/извини → SORRY; swat/slap/hit/шлёп/шлеп/ударить → SWAT;
  spray/bug spray/дихлофос/брызг → SPRAY; flower/🌸/🌼/цвет → FLOWER;
  love/❤/люблю → LOVE; pet/pat/погладить/глажу → PET; hi/hello/hey/привет → HI;
  if several keywords match, priority: SORRY > SWAT > SPRAY > FLOWER > LOVE > PET > HI;
- otherwise GIBBERISH if the text looks like a keyboard mash: a single token of ≥5
  letters with vowel ratio < 0.25 (count `aeiouy` and Russian vowels), e.g. `asdfghjkl`,
  `qwrtpsdf`, `ывапр`; empty text → OTHER;
- otherwise OTHER.

`MoodEngine` — the state machine. Constructor takes `clock: Callable[[], float] =
time.monotonic` (injectable for tests). State: `mood: Mood`, `angry_high: bool`,
`changed_at: float`.

`apply(action: Action) -> MoodChange` where `MoodChange` is a frozen dataclass with
`old: Mood`, `new: Mood`, `angry_high: bool`, `became_angry_high: bool` (True only on
the transition from not-angry-high to angry-high).

Transition rules:
- If currently ANGRY:
  - SORRY → NEUTRAL (angry_high cleared);
  - FLOWER, LOVE, PET, SWAT, SPRAY → ANGRY with angry_high = True (love-bombing detection);
  - anything else → unchanged.
- Otherwise: HI → NEUTRAL, FLOWER → HAPPY, LOVE → LOVING, PET → HAPPY, SPRAY → SAD,
  SWAT → ANGRY (angry_high False), SORRY → NEUTRAL, GIBBERISH → CONFUSED,
  OTHER → unchanged.
- Every apply (even unchanged) resets `changed_at` only if the mood or angry_high changed.

Decay — `tick() -> MoodChange | None`, called periodically by the GUI:
- angry_high for ≥ 60 s → plain ANGRY (angry_high False);
- ANGRY (not high) for ≥ 180 s → NEUTRAL;
- any other non-neutral mood for ≥ 120 s → NEUTRAL;
- returns the change or None. Durations are module constants.

Also `is_angry_high` property.

## Tests (tests/test_mood.py)

Cover: every row of the mood table in `CLAUDE.md`; the `asdfghjkl` → CONFUSED path via
`classify`; angry + sorry → neutral; angry + flower / love / pet → angry-high with
`became_angry_high` True once (second time False); swat while angry → angry-high;
keyword priority (`sorry for the swat` → SORRY); Russian keywords; decay timings with a
fake clock (including angry-high → angry → neutral chain); OTHER keeps mood.

## Done when

`pytest` green, smoke run exits with 124 and no traceback, report written per common rules.
