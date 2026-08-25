"""Tests for schema introspection and the SQL it builds (ADR-003).

`_date_coverage` is the one place in this codebase that assembles a query from names read
out of the catalog rather than writing it out in full, because the number of date columns
is not known until runtime. It previously did that with an f-string. These tests pin the
composition, not the connection.

Why the assertions are at composition level rather than end to end: proving the quoting
matters would require a table actually named something hostile, and the test suite cannot
create one. It connects as `analyst_ro`, which holds `SELECT` and nothing else, and a test
that quietly needed DDL rights would undermine the security claim the rest of the suite
exists to make. So the production fragment builder is called directly and the SQL it
returns is rendered and inspected.

These tests call `schema.coverage_fragment` itself rather than rebuilding the same
expression locally. That distinction is the whole value of the file: a copy of the
construction would keep passing if the real one regressed to an f-string, which is exactly
the defect these tests exist to catch.
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings
from src.schema import coverage_fragment, get_schema_context, get_schema_summary

pytestmark = pytest.mark.integration


def _render(table: str, column: str) -> str:
    """Render the production fragment to SQL text for inspection."""
    with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
        return coverage_fragment(table, column).as_string(conn)


def test_identifiers_are_quoted_not_interpolated() -> None:
    """The ordinary case. Identifiers arrive double-quoted, which is what an f-string
    did not do."""
    rendered = _render("port_calls", "arrival_ts")
    assert '"port_calls"' in rendered
    assert '"arrival_ts"' in rendered


def test_a_hostile_identifier_cannot_break_out_of_its_quotes() -> None:
    """The reason the change was worth making. A table named with an embedded quote and a
    statement terminator is rendered as a single quoted identifier with the quote doubled,
    so it stays one name instead of becoming a second statement.

    No such table exists here, and the names really do come from `information_schema`
    rather than from a user. The point is that the safety of this line stops depending on
    that remaining true.
    """
    rendered = _render('x"; DROP TABLE terminals; --', "arrival_ts")
    assert '"x""; DROP TABLE terminals; --"' in rendered
    assert not rendered.rstrip().endswith("--")
    # The payload is inert: one statement, and the DROP sits inside an identifier.
    assert rendered.count("SELECT") == 1


def test_the_label_is_a_literal_not_an_identifier() -> None:
    """The label is data the model reads, so it is a string literal in single quotes. If
    it were composed as an identifier the query would fail at parse time instead."""
    rendered = _render("port_calls", "arrival_ts")
    assert "'port_calls.arrival_ts'" in rendered


def test_schema_context_reports_real_tables_and_date_coverage() -> None:
    """End to end, against the live catalog: the rendered context must carry the tables,
    the join paths, and the data coverage block that lets the model resolve relative dates
    against the data rather than against today (ADR-001)."""
    context = get_schema_context()
    for table in ("terminals", "vessels", "cranes", "port_calls", "cargo_moves"):
        assert table in context
    assert "Foreign keys" in context
    assert "Data coverage" in context
    assert "port_calls.arrival_ts" in context


def test_schema_summary_covers_every_table() -> None:
    summaries = get_schema_summary()
    counts = {summary.table: summary.column_count for summary in summaries}
    assert set(counts) == {"terminals", "vessels", "cranes", "port_calls", "cargo_moves"}
    assert all(count > 0 for count in counts.values())


def test_the_sidebar_can_list_the_columns_it_counts() -> None:
    """The count and the list behind it come from one read, so they cannot disagree.

    `column_count` is derived from `columns` rather than stored beside it, which is the
    property being pinned: the sidebar makes the count the label of the expander that
    opens onto the list, so a count that exceeded the list would be a control promising
    rows it does not have.
    """
    summaries = {summary.table: summary for summary in get_schema_summary()}

    port_calls = summaries["port_calls"]
    assert port_calls.column_count == len(port_calls.columns)

    names = [column.name for column in port_calls.columns]
    assert names[0] == "port_call_id", (
        "columns are not in declaration order, so the key is no longer first"
    )
    assert "berth_wait_hours" in names

    assert all(column.data_type for column in port_calls.columns), (
        "a column has no type, so the listing cannot say what it holds"
    )


def test_the_column_listing_carries_the_units_the_type_cannot() -> None:
    """The reason the listing shows comments rather than only names and types.

    `berth_wait_hours` is `numeric`, and that a value is in HOURS is not recoverable from
    the type. It is recoverable from `COMMENT ON COLUMN`, which is the same text the SQL
    prompt is built from, so the sidebar and the model read one source. This is the whole
    argument for the expander existing; if the comments stopped arriving it would be a
    list of names a reviewer already knew.
    """
    summaries = {summary.table: summary for summary in get_schema_summary()}
    described = [
        column
        for summary in summaries.values()
        for column in summary.columns
        if column.description
    ]
    assert len(described) >= 20, (
        f"only {len(described)} columns carry a comment, so the listing has little to add"
    )

    wait = next(
        column
        for column in summaries["port_calls"].columns
        if column.name == "berth_wait_hours"
    )
    assert "hour" in wait.description.lower(), (
        f"{wait.description!r} does not state the unit, which the numeric type cannot"
    )


def test_every_table_offers_the_sidebar_a_human_name() -> None:
    """The sidebar shows a readable name per table, and it is READ, never written here.

    The names come from `COMMENT ON TABLE` in db/01_schema.sql, which is also what the
    SQL prompt is built from, so the label a user reads and the description the model
    reads cannot drift apart. This test is what stops a future comment being rewritten
    into a form that leaves the sidebar showing raw table names.
    """
    for summary in get_schema_summary():
        assert summary.name, (
            f"{summary.table} has no comment, so the sidebar would fall back to its name"
        )
        assert summary.name != summary.table, (
            f"{summary.table}'s display name is just the table name"
        )
        assert not summary.name.endswith("."), (
            f"{summary.table}'s display name kept its full stop"
        )


# ---------------------------------------------------------------------------------
# The COMMENT ON statements in db/01_schema.sql are functional, not decorative: they
# are the only channel carrying units, enum values and grain to the model, none of
# which a column type can express. Until these tests existed, deleting every comment
# in the schema would have left the whole suite green while accuracy silently fell,
# so the claim was enforced by a docstring rather than by anything executable.
#
# The oracle below is deliberately read from `pg_description` via pg_catalog joins.
# `src/schema.py` reads comments through `information_schema` plus `col_description`,
# so these queries share no code path with the thing under test. A copy of the
# production query would keep passing if the production query regressed.
# ---------------------------------------------------------------------------------

_TABLE_COMMENT_ORACLE = """
SELECT cl.relname AS table_name, d.description
FROM pg_description d
JOIN pg_class cl     ON cl.oid = d.objoid
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
WHERE ns.nspname = 'public' AND cl.relkind = 'r' AND d.objsubid = 0
"""

_COLUMN_COMMENT_ORACLE = """
SELECT cl.relname AS table_name, att.attname AS column_name, d.description
FROM pg_description d
JOIN pg_class cl      ON cl.oid = d.objoid
JOIN pg_namespace ns  ON ns.oid = cl.relnamespace
JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = d.objsubid
WHERE ns.nspname = 'public' AND cl.relkind = 'r' AND d.objsubid > 0
  AND NOT att.attisdropped
