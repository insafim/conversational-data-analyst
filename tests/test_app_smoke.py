"""`app.py`, rendered headlessly by Streamlit's own test harness.

The file had no test at all, which is how the record-after-render bug survived: nothing
executed the page, so the only way to catch it was to click at the wrong moment. These
tests do not re-assert what `tests/test_conversations.py` already covers. They cover the
part that only breaks when the page actually runs: that it renders, that a stored turn
rehydrates into the pane, and that the sidebar controls are wired to the session.

No question is ever submitted here, so no model is called and nothing is spent. The store
is pointed at the throwaway database from `tests/conftest.py`, so a run never writes into
the `ports_app` database the running application uses.

Requires:  docker compose up -d --wait && python db/seed.py
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.store
from src.config import settings
from src.models import AgentResult, ChartKind, ChartSpec, Outcome, QueryResult

pytestmark = pytest.mark.integration

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def app(store, monkeypatch) -> AppTest:
    """`app.py` wired to a throwaway store.

    `Settings` is a frozen dataclass, so the whole object is replaced rather than one
    field mutated, and it is replaced in `src.store` because that module bound the name at
    import time. The Streamlit caches are cleared because `_store` is a `cache_resource`
    and would otherwise hand a later test the previous test's dropped database.

    Source: https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest
    Verified: 2026-08-12, on the pinned Streamlit 1.61.1, by introspection rather than from
    the page: `streamlit.testing.v1.AppTest` exists and exposes `from_file`, `run`,
    `sidebar`, `button` and `chat_input`, and `st.cache_resource.clear` is callable. The
    cross-test caching claim was checked by removing both `clear()` calls and running this
    file: tests then fail against a previous test's dropped database, two of them when this
    was last measured on 2026-08-12. The count is incidental and will move as tests are
    added; that removing the calls breaks the file is the part being asserted.
    """
    monkeypatch.setattr(
        src.store, "settings", dataclasses.replace(settings, store_dsn=store.dsn)
    )
    st.cache_resource.clear()
    st.cache_data.clear()
    return AppTest.from_file(APP, default_timeout=60)


def _table_with(at: AppTest, column: str):
    """The rendered dataframe carrying `column`, by name rather than by position.

    The observability page renders more than one table and their order is a layout
    decision, so a positional lookup couples every assertion to where a section happens to
    sit on the page. It failed exactly that way once: a table added above the eval runs
    turned `at.dataframe[0]` into a different object, and the test went on passing.
    """
    matches = [frame.value for frame in at.dataframe if column in frame.value.columns]
    assert matches, (
        f"no rendered table has a {column!r} column; "
        f"found {[list(f.value.columns) for f in at.dataframe]}"
    )
    # Ambiguity is failed rather than resolved by taking the first match. Returning the
    # first would rebuild the exact bug this helper replaced: a caller would go on
    # asserting happily against whichever table happened to render earlier. The trigger
    # would be a column-name collision instead of a layout reorder, which is rarer and
    # correspondingly harder to spot.
    assert len(matches) == 1, (
        f"{len(matches)} rendered tables have a {column!r} column, so which one this "
        "returns is decided by page order rather than by the test"
    )
    return matches[0]


def _answered(**overrides) -> AgentResult:
    base = dict(
        question="Which terminal has the longest average berth wait?",
        outcome=Outcome.ANSWERED,
        answer="Jebel Ali Terminal 2, at 17.46 hours.",
        sql="SELECT terminal_name, avg_wait FROM terminals",
        result=QueryResult(
            columns=["terminal_name", "avg_wait"],
            column_types=["text", "numeric"],
            rows=[["Jebel Ali Terminal 2", 17.46], ["Rotterdam Delta", 9.1]],
            row_count=2,
            elapsed_s=0.02,
        ),
        chart=ChartSpec(
            kind=ChartKind.BAR, x="terminal_name", y=["avg_wait"], reason="2 categories"
        ),
        elapsed_s=6.94,
        llm_calls=4,
        cost_usd=0.0142,
    )
    return AgentResult(**{**base, **overrides})


def test_the_page_renders_with_no_saved_chats(app) -> None:
    """The first-run state. A new reviewer sees this before anything else."""
    at = app.run()

    assert not at.exception, at.exception
    assert at.title[0].value == "Conversational Data Analyst"
    labels = [button.label for button in at.sidebar.button]
    assert "New chat" in labels


def test_the_sidebar_offers_the_columns_behind_the_count_it_shows(app) -> None:
    """Schema handling, as a reviewer can actually check it.

    The count is the expander's label, so the control that states how many columns a table
    has is the same control that lists them. A count rendered as plain text beside a
    separate expander could disagree with it; this cannot.

    The sidebar keeps the human name in front, because the reason this listing is
    collapsed at all is that a non-technical user cannot form a question from a column
    list and did not come here for one.
    """
    at = app.run()

    assert not at.exception, at.exception
    labels = [e.label for e in at.sidebar.expander]
    assert "9 columns" in labels, "port_calls does not offer its columns"
    assert len(labels) == 5, f"expected one expander per table, found {labels}"

    # The names are behind the expander, and the human name is not.
    body = " ".join(m.value for m in at.sidebar.markdown)
    assert "berth_wait_hours" in body, "the column names never reached the sidebar"
    assert "Vessel visits" in body, "the human table name was displaced by the listing"


def test_a_saved_chat_is_listed_and_reopens_with_its_table_and_chart(app, store) -> None:
    """The whole point of the store, exercised through the page rather than around it.

    A reopened conversation that came back as text would be worse than not saving it, so
    the assertion is on the chart and the SQL rather than only on the answer.
    """
    conversation = store.create_conversation("Berth waits by terminal")
    store.append_turn(conversation, "Which terminal waits longest?", _answered())

    at = app.run()
    assert not at.exception, at.exception

    opener = [b for b in at.sidebar.button if b.label == "Berth waits by terminal"]
    assert opener, "the saved chat is not in the sidebar"

    at = opener[0].click().run()
    assert not at.exception, at.exception

    assert any("Jebel Ali Terminal 2" in m.value for m in at.markdown), "the answer is missing"

    # The SQL is asserted by its content, not by the presence of an expander. Auditability
    # is the app's first stated design goal, and an expander holding stale or empty SQL
    # satisfies a truthy check while failing the goal entirely.
    assert at.code, "the SQL expander is missing, so the answer cannot be audited"
    assert at.code[0].value == "SELECT terminal_name, avg_wait FROM terminals"

    captions = " ".join(c.value for c in at.caption)
    # AppTest 1.61.1 exposes no accessor for st.bar_chart, so the rendered proxy for the
    # chart is the rule caption app.py emits beside it. A rehydration that lost the
    # ChartSpec would drop this line, which is what makes it worth asserting.
    assert "Chart chosen by rule: 2 categories" in captions, "the chart did not survive reopening"

    # Telemetry is beside the answer again, and COLLAPSED, which is the whole of the
    # distinction. It was an always-visible caption until 2026-08-12, removed because a
    # single reading has no baseline; the Observability page supplies the baseline, so it
    # returned as a disclosure rather than as page furniture.
    #
    # The two halves are asserted separately on purpose. That it is in an expander LABEL
    # and not in a caption is what makes it collapsed, so checking only that the seconds
    # appear somewhere would pass for the shape that was deliberately rejected.
    labels = [e.label for e in at.expander]
    assert "Answered in 6.94s" in labels, "the turn's latency is not disclosed at all"
    assert not any("6.94s" in c.value for c in at.caption), (
        "the latency is a visible caption again, not a collapsed disclosure"
    )

    # $0.0142, not $0.01. A per-question cost rounded to the cent is the same string a
    # question costing half as much would produce, which is why `format_unit_usd` exists.
    assert any("$0.0142" in c.value for c in at.caption), (
        "the cost was rounded to the cent, so the figure stopped being a measurement"
    )
    assert any("4 model calls" in c.value for c in at.caption)


def test_a_resolved_follow_up_shows_what_it_was_taken_to_mean(app, store) -> None:
    """ADR-011's rewrite, rendered where the user can contradict it.

    An info box rather than the caption it was. A caption is the quietest element
    Streamlit has, and this is the one line on the page the reader is being asked to
    check: if the follow-up was resolved wrongly, everything below it is answering a
    different question. Asserted as `at.info` specifically, because "the text appears
    somewhere" was true of the version that was too quiet to do its job.
    """
    conversation = store.create_conversation("Follow-ups")
    store.append_turn(
        conversation,
        "and by containers?",
        _answered(
            interpreted_question="Which terminal moved the most containers?",
        ),
    )

    at = app.run()
    at = [b for b in at.sidebar.button if b.label == "Follow-ups"][0].click().run()

    assert not at.exception, at.exception
    boxes = [i.value for i in at.info]
    assert any("Which terminal moved the most containers?" in b for b in boxes), (
        f"the rewrite is not an info box; info boxes were {boxes}"
    )
    assert not any("Interpreted as" in c.value for c in at.caption), (
        "the rewrite is still rendered as a caption"
    )


def test_new_chat_clears_the_pane_without_deleting_anything(app, store) -> None:
    """New chat is not delete. The conversation stays in the sidebar to be reopened."""
    conversation = store.create_conversation("Berth waits by terminal")
    store.append_turn(conversation, "Which terminal waits longest?", _answered())

    at = app.run()
    at = [b for b in at.sidebar.button if b.label == "Berth waits by terminal"][0].click().run()
    assert any("Jebel Ali Terminal 2" in m.value for m in at.markdown)

    at = [b for b in at.sidebar.button if b.label == "New chat"][0].click().run()

    assert not at.exception, at.exception
    assert not any("Jebel Ali Terminal 2" in m.value for m in at.markdown), (
        "the pane kept the old turn"
    )
    assert store.list_conversations()[0].turn_count == 1, "New chat destroyed the conversation"
    assert [b for b in at.sidebar.button if b.label == "Berth waits by terminal"], (
        "the conversation left the sidebar"
    )


def test_a_session_that_started_without_a_store_picks_one_up_when_it_returns(
    app, store, store_dsn_for, monkeypatch
) -> None:
    """The session outlives any single rerun, so a ChatSession built during an outage would
    stay stuck with no store for as long as the tab stayed open, and would render as "no
    saved chats" rather than as a store that is down."""
    monkeypatch.setattr(
        src.store,
        "settings",
        dataclasses.replace(settings, store_dsn=store_dsn_for("definitely_absent_db")),
    )
    st.cache_resource.clear()
    at = app.run()
    assert not at.exception, at.exception

    # The store comes back, and a conversation already exists in it.
    store.create_conversation("Berth waits by terminal")
    monkeypatch.setattr(
        src.store, "settings", dataclasses.replace(settings, store_dsn=store.dsn)
    )
    st.cache_resource.clear()
    at = at.run()

    assert not at.exception, at.exception
    assert [b for b in at.sidebar.button if b.label == "Berth waits by terminal"], (
        "the session is still holding the dead store handle"
    )


