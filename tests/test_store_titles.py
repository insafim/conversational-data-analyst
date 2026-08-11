"""Conversation titles. Pure unit tests, no database.

Split from `tests/test_store.py`, which needs a live PostgreSQL, because this is string
handling and there is no reason to make a reviewer start a container to check it. The same
split exists between `test_schema_labels.py` and `test_schema.py`.
"""

from __future__ import annotations

from src.store import title_for


def test_a_short_question_is_its_own_title() -> None:
    assert title_for("Which terminal is busiest?") == "Which terminal is busiest?"


def test_a_long_question_is_cut_on_a_word_boundary() -> None:
    question = (
        "Show the total number of containers moved each month during 2025 across every terminal"
    )
    title = title_for(question)

    assert title.endswith("...")
    stem = title.removesuffix("...")
    assert not stem.endswith(" "), "the cut left a trailing space before the ellipsis"
    assert question.startswith(stem), "the title is not a prefix of the question"
    assert stem.split()[-1] in question.split(), "the last word was cut in half"


def test_a_single_enormous_word_is_cut_anyway() -> None:
    """The word-boundary rule must not produce an empty title for a question with no
    spaces in it, which is what a naive rsplit would do."""
    title = title_for("x" * 200)
    assert title.endswith("...") and len(title) < 80


def test_whitespace_is_normalised_so_a_pasted_question_does_not_break_the_list() -> None:
    assert title_for("  Which   terminal\n\nis busiest? ") == "Which terminal is busiest?"


def test_an_empty_question_still_gets_a_usable_title() -> None:
    """`ask()` refuses an empty question before the store is reached, but the store must
    not depend on a guarantee made somewhere else."""
    assert title_for("   ") == "Untitled chat"