"""


def _catalog(sql: str) -> list[tuple]:
    with psycopg.connect(settings.analyst_dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


_UNCOMMENTED_COLUMNS = """
SELECT cl.relname || '.' || att.attname
FROM pg_attribute att
JOIN pg_class cl      ON cl.oid = att.attrelid
JOIN pg_namespace ns  ON ns.oid = cl.relnamespace
LEFT JOIN pg_description d ON d.objoid = cl.oid AND d.objsubid = att.attnum
WHERE ns.nspname = 'public' AND cl.relkind = 'r'
  AND att.attnum > 0 AND NOT att.attisdropped
  AND d.description IS NULL
"""


def test_every_table_carries_a_comment() -> None:
    """Guard the guard. The two tests below iterate over whatever comments the catalog
    happens to hold, so both pass vacuously against a database with none. Pin
    non-emptiness here, or dropping the whole `COMMENT ON` block satisfies the suite."""
    tables = _catalog(_TABLE_COMMENT_ORACLE)
    assert len(tables) == 5, f"expected a comment on all 5 tables, found {len(tables)}"


def test_only_surrogate_keys_are_left_undocumented() -> None:
    """Every column except the generated primary keys must carry a comment.

    This encodes the instruction at the top of `db/01_schema.sql`: change a column, change
    its comment. A new column added without one fails here, which is the only mechanism
    that catches the omission — a missing comment breaks no query and no other test.

    Surrogate keys are exempt because `terminals.terminal_id` has no semantics a sentence
    could add. Every column that carries meaning a type cannot express is in scope.
    """
    undocumented = {row[0] for row in _catalog(_UNCOMMENTED_COLUMNS)}
    surrogate_keys = {
        "terminals.terminal_id",
        "vessels.vessel_id",
        "cranes.crane_id",
        "port_calls.port_call_id",
        "cargo_moves.move_id",
    }
    assert undocumented == surrogate_keys, (
        f"columns missing a COMMENT ON: {sorted(undocumented - surrogate_keys)}"
    )


def _rendered_table_comments(context: str) -> dict[str, str]:
    """Map each table to the comment rendered immediately above its CREATE TABLE line.

    `get_schema_context` emits a table comment as a single `-- ...` line directly
    preceding `CREATE TABLE <name> (`, so attribution is positional and checkable.
    """
    rendered: dict[str, str] = {}
    lines = context.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("CREATE TABLE "):
            continue
        table = stripped[len("CREATE TABLE "):].split("(")[0].strip()
        previous = lines[index - 1].strip() if index else ""
        rendered[table] = previous[2:].strip() if previous.startswith("--") else ""
    return rendered


def test_every_table_comment_reaches_the_rendered_context() -> None:
    """Attribution, not presence — the same correction applied to the column test.

    An audit demonstrated that the previous `description in context` form let a
    `terminals`/`cranes` table-comment swap through untouched: both strings were still
    somewhere in the prompt, just describing the wrong table. Misattribution is less
    likely here than for columns, because `schema.py` keys table comments by name rather
    than filling them by ordinal, but the assertion shape was identical and worth fixing
    for the same reason.
    """
    rendered = _rendered_table_comments(get_schema_context())
    misattributed = [
        table
        for table, description in _catalog(_TABLE_COMMENT_ORACLE)
        if rendered.get(table) != description
    ]
    assert not misattributed, (
        f"table comments missing from, or attached to the wrong table: {misattributed}"
    )


def _table_blocks(context: str) -> dict[str, str]:
    """Split the rendered context into one text block per table.

    Needed because attribution has to be checked within a table: `terminal_id` appears in
    both `terminals` and `cranes`, so a context-wide substring search cannot tell which
    column a comment is attached to.
    """
    blocks: dict[str, str] = {}
    current: str | None = None
    for line in context.splitlines():
        stripped = line.strip()
        if stripped.startswith("CREATE TABLE "):
            current = stripped[len("CREATE TABLE "):].split("(")[0].strip()
            blocks[current] = ""
        elif current is not None:
            if stripped.startswith(");"):
                current = None
            else:
                blocks[current] += line + "\n"
    return blocks


def test_every_column_comment_reaches_the_rendered_context() -> None:
    """The load-bearing one. If `schema.py` stopped selecting `col_description`, or the
    renderer stopped emitting it, generated SQL would still parse and still execute while
    silently losing every unit and enum definition. That failure is invisible to every
    other test in this suite.

    Attribution is checked, not mere presence. An earlier version asserted only
    `description in context`, which an audit correctly flagged as too weak: an off-by-one
    that attached `duration_minutes`'s comment to `container_count` would leave every
    string present somewhere and pass. Here each comment must appear on the line that
    declares its own column, inside its own table's block.
    """
    context = get_schema_context()
    blocks = _table_blocks(context)
    misattributed = []

    for table, column, description in _catalog(_COLUMN_COMMENT_ORACLE):
        block = blocks.get(table, "")
        on_own_line = any(
            line.strip().startswith(f"{column} ") and description in line
            for line in block.splitlines()
        )
        if not on_own_line:
            misattributed.append(f"{table}.{column}")

    assert not misattributed, (
        "column comments missing from, or not attached to, their own column: "
        f"{misattributed}"
    )


def test_units_and_enums_survive_into_the_prompt() -> None:
    """Spot-check the specific semantics that a column type cannot carry. These are the
    facts a wrong guess turns into a confidently wrong number rather than an error:
    hours vs minutes, and the closed set a status column accepts."""
    context = get_schema_context()
    assert "HOURS waited at anchor" in context          # port_calls.berth_wait_hours
    assert "MINUTES taken to complete the batch" in context  # cargo_moves.duration_minutes
    assert "One of: completed, cancelled" in context    # port_calls.status
    assert "One of: active, maintenance, retired" in context  # cranes.status


def test_port_name_is_disambiguated_from_terminal_name() -> None:
    """Regression guard for eval q19, which wrote `WHERE terminal_name = 'Jebel Ali'` and
    returned zero rows.

    Every terminal_name in this schema begins with its own port_name, so a bare port name
    is a plausible value for either column and nothing in the types resolves it. The only
    available fix was to state the relationship in the comments, which makes those two
    comment strings load-bearing for a measured accuracy score. Assert the relationship is
    still described rather than asserting the exact wording, so the sentences can be
    reworded without breaking this.
    """
    context = get_schema_context()
    assert "port_name" in context and "terminal_name" in context

    # The precondition that makes the ambiguity real. If it ever stops holding, this fix
    # is no longer needed and this test should be revisited rather than patched.
    rows = _catalog("SELECT terminal_name, port_name FROM terminals")
    assert all(terminal.startswith(port) for terminal, port in rows)

    port_comment = next(
        description
        for table, column, description in _catalog(_COLUMN_COMMENT_ORACLE)
        if (table, column) == ("terminals", "port_name")
    )
    assert "terminal_name" in port_comment, (
        "terminals.port_name must tell the model when NOT to use terminal_name"
    )
    assert "Jebel Ali" in port_comment, (
        "the ambiguous value itself should appear as an example port name"
    )


def test_three_of_the_five_model_prompts_carry_the_schema() -> None:
    """`docs/ARCHITECTURE.md` §4 says three of the five model-calling nodes interpolate the
    schema in full, and names them, which is the sentence that stops a reader believing the
    schema is what the strong tier is paying for.

    Asserted as a set rather than by checking the three named templates, so that a fourth
    prompt gaining `{schema}` fails here instead of quietly making the document wrong.
    """
    from src import prompts

    carriers = {
        name
        for name in dir(prompts)
        if name.endswith("_USER")
        and isinstance(getattr(prompts, name), str)
        and "{schema}" in getattr(prompts, name)
    }
    assert carriers == {"CLASSIFY_USER", "GENERATE_SQL_USER", "VERIFY_USER"}


def test_the_strong_tier_prompt_is_not_materially_larger_than_the_cheap_ones() -> None:
    """The claim in `docs/ARCHITECTURE.md` §4 is a relationship, not three digits.

    That section quotes rendered sizes measured on 2026-08-25 and concludes the three
    prompts sit within eight percent of each other, so the strong tier is not buying a
    bigger context. The digits move whenever anyone edits a prompt or a column comment,
    and pinning them would make every such edit fail here for no reason. The conclusion is
    what a reader relies on, so the conclusion is what this asserts.

    The bound is deliberately looser than the eight percent the document quotes. Eight is a
    measurement on a date; the claim is that no one prompt is materially larger than
    another. At eight the headroom is
    around a hundred characters, so a couple of added sentences would fail it while the
    claim stayed true. Fifteen still catches the regression worth catching: from today's
    spread it trips when one prompt gains roughly 800 characters, about a tenth of its
    length, which is a paragraph rather than a sentence.

    Growth in the schema itself cannot trip this: it is interpolated into all three
    templates, so it adds the same characters to each and moves the spread toward zero.
    """
    from src import prompts
    from src.schema import get_schema_context

    context = get_schema_context()
    question = "Which terminal has the longest average berth wait?"
    rendered = {
        "classify": len(prompts.CLASSIFY_SYSTEM)
        + len(prompts.CLASSIFY_USER.format(schema=context, question=question)),
        "generate_sql": len(prompts.GENERATE_SQL_SYSTEM)
        + len(prompts.GENERATE_SQL_USER.format(schema=context, question=question)),
        "verify": len(prompts.VERIFY_SYSTEM)
        + len(prompts.VERIFY_USER.format(schema=context, question=question, sql="SELECT 1")),
    }
    spread = (max(rendered.values()) - min(rendered.values())) / max(rendered.values())
    assert spread < 0.15, (
        f"one of these prompts has become materially larger than the others: {rendered}, "
        f"spread {spread:.1%}. docs/ARCHITECTURE.md §4 says they are comparable in size"
    )