def test_the_observability_page_shows_the_traffic_the_store_holds(app, store) -> None:
    """The live half, driven through the real page.

    `switch_page` is why both pages are files rather than callables: it only reaches
    file-based pages, so a callable page could not be tested at all. The title assertion
    is not decoration, it is what proves navigation happened. An earlier version of this
    test set a query parameter instead, silently rendered the Chat page, and passed on a
    bare `assert not at.exception`.
    """
    conversation = store.create_conversation("Berth waits")
    store.append_turn(conversation, "q", _answered(elapsed_s=4.0, cost_usd=0.004))
    store.append_turn(conversation, "q", _answered(elapsed_s=8.0, cost_usd=0.006))

    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    assert at.title[0].value == "Observability", "navigation did not reach the page"

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Questions"] == "2"
    assert metrics["Median latency"] == "6.00s", "the median of 4s and 8s is not shown"
    assert metrics["Conversations"] == "1"
    assert metrics["LLM calls"] == "8", "the call count tile is not wired to the store"
    # 7.80s, not 8.00s: `percentile_cont` interpolates, so p95 of 4s and 8s is
    # 4 + 0.95 * (8 - 4). Asserting the interpolated value also pins the choice of
    # `percentile_cont` over `percentile_disc`, which would return 8.00s here.
    assert metrics["p95 latency"] == "7.80s", "the p95 tile does not show the tail"

    # Both branches of the money format, which exist because these two figures genuinely
    # differ by orders of magnitude. A per-question cost rendered as "$0.01" reads as a
    # cent when it is half of one, and "$0.0100" for a total is noise.
    assert metrics["Total cost"] == "$0.01"
    assert metrics["Cost per question"] == "$0.0050", "a sub-cent cost was rounded away"


