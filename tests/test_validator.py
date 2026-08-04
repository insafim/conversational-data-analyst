"""Tests for the SQL validator — the code-level safety gate (ADR-004).

These are the most important tests in the repo. The validator is the layer that stops a
compromised model before it reaches the database, so every case here is a specific
attack, and each is named after what it would do if it got through.

No database is required: the validator is pure function of the SQL string.
"""

from __future__ import annotations

import pytest

from src.validator import validate_sql


# ---------------------------------------------------------------------------------
# Legitimate queries must pass. A validator that blocks real questions is not "safe",
# it is broken — false positives destroy the product just as surely as false negatives
# destroy the database.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM terminals",
        "SELECT terminal_name FROM terminals WHERE country = 'Netherlands'",
        # The real shape of an answer: multi-table join with aggregation.
        "SELECT t.terminal_name, ROUND(AVG(pc.berth_wait_hours), 2) AS avg_wait "
        "FROM port_calls pc JOIN terminals t ON t.terminal_id = pc.terminal_id "
        "GROUP BY t.terminal_name ORDER BY avg_wait DESC",
        # A read-only CTE is legitimate and must not be blocked just because CTEs can
        # also hide writes.
        "WITH monthly AS (SELECT DATE_TRUNC('month', move_ts) m, SUM(container_count) c "
        "FROM cargo_moves GROUP BY 1) SELECT * FROM monthly ORDER BY m",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 INTERSECT SELECT 1",
        # Comments are stripped by the parser: the statement really is just a SELECT,
        # so allowing this is correct rather than a lapse.
        "SELECT 1 /* ; DROP TABLE terminals */",
        "SELECT 1 -- ; DROP TABLE terminals",
        "SELECT (SELECT COUNT(*) FROM vessels) AS vessel_count",
    ],
)
def test_legitimate_select_is_allowed(sql: str) -> None:
    assert validate_sql(sql).ok, f"legitimate query was blocked: {sql}"


# ---------------------------------------------------------------------------------
# THE case that motivates AST parsing.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql,operation",
    [
        ("WITH d AS (DELETE FROM port_calls RETURNING *) SELECT COUNT(*) FROM d", "DELETE"),
        ("WITH i AS (INSERT INTO terminals VALUES (1) RETURNING *) SELECT * FROM i", "INSERT"),
        ("WITH u AS (UPDATE vessels SET operator = 'x' RETURNING *) SELECT * FROM u", "UPDATE"),
    ],
)
def test_data_modifying_cte_is_blocked(sql: str, operation: str) -> None:
    """A CTE can hide a write inside a statement whose top-level type is SELECT.

    This is the attack that makes AST-walking necessary. `sqlparse.get_type()` reports
    "SELECT" for all three of these, so the obvious implementation of this validator
    would execute them — and they modify or destroy data. Verified empirically against
    sqlglot before the validator was written.
    """
    result = validate_sql(sql)
    assert not result.ok, f"{operation} hidden in a CTE was NOT blocked"
    assert result.violation == "write_operation"


# ---------------------------------------------------------------------------------
# Stacked statements — classic SQL injection shape.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE terminals",
        "SELECT * FROM terminals; DELETE FROM port_calls",
        "SELECT 1; SELECT 2",  # blocked even though both halves are harmless
    ],
)
def test_stacked_statements_are_blocked(sql: str) -> None:
    result = validate_sql(sql)
    assert not result.ok
    assert result.violation == "multiple_statements"


# ---------------------------------------------------------------------------------
# Direct write and DDL operations.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE terminals",
        "DELETE FROM port_calls",
        "UPDATE vessels SET operator = 'x'",
        "INSERT INTO terminals VALUES (1)",
        "TRUNCATE terminals",
        "ALTER TABLE terminals ADD COLUMN x int",
        "CREATE TABLE evil (id int)",
        "GRANT ALL ON terminals TO PUBLIC",
        "COPY terminals FROM '/etc/passwd'",
        # SET is parsed as a Command node; denying Command closes the gap for every
        # statement type sqlglot has no dedicated node for.
        "SET default_transaction_read_only = off",
    ],
)
def test_write_and_ddl_are_blocked(sql: str) -> None:
    assert not validate_sql(sql).ok, f"destructive statement was NOT blocked: {sql}"


def test_select_into_is_blocked() -> None:
    """SELECT ... INTO creates a table: a write wearing a SELECT's clothes."""
    result = validate_sql("SELECT * INTO evil_copy FROM terminals")
    assert not result.ok
    assert result.violation == "select_into"


# ---------------------------------------------------------------------------------
# Structurally valid SELECTs that are still hostile.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql,violation",
    [
        ("SELECT pg_sleep(3600)", "forbidden_function"),
        ("SELECT pg_read_file('/etc/passwd')", "forbidden_function"),
        ("SELECT lo_import('/etc/shadow')", "forbidden_function"),
        ("SELECT * FROM pg_catalog.pg_authid", "system_schema"),
        ("SELECT * FROM information_schema.tables", "system_schema"),
    ],
)
def test_hostile_but_valid_selects_are_blocked(sql: str, violation: str) -> None:
    """These parse as ordinary SELECTs, so structure alone does not stop them.

    pg_sleep is a denial-of-service primitive; pg_read_file is a file read; the catalog
    queries are reconnaissance. PostgreSQL blocks most of these for this role anyway —
    this layer means the user gets a clear refusal instead of a raw permission error.
    """
    result = validate_sql(sql)
    assert not result.ok, f"hostile query was NOT blocked: {sql}"
    assert result.violation == violation


