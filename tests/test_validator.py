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
