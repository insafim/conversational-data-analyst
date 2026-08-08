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

    # Server and session disclosure. None of these read business data, so none of them
    # answer a business question; what they return is the host address, the backend PID,
    # and which privileges this role holds, which is the reconnaissance a caller performs
    # before attempting anything else. `inet_server_addr()` was verified to execute
    # successfully as `analyst_ro` and return the container's IP before being denied here.
    "inet_server_addr", "inet_server_port", "inet_client_addr", "inet_client_port",
    "pg_backend_pid", "pg_postmaster_start_time", "pg_get_userbyid",
    "has_table_privilege", "has_database_privilege", "has_schema_privilege",
    "current_schemas", "pg_get_viewdef", "pg_get_functiondef",

    # Sequence functions MUTATE STATE from inside a SELECT. `SELECT nextval('s')` and
    # `SELECT setval('s', 1)` are writes wearing a SELECT's clothes: they parse as an
    # ordinary query, contain no write node, and change the database.
    #
    # Found by probing rather than by reasoning. PostgreSQL already refuses them for this
    # role — `analyst_ro` was granted SELECT on tables and never USAGE on sequences, so
    # layer 1 held — but relying on that would leave the validator's own guarantee
    # ("no statement that passes this can modify anything") false as written.
    "nextval", "setval", "currval", "lastval",

    # Leaks server configuration (`SELECT current_setting('is_superuser')` returns a
    # value). Not data disclosure, but it is reconnaissance and no business question
    # needs it. This one DID execute successfully before being denied here.
    "current_setting",
})

# Functions sqlglot gives a dedicated node type rather than parsing as a named call.
# `version()` becomes `CurrentVersion`, `current_user` becomes `CurrentUser`, and so on,
# so a name-based deny-list never sees them. They are matched by class instead.
#
# Found by parsing each construct and printing the resulting node types. Every one of
# these executed successfully as `analyst_ro` beforehand: `version()` returns the exact
# PostgreSQL build and host architecture, and `current_user` confirms which role the
# agent connects as. Both are the first things an attacker wants and neither is an
# answer to a question about port operations.
_FORBIDDEN_FUNCTION_NODES: tuple[type[exp.Expression], ...] = (
    exp.CurrentUser, exp.SessionUser, exp.CurrentDatabase, exp.CurrentVersion,
    exp.CurrentSchema,
)

# Schemas holding server internals or credentials. Business questions never need these;
# a request for them is reconnaissance.
_FORBIDDEN_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast"})

# Every PostgreSQL system catalog and system view is named with this prefix, so denying
# the prefix denies the whole class rather than the handful of catalogs someone thought
# to list.
#
# The prefix is matched because the schema check above is not sufficient on its own: it
# reads `table.db`, which is empty for an unqualified reference, and `pg_catalog` sits in
# the default `search_path`. `SELECT rolname FROM pg_roles` therefore resolves to the
# catalog while presenting no schema for the check to match. Verified by running it:
# before this rule, that query passed the validator and returned the server's role names.
# `pg_database`, `pg_tables`, `pg_class`, `pg_proc` and `pg_extension` behaved the same
# way. The GRANTs do not stop these, because these catalogs are world-readable in
# PostgreSQL; this is one of the cases where layer 1 genuinely does not hold and the
# validator has to.
#
# The trade this makes: PostgreSQL does NOT reserve the `pg_` prefix for user tables
# (`CREATE TABLE pg_mytable` was run against this database and succeeded), so a business
# table named `pg_*` would be denied. That is accepted because no table in this schema
# uses the prefix, which is asserted against the live database in
# tests/test_security_boundary.py rather than left as an assumption. The check lives
# there rather than in tests/test_validator.py because the latter is deliberately
# database-free, as its module docstring states.
_SYSTEM_TABLE_PREFIX = "pg_"


def _fail(violation: str, reason: str) -> ValidationResult:
    return ValidationResult(ok=False, violation=violation, reason=reason)


def validate_sql(sql: str) -> ValidationResult:
    """Return ``ok=True`` only for a single, read-only, SELECT-family statement.

    Args:
        sql: Candidate SQL, as produced by the model. Treated as fully untrusted.

    Returns:
        A ValidationResult. On failure, ``reason`` is safe to show a user and
        ``violation`` is a stable code that the tests assert against, so a rejection can
        be pinned to a specific rule rather than to the wording of its message. Nothing
        aggregates these codes yet, so there is no rejection-rate metric; the code is
        shaped to support one when there is somewhere to send it.
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

    # 5b. Locking clauses (FOR UPDATE / FOR SHARE). These are structurally SELECTs, but
    #     they take row locks and can block other sessions — an availability problem
    #     rather than an integrity one, and never something an analytics question needs.
    #     PostgreSQL would also refuse them for this role, but failing here gives a clear
    #     reason instead of a raw permission error.
    if tree.args.get("locks"):
        return _fail(
            "locking_clause",
            "Locking clauses such as FOR UPDATE are not permitted on an analytics query.",
        )

    # 6. Denied functions. sqlglot maps functions it knows to dedicated node types and
    #    everything else to `Anonymous`; the calls we care about here (pg_sleep,
    #    pg_read_file, dblink, ...) are not standard SQL, so they arrive as Anonymous.
    #    Both cases are handled so the check does not depend on that staying true.
    for func in tree.find_all(exp.Anonymous, exp.Func):
        if isinstance(func, _FORBIDDEN_FUNCTION_NODES):
            return _fail(
                "forbidden_function",
                f"{type(func).__name__} discloses server details and is not permitted.",
            )
        name = func.name if isinstance(func, exp.Anonymous) else type(func).__name__
        if isinstance(name, str) and name.lower() in _FORBIDDEN_FUNCTIONS:
            return _fail("forbidden_function", f"The function {name}() is not permitted.")

    # 7. Denied schemas and system catalogs.
    #
    # CTE names parse as Table nodes too, so they are collected first and excluded.
    # Without this, `WITH pg_summary AS (...) SELECT * FROM pg_summary` is denied even
    # though it never touches a catalog. A validator that blocks legitimate questions
    # is not extra safe, it is broken, and the failure looks like bad model output.
    cte_names = {
        (cte.alias or "").lower() for cte in tree.find_all(exp.CTE)
    }
    for table in tree.find_all(exp.Table):
        if (table.db or "").lower() in _FORBIDDEN_SCHEMAS:
            return _fail(
                "system_schema",
                f"Access to the {table.db} schema is not permitted.",
            )
        name = (table.name or "").lower()
        if name.startswith(_SYSTEM_TABLE_PREFIX) and name not in cte_names:
            return _fail(
                "system_table",
                f"Access to the system catalog {table.name} is not permitted.",
            )

    return ValidationResult(ok=True)
