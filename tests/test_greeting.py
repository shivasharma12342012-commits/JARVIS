"""The line the window opens on.

Three promises to keep: it appears instantly, it is different every time, and
the model wrote it. The first two have to hold even when the model is slow,
absent, or answering with three paragraphs of preamble — so most of what is
here is about what happens when the model misbehaves.
"""
from __future__ import annotations

import threading
import time

import pytest

from jarvis import greeting as greeting_mod
from jarvis.greeting import Bank, Greeter, compose, fallback, tidy


def _no_greeting_threads(timeout=6.0):
    """Wait for every background writer to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name == "greeting" for t in threading.enumerate()):
            return
        time.sleep(0.02)
    raise AssertionError("a greeting thread never finished")


@pytest.fixture(autouse=True)
def _bank_in_tmp(tmp_path, monkeypatch):
    """Never touch the real bank in the checkout.

    A writer left over from the test before would otherwise arrive late and
    save its line into *this* test's bank, which is a test artefact rather than
    anything a launched window can do — so let them all land first.
    """
    _no_greeting_threads()
    monkeypatch.setattr(greeting_mod, "BANK_PATH", tmp_path / "greetings.json")
    yield
    _no_greeting_threads()


class Model:
    """A model that writes greetings, and can be made slow or broken."""

    def __init__(self, lines=(), delay=0.0, fail=False):
        self.lines = list(lines)
        self.delay = delay
        self.fail = fail
        self.asked = []

    def aside(self, instruction, **kwargs):
        self.asked.append((instruction, kwargs.get("system", "")))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("ollama is not running")
        return self.lines.pop(0) if self.lines else ""


# ── the bank ────────────────────────────────────────────────────────────────────
def test_a_line_survives_the_window_closing():
    Bank.load().add("Evening, Sir.")
    assert "Evening, Sir." in Bank.load().lines


def test_the_same_line_is_never_banked_twice():
    bank = Bank.load()
    assert bank.add("Morning, Sir.")
    assert not bank.add("Morning, Sir.")
    assert bank.lines.count("Morning, Sir.") == 1


def test_blank_lines_are_not_banked():
    bank = Bank.load()
    assert not bank.add("   ")
    assert bank.lines == []


def test_the_bank_stops_growing():
    bank = Bank.load()
    for index in range(greeting_mod.BANK_SIZE * 3):
        bank.add(f"Greeting number {index}, Sir.")
    assert len(bank.lines) == greeting_mod.BANK_SIZE
    # It is the oldest that go, not the newest.
    assert bank.lines[-1].startswith(f"Greeting number {greeting_mod.BANK_SIZE * 3 - 1}")


def test_taking_never_repeats_what_was_just_shown():
    bank = Bank.load()
    for line in ("One, Sir.", "Two, Sir.", "Three, Sir."):
        bank.add(line)
    shown = [bank.take() for _ in range(40)]
    assert all(shown[i] != shown[i + 1] for i in range(len(shown) - 1))
    # And over forty draws it has used all three, not alternated between two.
    assert set(shown) == {"One, Sir.", "Two, Sir.", "Three, Sir."}


def test_one_banked_line_is_shown_rather_than_nothing():
    """With a single line, "not the last one" is impossible — say it anyway."""
    bank = Bank.load()
    bank.add("The only one, Sir.")
    assert bank.take() == "The only one, Sir."
    assert bank.take() == "The only one, Sir."


def test_an_empty_bank_hands_back_nothing():
    assert Bank.load().take() == ""


def test_which_line_was_last_survives_a_restart():
    bank = Bank.load()
    for line in ("One, Sir.", "Two, Sir."):
        bank.add(line)
    first = bank.take()
    assert Bank.load().take() != first


def test_an_unreadable_bank_is_a_fresh_bank(tmp_path):
    greeting_mod.BANK_PATH.write_text("{not json at all", encoding="utf-8")
    assert Bank.load().lines == []


# ── tidying what the model returned ─────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("Evening, Sir.", "Evening, Sir."),
    ('  Evening, Sir.  ', "Evening, Sir."),
    ('"Evening, Sir."', "Evening, Sir."),
    ("'Evening, Sir.'", "Evening, Sir."),
    ("“Evening, Sir.”", "Evening, Sir."),
    ("Here is a greeting: Evening, Sir.", "Evening, Sir."),
    ("Here's one for you: Evening, Sir.", "Evening, Sir."),
    ("Greeting: Evening, Sir.", "Evening, Sir."),
    ("**Evening**, Sir.", "Evening, Sir."),
    ("```\nEvening, Sir.\n```", "Evening, Sir."),
    ("Evening, Sir.\n\nLet me know what you need!", "Evening, Sir."),
])
def test_the_model_is_taken_at_its_best(raw, expected):
    assert tidy(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "...", "###", "*"])
def test_nothing_usable_is_nothing(raw):
    assert tidy(raw) == ""


def test_an_essay_is_cut_to_two_sentences():
    essay = ("Evening, Sir. All is quiet. " + "And here is a third thought. " * 12)
    kept = tidy(essay)
    assert kept == "Evening, Sir. All is quiet."


def test_an_essay_with_no_sentence_breaks_is_refused():
    assert tidy("word " * 200) == ""


def test_devanagari_counts_as_letters():
    assert tidy("नमस्ते, Sir.") == "नमस्ते, Sir."


# ── asking for one ──────────────────────────────────────────────────────────────
def test_the_prompt_says_who_and_when():
    model = Model(["Evening, Sir."])
    assert compose(model) == "Evening, Sir."
    instruction, system = model.asked[0]
    assert "Sir" in system
    assert any(part in instruction
               for part in ("morning", "afternoon", "evening", "hours"))


def test_recent_lines_are_shown_so_they_are_not_echoed():
    model = Model(["Evening, Sir."])
    compose(model, recent=["Morning, Sir.", "Afternoon, Sir."])
    instruction, _ = model.asked[0]
    assert "Morning, Sir." in instruction and "Afternoon, Sir." in instruction


def test_a_low_battery_is_worth_a_mention():
    class Telemetry:
        cpu_percent = 4
        ram_percent = 30
        battery_percent = 11

    model = Model(["Evening, Sir."])
    compose(model, telemetry=Telemetry())
    assert "11 per cent" in model.asked[0][0]


def test_a_model_that_raises_costs_nothing():
    assert compose(Model(fail=True)) == ""


def test_something_that_is_not_a_model_costs_nothing():
    assert compose(object()) == ""
    assert compose(None) == ""


# ── the greeter ─────────────────────────────────────────────────────────────────
def test_there_is_always_a_line_even_with_nothing_banked():
    line = Greeter().current()
    assert line
    assert "Sir" in line


def test_the_shipped_lines_are_addressed_to_you():
    assert "Sir" in fallback()


def test_a_banked_line_is_preferred_to_a_shipped_one():
    bank = Bank.load()
    bank.add("A line the model wrote, Sir.")
    greeter = Greeter()
    assert greeter.current() == "A line the model wrote, Sir."


def test_asking_for_the_next_one_does_not_block():
    greeter = Greeter(Model(["Written slowly, Sir."], delay=1.5))
    greeter.current()
    started = time.monotonic()
    greeter.refresh()
    assert time.monotonic() - started < 0.4


def _settle(_greeter=None):
    _no_greeting_threads()


def test_a_quick_answer_replaces_what_is_on_screen():
    arrived = []
    greeter = Greeter(Model(["Fresh off the press, Sir."]), on_fresh=arrived.append)
    greeter.current()
    greeter.refresh()
    _settle(greeter)
    assert arrived == ["Fresh off the press, Sir."]
    assert "Fresh off the press, Sir." in Bank.load().lines


def test_a_late_answer_is_banked_rather_than_shown(monkeypatch):
    monkeypatch.setattr(greeting_mod, "SWAP_WINDOW", 0.0)
    arrived = []
    greeter = Greeter(Model(["Too late, Sir."]), on_fresh=arrived.append)
    greeter.current()
    greeter.refresh()
    _settle(greeter)
    assert arrived == []
    assert "Too late, Sir." in Bank.load().lines


def test_a_broken_model_changes_nothing():
    arrived = []
    greeter = Greeter(Model(fail=True), on_fresh=arrived.append)
    line = greeter.current()
    greeter.refresh()
    _settle(greeter)
    assert arrived == []
    assert line


def test_a_window_that_throws_does_not_kill_the_thread():
    def hostile(_line):
        raise RuntimeError("the window is gone")

    greeter = Greeter(Model(["Evening, Sir."]), on_fresh=hostile)
    greeter.current()
    greeter.refresh()
    _settle(greeter)
    assert "Evening, Sir." in Bank.load().lines


def test_turning_it_off_stops_asking(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "GREETING_FROM_MODEL", False)
    model = Model(["Evening, Sir."])
    greeter = Greeter(model)
    greeter.current()
    greeter.refresh()
    _settle(greeter)
    assert model.asked == []


def test_with_no_model_at_all_it_still_greets():
    greeter = Greeter(None)
    assert greeter.current()
    greeter.refresh()      # must not raise
