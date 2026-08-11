# ADR-009: Withheld Runtime Capabilities

- **Status:** Accepted
- **Date:** 2026-08-10
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-003](ADR-003-schema-introspection.md), [ADR-004](ADR-004-defence-in-depth-sql.md), [ADR-006](ADR-006-eval-execution-accuracy.md), [ADR-007](ADR-007-llm-provider-and-tiering.md)

## Context

A design review on 2026-08-09 and 2026-08-10 examined four additions that text-to-SQL
systems commonly acquire: PostgreSQL documentation in the prompt or behind retrieval, a
web search capability, a database tool surface such as an MCP server, and an LLM stage
inside the validator. All four were rejected. This record exists so that each absence
reads as a decision with evidence behind it rather than as an omission, the same posture
`db/02_roles.sql:46-48` takes toward privileges: absence is the point.

## The evidence base

The artifact set as of 2026-08-10 is `eval/results/run1.json` through `run14.json`. The
gold set grew during the series: runs 1 to 3 scored 30 cases, run 4 scored 33, runs 5
through 14 scored 36. Pass rates ran 86.7% to 97.2% across runs 1 to 7 and 36/36 on every
run from 8 through 14.

Fourteen items failed across the series. Every one is classified below from the recorded
`detail` and `answer` fields.

| Run | Item | Class | What the artifact records |
| --- | --- | --- | --- |
| run1 | q05 | semantic | 6 rows returned where the reference returns 1 (singular superlative without LIMIT 1) |
| run1 | q14 | semantic | same entity, different count: 51 vs 53 |
| run1 | q21 | semantic | rows disagree in order and values |
| run2 | q09 | semantic | extra column `port_name` beside the requested column |
| run3 | q09 | semantic | extra column, as run2 |
| run3 | q15 | semantic | 0 rows returned where the reference returns 18 |
| run3 | q22 | transport | `litellm.InternalServerError`, server disconnected before responding |
| run3 | a01 | transport | same provider failure, before any SQL existed |
| run4 | q09 | semantic | extra column, as run2 |
| run4 | q23 | semantic | capacity banded into 5 buckets where the reference returns 40 rows |
| run5 | q09 | semantic | extra column, as run2 |
| run5 | q27 | semantic | column count differs: 2 columns returned where the reference has 3, the monthly-total column missing |
| run6 | q19 | semantic | 0 rows returned where the reference returns 3 |
| run7 | q28 | semantic | value mismatch on a LAG column |

Twelve semantic mismatches, two provider transport failures, zero syntax or dialect
errors in any final outcome. One caveat is owed: four items, all q26 (run5, run12,
run13, run14), record `retried: true`, meaning the first attempt failed inside the
database and the single retry recovered it. The triggering error text is not persisted
in the artifacts, so those first attempts cannot be classified from the record. What the
record does show is that the retry, which feeds the PostgreSQL error back verbatim
(`src/prompts.py:120-132`), converted all four.

## Decisions

### 1. No PostgreSQL documentation, static or retrieved

Rejected. The failure class it would address, the model not knowing the dialect, has
zero instances in the artifact set. The dialect guidance this workload needed reduces to
two prompt rules, the `date_trunc(...)::date` preference at `src/prompts.py:81-82` and
the rounding rule at `:88`, plus the schema comments that [ADR-003](ADR-003-schema-introspection.md)
injects. A retrieval step would add a sequential call to the segment that already
dominates latency: [ADR-001](ADR-001-domain-and-data-model.md) records 2.1 ms of
planning and 1.2 ms of execution for the hardest gold query (ADR-001:120-121), inside a
loop whose per-question median ran 6.0 s to 6.9 s across runs 12 to 14. When generation does fail in the database, the retry already delivers the server's
own error message, which is dialect documentation scoped to the exact failure.

Revisit condition: a target dialect that is under-represented in model training data
(DuckDB, ClickHouse, extension surfaces such as pgvector or TimescaleDB), or permitting
extension functions. The remedy at that point is a short curated function reference in
the system prompt, which costs a fixed token count and no extra call, not retrieval over
a manual.

### 2. No web search

Rejected. The dependency list (`pyproject.toml:11-27`) contains no HTTP client; the
process egresses only to the LLM provider through litellm and to the database through
psycopg. Search would break that on four fronts. The grounding contract at
`src/prompts.py:141-159` requires every stated figure to appear in the returned rows,
and an externally fetched figure cannot satisfy it. The classifier already has a correct
behaviour for questions the database cannot answer: route `out_of_scope` and say so
(`src/prompts.py:36-37`); search converts that honest refusal into an unverifiable
answer. Search results are a third untrusted input, and unlike stored data, whose
injection risk `src/prompts.py:16-21` already documents and defends, they are
influenceable by anyone who can rank a page. And egress makes exfiltration possible
without any write privilege, a path the GRANT-based security claim
(`db/02_roles.sql:49-51`) cannot see.

