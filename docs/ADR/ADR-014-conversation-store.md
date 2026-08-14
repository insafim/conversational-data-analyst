# ADR-014: The Conversation Store Is A Separate Database

- **Status:** Accepted
- **Date:** 2026-08-12
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-003](ADR-003-schema-introspection.md), [ADR-004](ADR-004-defence-in-depth-sql.md), [ADR-008](ADR-008-ui-and-scope-boundary.md), [ADR-011](ADR-011-bounded-multi-turn.md)

## Context

Two things need persisting, and they turn out to be the same record. The observability
page needs each turn's latency, cost and outcome. Saved conversations need that same turn
in order to render again. Building two stores would be more code and would let the two
views disagree about what happened.

Where it lives is the interesting question, because this project already runs a
PostgreSQL database and the obvious move is to add a table to it.

## Decision

**A separate database, `ports_app`, owned by a role that has no business in the analytics
database.** Not a table in `ports`, not a schema inside it, and not SQLite.

The primary reason is the shape of a real deployment rather than anything local. In
production these are separate systems, for four independent reasons:

1. **Ownership.** The data being queried is usually someone else's: a warehouse, a read
   replica, a customer's database. The application often cannot create tables there at
   all, by policy.
2. **Lifecycle.** Application state migrates and deploys with the application. The
   analytics data is loaded by ETL on a different cadence, by a different team.
3. **Workload.** Analytics is big-scan OLAP; chat history is small-lookup OLTP. Different
   tuning, and often different engines entirely.
4. **Governance.** Chat history is user text, so it carries retention and deletion
   obligations the warehouse does not. "Delete my conversations" should never touch
   analytics data.

Modelling that separation here means the step to a separate server later is a connection
string, not a migration. `src/config.py` builds the store DSN by overriding one token.

### The local reinforcement

It is also the strongest isolation available inside one PostgreSQL instance. `analyst_ro`
holds no `CONNECT` on `ports_app`, so the agent's role cannot open a session there at all,
and PostgreSQL has no cross-database queries without FDW. The denial happens before a
query exists: nothing to validate, no schema to qualify, no `search_path` to escape.

That matters because of two lines in `db/02_roles.sql`:

```sql
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst_ro;
```

The second is deliberate: adding a table must not silently break the agent. It also means
history kept in `ports` would be readable by the agent the moment it was created, which
costs twice.

1. "What have other people asked?" becomes a question the agent answers correctly.
2. **The database the agent queries starts holding user-supplied text.** That is the
   second-order injection channel `src/prompts.py` documents and
   `tests/test_second_order_injection.py` polices. Today it is shut for a reason nobody
   had written down: every row in the analytics database is synthetic data this repository
   generated. Storing conversations there would open it as a side effect of a convenience
   feature.

### The line the whole thing turns on

PostgreSQL grants `CONNECT` on a new database to the `PUBLIC` pseudo-role, and every login
role inherits `PUBLIC`. Without an explicit revoke, `analyst_ro` could simply connect.

**Creating a separate database is not, by itself, an access control.**
`db/03_app_store.sql` revokes it, and
`tests/test_store_isolation.py::test_public_is_not_a_way_in_either` fails if that line is
ever removed.

## Alternatives considered

**A separate SQLite file.** Built first, then rejected on inspection. The arguments for it
are "no server" and "no infrastructure", and neither survives the observation that this
application cannot answer a single question without PostgreSQL: there is no state of the
world where the container is down and a working chat history helps. What it actually
bought was faster tests. What it cost was a second storage technology and a security
boundary with nothing to demonstrate, since an attack that cannot be attempted cannot be
shown to fail. It is also not what production application state looks like: SQLite cannot
serve two application instances, which is the first thing that changes at scale.

The argument for it was that the process then holds no write-capable database credential
at all. That property is real, and it is now gone. But it is not the property
[ADR-004](ADR-004-defence-in-depth-sql.md) claims. That claim is that *model-generated
SQL* cannot write, and it is untouched: generated SQL reaches a database only through
`src/executor.py` on the `analyst_ro` connection, and the model has no mechanism to choose
another.

