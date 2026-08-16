"""Rule-based chart selection. No LLM call (ADR-005).

Chart choice is a function of the *shape* of the result set, not of the language in the
question, so it belongs in code where it is deterministic, free, and unit-testable.

## The one deliberate exception to type-based classification

ADR-005 says columns are classified by the type PostgreSQL declares, not by inspecting
values. There is one documented exception, and it is load-bearing rather than a cheat.

The most common way to group by month in PostgreSQL is `to_char(ts, 'YYYY-MM')`, which
returns **text**. A purely type-driven rule therefore classifies the single most common
time-series result as categorical and renders a bar chart where a line chart is correct.
This was found by running the real query against the real database, not predicted.

Two fixes are applied together:

1. The SQL prompt asks the model to prefer `date_trunc(...)::date` for time grouping, so
   the column arrives genuinely typed. This is the primary fix.
2. This module falls back to recognising ISO-8601-shaped text (`2025`, `2025-03`,
   `2025-03-01`) as temporal. This is the safety net for when the model uses `to_char`
   anyway, which it sometimes will.

The fallback is a narrow, anchored pattern over the column's values — not a guess based
on the column's *name*, which would be the fragile version of this idea.
"""

from __future__ import annotations

import numbers
import re
from decimal import Decimal
from typing import NamedTuple

import pandas as pd

from .models import ChartKind, ChartSpec, QueryResult

# Maximum distinct categories worth drawing as bars. Beyond this a bar chart is an
# unreadable table with extra steps, so the honest output is a table. Judgement call,
# not a derived constant (ADR-005).
MAX_BAR_CATEGORIES = 12

# Maximum lines worth drawing on one time axis. Unlike the bar limit above this one is
# DERIVED, and the two must not be reconciled: 12 is a judgement about how many labels an
# axis can carry, 10 is the length of Streamlit's default categorical palette, whose own
# description states that colours "repeat cyclically if there are more categories than
# colors". At eleven series two lines are drawn in the same colour and the legend stops
# being a key, which is a failure of the encoding rather than a matter of taste.
# Source: streamlit/config.py, `theme.chartCategoricalColors` - Verified: 2026-08-16 by
# reading the installed 1.61.1 package, which lists ten light and ten dark defaults.
#
# It is the DEFAULT palette. A `.streamlit/config.toml` setting a longer one would move
# the true limit, and this module deliberately does not read UI config: a rule that did
# would stop being a function of the result shape alone (ADR-005).
MAX_LINE_SERIES = 10

_TEMPORAL_TYPES = frozenset({
    "date", "timestamp", "timestamptz", "time", "timetz",
})
_NUMERIC_TYPES = frozenset({
    "int2", "int4", "int8", "float4", "float8", "numeric", "money",
})

# Anchored ISO-8601 prefixes: a full date, a month, or a bare year.
_ISO_DATE_LIKE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def to_dataframe(result: QueryResult) -> pd.DataFrame:
    """Build a DataFrame from a QueryResult.

    `Decimal` is converted to float because PostgreSQL returns `numeric` as `Decimal`,
    which pandas stores as an opaque object column — charts then silently refuse to plot
    it. Aggregates are `numeric` far more often than not, so without this almost every
    chart in the app would be empty.
    """
    frame = pd.DataFrame(result.rows, columns=result.columns)
    for column in frame.columns:
        if frame[column].map(lambda v: isinstance(v, Decimal)).any():
            frame[column] = frame[column].map(
                lambda v: float(v) if isinstance(v, Decimal) else v
            )
    return frame


def _looks_temporal(values: list) -> bool:
    """True if every non-null value is an ISO-8601-shaped string. See module docstring."""
    seen = [v for v in values if v is not None]
    if not seen:
        return False
    return all(isinstance(v, str) and _ISO_DATE_LIKE.match(v) for v in seen)


class ColumnRoles(NamedTuple):
    """Columns grouped by the role they can play in a chart.

    Separating classification from rule selection keeps each rule a statement about
    *shape* — "a time axis and a measure is a line chart" — rather than a mixture of
    type-sniffing and charting policy.
    """

    temporal: list[str]
    numeric: list[str]
    categorical: list[str]


