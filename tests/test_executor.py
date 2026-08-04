"""Integration tests for query execution and its runtime limits (ADR-004).

Requires a running, seeded database:  docker compose up -d && python db/seed.py
"""

from __future__ import annotations

import pytest

from src.executor import ExecutionError, run_query

pytestmark = pytest.mark.integration


def test_returns_rows_columns_and_types() -> None:
    result = run_query("SELECT terminal_name, berth_count FROM terminals ORDER BY terminal_name")
    assert result.row_count == 6
    assert result.columns == ["terminal_name", "berth_count"]
    # Types come from the database, and chart selection depends on them (ADR-005).
    assert result.column_types[0] == "text"
    assert result.column_types[1] in ("int2", "int4", "int8")
    assert result.elapsed_s > 0


def test_row_cap_truncates_and_flags_it() -> None:
    """Bounds memory in this process for a query that is cheap to run but returns a lot."""
    result = run_query("SELECT port_call_id FROM port_calls", row_cap=10)
    assert result.row_count == 10
    assert result.truncated is True


def test_row_cap_does_not_flag_when_everything_fits() -> None:
    result = run_query("SELECT terminal_name FROM terminals", row_cap=100)
    assert result.truncated is False


def test_row_cap_wrapping_preserves_inner_ordering_and_limit() -> None:
    """The cap is applied by wrapping the query, not by appending LIMIT — appending
    would corrupt a query that already ends in LIMIT or ORDER BY."""
    result = run_query(
        "SELECT vessel_name, capacity_teu FROM vessels ORDER BY capacity_teu DESC LIMIT 3"
    )
    assert result.row_count == 3
    capacities = [row[1] for row in result.rows]
    assert capacities == sorted(capacities, reverse=True)


def test_trailing_semicolon_is_handled() -> None:
    """Models emit trailing semicolons; wrapping such SQL naively is a syntax error."""
    assert run_query("SELECT COUNT(*) FROM terminals;").row_count == 1


def test_database_error_is_raised_with_its_message() -> None:
    """The PostgreSQL message is what makes the single retry useful (ADR-002)."""
    with pytest.raises(ExecutionError) as exc:
        run_query("SELECT * FROM no_such_table")
    assert "no_such_table" in str(exc.value)


def test_statement_timeout_bounds_an_expensive_query() -> None:
    with pytest.raises(ExecutionError) as exc:
        run_query("SELECT COUNT(*) FROM port_calls a, port_calls b, port_calls c, port_calls d")
    assert "timeout" in str(exc.value).lower()


def test_empty_result_is_returned_not_raised() -> None:
    """No rows is a valid answer, not an error; the summariser depends on this."""
    result = run_query("SELECT terminal_name FROM terminals WHERE country = 'Atlantis'")
    assert result.row_count == 0
    assert result.rows == []
