"""Executes validated SQL against the database as the read-only analyst role.

This module is the last layer before the database and it is where the runtime limits
live. It assumes its input has already passed `validator.validate_sql`, but it does not
*depend* on that for safety: the connection authenticates as `analyst_ro`, which holds
SELECT and nothing else, so even a defect in the validator cannot turn into a write.
That redundancy is the point of defence in depth (ADR-004).

Three limits are applied here, each bounding a different failure:

* **Statement timeout** — bounds a query that is valid but ruinously expensive.
* **Row cap** — bounds a query that is cheap to run but returns millions of rows, which
  would exhaust memory in this process rather than in the database.
* **Read-only transaction** — a belt-and-braces guard that makes accidental writes fail
  early with a clear error. It is *not* the security boundary; the GRANTs are.
"""

from __future__ import annotations

import time

import psycopg
from psycopg import sql as pgsql

from .config import settings
from .models import QueryResult


class ExecutionError(RuntimeError):
    """A query failed in the database. Carries the PostgreSQL message so the retry
    edge can feed it back to the model as context (ADR-002)."""


def _type_name(oid: int) -> str:
    """Map a PostgreSQL type OID to its name, for chart-type selection (ADR-005).

    Chart rules key off the *declared* types the database returns rather than sniffing
    values, so a column of integers that happens to hold years is still an integer, and
    a date is still a date even when every value is identical.
    """
    try:
        info = psycopg.postgres.types.get(oid)
        return info.name if info else "unknown"
    except Exception:  # noqa: BLE001 — type naming must never break query execution
        return "unknown"


def run_query(sql: str, row_cap: int | None = None) -> QueryResult:
    """Execute a validated SELECT and return its rows.

    Args:
        sql: SQL that has already passed the validator.
        row_cap: Maximum rows to return. Defaults to the configured cap.

    Returns:
        QueryResult, with `truncated=True` if the cap trimmed the output.

    Raises:
        ExecutionError: if the database rejects or fails the query.
    """
    cap = row_cap if row_cap is not None else settings.row_cap
    inner = sql.strip().rstrip(";")

    # Wrap rather than append LIMIT: appending would corrupt a query that already ends
    # in LIMIT or ORDER BY, and would silently change the meaning of a UNION. Fetching
    # cap+1 rows is how truncation is detected without a second COUNT query.
    capped = pgsql.SQL("SELECT * FROM ({inner}) AS _capped LIMIT {limit}").format(
        inner=pgsql.SQL(inner),  # already validated as a single read-only SELECT
        limit=pgsql.Literal(cap + 1),
    )

    started = time.perf_counter()
    try:
        with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
            # Re-assert the limits per connection. The role carries these settings
            # already; setting them again means the guarantee does not depend on the
            # database having been provisioned correctly.
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    pgsql.SQL("SET statement_timeout = {}").format(
                        pgsql.Literal(f"{settings.statement_timeout_ms}ms")
                    )
                )
                cur.execute(capped)
                fetched = cur.fetchall()
                description = cur.description or []
                columns = [d.name for d in description]
                column_types = [_type_name(d.type_code) for d in description]
    except psycopg.Error as exc:
        # Surface the database's own message: it is what makes the single retry
        # useful, because the model can see exactly what it got wrong.
        raise ExecutionError(str(exc).strip()) from exc

    elapsed = time.perf_counter() - started
    truncated = len(fetched) > cap
    rows = [list(r) for r in fetched[:cap]]

    return QueryResult(
        columns=columns,
        column_types=column_types,
        rows=rows,
        row_count=len(rows),
        elapsed_s=round(elapsed, 3),
        truncated=truncated,
    )
