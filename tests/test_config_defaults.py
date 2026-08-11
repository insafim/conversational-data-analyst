"""Tests for environment parsing in `src/config.py`.

Pure unit tests: no database, no network, no model.

These exist for one specific reason. `RUNTIME_VERIFICATION` defaults to false, and that
default is a measured decision (ADR-012's addendum: runs 20 to 25 showed the feature
costing more execution accuracy than it bought in groundedness). The default is therefore
the behaviour every user gets, and it is expressed as a boolean parsed out of a string.

A sign error in that parse is the failure mode worth guarding: it would ship the exact
configuration the measurement rejected, silently, while every other test still passed,
because the rest of the suite states the configuration it wants explicitly. The parse was
also *rewritten* when the default flipped, from a negative `not in (...)` test to a
positive `in (...)` one, which is precisely the edit that inverts a boolean by accident.
"""

from __future__ import annotations

import pytest

from src.config import Settings


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", "ON"])
def test_runtime_verification_is_enabled_only_by_an_affirmative_value(monkeypatch, value):
    monkeypatch.setenv("RUNTIME_VERIFICATION", value)
    assert Settings.load().runtime_verification is True


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "0", "no", "off", "", "nope", "maybe", "2"]
)
def test_anything_else_leaves_runtime_verification_disabled(monkeypatch, value):
    """Unrecognised values fail toward the measured-better configuration rather than
    toward the one the evidence rejected."""
    monkeypatch.setenv("RUNTIME_VERIFICATION", value)
    assert Settings.load().runtime_verification is False


def test_runtime_verification_is_off_when_the_variable_is_unset(monkeypatch):
    """The shipped default. This is the assertion that would have caught the parse being
    inverted when the default was flipped."""
    monkeypatch.delenv("RUNTIME_VERIFICATION", raising=False)
    assert Settings.load().runtime_verification is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", "ON"])
def test_the_reading_is_enabled_by_any_affirmative_value(monkeypatch, value):
    """Pins the allow-list itself, not just the default.

    Without this, narrowing the accepted set from ("1", "true", "yes", "on") to
    ("1", "true") leaves every other test in this file green, so a user who wrote
    SQL_READING=yes would silently get the feature turned off.
    """
    monkeypatch.setenv("SQL_READING", value)
    assert Settings.load().sql_reading is True


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "0", "no", "off", "", "nope", "maybe", "2"]
)
def test_anything_but_an_affirmative_value_disables_the_reading(monkeypatch, value):
    """A positive allow-list, like `runtime_verification`'s parser. An unrecognised
    value disables the reading rather than being treated as a negative, so the name says
    "anything but an affirmative" rather than "an explicit negative"."""
    monkeypatch.setenv("SQL_READING", value)
    assert Settings.load().sql_reading is False


def test_the_reading_is_on_when_the_variable_is_unset(monkeypatch):
    """The shipped default, and the opposite of `runtime_verification`'s.

    The two defaults disagree on purpose. What ADR-012 measured as harmful was the
    regeneration an objection triggers; the reading is the same call's other output and
    changes no SQL, so it defaults on while the objection loop defaults off.
    """
    monkeypatch.delenv("SQL_READING", raising=False)
    assert Settings.load().sql_reading is True


def test_the_history_window_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("HISTORY_TURNS", "5")
    assert Settings.load().history_turns == 5


def test_the_history_window_has_a_default(monkeypatch):
    monkeypatch.delenv("HISTORY_TURNS", raising=False)
    assert Settings.load().history_turns == 3
