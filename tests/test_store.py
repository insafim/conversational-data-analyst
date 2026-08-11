"""Conversation and telemetry persistence (ADR-014). Needs a live PostgreSQL.

Every test runs against a throwaway DATABASE created and dropped by the fixture, never
against the `ports_app` database the running application uses. A suite that wrote into
real chat history would be a feature nobody asked for, and the first symptom would be a
reviewer opening the app to find test conversations in the sidebar.

Requires:  docker compose up -d --wait && python db/seed.py
"""

from __future__ import annotations

import threading
import uuid

import psycopg
import pytest
from psycopg import sql as pgsql

from src.config import settings
from src.models import AgentResult, ChartKind, ChartSpec, Outcome, QueryResult, RetryReason
from src.store import Store, StoreError

pytestmark = pytest.mark.integration


def _dsn_for(database: str) -> str:
    """The store DSN, pointed at a different database.

    Rewriting one token is the whole reason a separate database is the right shape: in
    production the same substitution points at a different host.
    """
    parts = [p for p in settings.store_dsn.split() if not p.startswith("dbname=")]
    return " ".join([*parts, f"dbname={database}"])


@pytest.fixture
def store() -> Store:
    """A Store on a throwaway database.

    Created by the bootstrap superuser, because `app_rw` deliberately lacks CREATEDB and
    therefore cannot make databases: the same privilege boundary the application relies on
    applies to its own tests, which is the point.
    """
    name = f"ports_app_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(settings.admin_dsn, autocommit=True) as admin:
        admin.execute(
            pgsql.SQL("CREATE DATABASE {} OWNER app_rw").format(pgsql.Identifier(name))
        )
    try:
        made = Store(dsn=_dsn_for(name))
        yield made
        made.close()
    finally:
        with psycopg.connect(settings.admin_dsn, autocommit=True) as admin:
            # A lingering session would block the DROP, so any connection left behind by a
            # failing test is terminated rather than waited on. Without this a single bad
            # test hangs the suite instead of failing it.
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(pgsql.SQL("DROP DATABASE {}").format(pgsql.Identifier(name)))


def _answer(**overrides) -> AgentResult:
    base = dict(
        question="Which terminal has the longest average berth wait?",
        outcome=Outcome.ANSWERED,
        answer="Jebel Ali Terminal 2, at 17.46 hours.",
        sql="SELECT terminal_name FROM terminals",
        elapsed_s=6.94,
        llm_calls=4,
        cost_usd=0.0142,
    )
    return AgentResult(**{**base, **overrides})


# --- the round trip ----------------------------------------------------------------
def test_a_reopened_turn_carries_its_rows_so_the_chart_can_be_drawn_again(store) -> None:
    """The reason turns are stored whole rather than as metadata.

    A reopened conversation that had lost its tables and charts would be worse than not
    saving it, because the user would believe they had their history back.
    """
    rows = QueryResult(
        columns=["terminal_name", "avg_wait"],
        column_types=["text", "numeric"],
        rows=[["Jebel Ali Terminal 2", 17.46], ["Rotterdam Delta", 9.1]],
        row_count=2,
        elapsed_s=0.02,
        truncated=True,
    )
    chart = ChartSpec(kind=ChartKind.BAR, x="terminal_name", y=["avg_wait"], reason="6 categories")
    conversation = store.create_conversation("Berth waits")
    store.append_turn(
        conversation, "Which terminal waits longest?", _answer(result=rows, chart=chart)
    )

    restored = store.load_conversation(conversation)[0].result

    assert restored.result is not None
    assert restored.result.rows == [["Jebel Ali Terminal 2", 17.46], ["Rotterdam Delta", 9.1]]
    assert restored.result.column_types == ["text", "numeric"]
    assert restored.result.truncated is True, "the partial-result warning would not fire again"
    assert restored.chart is not None and restored.chart.kind is ChartKind.BAR
    assert restored.chart.reason == "6 categories"


