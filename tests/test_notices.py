"""What is shown beside an answer, and in what order. Pure unit tests, no Streamlit.

These exist because the ordering carries meaning and nothing was checking it. The
truncation warning was moved to sit before the chart on the argument that a caveat about a
picture has to arrive before the picture; that argument is only worth something if a later
edit cannot silently undo it.

The ordering assertions are written against the whole list rather than against individual
members, because "the caveat appears" and "the caveat appears before the chart" are
different claims and only the second one is the property being defended.
"""

from __future__ import annotations

import pytest

from src.models import AgentResult, Outcome, QueryResult
from src.notices import Level, Notice, answer_notices


def _result(**overrides) -> AgentResult:
    base = dict(question="How many berths?", outcome=Outcome.ANSWERED, answer="Six.")
    return AgentResult(**{**base, **overrides})


def _rows(truncated: bool, row_count: int = 500) -> QueryResult:
    return QueryResult(
        columns=["terminal_name"],
        rows=[["Jebel Ali"]],
        row_count=row_count,
        elapsed_s=0.01,
        truncated=truncated,
    )


def test_a_clean_answer_carries_no_notices() -> None:
    """The common case. Every notice is a reason to trust the answer less or a statement
    about what it measured; an answer with nothing to qualify should be uncluttered."""
    assert answer_notices(_result()) == []


def test_the_reading_is_a_caption_not_a_warning() -> None:
    """It says what was measured. That is context, not a defect, and rendering it as a
    warning would teach the reader that every answered question has something wrong."""
    notices = answer_notices(_result(reading="counts berths per terminal"))
    assert notices == [Notice(Level.CAPTION, "What was measured: counts berths per terminal")]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "caveat",
            "counts berths, not calls",
            "Possible mismatch with your question: counts berths, not calls",
        ),
        # The grounding notice does NOT quote the offending figure. The check has known
        # false positives (ADR-006), so naming a number would lend it more authority than
        # it has earned; the message points at the table instead.
        (
            "grounding_flag",
            "1,234",
            "could not be matched to the returned rows",
        ),
    ],
)
def test_an_advisory_finding_is_a_warning_and_says_which_one(
    field: str, value: str, expected: str
) -> None:
    """Asserts the TEXT, not just the level.

    An earlier version checked only that one warning came back. A probe then reworded the
    grounding notice and this test stayed green: the regression was caught solely by the
    combined ordering test, which asserts a substring of the same string for an unrelated
    reason. A branch whose wording is only defended by a distant test is one edit away
    from being undefended.
    """
    notices = answer_notices(_result(**{field: value}))
    assert len(notices) == 1
    assert notices[0].level is Level.WARNING
    assert expected in notices[0].text


def test_a_truncated_result_warns_even_with_nothing_else_to_say() -> None:
    notices = answer_notices(_result(result=_rows(truncated=True)))
    assert len(notices) == 1
    assert notices[0].level is Level.WARNING
    assert "first 500 rows" in notices[0].text


def test_an_untruncated_result_says_nothing_about_row_counts() -> None:
    assert answer_notices(_result(result=_rows(truncated=False))) == []


def test_a_result_that_is_absent_entirely_does_not_raise() -> None:
    """A refusal, a clarification and a provider failure all carry `result=None`. Reading
    `.truncated` off that would be an AttributeError on the error path, which is the worst
    place to have one."""
    assert answer_notices(_result(outcome=Outcome.REFUSED, result=None)) == []


def test_the_order_is_reading_then_caveat_then_grounding_then_truncation() -> None:
    """The contract. All four at once, which is rare in practice and exactly why it is
    worth pinning: it is the case nobody looks at by hand."""
    notices = answer_notices(
        _result(
            reading="counts berths per terminal",
            caveat="counts berths, not calls",
            grounding_flag="1,234",
            result=_rows(truncated=True),
        )
    )

    assert [n.level for n in notices] == [
        Level.CAPTION,
        Level.WARNING,
        Level.WARNING,
        Level.WARNING,
    ]
    assert notices[0].text.startswith("What was measured:")
    assert "Possible mismatch" in notices[1].text
    assert "could not be matched" in notices[2].text
    assert "Partial result" in notices[3].text


def test_truncation_is_the_last_notice_so_it_still_precedes_the_chart() -> None:
    """`app.py` renders this list and then the chart. Truncation being last in the list is
    what puts it immediately above the chart it describes, rather than after it."""
    notices = answer_notices(
        _result(caveat="counts berths, not calls", result=_rows(truncated=True))
    )
    assert "Partial result" in notices[-1].text
