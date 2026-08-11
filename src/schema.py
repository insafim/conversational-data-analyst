"""Introspects the live database and renders schema context for the prompt (ADR-003).

Read once at startup and cached. Nothing here is hard-coded about the port-operations
domain, so pointing this at a different PostgreSQL database is a connection-string
change rather than a code change.

The rendered text carries three things the model needs and cannot infer:

1. **Structure** — tables, columns, types, nullability, primary and foreign keys.
2. **Semantics** — the `COMMENT ON` text from the database, which is where units, enum
   values, and grain live. `berth_wait_hours` being *hours* is not recoverable from
   `numeric(10,2)`, and a model that guesses wrong produces SQL that runs and returns a
   confidently wrong number.
3. **Data coverage** — the actual min/max of each date column, so relative phrasing
   ("last quarter") resolves against the data rather than the system clock. The dataset
   uses a fixed historical window (ADR-001), so a model reasoning from today's date
   would silently query an empty range.

This module issues its own hand-written SQL against the catalog. It deliberately does
not pass through `validator.py`, which denies `information_schema` for *user* queries —
that denial is about blocking reconnaissance through the agent, not about this trusted,
parameter-free startup path.
"""

from __future__ import annotations

from functools import lru_cache

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from .config import settings

_COLUMNS_SQL = """
SELECT c.table_name,
       c.column_name,
       c.ordinal_position,
       c.data_type,
       c.is_nullable,
       col_description(format('public.%I', c.table_name)::regclass,
                       c.ordinal_position) AS column_comment
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_name = c.table_name AND t.table_schema = c.table_schema
WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

_TABLE_COMMENTS_SQL = """
SELECT t.table_name,
       obj_description(format('public.%I', t.table_name)::regclass) AS table_comment
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name
"""

# Primary and foreign keys, recovered from the catalog rather than assumed. Join paths
# are the single most valuable thing to give a model on a multi-table schema.
_KEYS_SQL = """
SELECT con.contype,
       cl.relname            AS table_name,
       att.attname           AS column_name,
       fcl.relname           AS ref_table,
       fatt.attname          AS ref_column
FROM pg_constraint con
JOIN pg_class cl        ON cl.oid = con.conrelid
JOIN pg_namespace ns    ON ns.oid = cl.relnamespace
JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute att   ON att.attrelid = cl.oid AND att.attnum = k.attnum
LEFT JOIN pg_class fcl  ON fcl.oid = con.confrelid
LEFT JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord)
       ON fk.ord = k.ord AND con.contype = 'f'
