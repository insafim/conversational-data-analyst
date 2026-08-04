# Conversational Data Analyst

Ask questions about port and terminal operations in plain English. The agent translates the
question to SQL, checks it, runs it read-only against PostgreSQL, answers in natural language, and
picks a chart when one helps.

Built as a take-home exercise. Three things it tries to do properly rather than broadly:

- **Safety is structural, not prompted.** The agent connects as a PostgreSQL role holding `SELECT`
  and nothing else. A fully jailbroken model still cannot write — [verified, not
  asserted](#guardrails-verified-not-asserted).
- **Correctness is measured, not claimed.** A gold set of 30 questions with hand-verified reference
  SQL produces a reproducible accuracy number, including for refusals and ambiguity.
- **Scope is controlled on purpose.** [What was left out, and
  why](docs/ADR/ADR-008-ui-and-scope-boundary.md).

> **Going deeper:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the full architecture
> reference — pipeline internals, the security model, schema handling, the evaluation
> method, and a consolidated table of design decisions with their trade-offs.
> [docs/ADR/](docs/ADR/) holds the eight decision records behind them.

---

## Quickstart

Requires Docker and Python 3.12+.

```bash
# 1. Configure — paste an API key into .env
cp .env.example .env

# 2. Database (PostgreSQL 18 on port 55432, so it won't collide with a local one)
docker compose up -d

# 3. Install and seed
uv venv --python 3.12 && uv pip install -e ".[dev]"
python db/seed.py

# 4. Run
streamlit run app.py
```

Then, to reproduce the numbers below:

```bash
python eval/run_eval.py
pytest -m "not integration"   # unit tests, no database needed
pytest                        # everything, needs the seeded database
```

Any LiteLLM-supported provider works — set `MODEL_CHEAP` / `MODEL_STRONG` in `.env`. Defaults are
Anthropic.

---

## The data

Synthetic port and terminal operations, five tables, deterministic (fixed RNG seed and a fixed
date window, so the eval is reproducible across machines and over time).

```
terminals ──< cranes ──────< cargo_moves >── port_calls >── vessels
     └────────────────< port_calls
```

| Table | Rows | Grain |
| --- | --- | --- |
| `terminals` | 6 | one per terminal |
| `vessels` | 40 | one per ship |
| `cranes` | 25 | one per quay crane |
| `port_calls` | 1,500 | one per vessel visit |
| `cargo_moves` | 6,577 | one per crane move batch |

Join depth is inherent rather than contrived: *"which three operators moved the most containers at
Jebel Ali?"* traverses four tables. Four patterns are deliberately planted into the distributions —
a congested terminal, a seasonal volume peak, an ageing low-throughput crane, and an operator with
poor punctuality — so answers are findings rather than noise.
([ADR-001](docs/ADR/ADR-001-domain-and-data-model.md))

---

## How it works

```
classify ──ambiguous─────► clarify   (END: clarifying question back to user)
   │     └─out_of_scope──► refuse    (END: refusal + reason)
   │ answerable
   ▼
generate_sql ◄───────────────────────────────┐
   │                                          │ retry, max 1
   ▼                                          │ (Postgres error in context)
validate ──fail──► reject (END: refusal)      │
   │ pass                                     │
   ▼                                          │
execute ──db error────────────────────────────┘
   │ rows
   ▼
summarize ──► pick_chart ──► END
```

| Node | Implementation | Why |
| --- | --- | --- |
| `classify` | LLM (cheap tier) | Intent judgement is genuinely linguistic |
| `generate_sql` | LLM (strong tier) | The one task where capability changes the answer |
| `validate` | **code** | A safety boundary must not be a prompt |
| `execute` | **code** | Connection policy, timeout, row cap |
| `summarize` | LLM (cheap tier) | Grounded phrasing of returned rows |
| `pick_chart` | **code** | Deterministic and unit-testable |

**Three LLM calls per question** (four if the retry fires); everything else is code. The model
decides *content*, the graph decides *flow*.

Two edges carry the security argument. `validate` sits on the **only** edge into `execute`, so no
path reaches the database unchecked. And the retry edge returns to `generate_sql`, never to
`execute` — so retried SQL is validated exactly like first-attempt SQL.

A fixed graph rather than an autonomous loop because the path here is known in advance, which makes
guardrails structural, cost a ceiling instead of a distribution, and failure modes enumerable.
The honest trade-off — LangGraph is oversized for six nodes — is argued in
[ADR-002](docs/ADR/ADR-002-fixed-path-graph-over-agent-loop.md).

**Schema handling.** Not hard-coded and not explored per query. `src/schema.py` introspects
`information_schema` and `pg_description` once at startup (~1,300 tokens for this schema) and
injects it into every SQL prompt. Column `COMMENT ON` text is functional documentation: it carries
units, enum values and grain — `berth_wait_hours` being *hours* is not recoverable from
`numeric(10,2)`, and a model that guesses wrong returns a confidently wrong number. Retrieval over
table metadata earns its place at hundreds of tables, not five.
([ADR-003](docs/ADR/ADR-003-schema-introspection.md))

---

## Guardrails: verified, not asserted

Three independent layers. Only the bottom two are load-bearing.

| # | Layer | Mechanism | Can the model affect it? |
| - | --- | --- | --- |
| 1 | **Database permissions** | `analyst_ro`: `CONNECT`, `USAGE`, `SELECT`. No write grant exists to revoke. | **No** — enforced by PostgreSQL, below the process |
| 2 | **Code validator** | sqlglot parse; one statement; SELECT-family root; **no write node anywhere in the tree**; denied functions and system schemas | **No** — pure code, no LLM in it |
| 3 | **Prompt hardening** | `classify` refuses hostile/out-of-scope questions before SQL is written | **Yes** — so it is not counted as a security control |

### The attack that shaped the validator

```sql
WITH d AS (DELETE FROM port_calls RETURNING *) SELECT count(*) FROM d
```

This statement's top-level node type is **`Select`**. A validator that checks "is this a SELECT?" —
the obvious implementation, and what `sqlparse.get_type()` reports — **passes it, and it empties the
table.** That is why the validator walks the entire parse tree and rejects a write node at any
depth, and why `sqlparse` was rejected as a security boundary in favour of `sqlglot`.

### Proving layer 1 actually holds

The role also sets `default_transaction_read_only = on` — but that parameter is `USERSET`, so a
session can simply switch it off. It can, and does. If that were the only control, the security
story would be theatre.

So the test suite **disables that guard first**, then attempts the writes:

| Attempt, with the read-only guard disabled | Result |
| --- | --- |
| `INSERT` / `UPDATE` / `DELETE` | `ERROR: permission denied for table ...` |
| `DROP TABLE` | `ERROR: must be owner of table ...` |
| `TRUNCATE` | `ERROR: permission denied for table ...` |
| `CREATE TABLE` | `ERROR: permission denied for schema public` |
| CTE-hidden `DELETE` | `ERROR: permission denied for table ...` |
| `SELECT ... INTO` | `ERROR: permission denied for schema public` |
| `SELECT pg_sleep(1)` | `ERROR: permission denied for function pg_sleep` |
| Read `pg_authid` | `ERROR: permission denied for table pg_authid` |

Every failure is on **permissions**, not on the bypassable flag. `tests/test_security_boundary.py`
asserts exactly that, and fails if a write is ever stopped only by the transaction guard — which
would mean the boundary had silently moved to the weaker layer.

**Prompts can be fooled. Permissions cannot.**
([ADR-004](docs/ADR/ADR-004-defence-in-depth-sql.md))

### Residual risk, stated plainly

- Full read access to all business data — no row-level security. Fine for synthetic data; the first
  thing to add for real client data.
- Schema structure is discoverable (the agent needs it). Credentials in `pg_authid` are not.
- An expensive-but-valid query can burn CPU. Bounded by a 5s statement timeout and a 500-row cap —
  both verified — but the timeout is also `USERSET`, so it is a seatbelt, not a boundary.
- Nothing here prevents SQL that is safe and runs but *answers the wrong question*. That is what the
  eval harness is for.

---

## Evaluation

<!-- EVAL_RESULTS_START -->
Three full runs of the 30-question gold set. Runs 2 and 3 use the final prompts; run 1 is shown
because what it found is more interesting than what it scored.

| | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| Execution accuracy (22) | 86.4% | **95.5%** | 86.4% |
| Ambiguity handling (3) | 100% | **100%** | 66.7% |
| Safety / refusals (5) | 100% | **100%** | 100% |
| Overall (30) | 90.0% | **96.7%** | 86.7% |
| Mean latency | 8.3s | 9.1s | 12.0s |
| Cost per run | $0.228 | $0.238 | $0.224 |

**~$0.008 per question**, 3 LLM calls each, ~74 calls per run.

**Read the variance, not the best number.** Runs 2 and 3 are the same code and the same prompts, and
they differ by 9 points. Two things drive that, and they are worth separating:

- **Provider instability.** Two of run 3's four failures (`q22`, `a01`) were `error` outcomes at 58s
  and 37s — LiteLLM's own log shows an SSL handshake timeout during that run. Those are availability
  events, not wrong answers. Excluding them, run 3's execution accuracy is 90.5% and its ambiguity
  handling is 100%. The harness now reports infrastructure errors separately for exactly this
  reason, but does **not** exclude them from the headline, because a metric that quietly drops its
  own failures is worse than a noisy one.
- **Genuine model variance.** At `temperature=0`, `q09` and `q15` flipped between runs. Sampling is
  not deterministic in practice.

So the honest claim is not "95.5%". It is: **execution accuracy sits in the high-80s to mid-90s, and
a single run of 22 items cannot distinguish 86% from 95%** — one item is 4.5 points. This is what
ADR-006 predicted about small gold sets, now measured rather than theorised. The fix is more items
and repeated runs, which is a real cost, not a footnote.

**The one number that did not move: safety — 5/5 in all three runs, 15/15 attempts.** That is the
result the design is built to guarantee, and it is the one guaranteed by permissions rather than by
the model.

Known failure: `q09` returns `terminal_name, port_name` where the reference selects `terminal_name`.
The answer is correct and arguably better; strict result-set comparison calls it wrong. Relaxing the
comparison was considered and **rejected** — loosening a metric after seeing what it fails is tuning
the metric to the result. See [ADR-006](docs/ADR/ADR-006-eval-execution-accuracy.md).
<!-- EVAL_RESULTS_END -->

The gold set has 30 items in three categories, because a system that answers well but cannot say no
is not deployable:

| Category | Items | What it asserts |
| --- | --- | --- |
| **answerable** | 22 | Agent SQL returns the same rows as hand-verified reference SQL |
| **ambiguous** | 3 | Agent asks a clarifying question instead of guessing |
| **adversarial** | 5 | Injection / destructive / out-of-scope requests are refused |

Correctness compares **result sets, not SQL text.** The same question has many correct SQL
formulations — join order, CTE versus subquery, `COUNT(*)` versus `COUNT(1)` — so string comparison
would measure stylistic conformance rather than whether the user got the right numbers. Comparison
is order-insensitive unless the question implies a ranking, with float tolerance for aggregates.

**Ambiguity is scored as a behaviour.** *"Which is the busiest terminal?"* — by port calls or by
containers moved? Those are different queries with different answers, so the correct response is a
question back. A confidently wrong number is worse for a client than no number, because wrong
numbers end up in decks and then in decisions.

One ambiguity case arose naturally from the data rather than being contrived: two operators are
named `Meridian Lines` and `Blue Meridian Shipping`, so *"how is Meridian performing?"* is genuinely
under-specified.

**Honest limitations.** At ~22 scored answerable items, one case is worth ~4.5 percentage points, so
the headline number has a wide confidence interval — it is a regression detector and a smoke test,
not a precise measure of general capability. Result-set comparison also passes if the *reference*
SQL is wrong, which is why every reference query was hand-verified against the data.
([ADR-006](docs/ADR/ADR-006-eval-execution-accuracy.md))

`run_eval.py` exits non-zero if any **safety** case fails, so in CI a safety regression breaks the
build even when overall accuracy still looks fine.

---

## Deliberately out of scope

One line of reasoning each, because "not built" and "not considered" are different claims.

| Omitted | Why |
| --- | --- |
| **Authentication** | One implicit user. Identity is a precondition for row-level security, which is why it heads the production path rather than being a UI feature. |
| **Caching** | Would cut cost and latency, but optimises a system whose correctness is not yet established. Correctness first. |
| **Multi-turn memory** | Single-turn by design. "And what about last year?" turns SQL generation into a coreference problem where ambiguity compounds across turns. LangGraph checkpointing is the intended mechanism. |
| **Streaming** | Perceived latency, not latency. With three calls the honest fix is fewer or faster calls. |
| **Deployment** | Runs locally. Containerising demonstrates a skill this brief does not assess. |
| **Semantic layer** | The most consequential omission — see below. |
| **Tracing / observability** | Cost and latency are measured per request but not persisted. No spend trend, no alerting. |

**On the semantic layer**, because it is the one worth raising before a reviewer does: without
governed metric definitions, "utilisation" resolves to whatever the model infers that day, and the
same question yields different SQL and different numbers across sessions. At this scale, column
comments are a partial substitute. At client scale they are not — and this, not model capability, is
the usual reason agent-analytics deployments stall.

---

## Path to production

1. **Row-level security** per tenant role, so each user sees only their data.
2. **A semantic layer** for governed metric definitions.
3. **The eval suite in CI** as a regression gate on every prompt or model change.
4. **Tracing and cost persistence** — per-query spend, latency percentiles, failure attribution.
5. **Retrieval over table metadata** once the schema outgrows the context window
   ([ADR-003](docs/ADR/ADR-003-schema-introspection.md)).

---

## Design decisions

Every non-obvious choice is recorded with its alternatives and its trade-offs.

| ADR | Decision |
| --- | --- |
| [001](docs/ADR/ADR-001-domain-and-data-model.md) | Port operations domain; five tables; planted signal; fixed date window |
| [002](docs/ADR/ADR-002-fixed-path-graph-over-agent-loop.md) | A fixed-path graph, not an autonomous agent loop |
| [003](docs/ADR/ADR-003-schema-introspection.md) | Schema context by startup introspection |
| [004](docs/ADR/ADR-004-defence-in-depth-sql.md) | Defence in depth for model-generated SQL |
| [005](docs/ADR/ADR-005-deterministic-chart-selection.md) | Chart type chosen by rules, not by the model |
| [006](docs/ADR/ADR-006-eval-execution-accuracy.md) | Evaluation by execution accuracy on a gold set |
| [007](docs/ADR/ADR-007-llm-provider-and-tiering.md) | Provider-agnostic access and two-tier model routing |
| [008](docs/ADR/ADR-008-ui-and-scope-boundary.md) | A thin Streamlit UI, and the scope boundary |

---

## Layout

```
├── app.py                  Streamlit chat UI
├── docker-compose.yml      PostgreSQL 18
├── db/
│   ├── 01_schema.sql       Tables, constraints, indexes, COMMENT ON (prompt context)
│   ├── 02_roles.sql        The read-only analyst_ro role
│   ├── seed.py             Deterministic synthetic data generator
│   └── verify_seed.sql     Sanity checks that the planted patterns are detectable
├── src/
│   ├── agent.py            LangGraph pipeline
│   ├── validator.py        The SQL safety gate
│   ├── executor.py         Read-only execution, timeout, row cap
│   ├── schema.py           Introspection and prompt context
│   ├── charts.py           Rule-based chart selection
│   ├── prompts.py          Prompt templates
│   ├── llm.py              Two-tier LiteLLM wrapper
│   ├── models.py           Typed state and results
│   └── config.py           Settings; separates admin and read-only identities
├── eval/
│   ├── gold_questions.jsonl  30 scored cases
│   └── run_eval.py           The harness
└── tests/                  87 tests
```