**A separate schema inside `ports`.** Built second, then rejected. It isolates correctly,
since `ALTER DEFAULT PRIVILEGES` is scoped to `public` and a schema with no `USAGE` grant
is unreachable. But it is a weaker analogue of production, keeps both concerns in one
database with one backup and one lifecycle, and buys nothing the separate database does
not.

**Keeping history in `public` with targeted `REVOKE`s.** A denylist, re-applied for every
table added, fighting an `ALTER DEFAULT PRIVILEGES` rule that is actively granting.
Rejected: the safe version of this is the absence of a grant, not the presence of a revoke.

## Not built, deliberately

**Semantic recall over conversation history.** The natural implementation is `pgvector` in
this same database rather than a second service such as Qdrant: one connection, one backup,
one transaction boundary, and the embedding sits beside the row it describes instead of in
a system that can drift out of sync with it. A vector service earns its place at a scale
this will not reach, and it is a retrieval index rather than a system of record: no foreign
keys, no transactions, and no `GROUP BY` for the aggregates the observability page needs.

It is not built, and the reason is not only scope. Retrieving past **answer text** by
similarity would pull row data back into prompts on a fuzzy path, reopening precisely the
channel [ADR-011](ADR-011-bounded-multi-turn.md) closed by carrying question and SQL only.
Injection resistance is a property this system holds deliberately and verifies
([GUARDRAILS.md](../GUARDRAILS.md)), so this would trade a demonstrated strength for an
unrequested feature. If it is ever wanted, that
trade is the decision to make first, not the storage.

## Consequences

**Positive**

- The agent cannot reach saved conversations, proven by tests rather than asserted,
  including a control test confirming it can still read the business tables. Without that
  control, every denial would also pass against an empty database, which is exactly what
  happened once during development.
- The analytics database still contains only synthetic data, so this feature does not
  change the second-order injection surface.
- Telemetry is `jsonb`, so the observability page aggregates in SQL rather than loading
  every turn into Python to average one number.
- Records are span-shaped, so an OpenTelemetry exporter is a later adapter rather than a
  rewrite. That is why no tracing library is vendored in.

**Negative / accepted**

- The application process now holds a write-capable credential. Narrow and asserted: a
  compromise can write `ports_app`, and `app_rw` has no `CONNECT` on `ports`, which
  `tests/test_store_isolation.py` checks in that direction too.
- The store's tests need a running database, so they sit in the integration suite. The
  pure part stays fast in `tests/test_store_titles.py`, the same split used for
  `test_schema_labels.py`.
- `db/03_app_store.sql` runs only on the container's first boot, so an existing data
  volume needs `docker compose down -v`. The store detects the missing database and says
  so, naming the remedy.
- Chat history itself is not something the requirements ask for. The store is justified
  by the observability page, since latency is reported and had to move out of the chat
  pane; saved conversations are the same rows rendered differently, which is why they cost
  little more than the store already did.

## Three things learned by running it rather than reasoning about it

- `app_rw` holds no `CREATEDB`, so the application cannot create its own database, only
  its tables. That is the correct boundary and it is why the missing-database error names
  an operator remedy instead of attempting a privileged operation.
- Opening a connection per operation, copied from `src/executor.py`, exhausted the role's
  `CONNECTION LIMIT 10`: a probe at 24 concurrent appends lost 8. The store holds one
  connection under a lock instead.
- An internal exception escaping the transaction handler left a connection
  idle-in-transaction holding locks, and the next `DROP DATABASE` blocked forever. The
  symptom was a test suite that hung rather than failed, which is the expensive kind.
  Every exception now rolls back.

## Note for a multi-user deployment

Nothing here is multi-tenant. There is one implicit user, so "the agent cannot reach the
store" is the whole requirement. Real deployment needs identity first, then row-level
security on these tables keyed to it, which is the same precondition
[ADR-004](ADR-004-defence-in-depth-sql.md) names for the business data.
