-- =============================================================================
-- Seed verification: prove the data has SIGNAL before building anything on it.
--
-- Random data produces flat aggregates, and against flat data a working agent and a
-- broken one look identical — every answer is "they're all about the same". So the
-- generator deliberately plants four patterns (ADR-001 §"Planted signal"), and this
-- file is how you confirm they actually survived generation and are detectable by
-- ordinary SQL.
--
-- Run after seeding:
--   docker exec -e PGPASSWORD=postgres cda_postgres \
--     psql -U postgres -d ports -f /dev/stdin < db/verify_seed.sql
--
-- Or from the host, if you have psql:
--   psql "postgresql://postgres:postgres@localhost:55432/ports" -f db/verify_seed.sql
--
-- Expected values below are from seed=42. They are exact, not approximate: the
-- generator is deterministic, so if these numbers differ, something has changed.
-- =============================================================================

\echo '=== 0. Row counts (expect 6 / 40 / 25 / 1500 / 6577) ==='
SELECT 'terminals'   AS table_name, count(*) FROM terminals
UNION ALL SELECT 'vessels',     count(*) FROM vessels
UNION ALL SELECT 'cranes',      count(*) FROM cranes
UNION ALL SELECT 'port_calls',  count(*) FROM port_calls
UNION ALL SELECT 'cargo_moves', count(*) FROM cargo_moves;


\echo ''
\echo '=== 1. CONGESTED TERMINAL — expect Jebel Ali Terminal 2 at ~17.5h, others ~5.6-5.9h ==='
-- A single clear outlier, roughly 3x its peers. If every terminal is within a
-- hour of the others, the congestion pattern did not survive generation and the
-- demo question "which terminal has the longest berth wait?" has no interesting answer.
SELECT t.terminal_name,
       round(avg(pc.berth_wait_hours), 2) AS avg_berth_wait_hours,
       count(*)                           AS port_calls
FROM port_calls pc
JOIN terminals t ON t.terminal_id = pc.terminal_id
GROUP BY t.terminal_name
ORDER BY avg_berth_wait_hours DESC;


\echo ''
\echo '=== 2. SEASONAL PEAK — expect Feb trough (~10.2k) and an autumn peak (Oct ~29.5k) ==='
-- The shape matters more than the absolute values: volume should dip in February and
-- rise into the pre-holiday shipping season. A flat line here means time-series
-- questions produce a boring chart with nothing to point at on camera.
SELECT to_char(cm.move_ts, 'YYYY-MM')  AS month,
       sum(cm.container_count)         AS containers
FROM cargo_moves cm
WHERE cm.move_ts >= DATE '2025-01-01'
  AND cm.move_ts <  DATE '2026-01-01'
GROUP BY 1
ORDER BY 1;


\echo ''
\echo '=== 3. AGEING CRANE — expect RTM-QC-01 (commissioned 2001) at ~17 moves/hr vs ~28 ==='
-- One crane should be clearly the laggard, and it should be the oldest one. If the
-- worst performer is a 2023 crane, the productivity story is noise rather than signal.
SELECT c.crane_code,
       c.commissioned_date,
       c.status,
       round(sum(cm.container_count) / (sum(cm.duration_minutes) / 60.0), 1)
           AS containers_per_hour
FROM cargo_moves cm
JOIN cranes c    ON c.crane_id    = cm.crane_id
JOIN terminals t ON t.terminal_id = c.terminal_id
WHERE t.port_name = 'Rotterdam'
GROUP BY c.crane_code, c.commissioned_date, c.status
ORDER BY containers_per_hour ASC;


\echo ''
\echo '=== 4. LAGGING OPERATOR — expect Meridian Lines ~12.8h vs ~6.1-6.9h for the rest ==='
-- Note this is a different question from pattern 1: that one is about WHERE a vessel
-- waits, this is about WHOSE vessel waits. Both should show an outlier, and they should
-- be independent of each other.
SELECT v.operator,
       round(avg(pc.berth_wait_hours), 2) AS avg_berth_wait_hours,
       count(*)                           AS port_calls
FROM port_calls pc
JOIN vessels v ON v.vessel_id = pc.vessel_id
GROUP BY v.operator
ORDER BY avg_berth_wait_hours DESC;


\echo ''
\echo '=== 5. REFERENTIAL REALISM — expect ZERO rows ==='
-- A crane belongs to exactly one terminal and can only work vessels berthed there.
-- A violation would not raise an error: the foreign keys would all still be satisfied,
-- and every "crane productivity by terminal" query would silently return nonsense
-- while appearing to work. That is the worst class of data bug, because the eval
-- would pass. Any row here is a generator defect.
SELECT cm.move_id, c.crane_code, c.terminal_id AS crane_terminal,
       pc.terminal_id AS call_terminal
FROM cargo_moves cm
JOIN cranes c     ON c.crane_id     = cm.crane_id
JOIN port_calls pc ON pc.port_call_id = cm.port_call_id
WHERE c.terminal_id <> pc.terminal_id;


\echo ''
\echo '=== 6. NULL HANDLING — expect ~45 cancelled calls, all with NULL berth/departure ==='
-- Cancelled calls exercise NULL handling in aggregates: AVG(berth_wait_hours) must
-- ignore them rather than treating them as zero, which would drag every average down.
SELECT status,
       count(*)                                          AS calls,
       count(berth_ts)                                   AS with_berth_ts,
       count(berth_wait_hours)                           AS with_wait_hours
FROM port_calls
GROUP BY status
ORDER BY status;


\echo ''
\echo '=== 7. FOUR-TABLE JOIN — the hardest query shape the agent must produce ==='
-- cargo_moves -> port_calls -> vessels, plus port_calls -> terminals. If this returns
-- rows, the schema genuinely requires multi-table joins to answer real questions,
-- which is the brief's stated requirement.
SELECT v.operator,
       sum(cm.container_count) AS containers
FROM cargo_moves cm
JOIN port_calls pc ON pc.port_call_id = cm.port_call_id
JOIN vessels v     ON v.vessel_id     = pc.vessel_id
JOIN terminals t   ON t.terminal_id   = pc.terminal_id
WHERE t.port_name = 'Jebel Ali'
GROUP BY v.operator
ORDER BY containers DESC;


\echo ''
\echo '=== 8. DATE WINDOW — expect exactly 2025-01-01 .. 2026-06-30 ==='
-- The window is fixed rather than relative to today, so reference SQL in the eval set
-- keeps working over time (ADR-001). If this drifts, the gold queries silently start
-- querying an empty range and accuracy decays for reasons unrelated to the agent.
SELECT min(arrival_ts)::date AS first_arrival,
       max(arrival_ts)::date AS last_arrival
FROM port_calls;
