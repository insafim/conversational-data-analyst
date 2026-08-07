"""Tests for schema introspection and the SQL it builds (ADR-003).

`_date_coverage` is the one place in this codebase that assembles a query from names read
out of the catalog rather than writing it out in full, because the number of date columns
is not known until runtime. It previously did that with an f-string. These tests pin the
composition, not the connection.

Why the assertions are at composition level rather than end to end: proving the quoting
matters would require a table actually named something hostile, and the test suite cannot
create one. It connects as `analyst_ro`, which holds `SELECT` and nothing else, and a test
that quietly needed DDL rights would undermine the security claim the rest of the suite
exists to make. So the production fragment builder is called directly and the SQL it
returns is rendered and inspected.

These tests call `schema.coverage_fragment` itself rather than rebuilding the same
expression locally. That distinction is the whole value of the file: a copy of the
construction would keep passing if the real one regressed to an f-string, which is exactly
the defect these tests exist to catch.
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings
from src.schema import coverage_fragment, get_schema_context, get_schema_summary

pytestmark = pytest.mark.integration


def _render(table: str, column: str) -> str:
    """Render the production fragment to SQL text for inspection."""
    with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
        return coverage_fragment(table, column).as_string(conn)


def test_identifiers_are_quoted_not_interpolated() -> None:
    """The ordinary case. Identifiers arrive double-quoted, which is what an f-string
    did not do."""
    rendered = _render("port_calls", "arrival_ts")
    assert '"port_calls"' in rendered
    assert '"arrival_ts"' in rendered


def test_a_hostile_identifier_cannot_break_out_of_its_quotes() -> None:
    """The reason the change was worth making. A table named with an embedded quote and a
    statement terminator is rendered as a single quoted identifier with the quote doubled,
    so it stays one name instead of becoming a second statement.

    No such table exists here, and the names really do come from `information_schema`
    rather than from a user. The point is that the safety of this line stops depending on
    that remaining true.
    """
    rendered = _render('x"; DROP TABLE terminals; --', "arrival_ts")
    assert '"x""; DROP TABLE terminals; --"' in rendered
    assert not rendered.rstrip().endswith("--")
    # The payload is inert: one statement, and the DROP sits inside an identifier.
    assert rendered.count("SELECT") == 1


def test_the_label_is_a_literal_not_an_identifier() -> None:
    """The label is data the model reads, so it is a string literal in single quotes. If
    it were composed as an identifier the query would fail at parse time instead."""
    rendered = _render("port_calls", "arrival_ts")
    assert "'port_calls.arrival_ts'" in rendered


def test_schema_context_reports_real_tables_and_date_coverage() -> None:
    """End to end, against the live catalog: the rendered context must carry the tables,
    the join paths, and the data coverage block that lets the model resolve relative dates
    against the data rather than against today (ADR-001)."""
    context = get_schema_context()
    for table in ("terminals", "vessels", "cranes", "port_calls", "cargo_moves"):
        assert table in context
    assert "Foreign keys" in context
    assert "Data coverage" in context
    assert "port_calls.arrival_ts" in context


def test_schema_summary_covers_every_table() -> None:
    summary = dict(get_schema_summary())
    assert set(summary) == {"terminals", "vessels", "cranes", "port_calls", "cargo_moves"}
    assert all(count > 0 for count in summary.values())
