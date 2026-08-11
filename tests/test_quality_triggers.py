"""Tests for the code-detected quality triggers (ADR-012).

No database and no model: these are pure functions over a question and a result shape.

The bias under test is asymmetric, and it is the reason the detector is written the way
it is. A MISSED trigger leaves the system exactly where it stood before this module
existed. A FALSE trigger spends a regeneration and can replace a correct query with a
worse one: it takes a right answer and makes it wrong. So the negative cases below
matter more than the positive ones, and there are deliberately more of them.
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.models import QueryResult, Route
from src.quality import asks_for_a_single_row, detect_quality_issue


def _result(row_count: int, truncated: bool = False) -> QueryResult:
    return QueryResult(
        columns=["label", "measure"],
        rows=[[f"row{i}", i] for i in range(min(row_count, 3))],
        row_count=row_count,
        elapsed_s=0.01,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------------
# Singular-superlative grammar. The rule at src/prompts.py that this detects breaches of.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "Which terminal has the longest average berth wait?",
        "What is the busiest terminal?",
        "Which crane moved the most containers?",
        "Which vessel had the highest berth wait?",
        "What was the worst month for cancellations?",
        # The measure is plural; the SUBJECT is not, and only the subject decides.
        "Which terminal moved the most containers?",
    ],
)
def test_singular_superlatives_are_detected(question) -> None:
    assert asks_for_a_single_row(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Plural head noun: many rows is the right shape.
        "Which terminals have the longest waits?",
        "What are the busiest terminals?",
        # An explicit count overrides the grammar.
        "Which three operators moved the most containers?",
        "Which 5 vessels called most often?",
        "Show me the top 10 cranes by containers moved.",
        # Per-group questions return every group.
        "Which terminal is busiest for each month?",
        "What is the highest wait per terminal?",
        "Give me a breakdown of the largest operators.",
        # No superlative at all.
        "How many port calls were there in 2025?",
        "Which terminals are in the Netherlands?",
        # Superlative, but not an interrogative asking for a row.
        "The busiest terminal moved a lot of containers.",
        "",
    ],
)
def test_non_singular_questions_are_not_detected(question) -> None:
    assert asks_for_a_single_row(question) is False


def test_a_year_is_not_mistaken_for_a_row_count() -> None:
    """"in 2025" must not read as an explicit count. A digit-anywhere rule would silence
    the detector on most real questions, which is how a check quietly stops checking."""
    assert asks_for_a_single_row("Which terminal was busiest in 2025?") is True


# ---------------------------------------------------------------------------------
# The three triggers.
# ---------------------------------------------------------------------------------
def test_multi_row_result_for_a_singular_superlative_fires() -> None:
    issue = detect_quality_issue(
        "Which terminal has the most berths?", Route.ANSWERABLE.value, _result(6)
    )
    assert issue is not None and issue.code == "multi_row_for_singular"
    assert "LIMIT 1" in issue.message


def test_a_single_row_answer_to_a_singular_superlative_does_not_fire() -> None:
    assert detect_quality_issue(
        "Which terminal has the most berths?", Route.ANSWERABLE.value, _result(1)
    ) is None


def test_an_empty_result_fires() -> None:
    issue = detect_quality_issue("How many port calls?", Route.ANSWERABLE.value, _result(0))
    assert issue is not None and issue.code == "empty_result"


def test_the_empty_result_message_permits_an_honest_empty_answer() -> None:
    """Without this escape the trigger would push the model to invent a filter that
    returns SOMETHING, which is the failure the whole system is built against. A
    deliberately empty gold case (q56) is a correct answer, not a defect.
    """
    issue = detect_quality_issue("How many port calls?", Route.ANSWERABLE.value, _result(0))
    assert issue is not None
    assert "return the same query again unchanged" in issue.message


def test_a_truncated_result_fires() -> None:
    issue = detect_quality_issue(
        "List every port call.", Route.ANSWERABLE.value, _result(settings.row_cap, truncated=True)
    )
    assert issue is not None and issue.code == "row_cap_saturated"


@pytest.mark.parametrize("route", ["ambiguous", "out_of_scope"])
def test_nothing_fires_for_a_question_that_was_not_routed_answerable(route) -> None:
    """An empty result is the CORRECT outcome for a refused or clarified question."""
    assert detect_quality_issue("anything", route, _result(0)) is None


def test_a_clean_result_fires_nothing() -> None:
    assert detect_quality_issue(
        "How many containers moved each month?", Route.ANSWERABLE.value, _result(12)
    ) is None


def test_only_one_issue_is_reported_at_a_time() -> None:
    """The budget allows one regeneration, and a prompt naming several problems is one
    the model satisfies partially."""
    issue = detect_quality_issue(
        "Which terminal is busiest?",
        Route.ANSWERABLE.value,
        _result(settings.row_cap, truncated=True),
    )
    assert issue is not None and issue.code == "row_cap_saturated"
