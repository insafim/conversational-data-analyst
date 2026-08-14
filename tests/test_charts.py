"""Tests for rule-based chart selection (ADR-005).

Chart choice is code precisely so it can be tested. These assert the rule that fired,
not just the chart kind, so a test failing tells you *which* rule broke.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.charts import (
    MAX_BAR_CATEGORIES,
    format_metric,
    metric_fields,
    pick_chart,
    to_dataframe,
)
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
    # The measure matters as much as the label. Asserting only `kind` and `x` left
    # `y=roles.numeric` free to be emptied or swapped for the label column, which draws a
    # bar chart of nothing while the whole suite stays green.
    assert spec.y == ["vessel_count"]


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
    # Both axes, for the reason given on the bar test. A scatter carries no label column,
    # so reversing x and y is silent: the chart still draws, against the wrong axes.
    assert spec.x == "capacity_teu"
    assert spec.y == ["avg_wait"]


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


# ---------------------------------------------------------------------------------
# Single-row results. A superlative question ("which terminal waits longest?") returns
# one row holding a label and a measure. That shape used to reach the bar rule and draw
# a single bar stretched across the full width of the container — the answer rendered as
# a wall. It reaches the first example button in the sidebar, so it was the most likely
# chart to appear on camera. No count is stated here on purpose: the gold set grows, and
# a number repeated in a comment is a third place for it to go stale. CHARTS.md section 8
# carries the current figure, and section 11 the command that reproduces it.
# ---------------------------------------------------------------------------------


def test_superlative_answer_is_a_labelled_metric_not_one_bar() -> None:
    """The regression guard. This is eval q01, the first example in the sidebar."""
    spec = pick_chart(result(
        ["terminal_name", "avg_berth_wait_hours"], ["text", "numeric"],
        [["Jebel Ali Terminal 2", Decimal("17.46")]],
    ))
    assert spec.kind == ChartKind.METRIC, "a one-row answer must never render as one bar"
    assert spec.x == "terminal_name", "the entity must be carried through as the label"
    assert spec.y == ["avg_berth_wait_hours"]


def test_metric_names_the_measure_not_the_first_column() -> None:
    """`y` must identify the measure by name.

    The UI reads the value by that name. Falling back to positional access puts the
    label string in the value slot, which raises when formatted as a number rather than
    rendering something merely ugly.
    """
    spec = pick_chart(result(
        ["vessel_name", "port_calls"], ["text", "int8"], [["MV Kestrel", 41]],
    ))
    assert spec.y == ["port_calls"]
    assert spec.x == "vessel_name"


def test_bare_single_number_keeps_its_unlabelled_metric() -> None:
    """The pre-existing single-column case must not regress: no label to carry."""
    spec = pick_chart(result(["port_call_count"], ["int8"], [[1500]]))
    assert spec.kind == ChartKind.METRIC
    assert spec.x is None


def test_single_row_with_extra_columns_is_a_table_not_one_bar() -> None:
    """Beyond a label and a measure there is nothing a metric can show, so the fallback
    must be a table. It must not fall through to the bar rule and draw one bar again."""
    spec = pick_chart(result(
        ["terminal_name", "port_name", "country", "avg_wait"],
        ["text", "text", "text", "numeric"],
        [["Jebel Ali Terminal 2", "Jebel Ali", "United Arab Emirates", Decimal("17.46")]],
    ))
    assert spec.kind == ChartKind.TABLE


def test_two_categories_still_render_as_bars() -> None:
    """The fix must not overreach. Two bars is a legible comparison and stays a chart;
    this is eval q12, loaded versus discharged."""
    spec = pick_chart(result(
        ["move_type", "total_containers"], ["text", "int8"],
        [["load", 120544], ["discharge", 118555]],
    ))
    assert spec.kind == ChartKind.BAR


def test_single_point_time_series_is_not_a_line() -> None:
    """A line through one point shows no trend. One month of data is a headline figure.

    Note this case is claimed by Rule 2 before Rule 3 is reached, so it does NOT guard
    Rule 3's row guard. `test_rule_3_row_guard_is_reachable` below does that.
    """
    spec = pick_chart(result(
        ["month", "total_containers"], ["date", "int8"], [["2025-01-01", 15421]],
    ))
    assert spec.kind == ChartKind.METRIC
    assert spec.x == "month"


def test_rule_3_row_guard_is_reachable() -> None:
    """Guard Rule 3's `row_count > 1` clause with a shape Rule 2 cannot intercept.

    Found by audit: the test above was written to protect this clause but never reaches
    it, because one row with one measure and two columns matches Rule 2 first. Deleting
    `and result.row_count > 1` left the whole chart suite green.

    Two measures defeat Rule 2's `len(numeric) == 1`, and three columns defeat its
    `<= 2`, so this input arrives at Rule 3 with a temporal column and a single row.
    """
    spec = pick_chart(result(
        ["month", "loaded", "discharged"], ["date", "int8", "int8"],
        [["2025-01-01", 120, 140]],
    ))
    assert spec.kind is not ChartKind.LINE, "a line through a single point is not a trend"
    assert spec.kind == ChartKind.TABLE


def test_single_row_of_two_measures_is_not_a_one_point_scatter() -> None:
    """Rule 5 had no row guard while Rules 2, 3 and 4 all had one.

    A scatter plot of one point draws no relationship. This is the shape a superlative
    takes whenever its label column is numeric — grouped by `year` or `crane_id` — since
    `classify_columns` reads a numeric ID as a measure rather than a label. Found by
    audit, not by rendering the gold set, which contains no such question.
    """
    spec = pick_chart(result(
        ["capacity_teu", "avg_berth_wait_hours"], ["int8", "numeric"],
        [[15000, Decimal("17.46")]],
    ))
    assert spec.kind is not ChartKind.SCATTER
    assert spec.kind == ChartKind.TABLE


# ---------------------------------------------------------------------------------
# What the metric card actually displays. This lived inline in `app.py` until the
# rule change, where nothing but running the app by hand exercised it — reverting it
# to positional access would have passed the entire suite and then crashed on the
# first superlative question. It is here so a test can reach it.
# ---------------------------------------------------------------------------------


def test_metric_reads_the_measure_by_name_not_by_position() -> None:
    """The regression guard for the crash.

    Column 0 of a superlative result is the label, a string. Positional access puts that
    string in the value slot, where number formatting raises `ValueError`. Asserting the
    value is the number, not merely that something rendered, is what makes this test able
    to fail.
    """
    r = result(["terminal_name", "avg_berth_wait_hours"], ["text", "numeric"],
               [["Jebel Ali Terminal 2", Decimal("17.46")]])
    fields = metric_fields(r, pick_chart(r))
    assert fields.value == "17.46", "the measure, not the label, belongs in the value slot"
    assert fields.label == "Jebel Ali Terminal 2"
    assert fields.help == "avg_berth_wait_hours", "the unit must appear somewhere on screen"


def test_metric_without_a_label_names_the_measure_instead() -> None:
    r = result(["port_call_count"], ["int8"], [[1500]])
    fields = metric_fields(r, pick_chart(r))
    assert fields.label == "port_call_count"
    assert fields.value == "1,500"
    assert fields.help is None


def test_measure_is_found_even_when_it_is_not_the_last_column() -> None:
    """Guards against a positional shortcut that happens to work on the common shape.
    Here the measure comes first, so 'take the last column' would also be wrong."""
    r = result(["avg_wait", "terminal_name"], ["numeric", "text"],
               [[Decimal("17.46"), "Jebel Ali Terminal 2"]])
    fields = metric_fields(r, pick_chart(r))
    assert fields.value == "17.46"
    assert fields.label == "Jebel Ali Terminal 2"


def test_counts_keep_separators_and_averages_keep_two_decimals() -> None:
    assert format_metric(239099) == "239,099"
    assert format_metric(Decimal("17.4599999")) == "17.46"
    assert format_metric(17.4599999) == "17.46"


def test_numpy_integers_are_not_dropped_to_a_bare_string() -> None:
    """`isinstance(numpy.int64(1), int)` is False.

    A plain `int` check therefore silently loses the thousands separators on exactly the
    COUNT and SUM aggregates this formatting exists for, and does it for any caller that
    routes a value through pandas. Found by audit, confirmed by running it.
    """
    numpy = pytest.importorskip("numpy")
    assert format_metric(numpy.int64(239099)) == "239,099"
    assert format_metric(numpy.float64(17.4599999)) == "17.46"


def test_booleans_are_not_formatted_as_numbers() -> None:
    """`isinstance(True, numbers.Integral)` is True, so a boolean would render as "1"
    without the explicit guard."""
    assert format_metric(True) == "True"


def test_a_null_measure_reads_as_missing_data_not_as_a_crash() -> None:
    """A null aggregate is a real outcome: AVG over a terminal whose calls were all
    cancelled returns NULL. Rendering that as "None" on a headline card looks like a
    failure, so it reads "n/a" instead. Threaded through `metric_fields` as well as the
    formatter, because the audit found the formatter tested in isolation only."""
    assert format_metric(None) == "n/a"

    r = result(["terminal_name", "avg_berth_wait_hours"], ["text", "numeric"],
               [["Colombo East Container Terminal", None]])
    fields = metric_fields(r, pick_chart(r))
    assert fields.value == "n/a"
    assert fields.label == "Colombo East Container Terminal"


def test_non_numeric_values_fall_through_unformatted() -> None:
    """The fallback branch, previously uncovered. Nothing routes a string or a date here
    today, but the function is public and returning something number-shaped would be
    worse than passing the value through."""
    assert format_metric("Rotterdam") == "Rotterdam"
    assert format_metric(date(2025, 3, 1)) == "2025-03-01"
