"""Integration tests for query execution and its runtime limits (ADR-004).

Requires a running, seeded database:  docker compose up -d && python db/seed.py

A note on what these can and cannot prove. The executor was changed from wrapping the
validated query (`SELECT * FROM (...) AS _capped LIMIT cap+1`) to executing it verbatim
through a server-side cursor and calling `fetchmany(cap + 1)`. No test here distinguishes
those two implementations, and none can, because they are behaviourally identical: both
obtain at most `cap + 1` rows and both then slice to `cap`. Even the sharpest candidate
case, a query carrying its own `LIMIT 3` run with `row_cap=1`, returns one row and
`truncated=True` under either version.

That equivalence is the point rather than a gap. The change was made so that this module
composes no SQL around model output, which is an argument about what the code can be shown
not to do, not about what it returns. The behaviour these tests pin is the contract that had
to survive it.
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


def test_exactly_cap_rows_is_not_reported_as_truncated() -> None:
    """The boundary the `> cap` comparison turns on. Ten rows against a cap of ten means
    `fetchmany(11)` yields 10, which is not more than 10. An off-by-one here would tell a
    user their answer was cut short when it was complete.

    The row count comes from `generate_series` rather than from a table, so this asserts a
    property of the executor rather than a fact about the seed. Re-seeding cannot make it
    fail for an unrelated reason.
    """
    result = run_query("SELECT * FROM generate_series(1, 10)", row_cap=10)
    assert result.row_count == 10
    assert result.truncated is False


def test_one_row_over_the_cap_is_reported_as_truncated() -> None:
    """The other side of the same boundary, so the pair pins the comparison rather than
    just one of its outcomes."""
    result = run_query("SELECT * FROM generate_series(1, 11)", row_cap=10)
    assert result.row_count == 10
    assert result.truncated is True


def test_cap_does_not_disturb_the_query_s_own_ordering_and_limit() -> None:
    """The cap is how many rows are fetched, not SQL this module writes. A query that
    already ends in ORDER BY ... LIMIT keeps its own meaning."""
    result = run_query(
        "SELECT vessel_name, capacity_teu FROM vessels ORDER BY capacity_teu DESC LIMIT 3"
    )
    assert result.row_count == 3
    capacities = [row[1] for row in result.rows]
    assert capacities == sorted(capacities, reverse=True)


def test_trailing_semicolon_is_handled() -> None:
    """Models emit trailing semicolons, which are illegal inside DECLARE."""
    assert run_query("SELECT COUNT(*) FROM terminals;").row_count == 1


def test_percent_in_a_like_pattern_is_not_treated_as_a_placeholder() -> None:
    """`%` is psycopg's placeholder marker. No parameters are passed, so psycopg does no
    client-side substitution and the character reaches PostgreSQL untouched."""
    result = run_query("SELECT terminal_name FROM terminals WHERE terminal_name LIKE '%Terminal%'")
    assert result.row_count > 0
    assert all("Terminal" in row[0] for row in result.rows)


def test_percent_as_modulo_is_not_treated_as_a_placeholder() -> None:
    """The same character in arithmetic position. Asserted as a property rather than a
    row count, because which ids satisfy the modulo is a fact about the seed, not about
    the executor."""
    result = run_query("SELECT port_call_id FROM port_calls WHERE port_call_id % 120 = 57")
    assert result.row_count > 0
    assert all(row[0] % 120 == 57 for row in result.rows)


def test_unbounded_generating_query_still_hits_the_timeout() -> None:
    """`generate_series` is deliberately allowed by the validator, because enumerating
    every function that can burn CPU is a losing game; the statement timeout is what
    bounds it (ADR-004). Executing through a server-side cursor does not weaken that."""
    with pytest.raises(ExecutionError) as exc:
        run_query("SELECT * FROM generate_series(1, 1000000000)", row_cap=5)
    assert "timeout" in str(exc.value).lower()


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