def test_the_observability_page_lists_the_committed_eval_runs(app, store) -> None:
    """The eval half reads `eval/results/` from disk, so it renders against the real
    repository rather than against the throwaway store."""
    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    assert at.dataframe, "the eval run table is missing"

    # Selected by its columns, NOT by `at.dataframe[0]`, which is what this was. The page
    # now renders a latest-questions table above this one, so the positional read silently
    # started checking the wrong table while still passing every assertion below it. A
    # test that keeps passing against the wrong object is worse than one that fails.
    runs = _table_with(at, "Run")
    assert len(runs) >= 20, "the committed run artefacts were not read"

    latest = runs[runs["Run"] == "run25"]
    assert not latest.empty, "run25 is missing from the table"
    row = latest.iloc[0]
    assert row["Cases"] == 108

    # The two rates are pinned and checked against each other, because they are the pair
    # the columns could be swapped between and the swap would otherwise be invisible.
    # These are the figures ADR-012 and the README quote for run 25.
    assert row["Overall"] == "94.4%", "the overall column does not match the artefact"
    assert row["Grounded"] == "97.4%", (
        "groundedness is wrong, which usually means it was divided by every case rather "
        "than by the cases where it was actually scored"
    )
    assert row["Overall"] != row["Grounded"], "the two columns are showing the same figure"


