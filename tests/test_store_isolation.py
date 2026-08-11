"""The agent cannot reach the conversation store. Proven, not asserted (ADR-014).

Chat history lives in its own database, `ports_app`, and `analyst_ro` holds no CONNECT on
it. So the denial happens before any query exists: the session never opens, and PostgreSQL
has no cross-database queries without FDW to route around it.

The alternative is what makes this worth testing. `db/02_roles.sql` grants `analyst_ro`
SELECT on every table in the analytics database's `public` schema and, via ALTER DEFAULT
PRIVILEGES, on every table added there later. History kept there would be readable by the
agent, and that costs twice:

1. "What have other people asked?" becomes a question the agent answers.
2. The database it queries starts holding **user-supplied text**, which is the
   second-order injection channel `src/prompts.py` documents and
   `tests/test_second_order_injection.py` polices. Today that channel is shut only
   because every row in the analytics database is synthetic data this repo generated.

These tests attack through the agent's OWN path, `run_query` in `src/executor.py`, plus a
raw connection attempt for the cases the validator would reject before the database saw
them. The last test is a control: every denial here would also pass if the agent had
simply lost all access, which is how a test like this passes for the wrong reason.

Requires:  docker compose up -d --wait && python db/seed.py
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings
from src.executor import ExecutionError, run_query
from src.models import AgentResult, Outcome
from src.store import Store, title_for

pytestmark = pytest.mark.integration


@pytest.fixture
def store_with_a_conversation() -> Store:
    """A real conversation in the real store, so the tests attack something that exists.

    A test that fails to read an empty table proves nothing: "no rows" and "no permission"
    look identical from outside if there was never a row to find.
    """
    store = Store()
    conversation = store.create_conversation(title_for("a private question"))
    store.append_turn(
        conversation,
        "SECRETQUESTIONTOKEN which terminal is busiest?",
        AgentResult(question="q", outcome=Outcome.ANSWERED, answer="SECRETANSWERTOKEN"),
    )
    yield store
    store.delete_conversation(conversation)
    store.close()


def _store_database() -> str:
    return next(
        part.split("=", 1)[1]
        for part in settings.store_dsn.split()
        if part.startswith("dbname=")
    )


def test_the_agents_role_cannot_even_connect_to_the_store(store_with_a_conversation) -> None:
    """The central claim, and it is shorter than the alternatives were.

    With the store in a separate database, the denial happens at connection time. There is
    no query to validate, no schema to qualify and no search_path to escape: the agent's
    role holds no CONNECT, so the session never opens.
    """
    analyst_on_store = " ".join(
        [
            *(p for p in settings.analyst_dsn.split() if not p.startswith("dbname=")),
            f"dbname={_store_database()}",
        ]
    )

    with pytest.raises(psycopg.OperationalError) as raised:
        psycopg.connect(analyst_on_store, connect_timeout=10)

    assert "permission denied" in str(raised.value).lower(), (
        f"expected a connection denial, got: {raised.value}"
    )


def test_public_is_not_a_way_in_either() -> None:
    """PostgreSQL grants CONNECT on a new database to PUBLIC, and every login role inherits
    PUBLIC. `db/03_app_store.sql` revokes it, and this is the test that would fail if that
    line were ever removed: creating a separate database is not, by itself, access control.
    """
    with psycopg.connect(settings.admin_dsn, connect_timeout=10) as admin:
        granted = admin.execute(
            "SELECT has_database_privilege('public', %s, 'CONNECT')", (_store_database(),)
        ).fetchone()[0]

    assert granted is False, "PUBLIC can connect to the store database"


def test_the_agent_cannot_reach_the_store_through_its_own_query_path(
    store_with_a_conversation,
) -> None:
    """Through `run_query`, the path model-generated SQL actually takes.

    PostgreSQL has no cross-database queries without FDW, so a fully qualified reference
    cannot resolve. The point is that the failure is structural rather than a string check
    that a cleverer query might slip past.
    """
    database = _store_database()
    for sql in (
        f"SELECT * FROM {database}.public.turn",
        f"SELECT * FROM {database}.public.conversation",
    ):
        with pytest.raises(ExecutionError):
            run_query(sql)


def test_the_stored_text_is_not_reachable_by_any_route_the_agent_has(
    store_with_a_conversation,
) -> None:
    """A sweep of the plausible routes, asserting the token never surfaces. Written as one
    test because the property is about the TOKEN never appearing, not about any single
    query failing."""
    database = _store_database()
    attempts = [
        "SELECT * FROM turn",
        "SELECT * FROM conversation",
        f"SELECT * FROM {database}.public.turn",
        "SELECT * FROM public.turn",
    ]
    for sql in attempts:
        try:
            result = run_query(sql)
        except ExecutionError:
            continue  # denied or unresolvable, which is the expected outcome
        rendered = str(result.rows)
        assert "SECRETQUESTIONTOKEN" not in rendered, f"stored question leaked via: {sql}"
        assert "SECRETANSWERTOKEN" not in rendered, f"stored answer leaked via: {sql}"


def test_the_store_role_cannot_read_the_business_data() -> None:
    """The boundary in the other direction, and it is stronger than it was.

    `app_rw` has no CONNECT on the analytics database at all, so a compromise of the
    application process cannot reach the data the agent reports on, in either direction.
    """
    store_on_analytics = " ".join(
        [
            *(p for p in settings.store_dsn.split() if not p.startswith("dbname=")),
            "dbname=" + settings.analyst_dsn.split("dbname=")[1].split()[0],
        ]
    )

    with pytest.raises(psycopg.OperationalError) as raised:
        psycopg.connect(store_on_analytics, connect_timeout=10)

    assert "permission denied" in str(raised.value).lower()


def test_the_business_tables_are_still_readable_by_the_agent() -> None:
    """The control. Every denial above would also pass if the agent had simply lost all
    access, which is how this kind of test passes for the wrong reason."""
    result = run_query("SELECT terminal_name FROM terminals LIMIT 1")
    assert result.row_count == 1
