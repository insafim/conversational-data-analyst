"""Integration tests proving the database-level security boundary is real (ADR-004).

The claim these tests defend is the central one in the whole project:

    Even a fully compromised model cannot write to this database, because the
    restriction is enforced by PostgreSQL's permission system, not by the agent.

That claim is worth nothing as an assertion in a README. Here it is executable.

The important subtlety, and the reason this file exists at all: the read-only *role*
also sets `default_transaction_read_only`, which makes writes fail with "read-only
transaction" — but that setting is USERSET, so a session can simply switch it off.
If it were the only control, the security story would be theatre. These tests therefore
**disable it first** and then attempt the writes, proving the GRANTs are what stop them.

Requires a running, seeded database:  docker compose up -d && python db/seed.py
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def read_write_attempt():
    """A connection as the analyst role with the read-only guard deliberately disabled.

    This simulates the strongest realistic attacker: one who knows about
    `default_transaction_read_only` and has removed it. Whatever still fails, fails
    because of the GRANTs.
    """

    def _run(sql: str) -> str | None:
        with psycopg.connect(settings.analyst_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
                try:
                    cur.execute(sql)
                    return None  # no error: the write was permitted
                except psycopg.Error as exc:
                    return str(exc)

    return _run


def test_the_readonly_guard_is_genuinely_bypassable() -> None:
    """Establishes the premise of every other test in this file.

    If this test ever fails, the others are no longer proving what they claim: they
    would be passing because of the transaction guard rather than the GRANTs.
    """
    with psycopg.connect(settings.analyst_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
            cur.execute("SHOW default_transaction_read_only")
            assert cur.fetchone()[0] == "off", (
                "The read-only guard could not be disabled. The bypass tests below "
                "would then prove nothing about the GRANTs."
            )


@pytest.mark.parametrize(
    "operation,sql",
    [
        ("INSERT", "INSERT INTO terminals (terminal_name, port_name, country, berth_count,"
                   " opened_year) VALUES ('pwned', 'x', 'y', 1, 2000)"),
        ("UPDATE", "UPDATE terminals SET port_name = 'hacked'"),
        ("DELETE", "DELETE FROM port_calls"),
        ("DROP", "DROP TABLE cargo_moves"),
        ("TRUNCATE", "TRUNCATE terminals CASCADE"),
        ("CREATE", "CREATE TABLE evil (id int)"),
        ("CTE DELETE", "WITH d AS (DELETE FROM port_calls RETURNING *) SELECT COUNT(*) FROM d"),
        ("SELECT INTO", "SELECT * INTO evil_copy FROM terminals"),
    ],
)
def test_writes_fail_on_permissions_even_with_the_guard_disabled(
    read_write_attempt, operation: str, sql: str
) -> None:
    """Every write must fail, and must fail on *permissions*, not on the guard."""
    error = read_write_attempt(sql)
    assert error is not None, f"{operation} SUCCEEDED as the read-only role"
    assert "read-only transaction" not in error, (
        f"{operation} was stopped by the bypassable transaction guard rather than by "
        f"the GRANTs. The security boundary is not where it is claimed to be. Got: {error}"
    )
    assert any(
        phrase in error.lower()
        for phrase in ("permission denied", "must be owner")
    ), f"{operation} failed for an unexpected reason: {error}"


def test_self_granting_privileges_does_not_actually_escalate() -> None:
    """A privilege-escalation attempt must not succeed — and "did it error?" is the
    wrong question to ask.

    PostgreSQL does NOT raise when a role issues a GRANT it has no authority for. It
    emits a *warning* ("no privileges were granted") and the statement reports success.
    A security test asserting that GRANT raises therefore fails, and — far worse —
    someone reading that success could conclude the role escalated its privileges.

    The only meaningful assertion is behavioural: attempt the grant, then check whether
    the privilege was actually acquired. It is not.
    """
    with psycopg.connect(settings.analyst_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
            try:
                cur.execute("GRANT INSERT ON terminals TO analyst_ro")
            except psycopg.Error:
                pass  # erroring is also an acceptable outcome; not granting is the requirement

            # The claim under test: no INSERT privilege was actually obtained.
            assert cur.execute(
                "SELECT has_table_privilege('analyst_ro', 'terminals', 'INSERT')"
            ).fetchone()[0] is False, "the read-only role acquired INSERT on terminals"

            with pytest.raises(psycopg.Error, match="permission denied"):
                cur.execute(
                    "INSERT INTO terminals (terminal_name, port_name, country, berth_count,"
                    " opened_year) VALUES ('pwned', 'x', 'y', 1, 2000)"
                )


def test_the_role_holds_select_and_nothing_else() -> None:
    """States the guarantee positively, table by table, rather than only by counter-example."""
    with psycopg.connect(settings.analyst_dsn) as conn:
        with conn.cursor() as cur:
            for table in ("terminals", "vessels", "cranes", "port_calls", "cargo_moves"):
                for privilege, expected in [
                    ("SELECT", True),
                    ("INSERT", False), ("UPDATE", False),
                    ("DELETE", False), ("TRUNCATE", False), ("REFERENCES", False),
                ]:
                    cur.execute(
                        "SELECT has_table_privilege('analyst_ro', %s, %s)", (table, privilege)
                    )
                    assert cur.fetchone()[0] is expected, (
                        f"analyst_ro has unexpected {privilege} on {table}"
                    )


def test_the_role_cannot_read_password_hashes(read_write_attempt) -> None:
    """pg_authid holds credentials and must remain unreachable."""
    error = read_write_attempt("SELECT rolpassword FROM pg_authid")
    assert error is not None and "permission denied" in error.lower()


def test_denial_of_service_primitive_is_revoked(read_write_attempt) -> None:
    """A SELECT-only role cannot corrupt data, but pg_sleep would still hold a
    connection open indefinitely."""
    error = read_write_attempt("SELECT pg_sleep(1)")
    assert error is not None and "permission denied" in error.lower()


def test_legitimate_reads_still_work() -> None:
    """The boundary must not be so tight that the product stops working."""
    with psycopg.connect(settings.analyst_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM terminals")
            assert cur.fetchone()[0] > 0, "database is not seeded; run python db/seed.py"


def test_statement_timeout_is_enforced() -> None:
    """An expensive query must be cut off rather than running indefinitely."""
    with psycopg.connect(settings.analyst_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] != "0", "no statement timeout is configured"
            with pytest.raises(psycopg.errors.QueryCanceled):
                cur.execute(
                    "SELECT COUNT(*) FROM port_calls a, port_calls b, port_calls c, port_calls d"
                )


def test_system_catalogs_are_readable_so_the_validator_must_block_them() -> None:
    """Establishes why the validator's system-catalog rule cannot be dropped.

    The rest of this file shows the GRANTs holding. This shows where they do not. These
    catalogs are world-readable in PostgreSQL, so `analyst_ro` can read them and no
    privilege change fixes that. The validator is the only layer that stops them, which
    is the opposite of the usual argument in this repo and worth pinning down: if this
    test ever starts failing, the database has changed and the validator rule is
    redundant rather than load-bearing.
    """
    readable = []
    for sql in ("SELECT rolname FROM pg_roles LIMIT 1",
                "SELECT datname FROM pg_database LIMIT 1",
                "SELECT version()"):
        with psycopg.connect(settings.analyst_dsn) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql)
                    cur.fetchall()
                    readable.append(sql)
                except psycopg.Error:
                    pass
    assert readable, (
        "no system catalog was readable; the validator's system_table rule may now be "
        "redundant, so re-check the premise before relying on it"
    )


def test_no_business_table_is_hidden_by_the_system_catalog_prefix_rule() -> None:
    """The validator denies any table named `pg_*`, so no real table may be named that.

    PostgreSQL does not reserve the prefix for user tables (`CREATE TABLE pg_mytable`
    succeeds), so this is a convention the validator imposes rather than one the database
    enforces. Asserting it against the live schema turns "no table is called that" from an
    assumption into a checked fact, and fails loudly if someone later adds one and finds
    it silently unqueryable.
    """
    from src.validator import validate_sql

    with psycopg.connect(settings.analyst_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = [row[0] for row in cur.fetchall()]

    assert tables, "no business tables found; is the database seeded?"
    for table in tables:
        assert not table.lower().startswith("pg_"), (
            f"table {table!r} starts with pg_ and is therefore denied by the validator"
        )
        assert validate_sql(f"SELECT * FROM {table}").ok, (
            f"the validator blocks a real business table: {table}"
        )
