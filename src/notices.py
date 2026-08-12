"""What is shown beside an answer, and in what order. No Streamlit here.

Two functions, and they sit together because they answer the same question at two volumes.
`answer_notices` decides what qualifies the answer and must be read; `turn_telemetry`
decides what describes the machinery and is offered collapsed. Both are pure over
`AgentResult`, so the wording, the ordering and the arithmetic are asserted in
`tests/test_notices.py` without a database, a model or a browser.


The chat view used to decide this inline, which made the ordering an eyeballed property: the
only way to know whether the truncation warning came before or after the chart was to read
the function or run the app. That is thin ice for a rule that carries meaning. A caveat
about a chart has to arrive before the chart, because a warning read after the picture has
already been believed arrived too late.

So the decision lives here as a pure function over `AgentResult`, and the view renders what
it returns without choosing anything. Two things follow. The ordering becomes executable,
asserted in `tests/test_notices.py` without a database, a model or a browser. And ADR-008's
claim that the UI layer is thin stays true rather than slowly becoming aspirational as the
UI grows.

Deliberately NOT here: the outcome badge and the "Interpreted as:" caption. Both belong
above the answer rather than beside it, because a misread question makes the answer
irrelevant, and grouping them with the trust signals would put them in the wrong place.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

from .models import AgentResult, Outcome
from .telemetry import count_of, format_unit_usd


class Level(StrEnum):
    """How prominently a notice is rendered.

    Two levels, not three. A caption states what was measured; a warning says trust this
    less. Anything finer would be a distinction the reader cannot act on differently.
    """

    CAPTION = "caption"
    WARNING = "warning"


class Notice(NamedTuple):
    level: Level
    text: str


def answer_notices(result: AgentResult) -> list[Notice]:
    """The captions and warnings that belong between an answer and its chart, in order.

    Order is the contract, and it is intentional:

    1. what was measured, because it qualifies the answer just read;
    2. the verifier's caveat, if one survived its retry;
    3. the groundedness flag, if a figure could not be matched to the rows;
    4. truncation, last of the warnings but still before the chart it describes.

    Truncation sits with the other two rather than beside the SQL because it is the same
    kind of signal, and a reader who never opens the SQL expander would otherwise never
    learn that the chart above was drawn from a partial set.
    """
    notices: list[Notice] = []

    if result.reading:
        notices.append(Notice(Level.CAPTION, f"What was measured: {result.reading}"))

    if result.caveat:
        notices.append(
            Notice(Level.WARNING, f"Possible mismatch with your question: {result.caveat}")
        )

    if result.grounding_flag:
        notices.append(
            Notice(
                Level.WARNING,
                "A figure in this answer could not be matched to the returned rows. "
                "Check it against the table before relying on it.",
            )
        )

    if result.result is not None and result.result.truncated:
        notices.append(
            Notice(
                Level.WARNING,
                f"Partial result: the first {result.result.row_count} rows only. "
                "The answer and the chart below describe that subset, not the whole set.",
            )
        )

    return notices


# What the turn did, in the past tense, so the disclosure states an outcome rather than
# only a duration. `Outcome.ERROR` says "Failed after" rather than "in": the seconds were
# spent and then nothing was delivered, and "Failed in 9s" reads like a deadline was met.
_ELAPSED_VERB = {
    Outcome.ANSWERED: "Answered in",
    Outcome.CLARIFY: "Asked back in",
    Outcome.REFUSED: "Refused in",
    Outcome.REJECTED: "Blocked in",
    Outcome.ERROR: "Failed after",
}


class TurnTelemetry(NamedTuple):
    """What one turn cost, ready to render as a collapsed disclosure.

    Three fields because they are read at three different depths: `label` is visible
    without opening anything, `summary` is the headline once opened, and `stages` is the
    detail underneath.
    """

    label: str
    summary: str
    stages: list[tuple[str, float]]


def turn_telemetry(result: AgentResult) -> TurnTelemetry | None:
    """Seconds, cost and the stage breakdown for one answer, or None if there is nothing
    to disclose.

    **Why this is beside the answer again.** It was here, moved to the Observability page
    on 2026-08-12, and is back in a collapsed form. The argument for removing it was that a
    single reading has no baseline, so a reader cannot tell whether six seconds is fast.
    That argument was correct and is now spent: the panel supplies the baseline, so the
    per-turn figure has something to be read against. What is refused is the earlier
    shape, an always-visible caption competing with the answer. Collapsed, it is the same
    kind of object as `View SQL`: available to a reader who wants to check the machinery,
    invisible to one who wants the answer.

    Returns None when no model was called. That is exactly the two guards at the top of
    `ask()`, the empty question and the over-long one, which refuse before any provider is
    reached. They have no stages and a duration of roughly zero, and "Refused in 0.00s"
    invites a reader to wonder what was measured when the answer is nothing.
    """
    if result.llm_calls <= 0:
        return None

    verb = _ELAPSED_VERB.get(result.outcome, "Finished in")

    # Cost and call count on every turn; rows only when a query ran. A refusal reporting
    # "0 rows" would imply an empty result set rather than a query that never happened.
    parts = [format_unit_usd(result.cost_usd), count_of(result.llm_calls, "model call")]
    if result.result is not None:
        parts.append(count_of(result.result.row_count, "row"))
    if result.retry_reasons:
        # Named rather than counted, for the reason ADR-012 gives: a bare "1 retry" cannot
        # tell a database error from the verifier changing the query.
        parts.append(f"retried: {', '.join(r.value for r in result.retry_reasons)}")

    return TurnTelemetry(
        label=f"{verb} {result.elapsed_s:.2f}s",
        summary=" · ".join(parts),
        # Slowest first: the reason to open this is to find out what took the time.
        stages=sorted(result.stage_timings.items(), key=lambda item: item[1], reverse=True),
    )
