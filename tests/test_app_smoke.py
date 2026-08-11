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


def _answered() -> AgentResult:
    return AgentResult(
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


def test_the_page_renders_with_no_saved_chats(app) -> None:
    """The first-run state. A new reviewer sees this before anything else."""
    at = app.run()

    assert not at.exception, at.exception
    assert at.title[0].value == "Conversational Data Analyst"
    labels = [button.label for button in at.sidebar.button]
    assert "New chat" in labels


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
    assert "6.94s" in captions, "the telemetry caption is missing"


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
