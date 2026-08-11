"""Schema and loader for the gold set (ADR-006).

The gold set is the definition of "correct" for this project: the accuracy, ambiguity
and safety figures in the README are all derived from it. A malformed case therefore
corrupts a published number, so the file is parsed into validated models rather than
into bare dicts.

Two properties are worth naming, because they are what the schema buys:

* **Failures happen before the run, not during it.** ``run_eval.py`` dispatches on
  ``category`` to pick a scorer. An unrecognised category previously raised ``KeyError``
  part-way through a scored run, after the LLM calls for the preceding cases had already
  been paid for. Validating up front turns that into a load-time error.
* **The two record shapes are made explicit.** An ``answerable`` case carries
  ``gold_sql`` and ``ordered``; an ``ambiguous`` or ``adversarial`` case carries
  ``expect``. Nothing enforced that split before, so a case missing ``gold_sql`` parsed
  cleanly and failed only when it was scored.

``extra="forbid"`` is set deliberately. A misspelled key is the failure mode that
matters most here: ``orderd: true`` on a ranking question would otherwise be silently
dropped, and the case would be scored order-insensitively without anyone noticing.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

# Behavioural tag vocabulary, adapted from the 19-category external reference set in
# Questions/ (treated as reference only; categories that require multi-turn state or
# that this system handles in code are excluded or folded). The tag names the way a
# user asks, orthogonally to WHAT the reference SQL exercises (`topics`).
BehaviourTag = Literal[
    "happy_path",        # clean phrasing, core capability
    "edge_data",         # boundary data: empty groups, NULLs, coverage edges
    "fact_check",        # user asserts a figure and asks for confirmation
    "external_data",     # needs data outside this database; refusal expected
    "vague",             # underdetermined; tests the sparing-clarify rule
    "phrasing",          # typos, non-native grammar, abbreviations
    "comparative",       # rankings and A-versus-B
    "multi_part",        # several measures in one question
    "unavailable_data",  # periods or entities outside coverage
    "hypothetical",      # what-if simulation; refusal expected
    "meta",              # questions about the agent itself
    "manipulation",      # injection, fabrication requests, authority claims
    "format",            # output-format requests
    "recency",           # "latest", resolved against data coverage
    "sensitive",         # catalog, credential, or privilege probing
    "destructive",       # write requests: INSERT, UPDATE, DELETE, DDL
]

__all__ = [
    "GOLD_PATH",
    "AnswerableCase",
    "AmbiguousCase",
    "AdversarialCase",
    "GoldCase",
    "load_gold_set",
]

GOLD_PATH = Path(__file__).parent / "gold_questions.yaml"


class _CaseBase(BaseModel):
    """Fields common to every category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="Stable identifier, quoted in eval output.")
    question: str = Field(min_length=1, description="Asked verbatim; the agent sees this string.")
    note: str = Field(
        min_length=1,
        description="Why the case is in the set and what it exercises. Required, because a "
        "case whose purpose is not recorded cannot be reviewed when it starts failing.",
    )
    topics: list[int] = Field(
        default_factory=list,
        description="SQL syllabus topic ids (0-74) the reference answer exercises. The "
        "coverage report aggregates these; a case may legitimately carry none "
        "(behavioural cases whose SQL shape is incidental).",
    )
    behaviour: BehaviourTag | None = Field(
        default=None,
        description="How the question is asked, from the fixed vocabulary above. "
        "Orthogonal to `topics`. Optional during authoring; the coverage report "
        "counts untagged cases so gaps are visible rather than silent.",
    )

    @field_validator("topics")
    @classmethod
    def _topics_in_syllabus_range(cls, value: list[int]) -> list[int]:
        """The syllabus runs 0 to 74; anything else is a typo, and a wrong tag would
        silently corrupt the coverage report rather than fail a case."""
        bad = [t for t in value if not 0 <= t <= 74]
        if bad:
            raise ValueError(f"topics outside the 0-74 syllabus range: {bad}")
        return value


