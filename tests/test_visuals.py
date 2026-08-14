"""The presentation frame's schema claims, pinned to the DDL they claim to show.

`docs/visuals/data.html` gained a hover interaction: resting on a table reveals its
columns, types and key roles. That copied 37 column names out of `db/01_schema.sql` and
into a frame, which creates the one failure mode that matters for any figure copied onto
these boards: **the source changes and the frame does not.**

A stale row count is embarrassing. A stale column list is worse, because the frame is shown
to a reviewer as a description of the schema the model is handed at query time, and a column
that does not exist is a claim about the system that is simply false.

So the frame is not trusted to stay in step. This module parses the DDL and the frame and
asserts they agree on two things:

- **Columns.** Which exist, in what order, with what declared type, and which are primary or
  foreign keys.
- **Join paths.** Which pairs of tables are joined at all. A role label of `FK` says a column
  is a foreign key but not what it points at, so column parity alone would survive an FK
  being repointed at a different table. The frame states its targets in the `data-join`
  edges it draws, and those are what the fifth test compares.

The parsers take text rather than reading from disk, so the last test can feed them a
deliberately mismatched pair and prove the comparison actually bites. Without it, the only
evidence of sensitivity is a mutation someone ran once by hand and never again.

Not marked `integration`: it reads two files and touches no database, no model and no
network. It deliberately parses the DDL text rather than querying `information_schema`, so
it holds even when nothing is running.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "db" / "01_schema.sql"
FRAME_HTML = ROOT / "docs" / "visuals" / "data.html"

# A column definition line: exactly four spaces, a lowercase name, then a declared type.
# Comment lines (`    -- ...`) and constraint lines (`    CONSTRAINT ...`) do not match,
# and neither do the eight-space continuation lines of a generated column.
COLUMN_LINE = re.compile(
    r"^ {4}([a-z_]\w*) +([a-z]+(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)(.*)$",
    re.MULTILINE,
)
CREATE_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.DOTALL)
# `\s+` rather than a space: cranes puts REFERENCES on the following line. Only keys declared
# inside a CREATE TABLE body are found, which is every key this schema has. A relationship
# added later by `ALTER TABLE ... ADD CONSTRAINT` would be invisible here, so add it to this
# pattern rather than assuming it is covered.
FOREIGN_KEY = re.compile(r"FOREIGN KEY \((\w+)\)\s+REFERENCES (\w+)")

# The frame carries its columns as a JS literal, one entry per table, and its join paths as
# `data-join` attributes on the SVG groups that draw them.
TABLES_BLOCK = re.compile(r"const TABLES = \{(.*?)\n\};", re.DOTALL)
FRAME_TABLE = re.compile(r"(\w+): \{ x: \d+, accent: \"#\w+\", columns: \[(.*?)\]\}", re.DOTALL)
FRAME_COLUMN = re.compile(r"\[\"([a-z_]\w*)\", \"([^\"]+)\", \"([A-Z]*)\"\]")
FRAME_JOIN = re.compile(r"data-join=\"(\w+) (\w+)\"")

Columns = dict[str, list[tuple[str, str, str]]]


def _normalise(declared_type: str) -> str:
    """`numeric(10, 2)` in the DDL and `numeric(10,2)` on the frame are the same type."""
    return re.sub(r"\s+", "", declared_type)


def _columns_from_ddl(sql: str) -> Columns:
    tables: Columns = {}

    for table, body in CREATE_TABLE.findall(sql):
        referencing = {column for column, _ in FOREIGN_KEY.findall(body)}
        columns = []
        for name, declared_type, tail in COLUMN_LINE.findall(body):
            if "PRIMARY KEY" in tail:
                key = "PK"
            elif name in referencing:
                key = "FK"
            else:
                key = ""
            columns.append((name, _normalise(declared_type), key))
        tables[table] = columns

    return tables


def _columns_from_frame(html: str) -> Columns:
    block = TABLES_BLOCK.search(html)
    assert block is not None, "data.html no longer declares a TABLES literal"

    return {
        table: [
            (name, _normalise(declared_type), key)
            for name, declared_type, key in FRAME_COLUMN.findall(columns)
        ]
        for table, columns in FRAME_TABLE.findall(block.group(1))
    }


def _joins_from_ddl(sql: str) -> set[frozenset[str]]:
    joins = set()
    for table, body in CREATE_TABLE.findall(sql):
        for _, target in FOREIGN_KEY.findall(body):
            joins.add(frozenset({table, target}))
    return joins


def _joins_from_frame(html: str) -> set[frozenset[str]]:
    """Unordered pairs. The frame draws each arrow from the one side to the many side, which
    is the opposite order from the DDL's referencing-to-referenced, and the direction is
    already asserted by eye on the rendered frame rather than here."""
    return {frozenset(pair) for pair in FRAME_JOIN.findall(html)}


def _ddl() -> Columns:
    return _columns_from_ddl(SCHEMA_SQL.read_text(encoding="utf-8"))


def _frame() -> Columns:
    return _columns_from_frame(FRAME_HTML.read_text(encoding="utf-8"))


def test_frame_covers_every_table_in_the_schema() -> None:
    """Five tables in the DDL, five on the frame. A sixth would go unshown in silence."""
    assert set(_frame()) == set(_ddl())


@pytest.mark.parametrize("table", sorted(_ddl()))
def test_frame_columns_match_the_ddl(table: str) -> None:
    """Order, name, declared type and key role, per table, parametrised so the failure
    message names the table that drifted rather than dumping all five."""
    assert _frame()[table] == _ddl()[table]


def test_frame_draws_exactly_the_schemas_join_paths() -> None:
    """The gap column parity leaves open. `FK` says a column is a foreign key, not what it
    references, so repointing `cargo_moves.crane_id` at `terminals` would keep every column
    tuple identical while making the drawn `cranes` edge a lie. The edges are the frame's
    statement of the targets, so they are what gets compared."""
    assert _joins_from_frame(FRAME_HTML.read_text(encoding="utf-8")) == _joins_from_ddl(
        SCHEMA_SQL.read_text(encoding="utf-8")
    )


def test_both_parsers_found_real_content() -> None:
    """Guards the parsers themselves. Every assertion above compares regex output against
    regex output, so a pattern that silently matched nothing could make them pass on two
    empty lists. Pin the shape both sides are known to have: 37 columns, five primary keys,
    and five foreign keys, which is the same five join paths DATA.md documents."""
    for source, columns in (("ddl", _ddl()), ("frame", _frame())):
        keys = [key for table in columns.values() for _, _, key in table]
        assert sum(len(table) for table in columns.values()) == 37, source
        assert keys.count("PK") == 5, source
        assert keys.count("FK") == 5, source


SYNTHETIC_SQL = """
CREATE TABLE widgets (
    widget_id   integer  GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    gadget_id   integer  NOT NULL,
    label       text     NOT NULL,
    CONSTRAINT widgets_gadget_fk FOREIGN KEY (gadget_id) REFERENCES gadgets (gadget_id)
);
"""

# One document, because the real frame is one document: the drawn edges and the column
# literal live in the same file and are read by two independent patterns.
SYNTHETIC_FRAME = """
<g class="dimmable" data-join="widgets gadgets"></g>

