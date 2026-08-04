"""Tests for rule-based chart selection (ADR-005).

Chart choice is code precisely so it can be tested. These assert the rule that fired,
not just the chart kind, so a test failing tells you *which* rule broke.
"""

from __future__ import annotations

from decimal import Decimal

from src.charts import MAX_BAR_CATEGORIES, pick_chart, to_dataframe
from src.models import ChartKind, QueryResult


def result(columns, types, rows) -> QueryResult:
    return QueryResult(
        columns=columns, column_types=types, rows=rows,
        row_count=len(rows), elapsed_s=0.01,
    )


def test_no_rows_produces_no_chart() -> None:
    spec = pick_chart(result(["terminal_name"], ["text"], []))
    assert spec.kind == ChartKind.NONE


def test_single_number_produces_a_metric() -> None:
    spec = pick_chart(result(["port_call_count"], ["int8"], [[1500]]))
    assert spec.kind == ChartKind.METRIC


def test_date_column_produces_a_line_chart() -> None:
    spec = pick_chart(result(
        ["month", "total_containers"], ["date", "int8"],
        [["2025-01-01", 15421], ["2025-02-01", 10203], ["2025-03-01", 14814]],
    ))
    assert spec.kind == ChartKind.LINE
    assert spec.x == "month"
    assert spec.y == ["total_containers"]


def test_iso_text_month_is_treated_as_temporal() -> None:
    """The `to_char(ts,'YYYY-MM')` case.

    This returns TEXT, so a purely type-driven rule would call it categorical and draw
    bars. That was found by running the query against the real database, and this test
    is the regression guard for it. See ADR-005.
    """
    spec = pick_chart(result(
        ["month", "total_containers"], ["text", "int8"],
        [["2025-01", 15421], ["2025-02", 10203], ["2025-03", 14814]],
    ))
    assert spec.kind == ChartKind.LINE, "ISO-shaped text month was not recognised as a time axis"


def test_ordinary_text_is_not_mistaken_for_a_date() -> None:
    """The temporal fallback must be narrow. Terminal names are not dates."""
    spec = pick_chart(result(
        ["terminal_name", "avg_wait"], ["text", "numeric"],
        [["Jebel Ali Terminal 2", Decimal("17.46")], ["Felixstowe South", Decimal("5.94")]],
    ))
    assert spec.kind == ChartKind.BAR


def test_category_with_measure_produces_a_bar_chart() -> None:
    spec = pick_chart(result(
        ["operator", "vessel_count"], ["text", "int8"],
        [["Meridian Lines", 7], ["Halcyon Freight", 6], ["Orion Sealift", 6]],
    ))
    assert spec.kind == ChartKind.BAR
    assert spec.x == "operator"


def test_too_many_categories_falls_back_to_a_table() -> None:
    """A bar chart of hundreds of categories is an unreadable table with extra steps."""
    rows = [[f"vessel {i}", i] for i in range(MAX_BAR_CATEGORIES + 5)]
    spec = pick_chart(result(["vessel_name", "calls"], ["text", "int8"], rows))
    assert spec.kind == ChartKind.TABLE
    assert "distinct values" in spec.reason


def test_exactly_at_the_category_limit_still_charts() -> None:
    """Boundary check: the limit is inclusive."""
    rows = [[f"terminal {i}", i] for i in range(MAX_BAR_CATEGORIES)]
    spec = pick_chart(result(["terminal_name", "calls"], ["text", "int8"], rows))
    assert spec.kind == ChartKind.BAR


def test_two_measures_produce_a_scatter() -> None:
    spec = pick_chart(result(
        ["capacity_teu", "avg_wait"], ["int8", "numeric"],
        [[14000, Decimal("4.2")], [18000, Decimal("6.1")]],
    ))
    assert spec.kind == ChartKind.SCATTER


def test_wide_result_falls_back_to_a_table() -> None:
    spec = pick_chart(result(
        ["vessel_name", "operator", "flag_country"], ["text", "text", "text"],
        [["MV Aurora", "Meridian Lines", "Panama"]],
    ))
    assert spec.kind == ChartKind.TABLE


def test_every_spec_explains_which_rule_fired() -> None:
    """The reason is shown in the UI, so it must always be populated."""
    cases = [
        result(["a"], ["text"], []),
        result(["n"], ["int8"], [[1]]),
        result(["m", "v"], ["date", "int8"], [["2025-01-01", 1]]),
        result(["c", "v"], ["text", "int8"], [["x", 1]]),
    ]
    for case in cases:
        assert len(pick_chart(case).reason) > 15


def test_decimal_columns_become_floats_for_plotting() -> None:
    """PostgreSQL returns numeric as Decimal, which pandas stores as an opaque object
    column that charts silently refuse to plot. Aggregates are numeric more often than
    not, so without this conversion almost every chart would render empty."""
    frame = to_dataframe(result(
        ["terminal_name", "avg_wait"], ["text", "numeric"],
        [["Jebel Ali Terminal 2", Decimal("17.46")]],
    ))
    assert frame["avg_wait"].dtype.kind == "f"
    assert frame["avg_wait"].iloc[0] == 17.46


def test_descriptive_companion_column_still_charts() -> None:
    """Models add descriptive columns unasked: `terminal_name, port_name, avg_wait`.

    A strict "exactly one categorical" rule sends that to a table where a bar chart is
    plainly right. Observed on the first live query. Tolerated only because the first
    column uniquely labels each row.
    """
    spec = pick_chart(result(
        ["terminal_name", "port_name", "avg_wait"], ["text", "text", "numeric"],
        [["Jebel Ali Terminal 2", "Jebel Ali", Decimal("17.46")],
         ["Felixstowe South", "Felixstowe", Decimal("5.94")]],
    ))
    assert spec.kind == ChartKind.BAR
    assert spec.x == "terminal_name"


def test_genuine_two_dimensional_breakdown_stays_a_table() -> None:
    """The relaxation must not collapse a real dimension.

    Here terminal_name repeats across move types, so a single-axis bar chart would
    silently hide the second dimension. A table is the honest rendering.
    """
    spec = pick_chart(result(
        ["terminal_name", "move_type", "containers"], ["text", "text", "int8"],
        [["Rotterdam Delta Terminal", "load", 100],
         ["Rotterdam Delta Terminal", "discharge", 120],
         ["Felixstowe South", "load", 90],
         ["Felixstowe South", "discharge", 80]],
    ))
    assert spec.kind == ChartKind.TABLE
