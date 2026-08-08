# ADR-001 — Domain and Data Model

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-006](ADR-006-eval-execution-accuracy.md), [ADR-003](ADR-003-schema-introspection.md)

## Context

The brief requires a database with "a minimum of 4 tables that require joins to answer
questions", using input data of our choice, synthetic where appropriate. The dataset is not
incidental: it determines whether the demo produces *findings* or *noise*, and it sets the
ceiling on how interesting the natural-language questions can be.

Three constraints shaped the choice:

1. **Join depth must be inherent, not contrived.** If the interesting questions can be answered
   from one table, the exercise tests nothing. Join requirements should fall out of the domain's
   natural shape.
2. **The audience is a consulting firm.** A domain that resembles their client work makes every
   demo query sound like a real client question rather than a toy.
3. **Results must be reproducible.** The eval harness compares result sets against reference SQL.
   Any non-determinism in the data makes the pass rate meaningless.

## Decision

**Domain: port and terminal operations** — vessels, terminals, cranes, port calls, and container
moves. Five tables, synthetic data, fixed-seed generation.

### Schema

```
terminals    (terminal_id PK, terminal_name, port_name, country, berth_count, opened_year)
vessels      (vessel_id PK, vessel_name, imo_number, vessel_type, capacity_teu,
              operator, flag_country, year_built)
cranes       (crane_id PK, terminal_id FK→terminals, crane_code, model,
              commissioned_date, max_lift_tonnes, status)
port_calls   (port_call_id PK, vessel_id FK→vessels, terminal_id FK→terminals,
              arrival_ts, berth_ts, departure_ts, berth_wait_hours GENERATED, status)
cargo_moves  (move_id PK, port_call_id FK→port_calls, crane_id FK→cranes,
              move_type, container_count, move_ts, duration_minutes)
```

Two dimension tables (`vessels`, `terminals`), one equipment dimension (`cranes`), and two fact
tables at different grains (`port_calls` per visit, `cargo_moves` per crane operation). This is a
recognisable star-ish shape, which is what a real analytics database looks like.

### Join depth is structural

| Question | Tables joined |
| --- | --- |
| "Average berth wait by terminal" | 2 |
| "Container moves per month by terminal" | 3 |
| "Crane productivity by crane model and port" | 3 |
| "Top shipping operators by containers handled at Jebel Ali" | 4 |

The last one traverses `cargo_moves → port_calls → vessels` and `port_calls → terminals`. Four
tables in one query, arising naturally from the question rather than from artificial schema
splitting.

### Planted signal

Random data produces flat aggregates and boring answers. The generator deliberately injects four
patterns so that the agent's answers are findings:

1. **One congested terminal** — systematically higher berth waits than its peers.
2. **A seasonal peak** — container volumes rise into the pre-holiday shipping season and dip in
   February, so time-series questions produce a visible shape.
3. **One ageing crane** — commissioned early, measurably lower moves per hour, more time in
   maintenance status.
4. **One underperforming operator** — arrives outside its booked window more often, and pays for
   it in berth wait.

Each pattern is discoverable by a natural question, which is what makes the demo land.

### Fixed date window, not relative-to-now

Data spans a **fixed** window (2025-01-01 to 2026-06-30) rather than "the last 18 months from
today". Rationale: reference SQL in the eval set contains literal date predicates. If the data
window moved with the wall clock, every gold query would silently drift out of range and the
measured accuracy would decay over time for reasons unrelated to the agent.

The consequence is that relative phrasing ("last quarter") cannot be resolved from the system
clock. The dataset's actual date range is therefore injected into the SQL-generation prompt as
context, so the model resolves relative dates against *the data*, not against today. This is a
real NL2SQL problem, not an artefact of using synthetic data — production systems face the same
question the moment a user says "recently".

## Alternatives considered

**A generic e-commerce schema (customers/orders/products/order_items).** Rejected. It is the
default choice, it is over-represented in LLM training data — which inflates apparent SQL accuracy
and makes the eval flattering rather than informative — and it says nothing about the domain the
audience works in.

**A public real-world dataset.** Rejected. Adds download and licensing friction to a repo whose
README promises a two-command start, and real data cannot have signal deliberately planted, so the
demo becomes hostage to whatever the data happens to contain.

**More tables (8–10) for a richer schema.** Rejected as overbuilding. Five tables already force
four-table joins. Additional tables would increase prompt size and SQL error surface without
testing anything the brief asks about.

**Fewer tables (the minimum 4).** Rejected. It meets the letter of the brief with no margin. Five
gives one more join path at negligible cost.

## Where this model stops holding

