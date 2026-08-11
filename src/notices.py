"""What is shown beside an answer, and in what order. No Streamlit here.

`app.py` used to decide this inline, which made the ordering an eyeballed property: the
only way to know whether the truncation warning came before or after the chart was to read
the function or run the app. That is thin ice for a rule that carries meaning. A caveat
about a chart has to arrive before the chart, because a warning read after the picture has
already been believed arrived too late.

So the decision lives here as a pure function over `AgentResult`, and `app.py` renders what
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

from .models import AgentResult


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