# ---------------------------------------------------------------------------------
# Fail-closed behaviour.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", ["", "   ", "\n"])
def test_empty_input_is_rejected(sql: str) -> None:
    assert validate_sql(sql).violation == "empty"


def test_unparseable_sql_is_rejected_not_passed_through() -> None:
    """If we cannot parse it, we cannot claim it is safe. Fail closed, always."""
    result = validate_sql("SELECT FROM WHERE ((( ")
    assert not result.ok
    assert result.violation == "unparseable"


def test_rejection_always_carries_a_user_safe_reason() -> None:
    """Every refusal is shown to a user, so every refusal needs an explanation."""
    for sql in ["DROP TABLE t", "SELECT 1; SELECT 2", "SELECT pg_sleep(1)", ""]:
        result = validate_sql(sql)
        assert not result.ok
        assert result.reason and len(result.reason) > 10, f"unhelpful reason for: {sql}"


# ---------------------------------------------------------------------------------
# Extended attack surface. These were probed against the validator directly rather
# than assumed; each line is a construct that is either structurally a SELECT or that
# sqlglot handles unusually, so none of them are obvious from reading the code.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql,why",
    [
        ("DO $$ BEGIN PERFORM 1; END $$", "anonymous code block can execute arbitrary PL/pgSQL"),
        ("CALL some_procedure()", "procedures can modify data"),
        ("REFRESH MATERIALIZED VIEW mv", "writes to a view's backing store"),
        ("LOCK TABLE terminals IN ACCESS EXCLUSIVE MODE", "blocks every other session"),
        ("NOTIFY channel, 'msg'", "side effect outside the query"),
        ("VACUUM FULL terminals", "rewrites the table and takes an exclusive lock"),
        ("CREATE TEMP TABLE t AS SELECT 1", "TEMP still creates an object"),
        ("BEGIN; DROP TABLE terminals; COMMIT", "transaction wrapper around a drop"),
        # EXPLAIN ANALYZE genuinely EXECUTES the statement it is given, so it is a
        # complete bypass of any check that only inspects the outer statement type.
        ("EXPLAIN ANALYZE DELETE FROM port_calls", "EXPLAIN ANALYZE executes its argument"),
        ("EXPLAIN ANALYZE SELECT 1", "denied as a class; EXPLAIN is not a business question"),
        # Locking clauses are structurally SELECTs but take row locks.
        ("SELECT * FROM terminals FOR UPDATE", "takes row locks"),
        ("SELECT * FROM terminals FOR SHARE", "takes row locks"),
    ],
)
def test_extended_attack_surface_is_blocked(sql: str, why: str) -> None:
    assert not validate_sql(sql).ok, f"NOT blocked ({why}): {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # Case variation must not defeat the function and schema deny-lists.
        "SELECT PG_SLEEP(10)",
        "SELECT Pg_Read_File('/etc/passwd')",
        "SELECT * FROM PG_CATALOG.PG_AUTHID",
        # Quoted identifiers are a classic way past naive string matching.
        'SELECT * FROM "pg_catalog"."pg_authid"',
        # Hostile branch hidden in a UNION, or nested a level deeper than expected.
        "SELECT 1 UNION SELECT count(*) FROM pg_catalog.pg_authid",
        "SELECT (SELECT count(*) FROM pg_catalog.pg_authid) AS x",
        "SELECT * FROM terminals WHERE terminal_id IN (SELECT 1 FROM pg_authid)",
        # DML nested two CTE levels down, not just one.
        "WITH a AS (WITH b AS (DELETE FROM port_calls RETURNING *) SELECT * FROM b) "
        "SELECT * FROM a",
    ],
)
def test_evasion_techniques_are_blocked(sql: str) -> None:
    """Case changes, quoting, nesting and set operations must not provide a way past."""
    assert not validate_sql(sql).ok, f"evasion succeeded: {sql}"


@pytest.mark.parametrize(
    "sql,why",
    [
        # Sequence functions mutate state from inside a SELECT — writes wearing a
        # SELECT's clothes. They parse as ordinary queries and contain no write node.
        ("SELECT nextval('terminals_terminal_id_seq')", "advances a sequence"),
        ("SELECT setval('terminals_terminal_id_seq', 1)", "resets a sequence"),
        ("SELECT currval('terminals_terminal_id_seq')", "sequence introspection"),
        ("SELECT lastval()", "sequence introspection"),
        # Leaks server configuration. This one executed successfully before being denied.
        ("SELECT current_setting('is_superuser')", "server config disclosure"),
        # Nested inside a subquery, to confirm the check walks the whole tree.
        ("SELECT (SELECT nextval('terminals_terminal_id_seq')) AS x", "nested mutation"),
    ],
)
def test_state_mutating_and_disclosing_functions_are_blocked(sql: str, why: str) -> None:
    """A SELECT that changes the database, or reveals server internals.

    PostgreSQL blocks the sequence cases for this role anyway — `analyst_ro` holds SELECT
    on tables and was never granted USAGE on sequences — so layer 1 held throughout. They
    are denied here so the validator's own guarantee is true as stated, rather than true
    only because a lower layer happened to catch it.
    """
    result = validate_sql(sql)
    assert not result.ok, f"NOT blocked ({why}): {sql}"
    assert result.violation == "forbidden_function"