def classify_columns(result: QueryResult) -> ColumnRoles:
    """Assign each column a charting role from its declared PostgreSQL type.

    Types come from the database rather than from inspecting values, with the single
    documented exception of ISO-shaped text (see the module docstring). A column that is
    neither temporal nor numeric is categorical by default, which is the safe fallback:
    an unknown type becomes a label rather than a measure, so it can never be silently
    plotted as a quantity.
    """
    temporal: list[str] = []
    numeric: list[str] = []
    categorical: list[str] = []

    for index, name in enumerate(result.columns):
        col_type = result.column_types[index] if index < len(result.column_types) else "unknown"
        values = [row[index] for row in result.rows]
        if col_type in _TEMPORAL_TYPES or _looks_temporal(values):
            temporal.append(name)
        elif col_type in _NUMERIC_TYPES:
            numeric.append(name)
        else:
            categorical.append(name)

    return ColumnRoles(temporal=temporal, numeric=numeric, categorical=categorical)


def _line_or_table(result: QueryResult, roles: ColumnRoles) -> ChartSpec:
    """Rule 3: a time axis and a measure, split into one line per category where the rows
    are a breakdown, or a table when no honest line can be drawn.

    The defect this exists for was seen in the running app, not in the gold set. A
    follow-up question returned `month, operator, total_containers`, 33 rows of twelve
    months by three operators, and the rule as written kept `x` and every measure and
    dropped the category. `st.line_chart` then joined all 33 rows in row order, so inside
    each month the line jumped between the three operators' values and the chart read as a
    sawtooth. Rule 4 has refused that shape since the first live queries; this rule did
    not, which `docs/CHARTS.md` section 9 recorded as an accepted limitation.

    The uniqueness test below is on the column that becomes **x**, exactly as
    `_bar_or_table` tests the column that becomes x there. That is the whole rule, not an
    analogy to it: the rendered defect is literally two y values at one x, so asking
    whether x repeats asks whether the defect is present. Testing the *category* for
    uniqueness instead is the plausible wrong edit, and it silently breaks `q60`, whose
    six terminal names are unique across six rows and would therefore be read as
    descriptive companions.
    """
    x = roles.temporal[0]
    unchanged = ChartSpec(
        kind=ChartKind.LINE, x=x, y=roles.numeric,
        reason=f"'{x}' is a time axis, so the trend renders as a line chart.",
    )

    # Nothing to split by.
    if not roles.categorical:
        return unchanged

    # One row per period, so the categorical columns describe the row rather than divide
    # it: `q26` answers with the winning terminal beside each quarter. Connecting the
    # points in x order is monotonic in x and the chart is a legitimate reading of the
    # rows, which is what it has always drawn. First among the categorical branches for
    # the same reason the companion relaxation comes first in `_bar_or_table`, and the
    # reason string is deliberately left unchanged: that helper does not announce its
    # relaxation either.
    x_values = [row[result.columns.index(x)] for row in result.rows]
    if len(set(x_values)) == result.row_count:
        return unchanged

    # From here the rows are a genuine breakdown over time. The series is the first
    # categorical column, matching Rule 4's convention rather than searching for the
    # column that would work best; see docs/CHARTS.md section 9 for what that costs.
    series = roles.categorical[0]
    series_values = [row[result.columns.index(series)] for row in result.rows]
    distinct = len(set(series_values))

    # A property of the columns, so it is tested before anything about the rows: colour is
    # one channel and two measures would both need it.
    if len(roles.numeric) > 1:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                f"'{series}' splits the rows into series and {len(roles.numeric)} measures "
                f"would need the same colour, so this is shown as a table."
            ),
        )

    # The mirror of Rule 4's multi-dimensional refusal, and it precedes the series limit
    # for the same reason that one precedes the category limit: when both fire, a hidden
    # dimension is the truer account than too many lines.
    if len(set(zip(x_values, series_values))) != result.row_count:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                f"'{x}' and '{series}' do not identify each row, so a third dimension "
                f"would be hidden and this is shown as a table."
            ),
        )

    if distinct > MAX_LINE_SERIES:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                f"'{series}' has {distinct} distinct values, above the {MAX_LINE_SERIES}-series "
                f"limit for a readable line chart."
            ),
        )

    # Last, because it is the most data-dependent guard and only worth asking once the
    # series column has been accepted. "Invisible" is a measured fact rather than a
    # flourish: Streamlit renders a line chart as three layers, and both point layers are
    # hover-only (the second has `opacity: 0`, the third carries
    # `transform: [{"filter": {"param": "param_1", "empty": false}}]`), so a series holding
    # one row draws a zero-length line and nothing else. `q60` is exactly this shape, six
    # terminals each with one worst quarter.
    # Source: the spec `st.line_chart` emits, read via
    # `json.loads(AppTest.from_string(...).run().get("vega_lite_chart")[0].proto.spec)` on
    # streamlit 1.61.1 - Verified: 2026-08-16. Not from documentation, which does not
    # describe the layering.
    if max(series_values.count(value) for value in set(series_values)) < 2:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                f"Every value of '{series}' appears in one row, so each line would be a "
                f"single invisible point and this is shown as a table."
            ),
        )

    return ChartSpec(
        kind=ChartKind.LINE, x=x, y=[roles.numeric[0]], series=series,
        reason=(
            f"'{x}' is a time axis split into {distinct} series by '{series}', so the "
            f"trend renders as one line per series."
        ),
    )


