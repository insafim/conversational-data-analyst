"""Characterization test for the seed generator.

Written BEFORE refactoring `db/seed.py`, to pin its current output exactly. The
generator is the one component where characterization can be total rather than
approximate: it is deterministic by design (fixed RNG seed, fixed date window, no clock
reads), so every generated value is reproducible.

The digests below were captured from the pre-refactor generator. Any change to the
order in which random numbers are drawn — reordering two `RNG.choice` calls, extracting
a helper that consumes the RNG differently, hoisting a loop — shifts the entire stream
and moves these digests, even though row counts and every constraint would still look
correct.

That is precisely the failure this test exists to catch. It is invisible to every other
test in the suite: the schema still validates, the eval still runs, and the planted
patterns still broadly appear. What breaks silently is the *reproducibility guarantee*
the eval's reference SQL depends on (ADR-001, ADR-006).

Requires a seeded database. Run `python db/seed.py` first.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from src.config import settings

pytestmark = pytest.mark.integration

# (row_count, sha256-prefix of the full ordered dump) captured from seed=42.
EXPECTED: dict[str, tuple[int, str]] = {
    "terminals": (6, "4d2962a21c97ed17"),
    "vessels": (40, "8bc555e792a8f18c"),
    "cranes": (25, "857333f4674f4f18"),
    "port_calls": (1500, "d3fa01dd01e086bf"),
    "cargo_moves": (6577, "122798f8b7a0175a"),
}

_PRIMARY_KEY = {
    "terminals": "terminal_id",
    "vessels": "vessel_id",
    "cranes": "crane_id",
    "port_calls": "port_call_id",
    "cargo_moves": "move_id",
}


def _digest(table: str) -> tuple[int, str]:
    """Row count and a stable digest of the table's full contents, in primary-key order."""
    with psycopg.connect(settings.analyst_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY {_PRIMARY_KEY[table]}")
            hasher = hashlib.sha256()
            rows = 0
            for row in cur:
                hasher.update(repr(row).encode())
                rows += 1
    return rows, hasher.hexdigest()[:16]


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_generated_data_is_byte_identical(table: str) -> None:
    """Every generated value must be unchanged by refactoring.

    A digest mismatch with a matching row count means the data is *plausible but
    different* — the RNG draw order changed. Reference SQL in the eval set encodes
    literal expected values, so this silently invalidates the accuracy metric.
    """
    expected_rows, expected_digest = EXPECTED[table]
    actual_rows, actual_digest = _digest(table)

    assert actual_rows == expected_rows, (
        f"{table}: expected {expected_rows} rows, got {actual_rows}"
    )
    assert actual_digest == expected_digest, (
        f"{table}: contents changed (rows still {actual_rows}). The RNG draw order has "
        f"shifted, so the data is different but still plausible. Re-seed from a clean "
        f"database; if it still differs, the generator's behaviour was not preserved."
    )


def test_planted_patterns_survive() -> None:
    """The four planted patterns must remain detectable (ADR-001).

    Complements the digest check: digests prove the data is identical, this proves the
    data is still *useful*. If someone intentionally re-baselines the digests, this is
    the test that still fails when the signal has been destroyed.
    """
    with psycopg.connect(settings.analyst_dsn) as conn, conn.cursor() as cur:
        # 1. One congested terminal, clearly separated from the pack.
        cur.execute(
            "SELECT t.terminal_name, avg(pc.berth_wait_hours) "
            "FROM port_calls pc JOIN terminals t USING (terminal_id) "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        waits = cur.fetchall()
        assert waits[0][0] == "Jebel Ali Terminal 2"
        assert float(waits[0][1]) > 2 * float(waits[1][1]), "congestion outlier is gone"

        # 2. Seasonal shape: February trough below the autumn peak.
        cur.execute(
            "SELECT extract(month from move_ts)::int, sum(container_count) "
            "FROM cargo_moves WHERE move_ts >= '2025-01-01' AND move_ts < '2026-01-01' "
            "GROUP BY 1"
        )
        monthly = dict(cur.fetchall())
        assert monthly[2] < monthly[10], "seasonal shape is gone"

        # 3. The ageing crane is the least productive at its terminal.
        cur.execute(
            "SELECT c.crane_code "
            "FROM cargo_moves cm JOIN cranes c USING (crane_id) "
            "JOIN terminals t USING (terminal_id) WHERE t.port_name = 'Rotterdam' "
            "GROUP BY c.crane_code "
            "ORDER BY sum(cm.container_count) / (sum(cm.duration_minutes) / 60.0) ASC"
        )
        assert cur.fetchone()[0] == "RTM-QC-01", "ageing crane is no longer the laggard"

        # 4. The lagging operator waits longest.
        cur.execute(
            "SELECT v.operator FROM port_calls pc JOIN vessels v USING (vessel_id) "
            "GROUP BY 1 ORDER BY avg(pc.berth_wait_hours) DESC"
        )
        assert cur.fetchone()[0] == "Meridian Lines", "lagging operator pattern is gone"


def test_crane_and_port_call_terminals_always_match() -> None:
    """A crane can only service vessels berthed at its own terminal.

    This invariant is not enforced by any foreign key — a violation satisfies the schema
    perfectly and makes every crane-productivity answer silently wrong. It is the single
    most important property to preserve when restructuring the generator, because
    breaking it produces no error anywhere.
    """
    with psycopg.connect(settings.analyst_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM cargo_moves cm "
            "JOIN cranes c ON c.crane_id = cm.crane_id "
            "JOIN port_calls pc ON pc.port_call_id = cm.port_call_id "
            "WHERE c.terminal_id <> pc.terminal_id"
        )
        assert cur.fetchone()[0] == 0, "a crane serviced a vessel at another terminal"
