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

import re
from decimal import Decimal

import pandas as pd

from .models import ChartKind, ChartSpec, QueryResult

# Maximum distinct categories worth drawing as bars. Beyond this a bar chart is an
# unreadable table with extra steps, so the honest output is a table. Judgement call,
# not a derived constant (ADR-005).
MAX_BAR_CATEGORIES = 12

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


def pick_chart(result: QueryResult) -> ChartSpec:
    """Choose a chart for a result set. Rules are applied in order; first match wins."""
    columns, types = result.columns, result.column_types

    # Rule 1 — nothing to draw.
    if result.row_count == 0:
        return ChartSpec(
            kind=ChartKind.NONE, reason="No rows returned, so there is nothing to chart."
        )

    temporal: list[str] = []
    numeric: list[str] = []
    categorical: list[str] = []
    for index, name in enumerate(columns):
        col_type = types[index] if index < len(types) else "unknown"
        values = [row[index] for row in result.rows]
        if col_type in _TEMPORAL_TYPES or _looks_temporal(values):
            temporal.append(name)
        elif col_type in _NUMERIC_TYPES:
            numeric.append(name)
        else:
            categorical.append(name)

    # Rule 2 — one row, one number: a headline figure needs no axes.
    if result.row_count == 1 and len(numeric) == 1 and len(columns) == 1:
        return ChartSpec(
            kind=ChartKind.METRIC, y=[numeric[0]],
            reason="Single row with a single numeric value renders as a metric.",
        )

    # Rule 3 — a time axis plus at least one measure is a line chart.
    if temporal and numeric:
        return ChartSpec(
            kind=ChartKind.LINE, x=temporal[0], y=numeric,
            reason=f"'{temporal[0]}' is a time axis, so the trend renders as a line chart.",
        )

    # Rule 4 — a label column plus a measure.
    #
    # The label is normally the only categorical column. But models routinely add a
    # descriptive companion column — asked for average wait by terminal, they return
    # `terminal_name, port_name, avg_wait` — which under a strict "exactly one
    # categorical" test falls through to a table where a bar chart is plainly right.
    # Observed on the very first live query, not anticipated.
    #
    # The relaxation is deliberately narrow: extra categorical columns are tolerated
    # only when the FIRST one already identifies each row uniquely, i.e. it is a label
    # and the others are attributes of it. If the first column repeats, the rows are a
    # genuine multi-dimensional breakdown and a single-axis bar chart would silently
    # collapse a dimension — so those still render as a table.
    if categorical and numeric:
        label = categorical[0]
        distinct = len({row[columns.index(label)] for row in result.rows})
        is_label_like = distinct == result.row_count
        if len(categorical) > 1 and not is_label_like:
            return ChartSpec(
                kind=ChartKind.TABLE,
                reason=(
                    "Several category columns without a unique label, so this is a "
                    "multi-dimensional breakdown and is shown as a table."
                ),
            )
        if distinct <= MAX_BAR_CATEGORIES:
            return ChartSpec(
                kind=ChartKind.BAR, x=label, y=numeric,
                reason=(
                    f"Category '{label}' with {distinct} distinct values renders as bars."
                ),
            )
        return ChartSpec(
            kind=ChartKind.TABLE,
            reason=(
                f"'{categorical[0]}' has {distinct} distinct values, above the "
                f"{MAX_BAR_CATEGORIES}-category limit for a readable bar chart."
            ),
        )

    # Rule 5 — two measures and nothing to group by: a relationship, so scatter.
    if len(numeric) == 2 and not categorical and not temporal:
        return ChartSpec(
            kind=ChartKind.SCATTER, x=numeric[0], y=[numeric[1]],
            reason="Two numeric columns with no category or time axis render as a scatter plot.",
        )

    # Rule 6 — anything else is most honestly shown as a table.
    return ChartSpec(
        kind=ChartKind.TABLE,
        reason="The result shape does not match a chart type, so it is shown as a table.",
    )
