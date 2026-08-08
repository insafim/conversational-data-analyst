"""Typed state passed between graph nodes, and the result returned to callers.

These types are the contract between stages. Making them explicit is most of the
argument for using a state graph at all (ADR-002): each node declares what it consumes
and produces, so the pipeline cannot silently grow undeclared coupling.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Route(StrEnum):
    """How `classify` judged the question."""

    ANSWERABLE = "answerable"
    AMBIGUOUS = "ambiguous"       # under-specified: ask back rather than guess (ADR-006)
    OUT_OF_SCOPE = "out_of_scope"  # not answerable from this database at all


class Outcome(StrEnum):
    """Terminal state of a run. Every request ends in exactly one of these."""

    ANSWERED = "answered"
    CLARIFY = "clarify"    # returned a clarifying question
    REFUSED = "refused"    # classify rejected it before any SQL was written
    REJECTED = "rejected"  # the validator blocked generated SQL (ADR-004)
    ERROR = "error"        # execution or provider failure after retries


class ChartKind(StrEnum):
    NONE = "none"
    METRIC = "metric"
    LINE = "line"
    BAR = "bar"
    SCATTER = "scatter"
    TABLE = "table"


class ValidationResult(BaseModel):
    """Outcome of the code-level SQL gate. `ok=False` is terminal — never retried,
    because a rejected query is a safety decision, not a transient failure."""

    ok: bool
    reason: str | None = Field(
        default=None, description="Human-readable explanation shown to the user on rejection."
    )
    violation: str | None = Field(
        default=None, description="Machine-readable code, e.g. 'multiple_statements'."
    )


class QueryResult(BaseModel):
    """Rows returned by a successfully executed query."""

    columns: list[str]
    column_types: list[str] = Field(
        default_factory=list,
        description="PostgreSQL type name per column, used for chart selection (ADR-005).",
    )
    rows: list[list[Any]]
    row_count: int
    elapsed_s: float
    truncated: bool = Field(
        default=False, description="True if the row cap trimmed the result set."
    )


class ChartSpec(BaseModel):
    """Chart chosen by rules, never by the model (ADR-005)."""

    kind: ChartKind
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    reason: str = Field(description="Which rule fired. Surfaced in the UI and asserted in tests.")


class AgentResult(BaseModel):
    """What the UI and the eval harness both consume.

    `outcome` is populated on every path, including failures, so a run can always be
    attributed to the stage that failed (ADR-006). `sql` is populated on every path that
    reached SQL generation, which excludes a refusal, a clarification, and a provider
    failure raised out of the graph before it returned any state. A consumer must treat
    `sql` as optional and read `outcome` to know why it is absent.
    """

    question: str
    outcome: Outcome
    answer: str
    sql: str | None = None
    result: QueryResult | None = None
    chart: ChartSpec | None = None

    elapsed_s: float = 0.0
    stage_timings: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Seconds spent in each graph node, keyed by node name. Populated for every "
            "run so latency can be attributed to a stage rather than only totalled. "
            "A node that ran twice (the SQL retry) reports the sum of both passes."
        ),
    )
    llm_calls: int = 0
    cost_usd: float = 0.0
    retried: bool = False
    error: str | None = None
