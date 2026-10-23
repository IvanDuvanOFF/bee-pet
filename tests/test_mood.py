"""Тесты автомата настроения."""
import pytest

from bee_pet.mood import (ACTION_PHRASES, ANGRY_HIGH_DECAY, Action, Mood, MoodEngine,
                          classify, parse_mood_tag)


class FakeClock:
    """Управляемые часы."""
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make() -> tuple[MoodEngine, FakeClock]:
    clock = FakeClock()
    return MoodEngine(clock), clock


def angry() -> tuple[MoodEngine, FakeClock]:
    eng, clock = make()
    eng.apply(Action.SWAT)
    return eng, clock


@pytest.mark.parametrize("text,mood", [
    ("hi bee!", Mood.NEUTRAL),
    ("*gives you a flower*", Mood.HAPPY),
    ("i love you", Mood.LOVING),
    ("*sprays you with bug spray*", Mood.SAD),
    ("*swats you*", Mood.ANGRY),
    ("*pets you*", Mood.HAPPY),
    ("asdfghjkl", Mood.CONFUSED),
])
def test_mood_table(text, mood):
    eng, _ = make()
    eng.apply(classify(text))
    assert eng.mood is mood


def test_phrases_roundtrip():
    for action, phrase in ACTION_PHRASES.items():
        assert classify(phrase) is action
        assert classify("  " + phrase.upper() + " ") is action


def test_gibberish_and_other():
    assert classify("asdfghjkl") is Action.GIBBERISH
    assert classify("qwrtpsdf") is Action.GIBBERISH
    assert classify("ывапр") is Action.GIBBERISH
    assert classify("") is Action.OTHER
    assert classify("what is the weather") is Action.OTHER
    assert classify("this") is Action.OTHER  # "hi" внутри слова не считается


def test_priority_and_russian():
    assert classify("sorry for the swat") is Action.SORRY
    assert classify("swat the bug spray") is Action.SWAT
    assert classify("прости") is Action.SORRY
    assert classify("шлёпнуть тебя") is Action.SWAT
    assert classify("дихлофос") is Action.SPRAY
    assert classify("дарю цветок") is Action.FLOWER
    assert classify("я люблю тебя") is Action.LOVE
    assert classify("погладить") is Action.PET
    assert classify("привет") is Action.HI
    assert classify("🌸") is Action.FLOWER
    assert classify("❤") is Action.LOVE


def test_angry_sorry():
    eng, _ = angry()
    ch = eng.apply(Action.SORRY)
    assert eng.mood is Mood.NEUTRAL and not eng.is_angry_high
    assert not ch.became_angry_high


@pytest.mark.parametrize("action", [Action.FLOWER, Action.LOVE, Action.PET,
                                    Action.SWAT, Action.SPRAY])
def test_angry_escalates(action):
    eng, _ = angry()
    assert not eng.is_angry_high
    first = eng.apply(action)
    assert eng.mood is Mood.ANGRY and eng.is_angry_high
    assert first.became_angry_high
    assert not eng.apply(action).became_angry_high


def test_angry_other_unchanged():
    eng, _ = angry()
    ch = eng.apply(Action.OTHER)
    assert ch.new is Mood.ANGRY and not ch.angry_high
    eng.apply(Action.HI)
    assert eng.mood is Mood.ANGRY


def test_other_keeps_mood():
    eng, _ = make()
    eng.apply(Action.FLOWER)
    eng.apply(Action.OTHER)
    assert eng.mood is Mood.HAPPY


def test_changed_at_reset_only_on_change():
    eng, clock = make()
    eng.apply(Action.FLOWER)
    t0 = eng.changed_at
    clock.t += 10
    eng.apply(Action.PET)  # HAPPY -> HAPPY
    assert eng.changed_at == t0
    eng.apply(Action.SPRAY)
    assert eng.changed_at == clock.t


def test_decay_other_mood():
    eng, clock = make()
    eng.apply(Action.FLOWER)
    clock.t += 119
    assert eng.tick() is None
    clock.t += 1
    ch = eng.tick()
    assert ch is not None and ch.new is Mood.NEUTRAL
    assert eng.tick() is None


def test_decay_chain():
    eng, clock = angry()
    eng.apply(Action.FLOWER)
    clock.t += 59
    assert eng.tick() is None
    clock.t += 1
    ch = eng.tick()
    assert ch.new is Mood.ANGRY and not ch.angry_high and not eng.is_angry_high
    clock.t += 179
    assert eng.tick() is None
    clock.t += 1
    assert eng.tick().new is Mood.NEUTRAL


def test_angry_decay_from_swat():
    eng, clock = angry()
    clock.t += 180
    assert eng.tick().new is Mood.NEUTRAL


def test_provocation_while_angry_extends_decay():
    """Злость отсчитывается от последней провокации, а не от первого шлепка."""
    eng, clock = angry()          # t=1000: шлепок
    eng.apply(Action.FLOWER)      # angry high
    clock.t += 50
    ch = eng.apply(Action.FLOWER)  # t=1050: ещё цветок — снова провокация
    assert eng.changed_at == clock.t and not ch.became_angry_high
    clock.t += 50                 # t=1100: прошло 100 с от начала, 50 от провокации
    assert eng.tick() is None
    assert eng.mood is Mood.ANGRY and eng.is_angry_high
    clock.t += 9                  # 59 с от последней провокации
    assert eng.tick() is None
    clock.t += 1                  # 60 с
    ch = eng.tick()
    assert ch.new is Mood.ANGRY and not eng.is_angry_high


