"""Тесты автомата настроения."""
import pytest

from bee_pet.mood import (ACTION_PHRASES, Action, Mood, MoodEngine, classify)


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
