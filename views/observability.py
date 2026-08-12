"""The observability page: what this application measured about itself.

Two halves that are never added together. The live half is whatever a user happened to
ask; the eval half is a fixed 108-case benchmark run deliberately. A combined average
would describe neither, so they are separate sections with their own headings.

A page script rather than a callable passed to `st.Page`, which is a testability decision
rather than a stylistic one: `AppTest.switch_page` only reaches file-based pages, so a
callable page cannot be driven by a test at all. Source and verification are in `app.py`,
where the pages are registered.

Each half now answers a different question about the guardrails, and the split is
deliberate. The live half counts what this application happened to be asked, which on a
fresh store is nothing at all. The eval half carries the 19 adversarial cases, which is
the half that can actually prove read-only enforcement, because it does not depend on a
reviewer having tried an attack in this browser. The live panel says so and points down
the page rather than borrowing the eval figure into its own section.

Everything it aggregates is decided elsewhere. `Store.telemetry()` does the SQL,
`src/telemetry.py` reads the committed eval artefacts and owns what every measurement is
called, both asserted in `tests/test_telemetry.py` without a browser. This file renders.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.conversations import STORE_FAILURES
from src.telemetry import (
    count_of,
    format_unit_usd,
    format_usd,
    load_eval_runs,
    outcome_tiles,
)
from views.state import chat_session

session = chat_session()

# How many recent questions the live half lists. Ten because it is a window on what just
# happened, not an archive: the aggregate tiles above already describe the whole store,
# and a longer list would push the eval section off the screen.
_RECENT_QUESTIONS = 10

# How often the live half redraws while the page is open. Five seconds is chosen against
# what it is watching rather than for smoothness: a turn takes about six seconds, so a
# shorter interval mostly redraws identical numbers, and a longer one makes a question
# asked in another tab look like it was dropped.
_REFRESH_SECONDS = 5


def _bar_table(counts: dict[str, int]) -> None:
    """A count table, widest first. `st.bar_chart` needs a frame and an axis choice for
    what is a handful of labelled integers, so the plain table is the smaller thing."""
    total = sum(counts.values())
    for label, count in counts.items():
        share = 100.0 * count / total if total else 0.0
        st.text(f"{label:<22} {count:>5}  {share:>5.1f}%")


@st.fragment(run_every=_REFRESH_SECONDS)
def live_telemetry() -> None:
    """Traffic this application actually served, re-read on a timer.

    A fragment rather than a whole-page rerun so the eval section below, which reads 25
    files from disk, is not rebuilt every few seconds to show numbers that did not change.

    **Why watching live traffic needs a second tab.** Three facts, each read from the
    installed Streamlit 1.61.1 rather than assumed, on 2026-08-12:

    1. `run_every` does not start a server-side timer. It sends an `AutoRerun` message
       carrying the interval to the browser (`streamlit/runtime/fragment.py`, which sets
       `msg.auto_rerun.interval`), so the browser asks for each refresh.
    2. A rerun request is only serviced at a yield point, and the yield points are `st.*`
       calls. Streamlit's own comment on `_maybe_handle_execution_control_request` says
       most `st.foo` commands "act as implicit yield points". `ask()` spends its six
       seconds in model and database calls and touches no Streamlit API, so a refresh
       queued during it cannot be serviced until it returns.
    3. Each websocket connection gets its own session
       (`web/server/starlette/starlette_websocket.py` calls `runtime.connect_session`),
       and a second tab is a second websocket.

    Navigation makes the first two mostly moot: this page and the chat are separate pages,
    so a tab showing this panel is not running `ask()` at all. The third is what makes the
    advice work. Two tabs are two sessions, so the panel keeps refreshing while the other
    tab waits on an answer.
    """
    if session.store is None:
        st.info("The conversation store is unavailable, so there is no live traffic to show.")
        return

    try:
        live = session.store.telemetry()
    except STORE_FAILURES:
        st.warning("The conversation store could not be read just now.")
        return

    if not live.turns:
        st.info("No questions have been asked yet. Ask one on the Chat page.")
        return

    first, second, third, fourth = st.columns(4)
    first.metric("Questions", f"{live.turns:,}")
    second.metric("Median latency", f"{live.median_latency_s:.2f}s", help="p50 across turns")
    third.metric("p95 latency", f"{live.p95_latency_s:.2f}s")
    fourth.metric("Total cost", format_usd(live.total_cost_usd))

    fifth, sixth, seventh = st.columns(3)
    fifth.metric("Cost per question", format_unit_usd(live.mean_cost_usd))
    sixth.metric("LLM calls", f"{live.llm_calls:,}")
    seventh.metric("Conversations", f"{live.conversations:,}")

    # --- guardrails ---------------------------------------------------------------
    # Tiles rather than the count table this was, because the counts are the point rather
    # than their shares: "blocked 1" is a fact about the validator, and "blocked 4.2% of
    # traffic" is a fact about what happened to get asked today.
    st.markdown("**Guardrails**")
    tiles = outcome_tiles(live.outcomes)
    for column, (label, count) in zip(st.columns(len(tiles)), tiles, strict=True):
        column.metric(label, f"{count:,}")
    st.caption(
        "A refusal and a clarification are successful outcomes, not failures. Only "
        "`error` is the system failing to answer. These count what this application "
        "happened to be asked; the enforcement itself is proven against the 19 "
        "adversarial cases in the Evaluation section below, which is evidence that does "
        "not depend on anyone having tried an attack in this browser."
    )

    st.markdown("**Where the time goes**")
    if live.stage_means_s:
        st.bar_chart(
            {
                "stage": list(live.stage_means_s.keys()),
                "seconds": list(live.stage_means_s.values()),
            },
            x="stage",
            y="seconds",
            horizontal=True,
            # Longest bar at the top. Without it the bars come back in alphabetical order,
            # which buries the one fact the chart exists to state. Verified from the
            # generated Vega-Lite spec on the pinned 1.61.1, not assumed: this renders as
            # `sort: {field: seconds, order: descending}` on the categorical axis.
            sort="-seconds",
            # The labels follow the DATA role, not the rendered axis. `horizontal=True`
            # draws the `x` column up the side and the `y` column along the bottom, but
            # `x_label` still names the `x` column. Written the other way round first,
            # which put "mean seconds" against the stage names; the spec showed it.
            x_label="",
            y_label="mean seconds",
        )
    st.caption("Mean seconds per stage. Stages are timed individually, so they sum to "
               "slightly less than the total; the difference is graph overhead.")

    if live.retry_reasons:
        st.markdown("**Retries, by reason**")
        _bar_table(live.retry_reasons)
        st.caption(
            "Named rather than counted (ADR-012): a count cannot tell a database error "
            "from the verifier changing the query. A turn that retried twice appears twice."
        )

    # --- the questions themselves -------------------------------------------------
    st.markdown("**Latest questions**")
    try:
        recent = session.store.recent_turns(limit=_RECENT_QUESTIONS)
    except STORE_FAILURES:
        st.caption("The recent questions could not be read just now.")
        return

    # `st.dataframe` renders its cells as text. That is load-bearing rather than
    # incidental: every question in this table is user-supplied, and a surface that
    # rendered markdown or HTML here would be a stored-content injection channel into the
    # operator's own page, which is the class of problem ADR-011 keeps closed elsewhere.
    st.dataframe(
        [
            {
                "Asked": turn.asked_at[11:19],
                "Question": turn.question,
                "Outcome": turn.result.outcome.value,
                "Latency": f"{turn.result.elapsed_s:.2f}s",
                "Cost": format_unit_usd(turn.result.cost_usd),
            }
            for turn in recent
        ],
        width="stretch",
        hide_index=True,
    )


with st.sidebar:
    st.header("Observability")
    st.caption(
        "What this application measured about itself. The live half reads the "
        "conversation store; the eval half reads the committed runs in `eval/results/`."
    )
    st.divider()
    st.caption(
        "Cost and latency were measured per request before this page existed, and "
        "discarded when the answer was rendered. Storing the turn (ADR-014) is what "
        "made a trend possible."
    )

st.title("Observability")

st.subheader("Live")
st.caption(
    f"Refreshes every {_REFRESH_SECONDS} seconds. To watch turns arrive as they are "
    "answered, keep this open in a second tab and ask in the first: each tab is its own "
    "session, so this one keeps refreshing while the other waits on an answer."
)
live_telemetry()

st.divider()

st.subheader("Evaluation runs")
runs = load_eval_runs(Path(__file__).resolve().parent.parent / "eval" / "results")
if not runs:
    st.info("No committed eval runs were found in `eval/results/`.")
else:
    # The newest run, scored by what the gold set was testing. Three denominators over one
    # file, and the overall score hides all three: an adversarial case "passes" by being
    # refused or blocked, so safety can read 100% inside a run whose overall score is 94%.
    #
    # The run is NAMED in the caption rather than implied. Three configurations exist in
    # `eval/results/` and conflating them is the easiest mistake this repository offers:
    # runs 21, 23 and 25 are the both-switches-off baseline, runs 20, 22 and 24 have
    # runtime verification on, and the newest run is what actually ships. A figure quoted
    # without its run number is not checkable.
    latest = runs[0]
    scored = latest.labelled_categories()
    if scored:
        for column, (label, score) in zip(st.columns(len(scored)), scored, strict=True):
            column.metric(
                label,
                f"{100 * score.rate:.1f}%",
                help=f"{score.passed} of {count_of(score.cases, 'case')}",
            )
        st.caption(
            f"`{latest.name}`, the newest committed run, scored by category. Safety is "
            "the adversarial subset: prompt injection, DDL, DML, multiple statements and "
            "catalog reconnaissance, each of which passes only by being refused before "
            "the database or blocked by the validator. Execution accuracy is the "
            "answerable subset, which is a different denominator from the overall score "
            "in the table below."
        )
        st.divider()

    st.caption(
        "Committed artefacts, newest first (ADR-010). Execution accuracy and groundedness "
        "are shown apart because they moved in opposite directions when runtime "
        "verification was switched on, which is the measurement ADR-012 rests on."
    )

    st.dataframe(
        [
            {
                "Run": run.name,
                "Cases": run.cases,
                # "Overall" rather than "Accuracy", matching what the harness prints and
                # what the README quotes. Execution accuracy is a different number, scored
                # on the answerable subset only, and two figures both called accuracy in
                # one repository is how a deck ends up quoting the wrong one.
                "Overall": f"{100 * run.pass_rate:.1f}%",
                "Grounded": f"{100 * run.grounded_rate:.1f}%",
                # Separated from accuracy on purpose. An `error` outcome is the pipeline
                # failing to answer, usually a provider or database fault, and folding it
                # into a wrong-answer rate would blame the model for an outage. Run 18 is
                # the case in point: committed, and invalid because of a DNS failure.
                "Errors": run.outcomes.get("error", 0),
                "Median latency": f"{run.median_latency_s:.2f}s",
                "Cost": format_usd(run.total_cost_usd),
            }
            for run in runs
        ],
        width="stretch",
        hide_index=True,
    )


