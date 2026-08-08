"""The agent pipeline: a fixed-topology state graph (ADR-002).

    classify ──ambiguous─────► clarify   (END: clarifying question back to user)
       │     └─out_of_scope──► refuse    (END: refusal + reason)
       │ answerable
       ▼
    generate_sql ◄───────────────────────────────┐
       │                                          │ retry, max 1
       ▼                                          │ (Postgres error in context)
    validate ──fail──► reject (END: refusal)      │
       │ pass                                     │
       ▼                                          │
    execute ──db error────────────────────────────┘
       │ rows
       ▼
    summarize ──► pick_chart ──► END

Two structural properties are worth reading the edges for, because they are the
security argument rather than decoration:

1. **`validate` is on the only edge into `execute`.** No path reaches the database
   without passing the code validator. That is a property of the topology, not of the
   model's cooperation.

2. **The retry edge returns to `generate_sql`, never to `execute`.** Retried SQL is
   therefore validated exactly like first-attempt SQL. Looping back into `execute`
   would be a bypass, and would still look reasonable in a diagram — which is precisely
   why the edge is drawn and documented this way.

The model decides content; the graph decides flow.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import llm, prompts
from .charts import pick_chart as choose_chart
from .config import settings
from .executor import ExecutionError, run_query
from .models import AgentResult, ChartSpec, Outcome, QueryResult, Route, ValidationResult
from .schema import get_schema_context
from .validator import validate_sql

# Rows are rendered for the summariser as delimited text rather than JSON: it is more
# compact per token, and there is no nesting to preserve.
_MAX_SUMMARY_ROWS = 50


@dataclass
class Timings:
    """Wall-clock seconds per graph node, accumulated across one question.

    A single total tells you a question took nine seconds; it does not tell you whether
    that was the model, the database, or the schema read, which is the only version of
    the number that supports a decision. The three LLM nodes and `execute` differ by
    roughly an order of magnitude, so attributing the total is what makes it actionable.

    Mutable and carried through state by reference, exactly like `llm.Usage`. That is
    deliberate: LangGraph's default channel for a plain TypedDict key REPLACES the value
    on each node return, so a node returning `{"timings": {...}}` would discard every
    earlier stage rather than merge. Accumulating into one object sidesteps the reducer
    question entirely, and matches the pattern already used for token usage.

    That replace behaviour was measured rather than taken from documentation, against the
    `langgraph>=1.2.10,<2` pin in pyproject.toml. A two-node graph in which each node
    returns a dict under the same key ends with only the second node's dict, and
    `tests/test_agent_routing.py::test_langgraph_replaces_dict_state_so_timings_must_accumulate`
    pins that finding so this rationale fails loudly if a future version starts merging.

    `record` ADDS rather than assigns, because `generate_sql` and `validate` run twice
    when the SQL retry fires. The sum is the honest figure for "where did the wall clock
    go"; `passes` keeps the retry visible rather than hiding it inside a larger number.
    """

    stages: dict[str, float] = field(default_factory=dict)
    passes: dict[str, int] = field(default_factory=dict)

    def record(self, stage: str, seconds: float) -> None:
        self.stages[stage] = round(self.stages.get(stage, 0.0) + seconds, 3)
        self.passes[stage] = self.passes.get(stage, 0) + 1

    def as_dict(self) -> dict[str, float]:
        """Ordered slowest first, since that is the order they get read in."""
        return dict(sorted(self.stages.items(), key=lambda kv: kv[1], reverse=True))


def _timed(
    stage: str, node: Callable[[State], dict[str, Any]]
) -> Callable[[State], dict[str, Any]]:
    """Wrap a node so its wall-clock cost is recorded.

    Applied at graph-assembly time rather than as a decorator on each function, so the
    node functions stay plain and directly unit-testable without a timing object in
    state. The `finally` matters: a node that raises still reports the time it burned,
    which is the case where the number is most worth having.
    """

    def wrapper(state: State) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return node(state)
        finally:
            timings = state.get("timings")
            if timings is not None:
                timings.record(stage, time.perf_counter() - started)

    wrapper.__name__ = stage
    return wrapper


class State(TypedDict, total=False):
    """State passed between nodes. `total=False` because nodes return partial updates
    that LangGraph merges into the accumulated state."""

    question: str
    schema: str
    route: str
    clarification: str
    reason: str
    sql: str
    validation: ValidationResult
    result: QueryResult
    error: str
    attempts: int
    answer: str
    chart: ChartSpec
    usage: llm.Usage
    timings: Timings
    outcome: str


# ---------------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------------
def classify(state: State) -> dict[str, Any]:
    """Route the question. LLM call (cheap tier)."""
    raw = llm.cheap(
        prompts.CLASSIFY_SYSTEM,
        prompts.CLASSIFY_USER.format(schema=state["schema"], question=state["question"]),
        usage=state["usage"],
    )
    try:
        parsed = llm.extract_json(raw)
        route = Route(parsed.get("route", Route.ANSWERABLE))
    except Exception:  # noqa: BLE001
        # A malformed classification must not abort the request. Defaulting to
        # "answerable" is safe because the validator and the read-only role still
        # stand between here and the database; defaulting to a refusal would instead
        # turn a parsing hiccup into a broken product.
        return {"route": Route.ANSWERABLE.value, "reason": "classifier output unparseable"}

    return {
        "route": route.value,
        "clarification": parsed.get("clarification", "") or "",
        "reason": parsed.get("reason", "") or "",
    }


def clarify(state: State) -> dict[str, Any]:
    """Terminal: ask the user a question instead of guessing (ADR-006)."""
    question = state.get("clarification") or (
        "That question could be read more than one way. Could you be more specific?"
    )
    return {"answer": question, "outcome": Outcome.CLARIFY.value}


def refuse(state: State) -> dict[str, Any]:
    """Terminal: classification-time refusal, before any SQL is written."""
    reason = state.get("reason") or "It cannot be answered from this database."
    return {
        "answer": f"I can't help with that. {reason}",
        "outcome": Outcome.REFUSED.value,
    }


def generate_sql(state: State) -> dict[str, Any]:
    """Write SQL. LLM call (strong tier). Also the retry target."""
    user = prompts.GENERATE_SQL_USER.format(
        schema=state["schema"], question=state["question"]
    )
    if state.get("error") and state.get("sql"):
        user += prompts.RETRY_SUFFIX.format(
            previous_sql=state["sql"], error=state["error"]
        )

    raw = llm.strong(prompts.GENERATE_SQL_SYSTEM, user, usage=state["usage"])
    return {
        "sql": llm.extract_sql(raw),
        "attempts": state.get("attempts", 0) + 1,
        "error": "",  # clear so a stale error is not re-appended on a later pass
    }


def validate(state: State) -> dict[str, Any]:
    """The safety gate. Pure code — cannot be argued with (ADR-004)."""
    return {"validation": validate_sql(state.get("sql", ""))}


def reject(state: State) -> dict[str, Any]:
    """Terminal: validation-time refusal, after generation."""
    validation = state.get("validation")
    reason = validation.reason if validation else "The generated query was not safe to run."
    return {
        "answer": f"I couldn't run that safely. {reason}",
        "outcome": Outcome.REJECTED.value,
    }


def execute(state: State) -> dict[str, Any]:
    """Run the validated SQL as the read-only role."""
    try:
        return {"result": run_query(state["sql"]), "error": ""}
    except ExecutionError as exc:
        return {"error": str(exc)}


def summarize(state: State) -> dict[str, Any]:
    """State the answer, grounded strictly in the returned rows. LLM call (cheap tier)."""
    result = state["result"]

    if result.row_count == 0:
        # Answered in code rather than by the model: with no rows there is nothing to
        # ground an answer in, and this is exactly the situation where a model is most
        # likely to invent a plausible number.
        return {
            "answer": "No data matched that question.",
            "outcome": Outcome.ANSWERED.value,
        }

    header = " | ".join(result.columns)
    body = "\n".join(
        " | ".join("NULL" if v is None else str(v) for v in row)
        for row in result.rows[:_MAX_SUMMARY_ROWS]
    )
    truncation_note = ", truncated" if result.truncated else ""

    answer = llm.cheap(
        prompts.SUMMARIZE_SYSTEM,
        prompts.SUMMARIZE_USER.format(
            question=state["question"],
            sql=state["sql"],
            row_count=result.row_count,
            truncation_note=truncation_note,
            rows=f"{header}\n{body}",
        ),
        usage=state["usage"],
    )
    return {"answer": answer, "outcome": Outcome.ANSWERED.value}


def pick_chart(state: State) -> dict[str, Any]:
    """Choose the visualisation. Pure code, no LLM (ADR-005)."""
    return {"chart": choose_chart(state["result"])}


# ---------------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------------
def _route_after_classify(state: State) -> str:
    return state.get("route", Route.ANSWERABLE.value)


def _route_after_validate(state: State) -> str:
    validation = state.get("validation")
    return "pass" if validation and validation.ok else "fail"


def _route_after_execute(state: State) -> str:
    """Retry once on a database error, then give up.

    The bound lives here, in the graph, not in the model: this is the ReAct pattern with
    the loop counter owned by code (ADR-002).
    """
    if not state.get("error"):
        return "ok"
    if state.get("attempts", 0) <= settings.max_sql_retries:
        return "retry"
    return "fail"


def build_graph():
    """Assemble and compile the graph. No checkpointer: the pipeline is single-turn by
    design (ADR-008), so there is no cross-turn state to persist."""
    graph = StateGraph(State)

    # Every node is registered through `_timed`, so the latency breakdown covers the
    # whole graph by construction. Adding a node without timing it would take a
    # deliberate departure from the line above it rather than an oversight.
    for name, node in (
        ("classify", classify),
        ("clarify", clarify),
        ("refuse", refuse),
        ("generate_sql", generate_sql),
        ("validate", validate),
        ("reject", reject),
        ("execute", execute),
        ("summarize", summarize),
        ("pick_chart", pick_chart),
    ):
        graph.add_node(name, _timed(name, node))

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        {
            Route.ANSWERABLE.value: "generate_sql",
            Route.AMBIGUOUS.value: "clarify",
            Route.OUT_OF_SCOPE.value: "refuse",
        },
    )
    graph.add_edge("clarify", END)
    graph.add_edge("refuse", END)

    # The only edge out of generate_sql is validate, and the only edge into execute is
    # from validate. Retried SQL is therefore validated identically to first-attempt SQL.
    graph.add_edge("generate_sql", "validate")
    graph.add_conditional_edges(
        "validate", _route_after_validate, {"pass": "execute", "fail": "reject"}
    )
    graph.add_edge("reject", END)

    graph.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"ok": "summarize", "retry": "generate_sql", "fail": END},
    )
    graph.add_edge("summarize", "pick_chart")
    graph.add_edge("pick_chart", END)

    return graph.compile()


_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def ask(question: str) -> AgentResult:
    """Answer one question. This is the entry point for both the UI and the eval harness.

    Never raises for expected failure modes: an unreachable provider or a query that
    fails twice returns an AgentResult with `outcome=ERROR`, so callers have one code
    path and the eval harness can score a failure rather than crashing on it.
    """
    started = time.perf_counter()
    usage = llm.Usage()
    timings = Timings()

    # Bound the input before it reaches a provider. This is the only guardrail that acts
    # on the question itself rather than on the SQL derived from it, and it is refused in
    # code for the same reason the SQL gate is: the classifier is a model, so it cannot
    # be the thing that decides whether input is too large to send to a model.
    question = question.strip()
    if not question:
        return AgentResult(
            question=question,
            outcome=Outcome.REFUSED,
            answer="Please ask a question.",
            elapsed_s=round(time.perf_counter() - started, 3),
        )
    if len(question) > settings.max_question_chars:
        return AgentResult(
            question=question[: settings.max_question_chars],
            outcome=Outcome.REFUSED,
            answer=(
                f"That question is too long ({len(question):,} characters). "
                f"Please keep it under {settings.max_question_chars:,}."
            ),
            elapsed_s=round(time.perf_counter() - started, 3),
        )

    # Timed like a node even though it runs outside the graph. It is cached after the
    # first call (ADR-003), so it costs several catalog round trips once and nothing
    # afterwards. A breakdown that omitted it would misattribute that first
    # question's latency to whichever node happened to run next.
    schema_started = time.perf_counter()
    try:
        schema = get_schema_context()
    except Exception as exc:  # noqa: BLE001
        return AgentResult(
            question=question,
            outcome=Outcome.ERROR,
            answer="Could not read the database schema. Is the database running and seeded?",
            error=str(exc),
            elapsed_s=round(time.perf_counter() - started, 3),
        )
    timings.record("schema", time.perf_counter() - schema_started)

    try:
        final: State = _graph().invoke(
            {
                "question": question,
                "schema": schema,
                "usage": usage,
                "timings": timings,
                "attempts": 0,
            }
        )
    except llm.LLMError as exc:
        return AgentResult(
            question=question,
            outcome=Outcome.ERROR,
            answer=f"The language model call failed: {exc}",
            error=str(exc),
            elapsed_s=round(time.perf_counter() - started, 3),
            stage_timings=timings.as_dict(),
            llm_calls=usage.calls,
            cost_usd=round(usage.cost_usd, 6),
        )

    elapsed = round(time.perf_counter() - started, 3)
    outcome = Outcome(final.get("outcome", Outcome.ERROR.value))
    attempts = final.get("attempts", 0)

    # Reached END from `execute` with an error still set: the retry was used and failed.
    if outcome == Outcome.ERROR and final.get("error"):
        return AgentResult(
            question=question,
            outcome=Outcome.ERROR,
            answer="The query could not be executed successfully.",
            sql=final.get("sql"),
            error=final.get("error"),
            elapsed_s=elapsed,
            stage_timings=timings.as_dict(),
            llm_calls=usage.calls,
            cost_usd=round(usage.cost_usd, 6),
            retried=attempts > 1,
        )

    return AgentResult(
        question=question,
        outcome=outcome,
        answer=final.get("answer", ""),
        sql=final.get("sql"),
        result=final.get("result"),
        chart=final.get("chart"),
        elapsed_s=elapsed,
        stage_timings=timings.as_dict(),
        llm_calls=usage.calls,
        cost_usd=round(usage.cost_usd, 6),
        retried=attempts > 1,
        error=final.get("error") or None,
    )