def test_the_observability_page_scores_the_newest_run_by_category(app, store) -> None:
    """The three figures the brief's eval section asks for, on screen rather than in a
    file. Pinned to run 26, the configuration that ships, because the tiles read the
    newest committed run and a reviewer will read these aloud.

    Asserted through the page rather than only in `tests/test_telemetry.py` because the
    arithmetic being right and the arithmetic reaching a tile are different claims, and
    the second one is what a demo depends on.
    """
    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Execution accuracy"] == "93.5%"
    assert metrics["Ambiguity handling"] == "91.7%"
    assert metrics["Safety"] == "100.0%", (
        "the adversarial subset is not at 19/19, which is the read-only evidence"
    )
    # Distinct from the overall score in the table below, which is 94.4% for run 25 and
    # 94.4% for run 26. Two figures both called accuracy is how a deck quotes the wrong one.
    assert metrics["Execution accuracy"] != _table_with(at, "Run").iloc[0]["Overall"]


def test_the_stage_chart_is_sorted_and_labelled_the_way_it_is_read(app, store) -> None:
    """The chart's spec, not merely that drawing it raised nothing.

    Two reasons this is asserted properly rather than smoke-tested. Every other test on
    this page stores turns with no `stage_timings`, so `live.stage_means_s` came back empty
    and the chart branch was skipped in all of them: the `st.bar_chart` call had never once
    executed under test. And the arguments that make the chart readable are exactly the
    ones a bare "it rendered" check cannot see. `x_label` and `y_label` follow the DATA
    role, not the rendered axis, so under `horizontal=True` they were written the wrong way
    round first and put "mean seconds" against the stage names.

    `st.bar_chart` has no `AppTest` accessor, but it emits a `vega_lite_chart` element
    whose `proto.spec` is JSON, and that is a stable enough surface to assert the two
    properties the chart exists for: the slowest stage on top, and the seconds axis named.
    """
    conversation = store.create_conversation("Timed")
    store.append_turn(
        conversation,
        "q",
        _answered(stage_timings={"generate_sql": 3.3, "classify": 1.9, "summarize": 1.1}),
    )
    store.append_turn(
        conversation,
        "q",
        _answered(stage_timings={"generate_sql": 4.1, "classify": 1.5, "execute": 0.02}),
    )

    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    assert at.title[0].value == "Observability", "navigation did not reach the page"

    charts = at.get("vega_lite_chart")
    assert charts, "the stage chart did not render at all"
    encoding = json.loads(charts[0].proto.spec)["encoding"]

    # Horizontal bars: the measure is drawn along x and the categories up the y axis.
    assert encoding["x"]["field"] == "seconds"
    assert encoding["y"]["field"] == "stage"

    # Slowest first. Without this the bars come back alphabetically, which buries the one
    # fact the chart exists to state.
    assert encoding["y"]["sort"] == {"field": "seconds", "order": "descending"}

    # The labels, pinned to the axis each one actually reaches. This is the assertion that
    # fails if `x_label` and `y_label` are swapped back.
    assert encoding["x"]["title"] == "mean seconds", (
        "the measure axis is unlabelled, so the numbers have no unit"
    )
    assert encoding["y"]["title"] == "", "the stage names were given a redundant title"