[ADR-003](ADR-003-schema-introspection.md) records the point at which its own decision expires:
injecting the full schema is correct at five tables and wrong at five hundred, with the threshold
falling either where the schema text stops fitting alongside the question or where irrelevant
tables start degrading SQL accuracy by distraction. The data model deserves the same treatment,
since five tables holding 1,500 port calls and 6,577 cargo moves on single-node PostgreSQL are a
choice rather than a default. It has two expiry conditions, and they are reached independently.

**Interactive latency.** `pg_database_size` reports about 9,600 kB for the seeded database, and
`pg_total_relation_size`, summed across the five user tables, totals 1,680 kB, against the 16,384
8 kB buffers (128 MB) this container runs with, as reported by `pg_settings`. The working set is
therefore resident in the buffer pool with close to eighty times headroom, and it shows in the
plans: `EXPLAIN (ANALYZE, BUFFERS)` on `q19`, the four-table gold query, reports every node served
from `shared hit` with no `read`, at 2.1 ms planning and 1.2 ms execution. That, rather than index
design, is why query time is not what makes the loop take 5.9 to 12.0 seconds, the mean latency range
recorded across the six eval runs in the README; the three LLM calls are.

Index design is doing less work here than eight indexes suggests. Fifteen gold questions carry a
`WHERE` clause. Ten of them filter on an unindexed column: `terminals.port_name` (`q06`, `q08`,
`q15`, `q19`) and `terminals.country` (`q09`, `q25`) across 6 terminals, `port_calls.status`
(`q16`, `q20`) and `port_calls.remarks` (`q24`) across 1,500 port calls, and `cranes.status`
(`q11`) across 25 cranes. None of those five columns is indexed, so every one of those predicates
resolves by sequential scan. The other five filter only on indexed columns: `cargo_moves.move_ts`
(`q04`, `q27`), `port_calls.arrival_ts` (`q21`, `q26`), and `q28` on `terminals.terminal_name` and
`cargo_moves.move_ts` together. `terminal_name` is the one to be careful about, since it carries an
index only as a side effect of its `UNIQUE` constraint rather than from any of the eight.

Index *usage* is deliberately not quoted here. `pg_stat_user_indexes` is cumulative since the last
statistics reset, so it depends on what has been run against a particular volume, and re-creating
the container resets it. Any figure for it would be a fact about one machine on one afternoon
rather than a property of the design. Which columns carry an index is the durable claim, and it is
readable from `db/01_schema.sql` without running anything.

The expiry condition is therefore measurable rather than rhetorical: it arrives when the working
set stops fitting in the buffer pool, sequential scans over the dimensions stop being free, and the
aggregate scans behind a question like
"container moves per month by terminal" stop being cache-resident. Those queries scan far more rows
than they return, which is the access pattern columnar layout and partition pruning are built for
and which a larger instance does not change, so the answer at that point is columnar storage with a
query engine over it. The change reaches the agent rather than only the database. The SQL dialect
in the prompt changes, sub-second interactivity is gone so the UI has to surface progress and
per-query cost, and on an engine that bills per byte scanned, partition pruning becomes
correctness-adjacent, because a query missing its partition filter is an invoice rather than a slow
response.

**Ingestion.** The tables are populated by a single run of `db/seed.py`, including the post-hoc
`UPDATE` that plants the `remarks` values, and nothing changes them after that run completes. Real
terminal data arrives continuously from vessel traffic systems and crane telemetry, at which point
the engineering problem moves upstream of everything in this repo: batch or streaming pipelines,
schema evolution, late-arriving and corrected records, and data quality gates that must pass before
the agent is permitted to answer from a table at all. `db/seed.py` is replaced at that point rather
than extended, and the fixed-window reproducibility argument above has to be re-established against
a dataset that moves.

Both thresholds are a long way from where this prototype sits, and building for either one here
would be the overbuilding the brief warns against. They are recorded because the boundary of a
design is part of the design, and because the first question asked of any prototype is what it
costs to make it real.

## Consequences

**Positive**

- Join depth is inherent, so SQL generation is genuinely exercised.
- Planted signal means demo answers are interesting, and the charts have visible shape.
- Fixed seed plus fixed date window makes the eval pass rate reproducible across machines and
  across time.
- Domain resonance with the audience is free.

**Negative / accepted**

- Synthetic data cannot demonstrate handling of real-world dirt (nulls, duplicates, inconsistent
  encodings). Accepted: the brief explicitly permits synthetic data, and data cleaning is not on
  the scored list.
- The fixed window means the demo must be framed in terms of the data's own range. Handled by
  injecting the range into the prompt and surfacing it in the UI.
- Planted signal is a form of demo staging. Mitigated by keeping the patterns statistical rather
  than hard-coded — they are distributions, not scripted answers, and the agent has no knowledge
  of them.
