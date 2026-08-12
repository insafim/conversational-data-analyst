"""The observability page: what this application measured about itself.

Two halves that are never added together. The live half is whatever a user happened to
ask; the eval half is a fixed 108-case benchmark run deliberately. A combined average
would describe neither, so they are separate sections with their own headings.

A page script rather than a callable passed to `st.Page`, which is a testability decision
rather than a stylistic one: `AppTest.switch_page` only reaches file-based pages, so a
callable page cannot be driven by a test at all. Source and verification are in `app.py`,
where the pages are registered.

Everything it aggregates is decided elsewhere. `Store.telemetry()` does the SQL and
`src/telemetry.py` reads the committed eval artefacts, both asserted in
`tests/test_telemetry.py` without a browser. This file renders.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.conversations import STORE_FAILURES
from src.telemetry import load_eval_runs
from views.state import chat_session

session = chat_session()

# How often the live half redraws while the page is open. Five seconds is chosen against
# what it is watching rather than for smoothness: a turn takes about six seconds, so a
# shorter interval mostly redraws identical numbers, and a longer one makes a question
# asked in another tab look like it was dropped.
_REFRESH_SECONDS = 5


def _money(amount: float) -> str:
    """Costs here span four orders of magnitude, from a fraction of a cent per turn to
    dollars per eval run, and one format cannot show both. Sub-cent values keep four
    decimals rather than rendering as `$0.00`, which reads as free."""
    return f"${amount:.4f}" if amount < 0.01 else f"${amount:,.2f}"


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
    fourth.metric("Total cost", _money(live.total_cost_usd))

    fifth, sixth, seventh = st.columns(3)
    fifth.metric("Cost per question", _money(live.mean_cost_usd))
    sixth.metric("LLM calls", f"{live.llm_calls:,}")
    seventh.metric("Conversations", f"{live.conversations:,}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Outcomes**")
        _bar_table(live.outcomes)
        st.caption(
            "A refusal and a clarification are successful outcomes, not failures. Only "
            "`error` is the system failing to answer."
        )
    with right:
        st.markdown("**Where the time goes**")
        for stage, seconds in live.stage_means_s.items():
            st.text(f"{stage:<16} {seconds:>6.2f}s")
        st.caption("Mean seconds per stage. Stages are timed individually, so they sum to "
                   "slightly less than the total; the difference is graph overhead.")

    if live.retry_reasons:
        st.markdown("**Retries, by reason**")
        _bar_table(live.retry_reasons)
        st.caption(
            "Named rather than counted (ADR-012): a count cannot tell a database error "
            "from the verifier changing the query. A turn that retried twice appears twice."
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
                "Cost": _money(run.total_cost_usd),
            }
            for run in runs
        ],
        width="stretch",
        hide_index=True,
    )