class AnswerableCase(_CaseBase):
    """Scored by executing ``gold_sql`` and comparing rows against the agent's rows."""

    category: Literal["answerable"]
    gold_sql: str = Field(min_length=1, description="Hand-verified reference SQL.")
    ordered: bool = Field(
        description="Whether row order is part of the answer. Required rather than "
        "defaulted, so that every case states its ordering intent explicitly: the "
        "permissive value is the one that silently weakens a ranking check."
    )

    @field_validator("gold_sql")
    @classmethod
    def _strip_block_scalar_newline(cls, value: str) -> str:
        """Drop the newline YAML appends to a `|` block scalar.

        Under the default clip chomping, a literal block scalar retains exactly one
        trailing line break. PostgreSQL does not care, but the string is also fed to
        sqlglot and printed in failure output, so it is normalised once here rather than
        at each use.
        Source: https://yaml.org/spec/1.2.2/#8112-block-chomping-indicator - Verified: 2026-08-08
        """
        return value.strip()


class AmbiguousCase(_CaseBase):
    """Under-specified. Correct behaviour is asking back rather than guessing."""

    category: Literal["ambiguous"]
    expect: Literal["clarify"]
    expects_alternatives: list[str] = Field(
        min_length=2,
        description="Concrete readings the clarifying question should offer, as substrings "
        "matched case-insensitively against the agent's reply. At least two, because a "
        "question is only ambiguous if there is more than one thing it could mean.",
    )

    @field_validator("expects_alternatives")
    @classmethod
    def _reject_blank_alternatives(cls, value: list[str]) -> list[str]:
        """An empty or whitespace alternative matches every string, so it would silently
        turn the check it appears in into a no-op."""
        if any(not item.strip() for item in value):
            raise ValueError("expects_alternatives entries must be non-empty")
        return value


class AdversarialCase(_CaseBase):
    """Destructive, injected or out of scope. Correct behaviour is refusing."""

    category: Literal["adversarial"]
    expect: Literal["blocked"]


# Discriminated on `category`, so a validation error names the offending field on the
# right model instead of reporting a failure against all three union members.
GoldCase = Annotated[
    AnswerableCase | AmbiguousCase | AdversarialCase,
    Field(discriminator="category"),
]

_ADAPTER: TypeAdapter[list[GoldCase]] = TypeAdapter(list[GoldCase])


def load_gold_set(path: Path = GOLD_PATH) -> list[GoldCase]:
    """Parse and validate the gold set.

    Args:
        path: YAML file to read. Defaults to the checked-in gold set.

    Returns:
        The cases in file order. Order is preserved so that eval output and the results
        JSON stay comparable across runs.

    Raises:
        pydantic.ValidationError: A case has an unknown category, a missing or misspelled
            field, or a value of the wrong type.
        ValueError: The file is not a list of cases, or two cases share an id.
    """
    # safe_load, not load: the loader must not be able to construct arbitrary Python
    # objects from the file. This file is checked in rather than user-supplied, so the
    # exposure is low, but a SQL-injection-hardened project should not leave a
    # deserialisation gadget in its own test harness.
    # Source: https://pyyaml.org/wiki/PyYAMLDocumentation - Verified: 2026-08-08
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a YAML list of cases, got {type(raw).__name__}")

    # An empty set is refused rather than allowed to load. Scoring nothing is not a
    # neutral outcome here: run_eval would report 0/0 and exit successfully, which reads
    # as a clean run rather than as a harness that never tested anything.
    if not raw:
        raise ValueError(f"{path} contains no cases")

    cases = _ADAPTER.validate_python(raw)

    # Ids are quoted in failure output and used to compare runs, so a duplicate would
    # make two different cases indistinguishable in the results JSON.
    counts = Counter(case.id for case in cases)
    duplicates = sorted(case_id for case_id, n in counts.items() if n > 1)
    if duplicates:
        raise ValueError(f"{path} contains duplicate case ids: {', '.join(duplicates)}")

    return cases
