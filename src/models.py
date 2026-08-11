"""Typed state passed between graph nodes, and the result returned to callers.

These types are the contract between stages. Making them explicit is most of the
argument for using a state graph at all (ADR-002): each node declares what it consumes
and produces, so the pipeline cannot silently grow undeclared coupling.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class RetryReason(StrEnum):
    """Why a regeneration or re-summarisation fired (ADR-012).

    Replaces the single `retried` boolean. One bit could say that a retry happened but
    not which mechanism caused it, so a run-to-run movement in accuracy could not be
    attributed to the change that produced it.
    """

    DB_ERROR = "db_error"                    # PostgreSQL rejected the query
    VERIFIER_OBJECTION = "verifier_objection"  # the semantic verifier objected
    GROUND_CHECK = "ground_check"            # a figure in the answer was not in the rows
    QUALITY_TRIGGER = "quality_trigger"      # a code-detected result-shape problem


class Turn(BaseModel):
    """One prior exchange, as the rewrite node is allowed to see it (ADR-011).

    Question and SQL only. Answer text and result rows are deliberately absent: answer
    text quotes row data, which is the second-order injection channel `src/prompts.py`
    documents, and rows are that same risk plus token cost. A follow-up resolves from
    what was ASKED, never from what was returned.

    `sql` is empty for a turn that produced none, such as a clarification or a refusal. The
    question is still carried, because "which is the busiest terminal?" is what makes the
    user's next word ("by containers") resolvable.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    sql: str = ""


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


class Verification(BaseModel):
    """The semantic verifier's advisory reading of one SQL statement (ADR-012).

    Advisory by construction. It carries no `ok=False` that anything treats as a veto:
    `objection` routes to at most one regeneration and then becomes a caveat printed
    beside the answer. Only `ValidationResult` can stop a query, and only code writes it.

    `sql` records WHICH statement was judged. The verifier runs on a parallel branch, so
    a database-error retry can replace the SQL while a verdict for the previous statement
    is still in flight; without this field the graph could act on a stale objection.
    """

    model_config = ConfigDict(frozen=True)

    sql: str = Field(description="The exact statement this verdict was issued against.")
    aligned: bool = Field(description="False only when the verifier stated an objection.")
    objection: str | None = Field(
        default=None, description="What the verifier says the SQL does not do."
    )
    reading: str | None = Field(
        default=None,
        description="Plain-language statement of what the SQL measures, shown to the "
        "user. For a reader who cannot audit SQL this line, not the query, is the "
        "verification surface.",
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
    error: str | None = None

    # --- conversation (ADR-011) -----------------------------------------------------
    interpreted_question: str | None = Field(
        default=None,
        description="The standalone rewrite of a follow-up, populated only when the "
        "rewrite node changed the question. Displayed, because a resolution the user "
        "cannot see is a resolution the user cannot correct.",
    )

    # --- runtime verification (ADR-012) ---------------------------------------------
    retry_reasons: list[RetryReason] = Field(
        default_factory=list,
        description="Every regeneration or re-summarisation that fired, in order. A list "
        "rather than a boolean so a run comparison can attribute movement to the "
        "mechanism that caused it.",
    )
    reading: str | None = Field(
        default=None, description="The verifier's plain-language reading of the SQL."
    )
    caveat: str | None = Field(
        default=None,
        description="Shown beside the answer when the verifier objected twice. The "
        "answer still ships: an advisory check that could withhold answers would be a "
        "gate, and the LLM is not permitted to be one.",
    )
    grounding_flag: str | None = Field(
        default=None,
        description="Set when a figure survived the groundedness check twice. Logged and "
        "shown, never blocking, because the check has known false positives (ADR-006).",
    )

    @property
    def retried(self) -> bool:
        """Any retry at all. Kept because the UI and several tests ask exactly this
        question; `retry_reasons` is the record that carries the detail."""
        return bool(self.retry_reasons)
