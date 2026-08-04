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