Search-shaped work belongs at build time, as the existing citation discipline: a claim,
a source URL, and a verification date, as at `pyproject.toml:24`.

### 3. No database MCP surface

Rejected. An MCP server for PostgreSQL presents query and schema-inspection tools to the
model, which is the `run_sql` plus `inspect_schema` topology that
[ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md) rejects, combined with the
per-query schema discovery that [ADR-003](ADR-003-schema-introspection.md) rejects (2 to
4 additional sequential calls to rediscover roughly 1,500 tokens of schema that has not
changed since startup). The structural cost is specific: `validate` sits on the only
edge into `execute` (`src/agent.py:323-327`), so no SQL reaches the database without
passing it. Tool-mediated access moves the decision to query into the model. The
database grants would still hold, but the validator's stated guarantee, that no
statement passing it can modify anything, would stop being true of the system as wired,
the distinction `src/validator.py:95-98` records.

A compatible inversion exists and reverses nothing: exposing this agent as a tool to MCP
clients, so external assistants gain a validated, read-only, bounded query capability.
Noted as an integration option, not scheduled.

### 4. No LLM stage inside the validator

Rejected. The `validate` node is pure code because a safety boundary must not be a
prompt ([ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), node table). Adding a
model opinion to it has no good branch. If code overrides the model on disagreement, the
call is spent for nothing. If the model can approve what code rejects, the boundary is a
prompt again. If the model can only reject what code accepts, identical input starts
failing non-deterministically across runs, to defend a property that is already
decidable by parsing: `validate_sql` (`src/validator.py:153-154`) accepts exactly one
read-only SELECT-family statement, checked over sqlglot's tree. A probabilistic layer
cannot strengthen a decidable check.

Defence in depth is also not served. The three layers of
[ADR-004](ADR-004-defence-in-depth-sql.md) fail by different instruments: instructions,
a parser, and privileges. A second model-judged layer fails to the same instrument as
the first, so it adds surface without adding independence.

The genuine residual, whether the SQL answers the question asked, is a quality property,
and it is handled where it can be checked: offline, by executing agent SQL and
hand-verified reference SQL and comparing result sets (`eval/run_eval.py:112`,
[ADR-006](ADR-006-eval-execution-accuracy.md)), and at runtime by showing the SQL to the
reader, which `src/prompts.py:177` assumes when it instructs the summary not to describe
the SQL.

> **Superseded in part, same day, by [ADR-012](ADR-012-runtime-verification.md).** The
> "shown to the reader" half of the residual argument fails against the product's own
> persona: ADR-008 defines the user as a non-technical operations manager who cannot
> read SQL. ADR-012 adds an advisory, retry-bounded, fail-open semantic verifier for the
> correspondence property. What this section rejects remains rejected: no LLM holds
> approval authority over safety, and `validate` stays pure code on the only edge into
> `execute`.

## Deferred, not rejected: quality-triggered regeneration

One proposal from the same review is deferred rather than declined. The existing bounded
retry (one iteration, owned by the graph, `tests/test_agent_routing.py:174`) currently
fires only on a database error. Three code-detectable suspicion signals could also feed
it, at zero added cost on the happy path:

- An empty result set on a question classified `answerable`. Historical instances: run3
  q15 (0 rows vs 18), run6 q19 (0 rows vs 3). The graph currently reports empty results
  without consulting the model, asserted by `tests/test_agent_routing.py:227`.
- A row count contradicting the question's grammar. Historical instance: run1 q05, 6
  rows for a singular superlative; the rule the result violated is written at
  `src/prompts.py:89-94`.
- A result that saturates the row cap (`ROW_CAP`, default 500, `src/config.py`), which
  suggests a missing aggregation.

Deferral reasons, both factual: the current gold set has been 36/36 for runs 8 through
14, so the triggers have no live instances to measure against until the eval set
expansion of 2026-08-10 lands; and the results schema records `retried` as a single
boolean, so a quality retry would be indistinguishable from a database-error retry in
run comparisons until the artifact schema is extended.

## Consequences

- Each withheld capability now has a recorded reason and a revisit condition. A future
  change that adds one should supersede this ADR rather than accrete quietly.
- The evidence classification above is reproducible from the checked-in artifacts;
  nothing in it depends on recollection.
- A measurement gap is acknowledged: every item record in `eval/results/run*.json`
  carries one scalar `cost_usd`, and the per-node `stage_timings` field, present from
  `run7.json` onward, has no cost counterpart. Cost is therefore per question, not per
  node, and the tier split of [ADR-007](ADR-007-llm-provider-and-tiering.md) is
  justified by task shape but not yet measured per node. Harness work, noted for the
  eval expansion.
- None of this constrains offline tooling. The eval harness, seed characterisation, and
  build-time research may use anything; the boundary drawn here is the runtime request
  path.
