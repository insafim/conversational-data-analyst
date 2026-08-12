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

from src.models import AgentResult, Outcome, QueryResult, RetryReason
from src.notices import Level, Notice, answer_notices, turn_telemetry


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


# ---------------------------------------------------------------------------------
# What one turn cost. Collapsed beside the answer, so every word of it is decided here
# and the page opens an expander around the result.
# ---------------------------------------------------------------------------------


def _measured(**overrides) -> AgentResult:
    """A turn that actually reached a provider, which is the precondition for disclosure."""
    base = dict(
        question="How many berths?",
        outcome=Outcome.ANSWERED,
        answer="Six.",
        elapsed_s=6.94,
        llm_calls=4,
        cost_usd=0.0142,
        stage_timings={"generate_sql": 3.322, "classify": 1.947, "summarize": 1.14},
    )
    return AgentResult(**{**base, **overrides})


def test_a_turn_that_called_no_model_discloses_nothing() -> None:
    """The two guards at the top of `ask()`, the empty question and the over-long one,
    refuse before any provider is reached. They have no stages and a duration of roughly
    zero, and "Refused in 0.00s" invites a reader to ask what was measured when the honest
    answer is nothing."""
    assert turn_telemetry(_result(outcome=Outcome.REFUSED, llm_calls=0)) is None


def test_the_label_states_the_outcome_not_only_the_duration() -> None:
    """A bare "6.94s" says the machine was busy. Which outcome it was busy producing is
    the part a reader is entitled to without opening anything, and it is the same fact the
    badge above the answer carries, so the two cannot disagree."""
    assert turn_telemetry(_measured()).label == "Answered in 6.94s"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.ANSWERED, "Answered in 6.94s"),
        (Outcome.CLARIFY, "Asked back in 6.94s"),
        (Outcome.REFUSED, "Refused in 6.94s"),
        (Outcome.REJECTED, "Blocked in 6.94s"),
        # "after", not "in". The seconds were spent and then nothing was delivered;
        # "Failed in 6.94s" reads as though a deadline was met.
        (Outcome.ERROR, "Failed after 6.94s"),
    ],
)
def test_every_outcome_has_its_own_verb(outcome: Outcome, expected: str) -> None:
    """Every terminal state is covered, so a new one cannot fall through to a duration
    with no verb in front of it."""
    assert turn_telemetry(_measured(outcome=outcome)).label == expected


def test_the_cost_of_one_answer_keeps_its_precision() -> None:
    """The regression this function was written around.

    An answered question costs about a cent and a third. Rounded to the cent it reads
    `$0.01`, which is the same string a question costing half as much would produce, so
    the figure stops being a measurement. This is the assertion that fails if the per-unit
    format is ever collapsed back into the aggregate one.
    """
    summary = turn_telemetry(_measured()).summary
    assert "$0.0142" in summary, f"{summary!r} rounded the cost away"


def test_the_summary_names_the_calls_and_the_rows() -> None:
    summary = turn_telemetry(_measured(result=_rows(truncated=False, row_count=2))).summary
    assert "4 model calls" in summary
    assert "2 rows" in summary


def test_a_turn_with_no_query_does_not_claim_zero_rows() -> None:
    """A refusal never ran a query. Reporting "0 rows" would describe an empty result set,
    which is a different and checkable claim about the data."""
    summary = turn_telemetry(_measured(outcome=Outcome.REFUSED, result=None)).summary
    assert "rows" not in summary


def test_a_retry_is_named_rather_than_counted() -> None:
    """ADR-012's accounting rule, applied to the one place a user sees it. "1 retry"
    cannot tell a database error from the verifier changing the query, and the difference
    is the entire reason `retry_reasons` replaced a boolean."""
    summary = turn_telemetry(
        _measured(retry_reasons=[RetryReason.DB_ERROR, RetryReason.VERIFIER_OBJECTION])
    ).summary
    assert "db_error" in summary
    assert "verifier_objection" in summary


def test_a_turn_without_retries_says_nothing_about_them() -> None:
    assert "retried" not in turn_telemetry(_measured()).summary


def test_the_stages_arrive_slowest_first() -> None:
    """The reason to open this is to find out what took the time, so the answer to that
    question is the first line rather than somewhere in an alphabetical list."""
    stages = turn_telemetry(_measured()).stages
    assert [name for name, _ in stages] == ["generate_sql", "classify", "summarize"]
    assert [seconds for _, seconds in stages] == sorted(
        (seconds for _, seconds in stages), reverse=True
    )


def test_a_turn_with_no_stage_timings_still_discloses_its_total() -> None:
    """A provider failure raised out of the graph returns a result with an elapsed time and
    an empty breakdown. The disclosure must degrade to the total rather than vanish, since
    a failure that took nine seconds is exactly when someone wants the number."""
    telemetry = turn_telemetry(
        _measured(outcome=Outcome.ERROR, stage_timings={}, elapsed_s=9.1)
    )
    assert telemetry is not None
    assert telemetry.label == "Failed after 9.10s"
    assert telemetry.stages == []
