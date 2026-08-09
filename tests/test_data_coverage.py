"""The sidebar's data-coverage line, and why it is a correctness affordance.

Asked to project July 2026 port calls, the agent returned a historical average and
presented it as a forecast. `SUMMARIZE_SYSTEM` rule 6 now stops that, and
`tests/test_forecast_grounding.py` pins it. This file covers the half of the problem the
prompt cannot reach: the user asked about July because nothing on screen said the data
stops in June, so the question was a reasonable thing to ask. A guard that rejects a
question the interface invited is a worse design than an interface that does not invite
it.

The whole module is marked `integration` because importing `app` runs Streamlit's
module-level script body, which reads the catalog.
"""

from __future__ import annotations

import pytest

import app
from src.validator import validate_sql

pytestmark = pytest.mark.integration


def test_the_coverage_line_reports_the_window_the_data_actually_holds():
    """The string must be derived, never written down.

    A hard-coded window is wrong the moment the database is reseeded with a different
    range, and wrong silently, which is the failure mode this line exists to prevent.
    Asserting against `min`/`max` read back from the same table is what makes it a
    derived value rather than a caption that happens to be true today.
    """
    from src.executor import run_query

    bounds = run_query(
        "SELECT min(arrival_ts)::date, max(arrival_ts)::date FROM port_calls", row_cap=1
    )
    first, last = bounds.rows[0]

    coverage = app.data_coverage()

    assert coverage == f"{first:%B %Y} to {last:%B %Y}", (
        f"sidebar reports {coverage!r} for data spanning {first} to {last}"
    )


def test_the_coverage_line_names_a_month_and_year_not_a_raw_date():
    """Format is the point, not decoration.

    The reader is the same non-technical user who is not shown the column listing above
    this line. "2025-01-01 to 2026-06-30" is a database artefact; "January 2025 to June
    2026" is the same fact in their language, and the whole reason the line exists is to
    be read by someone who would skip a timestamp.
    """
    coverage = app.data_coverage()
    assert coverage is not None
    assert "-" not in coverage, f"{coverage!r} exposes a raw date format"
    assert " to " in coverage


def test_the_apps_own_sql_meets_the_bar_the_agents_sql_must_meet():
    """The validator exists for model output, and this query is hand-written, so nothing
    forces it through. Asserting it would pass anyway pins a property worth keeping: every
    statement this application sends to the database is a single read. If someone later
    adds a sidebar feature backed by something else, this fails rather than the invariant
    quietly ceasing to be true.
    """
    result = validate_sql(app._COVERAGE_SQL)
    assert result.ok, f"the sidebar's own query would be rejected: {result.reason}"
