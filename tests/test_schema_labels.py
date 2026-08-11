"""Turning a table's COMMENT ON into a sidebar label. Pure unit tests, no database.

`split_table_comment` is the reason the UI can show "Vessel visits" instead of
"port_calls" without a {table: label} mapping living in the application. The mapping
would have been five lines and a maintenance liability: it duplicates knowledge the
database already holds, and nothing would fail when the two disagreed.

The convention it relies on is that a table comment opens with a noun phrase naming what
the rows ARE. Every comment in db/01_schema.sql already satisfies this, which is checked
against the live catalog in tests/test_schema.py. What is checked here is the parsing,
including the case that reads badly, because a splitter with an unstated failure mode is
worse than one whose limits are written down.
"""

from __future__ import annotations

import pytest

from src.schema import split_table_comment


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        # The five real comments, verbatim from db/01_schema.sql.
        (
            "Vessel visits. One row per visit of a vessel to a terminal. This is the "
            "grain for berth wait, dwell time and visit-count questions.",
            (
                "Vessel visits",
                "One row per visit of a vessel to a terminal. This is the grain for "
                "berth wait, dwell time and visit-count questions.",
            ),
        ),
        (
            "Container terminals. One row per terminal. A port may contain several "
            "terminals.",
            ("Container terminals", "One row per terminal. A port may contain several terminals."),
        ),
        (
            "Quay cranes. One row per crane. Each crane belongs to exactly one terminal.",
            ("Quay cranes", "One row per crane. Each crane belongs to exactly one terminal."),
        ),
    ],
)
def test_the_first_sentence_becomes_the_name_and_the_rest_the_description(comment, expected):
    assert split_table_comment(comment) == expected


@pytest.mark.parametrize("comment", [None, "", "   ", "\n\t "])
def test_a_missing_comment_yields_no_name_so_the_caller_can_fall_back(comment):
    """The caller renders the raw table name when the name is empty. Returning a blank
    rather than raising is what keeps a schema with undocumented tables usable."""
    assert split_table_comment(comment) == ("", "")


def test_a_single_sentence_is_all_name_and_no_description():
    assert split_table_comment("Container terminals.") == ("Container terminals", "")


def test_a_comment_with_no_full_stop_is_still_a_name():
    assert split_table_comment("Container terminals") == ("Container terminals", "")


def test_surrounding_whitespace_does_not_reach_the_label():
    assert split_table_comment("  Vessel visits. One row per visit.  ") == (
        "Vessel visits",
        "One row per visit.",
    )


def test_a_trailing_full_stop_is_stripped_from_the_name_only():
    """The name is rendered in bold as a heading, where a full stop reads as a typo. The
    description is prose and keeps its punctuation."""
    name, description = split_table_comment("Quay cranes. One row per crane.")
    assert name == "Quay cranes"
    assert description == "One row per crane."


def test_an_abbreviation_in_the_first_sentence_splits_early_and_that_is_known():
    """A KNOWN limitation, pinned rather than hidden.

    The split is on the first ". ", so a comment whose opening sentence contains an
    abbreviation is cut at the abbreviation. No comment in this schema does, and the
    alternative is sentence segmentation, which is a dependency and a new failure mode
    for a five-table sidebar. If a future comment needs one, this test is where the
    decision to accept the limitation is recorded.
    """
    assert split_table_comment("Vessels, e.g. ships. One row per vessel.") == (
        "Vessels, e.g",
        "ships. One row per vessel.",
    )
