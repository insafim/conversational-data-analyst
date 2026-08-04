# ADR-003 — Schema Context by Startup Introspection

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-001](ADR-001-domain-and-data-model.md), [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)

## Context

To write correct SQL, the model must know the schema. "Schema handling" is named explicitly in the
brief's evaluation criteria, so *how* the model learns the schema is being assessed, not just
whether the resulting SQL runs.

There are three ways to supply it: paste it into the prompt as a literal, let the agent discover it
at query time with tools, or read it from the database and inject it.

## Decision

**Introspect the live database once at startup, serialise to annotated `CREATE TABLE`-style text,
cache it in process, and inject it into every SQL-generation prompt.**

The introspection reads `information_schema` and `pg_description` to recover, per table: columns
with types and nullability, primary keys, foreign-key relationships, and — importantly — the
`COMMENT ON` text attached to tables and columns.

Those comments are not decoration. They are written for the model as reader, and they carry the
things a type signature cannot: units (`berth_wait_hours` is hours, `capacity_teu` is TEU,
`duration_minutes` is minutes), permitted enum values (`move_type` is `load` or `discharge`), the
grain of each fact table, and the semantic difference between `arrival_ts`, `berth_ts` and
`departure_ts` — which is precisely the distinction a berth-wait question turns on. **The database
is the single source of truth for schema semantics, and the documentation lives next to the data
it describes rather than in a prompt file that drifts.**

Alongside the schema, the introspection captures the dataset's actual min/max timestamps and
injects them as context, so the model resolves relative phrasing ("last quarter") against the data
rather than the system clock — see [ADR-001](ADR-001-domain-and-data-model.md) on the fixed date
window.

## Why not the alternatives

**Hard-coded schema string in a prompt file.** Rejected. It is the fastest thing to write and the
first thing to rot: the day someone adds a column, the agent starts producing confidently wrong SQL
with no error and no signal. It also makes the system a demo of *this* database rather than a
system that works against a database. The portability argument matters commercially — introspection
means pointing this at a different PostgreSQL database is a connection-string change, not a code
change.

**Per-query agentic schema discovery** (give the model `list_tables` and `describe_table` tools and
let it explore). Rejected, and the reasoning generalises:

- It costs 2–4 additional sequential LLM round trips *per question*, on a system where latency is
  an assessed dimension, to rediscover information that has not changed since startup.
- It reintroduces model-controlled flow into a graph deliberately built to avoid it
  ([ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)).
- At five tables the entire schema is roughly 600–900 tokens. Retrieval is a technique for when
  context is scarce. Here it is not scarce, so discovery buys nothing and costs latency, money, and
  determinism.

The general principle, which is the part worth stating in an interview: **retrieval earns its place
when the schema no longer fits in context, not before.** Knowing when *not* to reach for RAG is the
same skill as knowing when to.

## Where this breaks, and what replaces it

This decision is correct at 5 tables and wrong at 500. The threshold is roughly where full schema
text stops fitting comfortably alongside the question and few-shot examples, or where irrelevant
tables start actively degrading SQL accuracy through distraction.

The production path, stated so the boundary is explicit rather than discovered later:

1. **Retrieval over table metadata** — embed table and column descriptions, retrieve the top-k
   relevant tables per question, inject only those. This is where RAG genuinely earns its place.
2. **A curated semantic layer** — a hand-maintained subset of tables and named metric definitions,
   so "revenue" resolves to one agreed expression rather than whatever the model infers.
3. **Cache invalidation** — startup-only caching assumes DDL does not change while the process
   runs. Correct for a demo; production needs a TTL or a migration-triggered refresh.

## Consequences

**Positive**

- Portable to any PostgreSQL database without code changes.
- Schema documentation lives in the database, next to the data, and cannot silently diverge.
- Zero per-query latency cost; the schema string is built once.
- Column comments give the model unit and enum information that types alone cannot express, which
  measurably reduces a whole class of SQL errors.

**Negative / accepted**

- Cached at startup, so DDL changes require a restart. Acceptable for this scope, named above.
- Full schema injection scales linearly with table count and stops being viable in the hundreds.
  Named above with the replacement design.
- Introspection quality depends on the schema actually being commented. Mitigated by treating
  `COMMENT ON` coverage as a requirement of the schema DDL, not an optional nicety.
