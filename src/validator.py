"""Code-level SQL gate. Runs before execution, on every query, with no exceptions.

This module is a security boundary. Three properties follow from that and are worth
stating because they constrain how it may be changed:

1. **It is code, not a prompt.** It cannot be argued with, jailbroken, or persuaded.
   A prompt injection that fully captures the model still has to produce SQL that
   passes this function.
2. **It fails closed.** Any unexpected condition — including a parse failure or an
   unanticipated exception — rejects the query. A validator that fails open is worse
   than no validator, because it creates false confidence.
3. **It is allow-list shaped.** Only a single, read-only SELECT-family statement is
   permitted. Everything else is denied by default, so a SQL feature nobody thought of
   is denied rather than accidentally allowed. A deny-list of scary keywords would be
   the opposite, and would be defeated by the first construct not on the list.

## Why AST parsing rather than string matching

The attack that motivates this design is a data-modifying CTE:

    WITH d AS (DELETE FROM port_calls RETURNING *) SELECT count(*) FROM d

Its top-level node type is `Select`. A validator that checks "is this a SELECT?" — which
is the obvious implementation, and what `sqlparse.get_type()` reports — **passes this
query**, and it deletes the table. Only walking the parse tree finds the `Delete` node
nested inside. This was verified empirically against sqlglot before this module was
written; see tests/test_validator.py, which encodes that finding as a regression test.

## What this layer does NOT stop

Stated explicitly, because a security control whose limits are undocumented gets trusted
beyond them. This layer does not stop a semantically wrong-but-safe query, and it does
not stop an expensive one. Those are handled by the row cap and statement timeout in
`executor.py`, and — as the final, non-bypassable backstop — by the fact that the
connection authenticates as a role holding only SELECT (ADR-004).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .models import ValidationResult

# Node types that must never appear anywhere in the tree, at any depth.
# `exp.Command` is the important catch-all: sqlglot parses statements it has no
# dedicated node for (SET, VACUUM, CALL, ...) into Command, so denying it closes the
# gap for constructs this list does not name individually.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
    exp.Grant, exp.Copy, exp.Command,
)

# Only these may be the root.
#
# `exp.SetOperation` is the shared base class of Union, Intersect and Except. Listing
# the base rather than the three subclasses matters: an earlier version allowed only
# `exp.Union` and silently rejected `SELECT ... INTERSECT SELECT ...`, which is a
# perfectly ordinary read-only query. A validator that blocks legitimate questions is
# not "extra safe" — it is broken, and the failure is invisible because it looks like
# the model wrote bad SQL.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select, exp.SetOperation, exp.Subquery,
)

# Functions that are structurally valid inside a SELECT but are either a
# denial-of-service primitive or a file/network read. PostgreSQL already blocks most of
# these for a non-superuser, but blocking them here means the attempt is refused with a
# clear reason instead of surfacing a raw permission error, and it keeps the guarantee
# if this ever runs against a less carefully configured database.
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    "dblink", "dblink_exec", "dblink_connect",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "query_to_xml", "xmlelement",
    "set_config", "pg_rotate_logfile",
})

# Schemas holding server internals or credentials. Business questions never need these;
# a request for them is reconnaissance.
_FORBIDDEN_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast"})

_FORBIDDEN_TABLES: frozenset[str] = frozenset({
    "pg_authid", "pg_shadow", "pg_user_mapping", "pg_settings", "pg_stat_activity",
})


def _fail(violation: str, reason: str) -> ValidationResult:
    return ValidationResult(ok=False, violation=violation, reason=reason)


def validate_sql(sql: str) -> ValidationResult:
    """Return ``ok=True`` only for a single, read-only, SELECT-family statement.

    Args:
        sql: Candidate SQL, as produced by the model. Treated as fully untrusted.

    Returns:
        A ValidationResult. On failure, ``reason`` is safe to show a user and
        ``violation`` is a stable code for tests and metrics.
    """
    if not sql or not sql.strip():
        return _fail("empty", "No SQL was produced.")

    # 1. Parse. A failure to parse is a rejection, never a pass-through: if we cannot
    #    understand the statement we cannot claim it is safe.
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001 — fail closed on ANY parser error
        return _fail("unparseable", f"The generated SQL could not be parsed safely: {exc}")

    statements = [s for s in statements if s is not None]

    # 2. Exactly one statement. This is what blocks stacked-query injection
    #    ("SELECT 1; DROP TABLE t"), independently of what the second statement is.
    if len(statements) != 1:
        return _fail(
            "multiple_statements",
            f"Only one statement may run per question; this contained {len(statements)}.",
        )

    tree = statements[0]

    # 3. Root must be SELECT-family.
    if not isinstance(tree, _ALLOWED_ROOTS):
        return _fail(
            "not_a_select",
            f"Only SELECT queries are permitted; this was {type(tree).__name__.upper()}.",
        )

    # 4. No write operation anywhere in the tree, at any nesting depth.
    #    This is the check that catches the data-modifying CTE described in the module
    #    docstring — the one that a root-type check alone would let through.
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return _fail(
                "write_operation",
                f"The query contains a {type(node).__name__.upper()} operation. "
                "This assistant is read-only.",
            )

    # 5. SELECT ... INTO creates a table, so it is a write wearing a SELECT's clothes.
    if tree.args.get("into"):
        return _fail("select_into", "SELECT ... INTO creates a table and is not permitted.")

    # 6. Denied functions. sqlglot maps functions it knows to dedicated node types and
    #    everything else to `Anonymous`; the calls we care about here (pg_sleep,
    #    pg_read_file, dblink, ...) are not standard SQL, so they arrive as Anonymous.
    #    Both cases are handled so the check does not depend on that staying true.
    for func in tree.find_all(exp.Anonymous, exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else type(func).__name__
        if isinstance(name, str) and name.lower() in _FORBIDDEN_FUNCTIONS:
            return _fail("forbidden_function", f"The function {name}() is not permitted.")

    # 7. Denied schemas and system tables.
    for table in tree.find_all(exp.Table):
        if (table.db or "").lower() in _FORBIDDEN_SCHEMAS:
            return _fail(
                "system_schema",
                f"Access to the {table.db} schema is not permitted.",
            )
        if (table.name or "").lower() in _FORBIDDEN_TABLES:
            return _fail(
                "system_table",
                f"Access to the system table {table.name} is not permitted.",
            )

    return ValidationResult(ok=True)