def _bar_or_table(result: QueryResult, roles: ColumnRoles) -> ChartSpec:
    """Rule 4: a label column plus a measure, or a table when that reading is unsafe.

    The label is normally the only categorical column. But models routinely add a
    descriptive companion column — asked for average wait by terminal, they return
    `terminal_name, port_name, avg_wait` — which under a strict "exactly one categorical"
    test falls through to a table where a bar chart is plainly right. Observed on the
    very first live query, not anticipated.

    The relaxation is deliberately narrow: extra categorical columns are tolerated only
    when the FIRST one already identifies each row uniquely, i.e. it is a label and the
    others are attributes of it. If the first column repeats, the rows are a genuine
    multi-dimensional breakdown and a single-axis bar chart would silently collapse a
    dimension — so those still render as a table.
    """
    # A bar chart of one bar is not a chart, it is a number drawn very wide. Rule 2 has
    # already claimed the readable single-row shapes, so anything arriving here with one
    # row carries extra columns that a metric cannot show, and a table is the honest
    # render. See the note on Rule 2 for how this defect reached the demo.
    if result.row_count == 1:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason="A single row is shown as a table rather than a one-bar chart.",
        )

    label = roles.categorical[0]
    distinct = len({row[result.columns.index(label)] for row in result.rows})
    is_label_like = distinct == result.row_count

    if len(roles.categorical) > 1 and not is_label_like:
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                "Several category columns without a unique label, so this is a "
                "multi-dimensional breakdown and is shown as a table."
            ),
        )

    if distinct <= MAX_BAR_CATEGORIES:
        return ChartSpec(
            kind=ChartKind.BAR, x=label, y=roles.numeric,
            reason=f"Category '{label}' with {distinct} distinct values renders as bars.",
        )

    return ChartSpec(
        kind=ChartKind.TABLE,
        reason=(
            f"'{label}' has {distinct} distinct values, above the "
            f"{MAX_BAR_CATEGORIES}-category limit for a readable bar chart."
        ),
    )


class MetricFields(NamedTuple):
    """The three strings a metric card renders."""

    label: str
    value: str
    help: str | None


def format_metric(value: object) -> str:
    """Render a headline number without lying about its precision.

    Reals are fixed to two decimals because they arrive as PostgreSQL `numeric`
    aggregates: an unformatted average prints as 17.459999999999999, which reads as false
    precision on the one figure the eye treats as the answer. Integers keep thousands
    separators and no decimal point, because a count of 239099 containers is not
    239,099.00.

    Three type checks here are less obvious than they look, and each was wrong in a
    first draft:

    Listed in the order the checks execute. Each was wrong in a first draft, and the ABCs
    are load-bearing rather than stylistic.
    Source: https://docs.python.org/3/library/numbers.html - Verified: 2026-08-08

    * `None` becomes "n/a" rather than "None". A null aggregate is a real result and
      "None" on a headline card reads as a failure rather than as missing data.
    * `bool` is rejected next, because `isinstance(True, numbers.Integral)` is True and a
      boolean measure would otherwise render as "1".
    * `numbers.Integral` rather than `int`. A value that has passed through pandas is a
      `numpy.int64`, and `isinstance(numpy.int64(1), int)` is **False** while
      `isinstance(numpy.int64(1), numbers.Integral)` is True, because numpy registers its
      scalar types with the ABCs rather than subclassing the builtins. A plain `int` check
      therefore drops the separators from exactly the COUNT and SUM aggregates this
      function exists to format. `metric_fields` reads `result.rows` and so never hands
      over a numpy type today, but this function is importable and the trap is invisible
      at the call site. Confirmed by running both checks, not by reading docs.
    * `Decimal` is named explicitly, because it is deliberately **not** registered as a
      `numbers.Real` — the stdlib keeps it out to prevent silent float/Decimal mixing.
    """
    # See the docstring for why each of these four branches is in this order.
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return f"{int(value):,}"
    if isinstance(value, (numbers.Real, Decimal)):
        return f"{float(value):,.2f}"
    return str(value)


