"""The agent pipeline: a fixed-topology state graph (ADR-002).

    contextualize   (ADR-011: only when history exists; first turns skip it entirely)
       │
       ▼
    classify ──ambiguous─────► clarify   (END: clarifying question back to user)
       │     └─out_of_scope──► refuse    (END: refusal + reason)
       │ answerable
       ▼
    generate_sql ◄───────────────────────────────┐
       │                                          │ regenerate, bounded per reason:
       ▼                                          │ db_error / quality_trigger /
    validate ──fail──► reject (END: refusal)      │ verifier_objection
       │ pass                                     │
       ├──────────────► verify ──► END            │ (ADR-012, advisory, parallel)
       ▼                                          │
    execute ──db error / bad result shape─────────┘
       │ rows
       ▼
    summarize ◄──── re-summarise (ground_check) ──┐
       │                                           │
       ▼                                           │
    ground_check ──► review ───────────────────────┘
                       │ ok
                       ▼
                   pick_chart ──► END

Three structural properties are worth reading the edges for, because they are the
security argument rather than decoration:

1. **`validate` is on the only edge into `execute`.** No path reaches the database
   without passing the code validator. That is a property of the topology, not of the
   model's cooperation.

2. **The retry edge returns to `generate_sql`, never to `execute`.** Retried SQL is
   therefore validated exactly like first-attempt SQL. Looping back into `execute`
   would be a bypass, and would still look reasonable in a diagram — which is precisely
   why the edge is drawn and documented this way.

3. **`verify` is a sibling of `execute`, not a gate before it** (ADR-012). It can send
   the graph back to `generate_sql` and it can attach a caveat, and that is the whole of
   its authority. An LLM in this graph may produce and may object; only code may permit.

The model decides content; the graph decides flow.

## Why the verifier submits a future instead of simply blocking

ADR-012 claims the verifier's wall-clock cost is near zero because it runs in parallel
with `execute` and `summarize`. A plain parallel node does not deliver that against the
installed langgraph: parallel branches overlap only WITHIN one superstep, and the
runtime synchronises between supersteps, so branches of unequal length do not interleave.
A blocking `verify` node would therefore share a superstep with `execute`, which
ADR-012 measures at 0.025 s, and with nothing else, putting the rest of its latency on
the critical path.

That is a claim about a third-party library rather than about this code, so it is
measured rather than asserted:
`tests/test_agent_routing.py::test_langgraph_barriers_between_supersteps_so_verify_must_not_block`
runs a two-node 0.3 s branch beside a single 0.4 s branch and asserts the total lands
near 0.7 s (0.4 then 0.3, synchronised) rather than 0.6 s (free overlap). Run that test
to reproduce the finding; if a future version removes the synchronisation it fails, and
this design can be simplified instead of the rationale quietly becoming folklore.

So `verify` submits the call to a thread pool and returns immediately, and `review`
collects the verdict after `summarize` has run. The overlap is then real. The cost of
this is that the verifier's latency is attributed to `review` in the stage breakdown,
which is the honest place for it: `review`'s timing is exactly the part of the verifier's
latency that the overlap failed to hide.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import llm, prompts
from .charts import pick_chart as choose_chart
from .config import settings
from .executor import ExecutionError, run_query
from .grounding import check_groundedness
from .models import (
    AgentResult,
    ChartSpec,
    Outcome,
    QueryResult,
    RetryReason,
    Route,
    Turn,
    ValidationResult,
    Verification,
)
from .quality import QualityIssue, detect_quality_issue
from .schema import get_schema_context
from .validator import validate_sql

# Rows are rendered for the summariser as delimited text rather than JSON: it is more
# compact per token, and there is no nesting to preserve.
_MAX_SUMMARY_ROWS = 50

# The semantic verifier runs off the critical path (see the module docstring). Bounded
# to a few workers: one in-flight verification per question, and a superseded one can
# still be draining while the next starts.
_VERIFIER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="verify")


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
    go". It does mean a stage total does not say how many passes produced it, which is
    why `AgentResult.retried` is reported separately rather than being inferred from
    these numbers.
    """

    stages: dict[str, float] = field(default_factory=dict)

    def record(self, stage: str, seconds: float) -> None:
        self.stages[stage] = round(self.stages.get(stage, 0.0) + seconds, 3)

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
    that LangGraph merges into the accumulated state.

    One constraint is not visible from the type and is load-bearing: **`execute` and
    `verify` run in the same superstep, so they must write disjoint keys.** LangGraph's
    default channel raises InvalidUpdateError when two nodes write the same key in one
    step, which is a good failure: it is the state-corruption bug it would otherwise
    have been. `verify` writes `verification_future` and nothing else.
    """

    question: str          # the question the pipeline works on: rewritten on a follow-up
    asked: str             # what the user actually typed, kept for display and reporting
    history: list[Turn]
    interpreted: str
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

    # --- runtime verification (ADR-012) ---------------------------------------------
    # Optional because `generate_sql` clears both on every regeneration: a verdict and a
    # result-shape complaint each belong to the attempt that produced them.
    verification_future: tuple[str, Future] | None  # (sql it was issued for, verdict)
    verification: Verification | None
    quality_issue: QualityIssue | None
    grounding: str          # detail of an unresolved groundedness violation
    ungrounded: list[float]
    caveat: str
    verify_enabled: bool
    next: str               # `review`'s decision, read by the router that follows it

    # Retries are counted per mechanism rather than in one budget, because each is
    # bounded at one by ADR-012 and a shared counter would let a database error consume
    # the verifier's regeneration (or the reverse) for reasons unrelated to it.
    db_retries: int
    verify_retries: int
    quality_retries: int
    resummarised: int
    retry_reasons: list[str]


# ---------------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------------
def contextualize(state: State) -> dict[str, Any]:
    """Rewrite a follow-up into a standalone question. LLM call (cheap tier), ADR-011.

    Reached only when history exists, by a conditional edge from START rather than by an
    early return, so a first turn pays nothing and reports no timing for a node that did
    not run.

    Fail-open in three ways, all of them deliberate. An unparseable verdict, an empty
    rewrite, or a rewrite longer than `max_question_chars` all leave the typed question
    untouched rather than refusing the turn. The bound is the same one `ask()` applies to
    typed input, but the ACTION on breaching it differs, and the difference is the point:
    a typed question over the limit is a user handing the model a document, while a
    rewrite over the limit is this node malfunctioning. Refusing the user's legitimate
    follow-up because our own rewrite ran away would convert an internal fault into a
    user-visible failure. The un-rewritten question still runs the full pipeline, where
    the classifier gets its chance to ask what "and Rotterdam?" means.
    """
    history = state.get("history") or []
    if not history:
        return {}

    rendered = "\n".join(
        prompts.HISTORY_TURN.format(
            index=index,
            question=turn.question,
            sql=turn.sql.replace("\n", " ") if turn.sql else "(no query was run)",
        )
        for index, turn in enumerate(history[-settings.history_turns :], start=1)
    )

    original = state["question"]
    try:
        raw = llm.cheap(
            prompts.CONTEXTUALIZE_SYSTEM,
            prompts.CONTEXTUALIZE_USER.format(history=rendered, question=original),
            usage=state["usage"],
        )
        rewritten = str(llm.extract_json(raw).get("question", "")).strip()
    except Exception:  # noqa: BLE001, see the fail-open note above
        return {}

    if not rewritten or len(rewritten) > settings.max_question_chars:
        return {}
    if rewritten == original:
        return {}  # already standalone: nothing to show the user

    # Shown to the user as "Interpreted as:". A resolution the user cannot see is a
    # resolution the user cannot correct, and this is the same argument the visible SQL
    # expander rests on (ADR-008), applied to interpretation instead of to the query.
    return {"question": rewritten, "interpreted": rewritten}
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
    """Write SQL. LLM call (strong tier). Also the target of every regeneration.

    Three things can send the graph back here, and each attaches its own evidence to the
    prompt (ADR-012). The mechanism is the one with a track record: the database error
    passed through verbatim recovered four of four historical retries, so an objection
    and a result-shape complaint are attached the same way rather than as a bare
    instruction to try again.

    Each cause is cleared as it is consumed, so a stale one cannot be re-appended to a
    later pass, the failure that would make attempt three argue with attempt one's
    problem.

    This node also RECORDS the reason (ADR-012 accounting). Recording it where the
    regeneration actually happens, rather than where it was decided, means the list
    cannot claim a retry that a router then declined to take.
    """
    user = prompts.GENERATE_SQL_USER.format(
        schema=state["schema"], question=state["question"]
    )
    previous_sql = state.get("sql")
    reasons = list(state.get("retry_reasons", []))
    if previous_sql:
        if state.get("error"):
            user += prompts.RETRY_SUFFIX.format(
                previous_sql=previous_sql, error=state["error"]
            )
            reasons.append(RetryReason.DB_ERROR)
        elif state.get("quality_issue"):
            user += prompts.QUALITY_SUFFIX.format(
                previous_sql=previous_sql, issue=state["quality_issue"].message
            )
            reasons.append(RetryReason.QUALITY_TRIGGER)
        elif (verification := state.get("verification")) and verification.objection:
            user += prompts.VERIFIER_SUFFIX.format(
                previous_sql=previous_sql, objection=verification.objection
            )
            reasons.append(RetryReason.VERIFIER_OBJECTION)

    raw = llm.strong(prompts.GENERATE_SQL_SYSTEM, user, usage=state["usage"])
    return {
        "sql": llm.extract_sql(raw),
        "attempts": state.get("attempts", 0) + 1,
        "error": "",
        "quality_issue": None,
        "verification": None,
        # Cleared with the rest: a groundedness violation belongs to the answer that
        # stated it, and regenerating the SQL discards that answer. Left set, it would
        # make the next summarisation argue with a figure from a result set that no
        # longer exists, and would bill a second `ground_check` reason for it.
        "grounding": "",
        "ungrounded": [],
        "retry_reasons": reasons,
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
    """Run the validated SQL as the read-only role.

    Also applies the code-detected quality triggers (ADR-012). They are checked HERE
    rather than after `summarize` because they are properties of the result set alone:
    detecting them earlier costs nothing and saves the summarisation call that a
    regeneration would have thrown away.

    Runs in the same superstep as `verify`, so every key it writes must be one `verify`
    does not.
    """
    try:
        result = run_query(state["sql"])
    except ExecutionError as exc:
        # Counted here rather than in the router, because a router is a pure function of
        # state and must not be the thing that advances the budget it reads.
        return {"error": str(exc), "db_retries": state.get("db_retries", 0) + 1}

    if not state.get("verify_enabled"):
        return {"result": result, "error": ""}

    issue = detect_quality_issue(state["question"], state.get("route", ""), result)
    if issue is None or state.get("quality_retries", 0) >= 1:
        # Past the budget the issue is recorded but not acted on: one regeneration per
        # mechanism, and a second pass that trips the same trigger ships as it is.
        return {"result": result, "error": ""}

    return {
        "result": result,
        "error": "",
        "quality_issue": issue,
        "quality_retries": state.get("quality_retries", 0) + 1,
    }


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

    user = prompts.SUMMARIZE_USER.format(
        question=state["question"],
        sql=state["sql"],
        row_count=result.row_count,
        truncation_note=truncation_note,
        rows=f"{header}\n{body}",
    )

    # Second pass, after ground_check named a figure that is in no row (ADR-012). The
    # offending figure is quoted back rather than described, because "be more grounded"
    # is not an instruction a model can act on.
    ungrounded = state.get("ungrounded") or []
    reasons = list(state.get("retry_reasons", []))
    if ungrounded:
        figures = ", ".join(f"{value:,.10g}" for value in ungrounded)
        user += prompts.RESUMMARIZE_SUFFIX.format(
            figures=figures,
            verb="does" if len(ungrounded) == 1 else "do",
            previous_answer=state.get("answer", ""),
        )
        reasons.append(RetryReason.GROUND_CHECK)

    answer = llm.cheap(prompts.SUMMARIZE_SYSTEM, user, usage=state["usage"])
    return {
        "answer": answer,
        "outcome": Outcome.ANSWERED.value,
        "ungrounded": [],
        "retry_reasons": reasons,
    }


def verify(state: State) -> dict[str, Any]:
    """Ask whether the SQL measures what was asked. LLM call (cheap tier), ADR-012.

    Returns immediately with a pending call rather than its result. The module docstring
    sets out why, and names the test that measures it: the runtime synchronises between
    supersteps, so a node that blocked here would overlap `execute` and nothing else.

    It reads the question, the schema and the SQL, and never the results. That is what
    makes running it beside `execute` sound: it is a check on intent, which is fully
    determined before a single row comes back.
    """
    sql = state.get("sql", "")
    question, schema, usage = state["question"], state["schema"], state["usage"]

    def run() -> Verification | None:
        try:
            raw = llm.cheap(
                prompts.VERIFY_SYSTEM,
                prompts.VERIFY_USER.format(schema=schema, question=question, sql=sql),
                usage=usage,
            )
            parsed = llm.extract_json(raw)
        except Exception:  # noqa: BLE001
            # Fail-open, and this is the whole availability argument for putting a model
            # here at all: a flaky checker must not be able to degrade a working system.
            return None

        objection = parsed.get("objection") or None
        aligned = bool(parsed.get("aligned", True))
        return Verification(
            sql=sql,
            aligned=aligned or not objection,
            objection=None if aligned else (str(objection) if objection else None),
            reading=str(parsed["reading"]).strip() if parsed.get("reading") else None,
        )

    return {"verification_future": (sql, _VERIFIER_POOL.submit(run))}


def ground_check(state: State) -> dict[str, Any]:
    """Check every figure in the answer against the rows. Pure code (ADR-006, ADR-012).

    The same function the eval scores with, so the runtime floor and the published metric
    cannot disagree about what grounded means.
    """
    result = state.get("result")
    check = check_groundedness(
        answer=state.get("answer", ""),
        rows=result.rows if result is not None else None,
        row_count=result.row_count if result is not None else None,
        question=state["question"],
        sql=state.get("sql"),
    )
    if check.ok:
        return {"grounding": "", "ungrounded": []}
    return {"grounding": check.detail, "ungrounded": check.ungrounded}


def review(state: State) -> dict[str, Any]:
    """Collect the advisory verdicts and decide what happens next (ADR-012).

    This is where the verifier's latency is finally paid, and only the part of it that
    running beside `execute` and `summarize` did not already absorb. A verdict issued
    against SQL that a database-error retry has since replaced is discarded: acting on it
    would be regenerating a query in response to a complaint about a different one.

    Precedence is deliberate. A verifier objection outranks a groundedness violation
    because it says the ROWS are wrong, and re-describing wrong rows more carefully is
    not an improvement. Both are advisory: at the end of their budgets the answer ships,
    carrying a visible caveat or flag. Nothing here can withhold an answer.
    """
    updates: dict[str, Any] = {}
    verification = state.get("verification")

    pending = state.get("verification_future")
    if pending is not None:
        issued_for, future = pending
        try:
            verdict = future.result(timeout=settings.llm_timeout_s)
        except Exception:  # noqa: BLE001, fail-open as above
            verdict = None
        if verdict is not None and issued_for == state.get("sql"):
            verification = verdict
            updates["verification"] = verdict
        updates["verification_future"] = None

    # Budgets are spent here, where the decision is made; the reason codes are recorded
    # by `generate_sql` and `summarize`, where the retry is actually performed.
    if verification is not None and verification.objection:
        if state.get("verify_retries", 0) < 1:
            return {
                **updates,
                "verify_retries": state.get("verify_retries", 0) + 1,
                "next": "regenerate",
            }
        # Objected twice. The answer still ships, with the concern named on screen:
        # suppressing it would be the silent downgrade this design exists to avoid.
        updates["caveat"] = verification.objection

    if state.get("grounding"):
        if state.get("resummarised", 0) < 1:
            return {
                **updates,
                "resummarised": state.get("resummarised", 0) + 1,
                "next": "resummarise",
            }
        # Served with the flag, never blocked. The check has known false positives
        # (ADR-006), so blocking would withhold answers that are in fact correct.

    return {**updates, "next": "ok"}


def pick_chart(state: State) -> dict[str, Any]:
    """Choose the visualisation. Pure code, no LLM (ADR-005)."""
    return {"chart": choose_chart(state["result"])}


# ---------------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------------
def _route_from_start(state: State) -> str:
    """First turns skip the rewrite node entirely, and pay nothing for it (ADR-011)."""
    return "contextualize" if state.get("history") else "classify"


def _route_after_classify(state: State) -> str:
    return state.get("route", Route.ANSWERABLE.value)


def _route_after_validate(state: State) -> list[str] | str:
    """Fan out to `execute` and, when enabled, `verify` alongside it (ADR-012).

    Returning a LIST is how LangGraph fans out from a conditional edge, and it is what
    makes the verifier switchable by topology rather than by a no-op node: with
    verification off the graph is byte-for-byte the pre-ADR-012 pipeline, which is the
    property the with/without comparison runs rest on.
    """
    validation = state.get("validation")
    if not (validation and validation.ok):
        return "fail"
    return ["execute", "verify"] if state.get("verify_enabled") else ["execute"]


def _route_after_execute(state: State) -> str:
    """Regenerate once on a database error or a bad result shape, then give up.

    The bound lives here, in the graph, not in the model: this is the ReAct pattern with
    the loop counter owned by code (ADR-002). Each cause carries its own budget, so a
    query that fails in the database has not spent the allowance for a query that returns
    the wrong shape.
    """
    if state.get("error"):
        return "retry" if state.get("db_retries", 0) <= settings.max_sql_retries else "fail"
    if state.get("quality_issue"):
        return "retry"
    return "ok"


def _route_after_summarize(state: State) -> str:
    """With verification off, the answer goes straight to the chart.

    `ground_check` and `review` are skipped rather than made no-ops, so that
    `RUNTIME_VERIFICATION=false` reproduces the pre-ADR-012 pipeline exactly: same nodes,
    same calls, same stage breakdown. A comparison against a baseline that quietly ran
    two extra nodes would not be a comparison.
    """
    return "ground_check" if state.get("verify_enabled") else "pick_chart"


def _route_after_review(state: State) -> str:
    return state.get("next", "ok")


def build_graph():
    """Assemble and compile the graph.

    No checkpointer, even though the pipeline is now multi-turn (ADR-011). The history is
    held by the caller, since the Streamlit session already stores it for display, and passed
    in as an input. Checkpointing is the mechanism for cross-SESSION persistence, which
    nothing here requires.
    """
    graph = StateGraph(State)

    # Every node is registered through `_timed`, so the latency breakdown covers the
    # whole graph by construction. Adding a node without timing it would take a
    # deliberate departure from the line above it rather than an oversight.
    for name, node in (
        ("contextualize", contextualize),
        ("classify", classify),
        ("clarify", clarify),
        ("refuse", refuse),
        ("generate_sql", generate_sql),
        ("validate", validate),
        ("reject", reject),
        ("execute", execute),
        ("verify", verify),
        ("summarize", summarize),
        ("ground_check", ground_check),
        ("review", review),
        ("pick_chart", pick_chart),
    ):
        graph.add_node(name, _timed(name, node))

    graph.add_conditional_edges(
        START,
        _route_from_start,
        {"contextualize": "contextualize", "classify": "classify"},
    )
    graph.add_edge("contextualize", "classify")
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
    #
    # `verify` hangs off the same router, which keeps that claim exactly as strong as it
    # was: the verifier is a sibling of `execute`, not a step on the way to it, and it
    # holds no key that any edge into the database reads.
    graph.add_edge("generate_sql", "validate")
    graph.add_conditional_edges(
        "validate",
        _route_after_validate,
        {"execute": "execute", "verify": "verify", "fail": "reject"},
    )
    graph.add_edge("reject", END)

    # The verifier's branch ends here. Its verdict is left in state for `review`, which
    # runs two supersteps later; ending a branch at END does not stop its siblings.
    graph.add_edge("verify", END)

    graph.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"ok": "summarize", "retry": "generate_sql", "fail": END},
    )
    graph.add_conditional_edges(
        "summarize",
        _route_after_summarize,
        {"ground_check": "ground_check", "pick_chart": "pick_chart"},
    )
    graph.add_edge("ground_check", "review")
    graph.add_conditional_edges(
        "review",
        _route_after_review,
        {"ok": "pick_chart", "regenerate": "generate_sql", "resummarise": "summarize"},
    )
    graph.add_edge("pick_chart", END)

    return graph.compile()


_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def ask(
    question: str,
    history: list[Turn] | None = None,
    verification: bool | None = None,
) -> AgentResult:
    """Answer one question. This is the entry point for both the UI and the eval harness.

    Never raises for expected failure modes: an unreachable provider or a query that
    fails twice returns an AgentResult with `outcome=ERROR`, so callers have one code
    path and the eval harness can score a failure rather than crashing on it.

    Args:
        question: The user's question, verbatim.
        history: Prior turns, oldest first (ADR-011). Only the last
            `settings.history_turns` are read. Empty or None means a first turn, which
            skips the rewrite node entirely. The CALLER owns this list; the agent holds
            no conversation state between calls.
        verification: Override for ADR-012's runtime verification. None uses
            `settings.runtime_verification`; False reproduces the pre-ADR-012 pipeline,
            which is what the comparison runs measure against.

    Returns:
        An AgentResult. `question` is always what the user typed;
        `interpreted_question` carries the rewrite when a follow-up was resolved.
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
                "asked": question,
                # Turns that produced no SQL are still carried: the question the user
                # asked before a clarification is exactly what makes their next word
                # ("by containers") resolvable. What is never carried is answer text or
                # rows (ADR-011).
                "history": list(history or []),
                "schema": schema,
                "usage": usage,
                "timings": timings,
                "attempts": 0,
                "verify_enabled": (
                    settings.runtime_verification if verification is None else verification
                ),
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
    verification_verdict = final.get("verification")
    common = {
        "interpreted_question": final.get("interpreted") or None,
        "elapsed_s": elapsed,
        "stage_timings": timings.as_dict(),
        "llm_calls": usage.calls,
        "cost_usd": round(usage.cost_usd, 6),
        "retry_reasons": final.get("retry_reasons") or [],
    }

    # Reached END from `execute` with an error still set: the retry was used and failed.
    if outcome == Outcome.ERROR and final.get("error"):
        return AgentResult(
            question=question,
            outcome=Outcome.ERROR,
            answer="The query could not be executed successfully.",
            sql=final.get("sql"),
            error=final.get("error"),
            **common,
        )

    return AgentResult(
        question=question,
        outcome=outcome,
        answer=final.get("answer", ""),
        sql=final.get("sql"),
        result=final.get("result"),
        chart=final.get("chart"),
        error=final.get("error") or None,
        reading=verification_verdict.reading if verification_verdict else None,
        caveat=final.get("caveat") or None,
        grounding_flag=final.get("grounding") or None,
        **common,
    )
