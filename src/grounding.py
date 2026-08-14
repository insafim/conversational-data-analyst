"""Structural groundedness check: do the answer's figures come from the data?

Written for the eval harness (ADR-006) and calibrated there across ten runs. ADR-012
moves it into the graph as well, so it now runs twice against the same answer for
different purposes: at runtime as a floor with one bounded re-summarisation, and offline
as the scored metric. It lives here rather than in `eval/` because a runtime pipeline
importing from its own test harness would invert the dependency, and because the two
callers must not be able to drift into checking different things. A runtime check that
passed what the eval scored as ungrounded would make both numbers meaningless.

The check is code, not a second model. An LLM judge would be non-deterministic and
circular, grading a model's output with a model, and would itself need validating
against human labels before its scores meant anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Numbers with at most two decimals, optionally comma-grouped: 17.46, 1,500, 6577
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Small integers are almost always ordinals, counts of listed items, or echoes of the
# question ("the top 3 operators"), not data values. Checking them produces false
# hallucination reports, which would make the metric useless.
_SMALL_INT_CEILING = 12

# Phrases that count as saying "nothing matched". Checked on the empty-result path,
# where a model is most likely to invent a plausible answer.
_DENIAL_PHRASES = (
    "no data", "no matching", "no results", "none", "no port calls",
    "nothing", "no records", "did not return", "no rows",
)


@dataclass(frozen=True)
class GroundingCheck:
    """Verdict plus the figures that failed, which the re-summarise prompt names."""

    ok: bool
    detail: str = ""
    ungrounded: list[float] = field(default_factory=list)


def numbers_in(text: str) -> list[float]:
    """Every number appearing in a string, comma separators removed."""
    found = []
    for token in _NUMBER_RE.findall(text or ""):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return found


def check_groundedness(
    answer: str,
    rows: list[list[Any]] | None,
    row_count: int | None,
    question: str,
    sql: str | None,
) -> GroundingCheck:
    """Verify the answer's figures actually came from the returned rows.

    The requirements name groundedness as an evaluated behaviour, and the failure that matters
    is a model *inventing* a number: a plausible figure that never appeared in the data
    is far more damaging than a visibly wrong one, because it survives review.

    A number in the answer is considered grounded if it appears in:
      * the returned rows (exactly, or as a rounding of a returned value),
      * the question (the user's own figures), or
      * the SQL (literals such as a year or a LIMIT).
    Row count is also allowed, since "6 terminals" is a legitimate observation.

    Args:
        answer: The text to check.
        rows: Returned rows, or None when nothing was executed.
        row_count: Rows returned, which may exceed ``len(rows)`` when the cap trimmed.
        question: The question as the summariser saw it, the REWRITTEN question on a
            follow-up turn (ADR-011), since that is the text whose figures the model was
            entitled to quote.
        sql: The executed statement, or None.

    Returns:
        A GroundingCheck. ``ok=True`` when nothing was executed, because an answer with
        no result set behind it has nothing to be ungrounded against.

    TWO KNOWN FALSE POSITIVES, stated rather than hidden. Both are tolerated, and the
    metric is therefore a floor on groundedness rather than a precise measure. They are
    also the reason ADR-012 makes the runtime use of this check advisory: blocking on a
    check with known false positives would withhold correct answers.

    1. **Derived figures.** "three times higher", "up 12%": arithmetic the model
       performed, present in no row. Tolerated because the alternative, permitting any
       arithmetic, permits exactly the invented numbers this exists to catch.

    2. **The magnitude of a negative value.** A `LAG` column holds -988 for a month that
       fell, and the natural English is "dropped 988 containers". The answer carries the
       magnitude, the data carries the sign, and set membership rejects it. The answer is
       correct and the checker is wrong. This is eval q28, flagged in every run since
       groundedness was first measured.

       Not fixed, because both available fixes are worse. Matching on absolute value
       would accept "July increased by 3,845" against a -3845 cell, trading this false
       positive for a false negative on exactly the sign errors that matter. Matching the
       magnitude only when a decrease word sits near the figure would remove this case but
       detect nothing new: the matching below is pure set membership with no relation to
       the surrounding words, so a sign error on a *positive* value ("August decreased by
       4,532" against +4532) already passes by exact match and would continue to, because
       that branch short-circuits before any proximity logic could run.

       See `tests/test_eval_scoring.py`, where both the false positive and that blind spot
       are pinned. The pins were themselves rebuilt after an audit found the first version
       still passed under the absolute-value fix it claimed to guard against.
    """
    if rows is None or row_count is None:
        return GroundingCheck(True)  # nothing was returned, so nothing could be ungrounded

    # Empty results are the highest-risk case: with nothing to ground an answer in, a
    # model is most likely to invent one.
    if row_count == 0:
        lowered = (answer or "").lower()
        if not any(phrase in lowered for phrase in _DENIAL_PHRASES):
            return GroundingCheck(
                False, f"empty result set but the answer did not say so: {answer!r}"
            )
        return GroundingCheck(True)

    allowed: set[float] = {float(row_count)}
    for row in rows:
        for value in row:
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float, Decimal)):
                allowed.add(float(value))
            else:
                allowed.update(numbers_in(str(value)))

    allowed.update(numbers_in(question))
    allowed.update(numbers_in(sql or ""))

    ungrounded = []
    for candidate in numbers_in(answer):
        if abs(candidate) <= _SMALL_INT_CEILING and candidate == int(candidate):
            continue
        # Grounded if it matches a permitted value, or is a rounding of one. The model
        # legitimately says "17.5 hours" for a stored 17.46.
        decimals = len(str(candidate).split(".")[1]) if "." in str(candidate) else 0
        if any(
            abs(candidate - value) < 1e-6 or round(value, decimals) == candidate
            for value in allowed
        ):
            continue
        ungrounded.append(candidate)

    if ungrounded:
        return GroundingCheck(
            False,
            f"answer contains figure(s) not present in the results, question or SQL: "
            f"{ungrounded}. Answer was: {answer!r}",
            ungrounded,
        )
    return GroundingCheck(True)
