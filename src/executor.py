"""Executes validated SQL against the database as the read-only analyst role.

This module is the last layer before the database and it is where the runtime limits
live. It assumes its input has already passed `validator.validate_sql`, but it does not
*depend* on that for safety: the connection authenticates as `analyst_ro`, which holds
SELECT and nothing else, so even a defect in the validator cannot turn into a write.
That redundancy is the point of defence in depth (ADR-004).

Three limits are applied here, each bounding a different failure:

* **Statement timeout**: bounds a query that is valid but ruinously expensive.
* **Row cap**: bounds a query that is cheap to run but returns millions of rows, which
  would exhaust memory in this process rather than in the database.
* **Read-only transaction**: a belt-and-braces guard that makes accidental writes fail
  early with a clear error. It is *not* the security boundary; the GRANTs are.

The validated statement is executed **verbatim**. This module composes no SQL around it,
which is deliberate: a query body cannot be passed as a bound parameter, because
parameters carry values and this text has to be parsed as syntax. Rather than build a
wrapper and then argue about the wrapper, the row cap is enforced by declaring a
server-side cursor and asking it for at most `cap + 1` rows. The database streams from a
portal, so a query returning millions of rows still costs this process only the rows it
actually reads.
"""

from __future__ import annotations

import time

import psycopg
from psycopg import sql as pgsql

from .config import settings
from .models import QueryResult


#: Name for the server-side cursor. Fixed rather than generated because each call opens
#: its own connection, so two cursors of this name never coexist.
_SERVER_CURSOR_NAME = "cda_reader"


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
    except Exception:  # noqa: BLE001, type naming must never break query execution
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
    # Trailing semicolons are common in model output and are illegal inside DECLARE.
    inner = sql.strip().rstrip(";")

    started = time.perf_counter()
    try:
        with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
            # Re-assert the limits per connection. The role carries these settings
            # already; setting them again means the guarantee does not depend on the
            # database having been provisioned correctly.
            conn.read_only = True
            with conn.cursor() as setup_cur:
                setup_cur.execute(
                    pgsql.SQL("SET statement_timeout = {}").format(
                        pgsql.Literal(f"{settings.statement_timeout_ms}ms")
                    )
                )
            # Server-side cursor.
            # Source: https://www.psycopg.org/psycopg3/docs/advanced/cursors.html - Verified: 2026-08-07
            # psycopg issues DECLARE ... CURSOR FOR <statement> and
            # then FETCH, so the validated text reaches the database unmodified and the
            # cap is expressed as how many rows are asked for. Requesting cap + 1 is how
            # truncation is detected without a second COUNT query.
            #
            # No parameters are passed, so psycopg performs no client-side placeholder
            # substitution and a literal % in the SQL (a LIKE pattern, or modulo) is
            # passed through untouched.
            read_cur = conn.cursor(name=_SERVER_CURSOR_NAME)
            try:
                read_cur.execute(inner)
                fetched = read_cur.fetchmany(cap + 1)
                description = read_cur.description or []
                columns = [d.name for d in description]
                column_types = [_type_name(d.type_code) for d in description]
            finally:
                # Closing issues CLOSE, which fails if the statement timeout already
                # aborted the transaction. Suppressing that failure is deliberate and
                # narrow: it only catches an error raised by close() itself, so when the
                # query failed, the original exception still propagates to the handler
                # below and reaches the retry edge intact.
                try:
                    read_cur.close()
                except psycopg.Error:
                    pass
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