def test_the_telemetry_fields_survive_because_the_panel_reads_the_same_row(store) -> None:
    """Saved chats and the observability page share one record. A field dropped on the way
    in would make the two views disagree about the same turn."""
    conversation = store.create_conversation("t")
    store.append_turn(
        conversation,
        "q",
        _answer(
            stage_timings={"generate_sql": 3.11, "summarize": 1.58},
            retry_reasons=[RetryReason.DB_ERROR],
            reading="counts berths per terminal",
            caveat="counts berths, not calls",
            grounding_flag="1,234",
            interpreted_question="how many berths at Rotterdam?",
        ),
    )

    restored = store.load_conversation(conversation)[0].result

    assert restored.stage_timings == {"generate_sql": 3.11, "summarize": 1.58}
    assert restored.retry_reasons == [RetryReason.DB_ERROR]
    assert restored.reading == "counts berths per terminal"
    assert restored.caveat == "counts berths, not calls"
    assert restored.grounding_flag == "1,234"
    assert restored.interpreted_question == "how many berths at Rotterdam?"
    assert restored.llm_calls == 4 and restored.cost_usd == 0.0142


def test_a_refusal_round_trips_even_though_it_has_no_sql_or_rows(store) -> None:
    """Not every turn is an answer. A store that assumed one would fail on the first
    injection attempt a reviewer types."""
    conversation = store.create_conversation("t")
    store.append_turn(
        conversation,
        "drop the port_calls table",
        _answer(outcome=Outcome.REFUSED, answer="I can't do that.", sql=None, result=None),
    )

    restored = store.load_conversation(conversation)[0].result
    assert restored.outcome is Outcome.REFUSED
    assert restored.sql is None and restored.result is None


def test_history_survives_the_object_that_wrote_it(store) -> None:
    """The point of item 1: a browser refresh or a restart must not lose the chat. A fresh
    Store on the same database is what a rerun produces."""
    conversation = store.create_conversation("Berth waits")
    store.append_turn(conversation, "Which terminal waits longest?", _answer())

    reopened = Store(dsn=store.dsn)

    assert [c.title for c in reopened.list_conversations()] == ["Berth waits"]
    assert reopened.load_conversation(conversation)[0].question == "Which terminal waits longest?"


# --- ordering and sequencing -------------------------------------------------------
def test_turns_are_numbered_in_the_order_they_were_asked(store) -> None:
    conversation = store.create_conversation("t")
    for question in ("first", "second", "third"):
        store.append_turn(conversation, question, _answer())

    turns = store.load_conversation(conversation)
    assert [t.seq for t in turns] == [1, 2, 3]
    assert [t.question for t in turns] == ["first", "second", "third"]


def test_sequence_numbers_are_per_conversation_not_global(store) -> None:
    """Otherwise a second chat would start at turn 4, which is visible to anyone reading
    the store and makes `seq` useless for ordering within a conversation."""
    a = store.create_conversation("a")
    b = store.create_conversation("b")
    store.append_turn(a, "q", _answer())
    store.append_turn(a, "q", _answer())
    store.append_turn(b, "q", _answer())

    assert [t.seq for t in store.load_conversation(b)] == [1]


def test_the_sidebar_lists_the_most_recently_used_chat_first(store) -> None:
    """Activity, not creation, decides the order."""
    older = store.create_conversation("older")
    newer = store.create_conversation("newer")
    store.append_turn(older, "q", _answer())  # activity in the OLDER one

    assert [c.id for c in store.list_conversations()] == [older, newer]


def test_an_empty_chat_still_appears_in_the_list(store) -> None:
    """A chat opened and not yet used exists to the user looking at the sidebar. Hiding it
    would make the New chat button appear to do nothing. This is why the listing is a LEFT
    JOIN; an inner join would silently drop it."""
    store.create_conversation("nothing asked yet")
    listed = store.list_conversations()
    assert len(listed) == 1 and listed[0].turn_count == 0


def test_recent_turns_span_conversations_for_the_observability_page(store) -> None:
    a = store.create_conversation("a")
    b = store.create_conversation("b")
    store.append_turn(a, "from a", _answer())
    store.append_turn(b, "from b", _answer())

    assert {t.question for t in store.recent_turns()} == {"from a", "from b"}
    assert store.turn_count() == 2


# --- renaming and deleting ---------------------------------------------------------
def test_renaming_a_chat_does_not_reorder_the_sidebar(store) -> None:
    """A rename is tidying, not activity. Bumping `updated_at` would shuffle the list
    under the user's cursor as a side effect of editing a label."""
    first = store.create_conversation("first")
    second = store.create_conversation("second")
    store.append_turn(first, "q", _answer())
    before = [c.id for c in store.list_conversations()]

    store.rename_conversation(second, "renamed")

    assert [c.id for c in store.list_conversations()] == before
    assert store.get_conversation(second).title == "renamed"


