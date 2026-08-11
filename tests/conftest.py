"""Fixtures shared by the store-backed tests.

The throwaway-database fixture started in `tests/test_store.py` and moved here when
`tests/test_conversations.py` needed the same thing. It is shared rather than copied so
that the isolation property it provides is stated once: no test ever writes into the
`ports_app` database the running application uses.

Requires:  docker compose up -d --wait && python db/seed.py
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql as pgsql

from src.config import settings
from src.store import Store


@pytest.fixture
def store_dsn_for() -> Callable[[str], str]:
    """Build the store DSN pointed at a different database.

    Rewriting one token is the whole reason a separate database is the right shape
    (ADR-014): in production the same substitution points at a different host.
    """

    def build(database: str) -> str:
        parts = [p for p in settings.store_dsn.split() if not p.startswith("dbname=")]
        return " ".join([*parts, f"dbname={database}"])

    return build


@pytest.fixture
def store(store_dsn_for: Callable[[str], str]) -> Iterator[Store]:
    """A Store on a throwaway database, dropped when the test ends.

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
        made = Store(dsn=store_dsn_for(name))
        yield made
        made.close()
    finally:
        with psycopg.connect(settings.admin_dsn, autocommit=True) as admin:
            # A lingering session would block the DROP, so any connection left behind by a
            # failing test is terminated rather than waited on. Without this a single bad
            # test hangs the suite instead of failing it.
            #
            # Verified: 2026-08-12, by probe against this project's PostgreSQL 18 rather
            # than from the manual. Holding one open session and dropping from another
            # raises psycopg.errors.ObjectInUse, 'database "..." is being accessed by other
            # users', and the drop succeeds once that session closes. This repo has already
            # paid for it: an exception escaping the store's transaction handler left a
            # connection idle-in-transaction, so the drop blocked and the suite hung rather
            # than failed, which is the expensive kind of failure because nothing points at
            # the cause.
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(pgsql.SQL("DROP DATABASE {}").format(pgsql.Identifier(name)))