def test_the_guardrail_tiles_count_the_blocks_including_the_zeroes(app, store) -> None:
    """A guardrail that never fired still has a figure, and it is zero. Omitting it would
    make an untested guardrail indistinguishable from an absent one, which is the exact
    confusion this panel exists to remove."""
    conversation = store.create_conversation("Mixed")
    store.append_turn(conversation, "q", _answered())
    store.append_turn(
        conversation,
        "drop the port_calls table",
        _answered(outcome=Outcome.REJECTED, answer="I couldn't run that safely.", chart=None),
    )

    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Blocked by validator"] == "1"
    assert metrics["Answered"] == "1"
    assert metrics["Refused"] == "0", "a guardrail that did not fire lost its tile"
    assert "Rejected" not in metrics, (
        "the block is attributed to the model rather than to the validator"
    )


def test_the_observability_page_lists_the_questions_that_were_asked(app, store) -> None:
    """The live half's most convincing element: the thing a viewer just watched happen,
    with its latency and cost beside it."""
    conversation = store.create_conversation("Berth waits")
    store.append_turn(conversation, "Which terminal waits longest?", _answered())

    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    recent = _table_with(at, "Question")
    assert recent.iloc[0]["Question"] == "Which terminal waits longest?"
    assert recent.iloc[0]["Outcome"] == "answered"
    assert recent.iloc[0]["Latency"] == "6.94s"
    assert recent.iloc[0]["Cost"] == "$0.0142", "the per-question cost was rounded away"


def test_the_observability_page_says_so_when_the_store_is_unreachable(
    app, store_dsn_for, monkeypatch
) -> None:
    """The live half must degrade the way every other store reader does: a message, not a
    traceback, and the eval half unaffected because it reads from disk."""
    monkeypatch.setattr(
        src.store,
        "settings",
        dataclasses.replace(settings, store_dsn=store_dsn_for("definitely_absent_db")),
    )
    st.cache_resource.clear()

    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    assert at.title[0].value == "Observability"
    assert any("store is unavailable" in i.value for i in at.info), (
        "the live half did not explain why it is empty"
    )
    assert at.dataframe, "the eval half was lost when the store went away"


def test_the_observability_page_survives_an_empty_store(app, store) -> None:
    """The state a reviewer opens it in: the app is running, nothing has been asked yet.
    The eval half must still render, because it does not depend on the store at all."""
    at = app.switch_page("views/observability.py").run()

    assert not at.exception, at.exception
    assert any("No questions have been asked yet" in i.value for i in at.info), (
        "an empty store did not explain itself"
    )
    assert at.dataframe, "the eval half was lost when the live half was empty"


def test_a_store_that_was_down_at_startup_recovers_without_a_restart(
    app, store, store_dsn_for, monkeypatch
) -> None:
    """The cache must not memoise a construction failure.

    `st.cache_resource` memoises a return value but not a raised exception, so
    `views/state.py` lets `_build_store` raise and catches outside it. Catching inside
    would cache `None` for the life of the process, and a reviewer who started the app
    before the database was ready would never get history back without restarting.

    Deliberately does NOT clear the cache between runs: clearing it would prove nothing,
    since that is what a restart does.
    """
    monkeypatch.setattr(
        src.store,
        "settings",
        dataclasses.replace(settings, store_dsn=store_dsn_for("definitely_absent_db")),
    )
    st.cache_resource.clear()
    at = app.run()
    assert not at.exception, at.exception
    assert any("not be saved" in c.value for c in at.sidebar.caption), (
        "the precondition did not hold: the store was reachable"
    )

    # The database comes back. No cache clear, no restart.
    store.create_conversation("Berth waits by terminal")
    monkeypatch.setattr(
        src.store, "settings", dataclasses.replace(settings, store_dsn=store.dsn)
    )
    at = at.run()

    assert not at.exception, at.exception
    assert [b for b in at.sidebar.button if b.label == "Berth waits by terminal"], (
        "the failed construction was cached, so the app never noticed the store return"
    )


def test_an_unreachable_store_still_renders_a_usable_chat(
    app, store_dsn_for, monkeypatch
) -> None:
    """`db/03_app_store.sql` runs only on the container's first boot, so an older data
    volume has no store database. The chat is the deliverable; losing history is a caption,
    not a stack trace."""
    absent = store_dsn_for("definitely_absent_db")
    monkeypatch.setattr(
        src.store, "settings", dataclasses.replace(settings, store_dsn=absent)
    )
    st.cache_resource.clear()

    at = app.run()

    assert not at.exception, at.exception
    # Rendered, not exercised: this file submits no question anywhere, so this asserts the
    # chat survived the missing store, not that a question would be answered.
    assert at.chat_input, "the chat input is gone, so there is nothing left to use"
    assert any("not be saved" in c.value for c in at.sidebar.caption), (
        "the user is not told that history is unavailable"
    )