LEFT JOIN pg_attribute fatt ON fatt.attrelid = fcl.oid AND fatt.attnum = fk.attnum
WHERE ns.nspname = 'public' AND con.contype IN ('p', 'f')
ORDER BY cl.relname, con.contype, k.ord
"""


def _fetch(sql: str | pgsql.Composable) -> list[dict]:
    with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return cur.fetchall()


def coverage_fragment(table: str, column: str) -> pgsql.Composable:
    """Build the min/max fragment for one date column.

    Split out of `_date_coverage` so it can be tested directly. This is the only place in
    the codebase that assembles SQL from names read out of the catalog rather than writing
    the query out in full, because the set of date columns is not known until runtime, so
    it is the only place where identifier quoting is load-bearing.

    Identifiers are composed with psycopg's `Identifier` rather than formatted into the
    string. These names come from `information_schema` rather than from a user, so this is
    not a live injection path, but a table named `x"; DROP ...` would break the query, and
    the difference between "cannot be exploited today" and "cannot be exploited" is the
    whole argument this codebase makes about model-generated SQL (ADR-004).
    """
    return pgsql.SQL(
        "SELECT {label} AS col, min({col})::text AS lo, max({col})::text AS hi FROM {tbl}"
    ).format(
        label=pgsql.Literal(f"{table}.{column}"),
        col=pgsql.Identifier(column),
        tbl=pgsql.Identifier(table),
    )


def _date_coverage() -> list[str]:
    """Min/max of every date-ish column, so relative dates resolve against the data."""
    date_columns = [
        (row["table_name"], row["column_name"])
        for row in _fetch(_COLUMNS_SQL)
        if row["data_type"] in ("date", "timestamp without time zone",
                                "timestamp with time zone")
    ]
    if not date_columns:
        return []

    # One round trip: a UNION ALL over each column rather than a query per column.
    parts = [coverage_fragment(table, column) for table, column in date_columns]
    rows = _fetch(pgsql.SQL(" UNION ALL ").join(parts))
    return [f"  {r['col']}: {r['lo']} .. {r['hi']}" for r in rows if r["lo"]]


@lru_cache(maxsize=1)
def get_schema_context() -> str:
    """Return the cached, rendered schema description injected into every SQL prompt.

    Cached for the process lifetime: DDL does not change while the app runs, and paying
    several catalog queries per question would add latency for no benefit (ADR-003).
    """
    columns = _fetch(_COLUMNS_SQL)
    table_comments = {r["table_name"]: r["table_comment"] for r in _fetch(_TABLE_COMMENTS_SQL)}
    keys = _fetch(_KEYS_SQL)

    primary: dict[str, list[str]] = {}
    foreign: list[str] = []
    for row in keys:
        if row["contype"] == "p":
            primary.setdefault(row["table_name"], []).append(row["column_name"])
        elif row["ref_table"]:
            foreign.append(
                f"  {row['table_name']}.{row['column_name']} -> "
                f"{row['ref_table']}.{row['ref_column']}"
            )

    by_table: dict[str, list[dict]] = {}
    for row in columns:
        by_table.setdefault(row["table_name"], []).append(row)

    blocks: list[str] = []
    for table, cols in by_table.items():
        lines = []
        if table_comments.get(table):
            lines.append(f"-- {table_comments[table]}")
        lines.append(f"CREATE TABLE {table} (")
        rendered = []
        for col in cols:
            null_marker = "" if col["is_nullable"] == "YES" else " NOT NULL"
            comment = f"  -- {col['column_comment']}" if col["column_comment"] else ""
            rendered.append(
                f"    {col['column_name']} {col['data_type']}{null_marker},{comment}"
            )
        lines.extend(rendered)
        if primary.get(table):
            lines.append(f"    PRIMARY KEY ({', '.join(primary[table])})")
        lines.append(");")
        blocks.append("\n".join(lines))

    sections = ["\n\n".join(blocks)]
    if foreign:
        sections.append("-- Foreign keys (join paths):\n" + "\n".join(sorted(set(foreign))))

    coverage = _date_coverage()
    if coverage:
        sections.append(
            "-- Data coverage. Resolve relative dates ('last quarter', 'recently')\n"
            "-- against THESE ranges, not against today's date:\n" + "\n".join(coverage)
        )

    return "\n\n".join(sections)


def split_table_comment(comment: str | None) -> tuple[str, str]:
    """Split a table's COMMENT ON into (display name, remaining description).

    The convention is the first sentence. Every table in `db/01_schema.sql` already opens
    with a noun phrase naming what the rows are ("Vessel visits.", "Quay cranes.",
    "Container handling activity."), so the sidebar can show a human name without a
    mapping table in the UI layer. That matters for ADR-003: the portability claim rests
    on this module holding no domain literals, and a hard-coded {table: label} dict would
    have put five of them here.

    Returns ("", "") for a table with no comment, which the caller renders by falling back
    to the table name. Split out of `get_schema_summary` so it is unit-testable without a
    database.
    """
    text = (comment or "").strip()
    if not text:
        return "", ""
    head, separator, tail = text.partition(". ")
    if not separator:
        # A single sentence, with or without its full stop: it is the name, and there is
        # no description left over.
        return text.rstrip("."), ""
    return head.strip().rstrip("."), tail.strip()


def get_schema_summary() -> list[tuple[str, int, str, str]]:
    """(table, column count, display name, description) for the UI sidebar.

    The display name and description come from the database's own `COMMENT ON TABLE`
    text, read through `split_table_comment`. A reader who does not know the schema needs
    "Vessel visits" far more than it needs "port_calls (9 cols)", and the same comments
    are already injected into the SQL prompt, so the two surfaces cannot drift apart.
    """
    counts: dict[str, int] = {}
    for row in _fetch(_COLUMNS_SQL):
        counts[row["table_name"]] = counts.get(row["table_name"], 0) + 1

    comments = {r["table_name"]: r["table_comment"] for r in _fetch(_TABLE_COMMENTS_SQL)}

    summary = []
    for table, column_count in sorted(counts.items()):
        name, description = split_table_comment(comments.get(table))
        summary.append((table, column_count, name, description))
    return summary
