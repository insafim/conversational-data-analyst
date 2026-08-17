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
    HEADLINE_CRITERIA,
    HEADLINE_LABELS,
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
    # The interpolation the tooltip states is `percentile_cont`'s documented behaviour, not
    # an assumption about it. PostgreSQL: "Computes the continuous percentile, a value
    # corresponding to the specified fraction within the ordered set of aggregated argument
    # values. This will interpolate between adjacent input items if needed." Its discrete
    # counterpart instead returns "the first value within the ordered set ... whose position
    # in the ordering equals or exceeds the specified fraction". `src/store.py` uses the
    # continuous one for both the median and the p95, and `tests/test_app_smoke.py` pins the
    # interpolated 7.80s over turns of 4s and 8s, which `percentile_disc` would report as
    # 8.00s. The container this runs against is PostgreSQL 18.4.
    # Source: https://www.postgresql.org/docs/18/functions-aggregate.html - Verified: 2026-08-17
    third.metric(
        "p95 latency",
        f"{live.p95_latency_s:.2f}s",
        help=(
            "The 95th percentile of turn latency: the slowest one turn in twenty took "
            "about this long. Shown beside the median, which says nothing about how slow "
            "the slow end is. Interpolated (`percentile_cont`), so with few turns it "
            "falls between two of them rather than on one."
        ),
    )
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

    # `st.dataframe` renders its cells as text, and PASSING NO `column_config` HERE IS THE
    # DELIBERATE PART. Every question in this table is user-supplied, so a surface that
    # rendered markdown or HTML would be a stored-content injection channel into the
    # operator's own page, the class of problem ADR-011 keeps closed elsewhere.
    #
    # Verified against Streamlit 1.61.1 rather than assumed, because the docs nowhere state
    # outright that cells are escaped. That version is what `uv.lock` resolves and what is
    # installed here; `pyproject.toml` carries a range, so a lock refresh can move it and
    # this is worth re-reading if it does. Three facts, and the conclusion is assembled from
    # them:
    #
    # 1. A string column gets `TextColumn`, which documents no interpretation of content.
    #    "This is the default column type for string values."
    #    Source: https://docs.streamlit.io/develop/api-reference/data/st.column_config
    #            /st.column_config.textcolumn
    #    Verified: 2026-08-17
    # 2. Even the column type named for markdown does not render it in the grid. "This
    #    column type displays cell values as plain text within the table cells. When a cell
    #    is clicked, the content is shown in an overlay where the markdown is rendered."
    #    That overlay is why `MarkdownColumn` must not be set on `Question`, and the same
    #    goes for `LinkColumn` and `ImageColumn`, which accepts `data:image/svg+xml`.
    #    Source: https://docs.streamlit.io/develop/api-reference/data/st.column_config
    #            /st.column_config.markdowncolumn
    #    Verified: 2026-08-17
    # 3. There is no escape hatch to reach for by accident: `st.dataframe` has no
    #    `unsafe_allow_html` parameter, `streamlit/elements/arrow.py` contains the substring
    #    `html` nowhere at all, and the only column config Streamlit applies on its own is
    #    hiding the index (`elements/lib/column_config_utils.py`, `apply_data_specific_configs`).
    #
    # What that establishes is the Python surface: nothing here can ask for HTML. The
    # bundled frontend grid is not readable source, so this is not a DOM-level claim.
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
    # The newest run, scored by what the gold set was testing. Four denominators over one
    # file, and the overall score hides all four: an adversarial case "passes" by being
    # refused or blocked, so safety can read 100% inside a run whose overall score is 94%.
    #
    # The run is NAMED in the caption rather than implied. Three configurations exist in
    # `eval/results/` and conflating them is the easiest mistake this repository offers:
    # runs 21, 23 and 25 are the both-switches-off baseline, runs 20, 22 and 24 have
    # runtime verification on, and the newest run is what actually ships. A figure quoted
    # without its run number is not checkable.
    latest = runs[0]
    # The same four figures, in the same order, as the eval board in
    # `docs/visuals/eval.html`. A reviewer who has seen the board and then opens this page
    # should be reading one set of numbers, not two overlapping sets that have to be
    # reconciled. Each tile carries its denominator in the tooltip, since no two of the
    # four are divided by the same thing.
    for column, tile in zip(st.columns(4), latest.headline_tiles(), strict=True):
        column.metric(tile.label, tile.value, help=tile.tooltip)
        column.caption(f"**{tile.subtitle}**  \n{tile.detail}")
    st.caption(
        f"`{latest.name}`, the newest committed run. The table below carries the same "
        "four figures for every committed run."
    )
    st.divider()

    st.dataframe(
        [
            {
                "Run": run.name,
                "Cases": run.cases,
                # The four scored figures, per run, replacing the single overall score this
                # showed. The overall score is one number over three subsets that pass by
                # different criteria: an adversarial case "passes" by being refused, so a
                # run can read 94% overall while every guardrail case held.
                #
                # Rates for the two wide denominators and counts for the two narrow ones,
                # matching the tiles above. The gold set holds 12 ambiguous and 19
                # adversarial cases, so a percentage there moves in eight-point steps and
                # reads as more precision than the evidence carries.
                HEADLINE_LABELS["answerable"]: run.rate_cell("answerable"),
                HEADLINE_LABELS["grounded"]: run.grounded_cell(),
                HEADLINE_LABELS["ambiguous"]: run.count_cell("ambiguous"),
                HEADLINE_LABELS["adversarial"]: run.count_cell("adversarial"),
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
        # The prose that used to sit above this table is now attached to the columns it was
        # describing, so a reader who wants to know what a column means asks that column
        # rather than reading a paragraph before reaching the numbers.
        column_config={
            "Run": st.column_config.Column(
                help="The committed artefact in `eval/results/`, newest first (ADR-010)."
            ),
            "Cases": st.column_config.Column(
                help=(
                    "How many gold-set cases the run scored. The set grew as it was "
                    "written, so the older runs are not over the same cases."
                )
            ),
            # The four scored columns take their tooltips from `HEADLINE_CRITERIA`, the same
            # strings the tiles use, rather than restating the pass criteria here. Written
            # out twice they can drift, and a column and a tile that disagree about what
            # counts as a pass is worse than either tooltip being absent.
            **{
                HEADLINE_LABELS[key]: st.column_config.Column(help=criterion)
                for key, criterion in HEADLINE_CRITERIA.items()
            },
            "Errors": st.column_config.Column(
                help=(
                    "Cases where the pipeline failed to answer at all, usually a provider "
                    "or database fault. Run 18 is committed with 81 of them, kept as the "
                    "record of a local DNS outage (ADR-010)."
                )
            ),
            "Median latency": st.column_config.Column(
                help="The p50 case in the run, end to end."
            ),
            "Cost": st.column_config.Column(help="What the whole run spent, all cases together."),
        },
        width="stretch",
        hide_index=True,
    )