def metric_fields(result: QueryResult, chart: ChartSpec) -> MetricFields:
    """Resolve a METRIC spec into what the UI shows.

    This is here rather than in the Streamlit layer because it is a decision, and the
    argument of ADR-005 is that chart decisions belong in code a test can reach. It was
    briefly inline in the view, where nothing but running the app by hand exercised it:
    reverting to positional access (`frame.iloc[0, 0]`) would have passed the entire
    suite while crashing on the first superlative question, because column 0 of
    `terminal_name, avg_wait` is a string and `f"{value:,}"` raises on a string.

    The measure is therefore looked up **by name**. When a label column is present the
    entity takes the label slot and the measure name moves to the tooltip, because
    "17.46" with the unit nowhere on screen is not an answer.
    """
    measure = chart.y[0] if chart.y else result.columns[0]
    row = result.rows[0]
    value = row[result.columns.index(measure)]

    if chart.x and chart.x in result.columns:
        return MetricFields(
            label=str(row[result.columns.index(chart.x)]),
            value=format_metric(value),
            help=measure,
        )
    return MetricFields(label=measure, value=format_metric(value), help=None)


def pick_chart(result: QueryResult) -> ChartSpec:
    """Choose a chart for a result set. Rules are applied in order; first match wins."""
    # Rule 1 — nothing to draw.
    if result.row_count == 0:
        return ChartSpec(
            kind=ChartKind.NONE, reason="No rows returned, so there is nothing to chart."
        )

    roles = classify_columns(result)
    temporal, numeric, categorical = roles

    # Rule 2 — a single row carrying a single measure is a headline figure, not a chart.
    #
    # The `<= 2` here rather than `== 1` is deliberate and was a real defect. A superlative
    # question ("which terminal has the longest berth wait?") answers with a label AND a
    # measure, so it returns two columns, missed this rule, and fell through to Rule 4 —
    # which drew a bar chart containing exactly one bar, stretched across the full width of
    # the container. Every "which X is the most Y" question in the demo hit it, including
    # the first example button in the UI. Found by rendering the gold set, not predicted.
    if result.row_count == 1 and len(numeric) == 1 and len(result.columns) <= 2:
        label = next((name for name in result.columns if name != numeric[0]), None)
        return ChartSpec(
            kind=ChartKind.METRIC, x=label, y=[numeric[0]],
            reason=(
                f"Single row with one measure renders as a metric labelled by '{label}'."
                if label
                else "Single row with a single numeric value renders as a metric."
            ),
        )

    # Rule 3 — a time axis plus at least one measure is a line chart. More than one row is
    # required: a line drawn through a single point shows no trend, so a one-row result is
    # left to the metric rule above or falls through to a table. This guard is preventive,
    # unlike the Rule 2 widening above: no gold question produced a one-point line, but the
    # same shape reaches it whenever a time filter narrows to a single period.
    #
    # See `_line_or_table` for when a category column becomes one line per value and when
    # it forces a table instead.
    if temporal and numeric and result.row_count > 1:
        return _line_or_table(result, roles)

    # Rule 4 — a label column plus a measure. See `_bar_or_table` for why extra
    # categorical columns are sometimes tolerated and sometimes force a table.
    if categorical and numeric:
        return _bar_or_table(result, roles)

    # Rule 5 — two measures and nothing to group by: a relationship, so scatter.
    #
    # The row guard is the same defect as the one-bar chart, found by audit rather than by
    # rendering: a scatter plot of a single point draws no relationship, and this is the
    # shape a superlative takes whenever its label column is itself numeric (grouped by
    # `year` or `crane_id`, say), because `classify_columns` reads a numeric ID as a
    # measure. Rules 2, 3 and 4 all refuse single rows; this one was missed.
    if len(numeric) == 2 and not categorical and not temporal and result.row_count > 1:
        return ChartSpec(
            kind=ChartKind.SCATTER, x=numeric[0], y=[numeric[1]],
            reason="Two numeric columns with no category or time axis render as a scatter plot.",
        )

    # Rule 6 — anything else is most honestly shown as a table.
    return ChartSpec(
        kind=ChartKind.TABLE,
        reason="The result shape does not match a chart type, so it is shown as a table.",
    )