const TABLES = {
  widgets: { x: 115, accent: "#6cb6ff", columns: [
    ["widget_id", "integer", "PK"],
    ["gadget_id", "integer", "FK"],
    ["label", "text", ""]
  ]}
};
"""


@pytest.mark.parametrize(
    ("mutation", "before", "after"),
    [
        ("dropped column", '["label", "text", ""]', ""),
        ("renamed column", '["label", "text", ""]', '["caption", "text", ""]'),
        ("drifted type", '["label", "text", ""]', '["label", "varchar(20)", ""]'),
        ("lost key role", '["widget_id", "integer", "PK"]', '["widget_id", "integer", ""]'),
        (
            "reordered columns",
            '["gadget_id", "integer", "FK"],\n    ["label", "text", ""]',
            '["label", "text", ""],\n    ["gadget_id", "integer", "FK"]',
        ),
    ],
)
def test_the_comparison_detects_drift(mutation: str, before: str, after: str) -> None:
    """Proof that the comparison bites, run every time rather than by hand once. The
    synthetic pair agrees to start with; each mutation breaks it in one of the five ways a
    real edit to `db/01_schema.sql` would, and each must produce a difference."""
    assert _columns_from_frame(SYNTHETIC_FRAME) == _columns_from_ddl(SYNTHETIC_SQL)

    drifted = SYNTHETIC_FRAME.replace(before, after)
    assert drifted != SYNTHETIC_FRAME, f"{mutation} did not change the fixture"
    assert _columns_from_frame(drifted) != _columns_from_ddl(SYNTHETIC_SQL), mutation


def test_the_join_comparison_detects_a_repointed_foreign_key() -> None:
    """The drift test_frame_draws_exactly_the_schemas_join_paths exists for: the column
    tuples are untouched, only the target moves. Mutated on each side in turn, because a
    comparison is only as sensitive as its less sensitive half."""
    assert _joins_from_frame(SYNTHETIC_FRAME) == _joins_from_ddl(SYNTHETIC_SQL)

    repointed_ddl = SYNTHETIC_SQL.replace("REFERENCES gadgets", "REFERENCES sprockets")
    assert _columns_from_ddl(repointed_ddl) == _columns_from_ddl(SYNTHETIC_SQL)
    assert _joins_from_ddl(repointed_ddl) != _joins_from_ddl(SYNTHETIC_SQL)

    redrawn_frame = SYNTHETIC_FRAME.replace(
        'data-join="widgets gadgets"', 'data-join="widgets sprockets"'
    )
    assert _columns_from_frame(redrawn_frame) == _columns_from_frame(SYNTHETIC_FRAME)
    assert _joins_from_frame(redrawn_frame) != _joins_from_frame(SYNTHETIC_FRAME)
