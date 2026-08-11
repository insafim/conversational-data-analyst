"""Code-detected result-shape triggers (ADR-012).

Three signals that a query ran safely and successfully and still did not answer the
question. None of them needs a model to spot. ADR-012 selects exactly these three and
records that each is free to detect and each has at least one historical instance,
naming the runs for two of them:

* an **empty result** on a question the classifier called answerable (run3 q15, run6 q19);
* a **multi-row result for a singular superlative**: "which terminal is busiest" asks
  for one row, and the grammar rule at `src/prompts.py` says so (run1 q05);
* a result that **saturates the row cap**, which means the answer is a truncation rather
  than a result.

They feed the same bounded regeneration as a database error. The design bias throughout
is toward missing a trigger rather than firing a false one: a false trigger spends a
regeneration and can replace a correct query with a worse one, while a missed trigger
leaves the system exactly where it stood before this module existed. That is why the
superlative detector below tests only the HEAD noun of the question and refuses to guess
about anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import settings
from .models import QueryResult, Route

# Words that make a question a superlative. An explicit list rather than an "-est"
# suffix rule, which would fire on "test", "request", "west" and "latest arrival time".
_SUPERLATIVES = frozenset({
    "busiest", "longest", "shortest", "highest", "lowest", "largest", "smallest",
    "biggest", "fastest", "slowest", "best", "worst", "most", "least", "top",
    "maximum", "minimum", "max", "min", "earliest", "latest", "deepest", "heaviest",
})

# Tokens skipped when looking for the head noun: articles, copulas, and any superlative
# acting as an adjective. "what is the busiest terminal" has head noun "terminal".
_ARTICLES_AND_COPULAS = frozenset({"is", "are", "was", "were", "the", "a", "an", "single", "one"})
_SKIPPABLE = _ARTICLES_AND_COPULAS | _SUPERLATIVES

# An explicit count means the question asked for several rows, whatever its grammar
# otherwise suggests. Standalone integers 2 to 20 only, so a year ("in 2025") is not
# mistaken for a row count.
_COUNT_WORDS = frozenset({
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "few", "several", "all", "both", "list", "rank", "ranking",
})
_SMALL_INTEGER = re.compile(r"\b(?:[2-9]|1[0-9]|20)\b")

# A per-group question returns every group, so many rows is the correct shape.
_PER_GROUP = frozenset({"each", "every", "per", "breakdown", "distribution", "respectively"})

_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class QualityIssue:
    """A result-shape problem, and the sentence the regeneration prompt shows the model."""

    code: str
    message: str


def asks_for_a_single_row(question: str) -> bool:
    """True when the question's grammar asks for exactly one row.

    Decided from the head noun of the interrogative phrase and nothing else. "Which
    terminal moved the most containers" is singular because its head noun is "terminal";
    the plural "containers" later in the sentence is the measure, not the subject, and a
    detector that read the whole sentence would be wrong about it.

    Returns False for anything it cannot decide, including every question that does not
    open with "which" or "what".
    """
    tokens = _WORD.findall(question.lower())
    if not tokens:
        return False
    if not any(token in _SUPERLATIVES for token in tokens):
        return False
    if any(token in _COUNT_WORDS for token in tokens) or _SMALL_INTEGER.search(question):
        return False
    if any(token in _PER_GROUP for token in tokens):
        return False

    try:
        start = next(i for i, token in enumerate(tokens) if token in ("which", "what"))
    except StopIteration:
        return False

    for token in tokens[start + 1 :]:
        if token in _SKIPPABLE:
            continue
        # First non-skippable token is the head noun. Plural heads ask for many rows.
        return not token.endswith("s")
    return False


def detect_quality_issue(
    question: str, route: str, result: QueryResult
) -> QualityIssue | None:
    """Return the result-shape problem to regenerate against, or None if there is none.

    Args:
        question: The question the SQL was written for, the REWRITTEN one on a follow-up
            turn (ADR-011), since that is the text whose grammar the SQL had to match.
        route: The classifier's route. Only `answerable` questions are checked: an empty
            result is the CORRECT outcome for a question routed elsewhere.
        result: The rows that came back.

    Returns:
        At most one issue, highest-confidence first. One is enough, because the budget
        allows one regeneration and naming several problems at once produces a prompt the
        model satisfies partially.
    """
    if route != Route.ANSWERABLE.value:
        return None

    if result.row_count == 0:
        return QualityIssue(
            "empty_result",
            "The query ran successfully but returned no rows, and the question was "
            "expected to be answerable. Check for a filter, a value spelling, or a date "
            "range that excludes everything. The schema's coverage notes give the real "
            "ranges. If no data genuinely matches the question, return the same query "
            "again unchanged; an honest empty result is a correct answer.",
        )

    if result.truncated:
        return QualityIssue(
            "row_cap_saturated",
            f"The query returned more rows than the {settings.row_cap}-row cap, so the "
            "user would see a truncation rather than an answer. If the question asks for "
            "a total, a ranking or a per-group figure, aggregate it in SQL and return "
            "that instead of the underlying rows.",
        )

    if result.row_count > 1 and asks_for_a_single_row(question):
        return QualityIssue(
            "multi_row_for_singular",
            f"The question is a singular superlative, so it asks for ONE row, but the "
            f"query returned {result.row_count}. Order by the measure and LIMIT 1.",
        )

    return None
