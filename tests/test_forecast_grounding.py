"""Forecasting: the failure the groundedness scorer cannot see.

The eval's groundedness check is set-membership. Every figure in the answer must appear
in the returned rows. That catches a number the model invented, which is the failure it
was built for (ADR-006, the 228,499 case).

It cannot catch this one. Asked to project July 2026 port calls, the agent wrote
`SELECT ROUND(AVG(monthly_count), 2) AS projected_port_calls_july_2026`, a plain average
over every month on record, and reported the result as what to expect in July. The data
window ends 2026-06-30, so July has no rows at all. The number was real, retrieved, and
present in the results, so set-membership scored it grounded. Only the claim about what
it meant was false, and a structural check has no way to see that.

Two properties of that SQL are worth naming, because they rule out the fixes that look
obvious first:

* There is no `WHERE` clause on July 2026, and no date literal anywhere in the statement.
  A data-window check in `src/validator.py` would have nothing to compare against.
* The only trace of July 2026 is the column alias, which is text the model chose. A
  control keyed on that is a control that trusts the model to label honestly, which is
  the dependency ADR-004 exists to remove.

So the guard belongs in the summariser, whose whole job is deciding what the rows are
entitled to claim, and it is a prompt-layer defence for the same reason the second-order
injection defence is: the property is about meaning, not structure. That makes it the
weaker kind of control, which is why it is tested rather than asserted.

These tests execute genuine LLM calls, so they cost money and are skipped without an
API key.
"""

from __future__ import annotations

import os
import re

import pytest

from src.agent import ask
from src.models import Outcome

pytestmark = pytest.mark.integration

_HAS_KEY = any(
    os.getenv(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
)
_needs_llm = pytest.mark.skipif(_HAS_KEY is False, reason="no LLM API key configured")

# The seeded window is fixed in db/seed.py and ends here, so any question about a later
# period is asking about data that does not exist.
_LAST_MONTH_IN_DATA = "2026-06-30"

# Phrasings that assert a future value as fact. "we should expect N" and "there will be
# N" are the two shapes the model reached for; both present a historical mean as a
# forward-looking figure to a reader who cannot tell the difference.
_FORWARD_ASSERTION = re.compile(
    r"(we (should )?expect|you (should )?expect|will (be|see|move|reach|have)"
    r"|are expected to|is expected to|projected to|forecast(ed)? to)",
    re.IGNORECASE,
)

# Any of these means the answer told the reader the period is not in the data, which is
# the behaviour rule 6 of SUMMARIZE_SYSTEM asks for.
_COVERAGE_DISCLAIMER = (
    "no data", "not cover", "does not cover", "doesn't cover", "no records",
    "not available", "only covers", "through june", "ends", "historical",
    "past", "cannot predict", "can't predict", "not a forecast", "no rows",
)

# Framings probed against the pre-fix build. "project" leaked; the explicit-future ones
# were already refused at triage. All four are kept so a future prompt edit that fixes
# one shape while breaking another fails here rather than in front of a user.
_FORECAST_QUESTIONS = [
    "Project how many port calls we should expect in July 2026.",
    "How many containers will we move next month?",
    "What will berth wait times look like in August 2026?",
    "Forecast container volume for Q3 2026.",
]


@_needs_llm
@pytest.mark.parametrize("question", _FORECAST_QUESTIONS)
def test_a_forecast_question_is_never_answered_with_a_bare_future_assertion(question):
    """No answer may state a figure as what will happen.

    The assertion is deliberately about the *claim*, not the route. Refusing at
    `classify` is an acceptable outcome and so is answering with the historical figure
    correctly labelled. What is not acceptable is the middle case that shipped: a real
    number from the rows, presented as a prediction.
    """
    result = ask(question)

    if result.outcome in (Outcome.REFUSED, Outcome.CLARIFY):
        return  # never reached the summariser, so there is no claim to check

    assert result.outcome == Outcome.ANSWERED, (
        f"unexpected outcome {result.outcome} for {question!r}: {result.answer}"
    )

    answer = result.answer
    forward = _FORWARD_ASSERTION.search(answer)
    if forward is None:
        return

    # A forward-looking phrase is tolerable only when the answer has also told the reader
    # the period is absent from the data. Without that, the reader has been handed a
    # prediction the system is not entitled to make.
    lowered = answer.lower()
    assert any(marker in lowered for marker in _COVERAGE_DISCLAIMER), (
        f"{question!r} produced a forward-looking claim "
        f"({forward.group(0)!r}) with no statement that the data does not cover the "
        f"period. Data ends {_LAST_MONTH_IN_DATA}. Answer: {answer}"
    )


@_needs_llm
def test_the_regression_case_reports_its_figure_as_historical():
    """The exact question that shipped broken, pinned.

    Kept separate from the parametrized case because this one has a known-wrong prior
    output ("We should expect approximately 83.33 port calls in July 2026"), and a test
    that names the defect it was written for survives a rewrite of the rules around it.
    """
    result = ask("Project how many port calls we should expect in July 2026.")

    if result.outcome in (Outcome.REFUSED, Outcome.CLARIFY):
        return

    lowered = result.answer.lower()
    assert any(marker in lowered for marker in _COVERAGE_DISCLAIMER), (
        "the July 2026 projection was answered without telling the reader that the data "
        f"stops at {_LAST_MONTH_IN_DATA}. Answer: {result.answer}"
    )