def test_deleting_a_chat_takes_its_turns_with_it(store) -> None:
    """ON DELETE CASCADE. Without it the store accumulates orphaned turns that the
    observability page would still count."""
    conversation = store.create_conversation("t")
    store.append_turn(conversation, "q", _answer())
    store.append_turn(conversation, "q", _answer())
    assert store.turn_count() == 2

    store.delete_conversation(conversation)

    assert store.list_conversations() == []
    assert store.turn_count() == 0, "turns were orphaned rather than deleted"


def test_deleting_one_chat_leaves_the_others_alone(store) -> None:
    keep = store.create_conversation("keep")
    drop = store.create_conversation("drop")
    store.append_turn(keep, "kept", _answer())
    store.append_turn(drop, "dropped", _answer())

    store.delete_conversation(drop)

    assert [c.id for c in store.list_conversations()] == [keep]
    assert [t.question for t in store.recent_turns()] == ["kept"]


def test_asking_for_an_unknown_conversation_returns_nothing_rather_than_raising(store) -> None:
    """The UI can hold a stale id: a chat deleted in one browser tab is still selected in
    another. Returning None lets the caller fall back; raising would show a traceback."""
    assert store.get_conversation("no-such-id") is None
    assert store.load_conversation("no-such-id") == []


def test_appending_to_a_conversation_that_does_not_exist_is_reported_clearly(store) -> None:
    """A foreign-key violation here would surface as an opaque database error. The store
    owns the check so the caller gets something it can act on."""
    with pytest.raises(StoreError) as raised:
        store.append_turn("no-such-id", "q", _answer())
    assert "no such conversation" in str(raised.value)


# --- failure modes -----------------------------------------------------------------
def test_a_missing_database_is_reported_with_the_remedy() -> None:
    """`db/03_app_store.sql` runs only on the container's first boot, so an older data
    volume has no store database at all. That is an operator problem, and the error has to
    name the remedy rather than surfacing a bare connection failure."""
    with pytest.raises(StoreError) as raised:
        Store(dsn=_dsn_for(f"definitely_absent_{uuid.uuid4().hex[:8]}"))

    message = str(raised.value)
    assert "03_app_store.sql" in message, "the error does not name the remedy"
    assert "down -v" in message, "the error does not say how to fix it"


# --- concurrency -------------------------------------------------------------------
def test_many_threads_sharing_one_store_do_not_collide(store) -> None:
    """Thread-safety of a single `Store`, which is what the app actually does.

    Streamlit reruns and the ADR-012 verifier touch this process from more than one
    thread, all through the one cached `Store`. This pins that they serialise cleanly.

    It does NOT test the `FOR UPDATE` row lock, and the previous version of this docstring
    wrongly claimed it did. `_run` holds a Python lock around every operation, so with one
    shared `Store` the database-level race cannot occur whether or not the SQL lock is
    there. Verified: removing `FOR UPDATE` left this passing 5 times out of 5. The test
    below is the one that exercises the lock.
    """
    conversation = store.create_conversation("t")
    errors: list[Exception] = []

    def append() -> None:
        try:
            store.append_turn(conversation, "q", _answer())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=append) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent appends raised: {errors}"
    assert sorted(t.seq for t in store.load_conversation(conversation)) == list(range(1, 13))


def test_independent_connections_cannot_collide_on_a_sequence_number(store) -> None:
    """The `FOR UPDATE` row lock, exercised for real.

    Each thread gets its OWN `Store`, so its own connection and its own Python lock. That
    is what makes the race reachable: under READ COMMITTED, two transactions can both read
    `max(seq)` before either commits, compute the same next value, and one loses to the
    UNIQUE constraint. The row lock is the only thing preventing it.

    A barrier is used rather than hoping the threads overlap, because a race that depends
    on scheduling luck is a test that passes on a fast machine and fails in CI.

    Six writers, not twelve: `app_rw` carries `CONNECTION LIMIT 10` and the fixture's own
    `Store` holds one. Exhausting the limit would fail this test for an unrelated reason.
    """
    conversation = store.create_conversation("t")
    writers = [Store(dsn=store.dsn) for _ in range(6)]
    errors: list[Exception] = []
    ready = threading.Barrier(len(writers))

    def append(writer: Store) -> None:
        try:
            ready.wait(timeout=10)
            writer.append_turn(conversation, "q", _answer())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=append, args=(w,)) for w in writers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"independent writers collided: {errors}"
        seqs = sorted(t.seq for t in store.load_conversation(conversation))
        assert seqs == list(range(1, len(writers) + 1)), f"sequence numbers were {seqs}"
    finally:
        for writer in writers:
            writer.close()