@pytest.mark.parametrize("action", [Action.PET, Action.LOVE, Action.SWAT, Action.SPRAY])
def test_escalator_while_angry_high_keeps_it(action):
    eng, clock = angry()
    eng.apply(Action.FLOWER)
    clock.t += 30
    ch = eng.apply(action)
    assert ch.new is Mood.ANGRY and ch.angry_high and eng.changed_at == clock.t


def test_angry_pet_never_turns_happy():
    """Гладить злую пчелу можно сколько угодно: картинка остаётся злой, пока не затухнет."""
    eng, clock = angry()
    for _ in range(10):
        clock.t += 100
        eng.tick()
        eng.apply(Action.PET)
        assert eng.mood is Mood.ANGRY


def test_other_while_angry_refreshes_plain_anger():
    eng, clock = angry()
    clock.t += 170
    ch = eng.apply(Action.OTHER)
    assert ch.new is Mood.ANGRY and not ch.angry_high and eng.changed_at == clock.t
    clock.t += 179
    assert eng.tick() is None
    clock.t += 1
    assert eng.tick().new is Mood.NEUTRAL


def test_sorry_still_calms_angry_high():
    eng, clock = angry()
    eng.apply(Action.FLOWER)
    ch = eng.apply(Action.SORRY)
    assert ch.new is Mood.NEUTRAL and not eng.is_angry_high


# --- настроение из тега модели -----------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("[happy:low] bzz!", (Mood.HAPPY, False)),
    ("[happy:high] bzz!", (Mood.HAPPY, False)),
    ("[angry:low] BZZT", (Mood.ANGRY, False)),
    ("[angry:high] *STINGS*", (Mood.ANGRY, True)),
    ("  [ ANGRY : High ]BZZT", (Mood.ANGRY, True)),
    ("[angry] bzz", (Mood.ANGRY, False)),
    ("[confused] bzz?", (Mood.CONFUSED, False)),
    ("[Loving:low] bzz", (Mood.LOVING, False)),
    ("[neutral:low]", (Mood.NEUTRAL, False)),
    ("[sad:high] bzz...", (Mood.SAD, False)),
    ("[grumpy:high] bzz", None),
    ("bzz [happy:low]", None),
    ("bzz", None),
    ("", None),
])
def test_parse_mood_tag(raw, expected):
    assert parse_mood_tag(raw) == expected


def test_set_from_model_sets_state_and_refreshes():
    eng, clock = make()
    clock.t += 5
    change = eng.set_from_model(Mood.HAPPY, False)
    assert (change.old, change.new, eng.mood) == (Mood.NEUTRAL, Mood.HAPPY, Mood.HAPPY)
    assert eng.changed_at == clock.t
    clock.t += 5
    eng.set_from_model(Mood.HAPPY, False)  # то же настроение — таймер всё равно заново
    assert eng.changed_at == clock.t


def test_set_from_model_became_angry_high_once():
    eng, clock = make()
    first = eng.set_from_model(Mood.ANGRY, False)
    assert not first.angry_high and not first.became_angry_high
    high = eng.set_from_model(Mood.ANGRY, True)
    assert high.angry_high and high.became_angry_high and eng.is_angry_high
    again = eng.set_from_model(Mood.ANGRY, True)
    assert again.angry_high and not again.became_angry_high
    calm = eng.set_from_model(Mood.NEUTRAL, False)
    assert calm.new is Mood.NEUTRAL and not eng.angry_high


def test_set_from_model_high_only_for_angry_and_decays():
    eng, clock = make()
    assert not eng.set_from_model(Mood.HAPPY, True).angry_high
    eng.set_from_model(Mood.ANGRY, True)
    clock.t += ANGRY_HIGH_DECAY
    change = eng.tick()
    assert change is not None and change.new is Mood.ANGRY and not change.angry_high


# --- удержание мышкой: GRAB и RELEASE -------------------------------------------

def test_grab_release_phrases_classified_exactly():
    assert ACTION_PHRASES[Action.GRAB] == "*grabs your wings*"
    assert ACTION_PHRASES[Action.RELEASE] == "*lets you go* sorry"
    assert classify("*grabs your wings*") is Action.GRAB
    assert classify("*lets you go* sorry") is Action.RELEASE


def test_grab_makes_calm_bee_angry_then_angry_high():
    eng, _ = make()
    change = eng.apply(Action.GRAB)
    assert eng.mood is Mood.ANGRY and not eng.angry_high and not change.became_angry_high
    change = eng.apply(Action.GRAB)
    assert eng.mood is Mood.ANGRY and eng.angry_high and change.became_angry_high


def test_release_calms_angry_bee_like_sorry():
    eng, _ = angry()
    eng.apply(Action.RELEASE)
    assert eng.mood is Mood.NEUTRAL and not eng.angry_high
    eng, _ = angry()
    eng.apply(Action.GRAB)
    eng.apply(Action.RELEASE)
    assert eng.mood is Mood.NEUTRAL


@pytest.mark.parametrize("start", [Action.HI, Action.FLOWER, Action.LOVE, Action.SPRAY])
def test_release_does_not_change_calm_mood(start):
    eng, _ = make()
    eng.apply(start)
    before = eng.mood
    eng.apply(Action.RELEASE)
    assert eng.mood is before
