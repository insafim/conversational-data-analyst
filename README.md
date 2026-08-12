# Conversational Data Analyst

Ask questions about port and terminal operations in plain English. The agent translates the
question to SQL, checks it, runs it read-only against PostgreSQL, answers in natural language, and
picks a chart when one helps.

Built as a take-home exercise. Three things it tries to do properly rather than broadly:

- **Safety is structural, not prompted.** The agent connects as a PostgreSQL role holding `SELECT`
  and nothing else. A fully jailbroken model still cannot write: [verified, not
  asserted](#guardrails-verified-not-asserted).
- **Correctness is measured, not claimed.** A gold set of 108 questions with hand-verified reference
  SQL produces a reproducible accuracy number, including for refusals and ambiguity. When a feature
  did not pay for itself, the measurement is what said so: see [runtime verification,
  measured](#runtime-verification-what-it-bought-and-what-it-cost).
- **Scope is controlled on purpose.** [What was left out, and
  why](docs/ADR/ADR-008-ui-and-scope-boundary.md).

> **Going deeper:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the full architecture
> reference: pipeline internals, the security model, schema handling, the evaluation
> method, and a consolidated table of design decisions with their trade-offs.
> [docs/DATA.md](docs/DATA.md) is the dataset reference: what is in it, how it was built,
> and **what you can ask it**, with a value inventory and a question catalogue.
> [docs/ADR/](docs/ADR/) holds the fourteen decision records behind them.

---

## Prerequisites

| Requirement | Why |
| --- | --- |
| **Docker** (running) | PostgreSQL 18 runs in a container; nothing else needs installing |
| **Python 3.12+** (`<3.14`) | `pandas` 3.x needs ≥3.11, `litellm` needs `<3.15` |
| [**uv**](https://docs.astral.sh/uv/) | Creates the venv and resolves dependencies |
| **One LLM API key** | Anthropic by default; any [LiteLLM-supported](https://docs.litellm.ai/docs/providers) provider works |

Nothing else: no local PostgreSQL, no libpq (the `psycopg[binary]` wheel bundles it).

---

## Quickstart

```bash
cp .env.example .env
```

**Now open `.env` and replace `sk-ant-...` with a real Anthropic API key.** This is a separate
step on purpose: it is the one thing the commands cannot do for you, and skipping it produces a
database and a UI that both come up healthy and then fail on the first question with an
authentication error, which reads like a bug rather than a missing key.

Then:

```bash
# 1. Database (PostgreSQL 18 on port 55432, so it won't collide with a local one).
#    --wait blocks until the healthcheck passes; without it, `docker compose up -d` returns
#    before the schema exists and the seed below fails on a connection or a missing relation.
docker compose up -d --wait

# 2. Install and seed. `uv sync` creates .venv and installs the exact versions pinned in
#    uv.lock, so this resolves identically today and in six months. pyproject.toml carries
#    ranges, which is the right contract for a library and the wrong one for a repository
#    whose README quotes measured numbers.
uv sync --extra dev
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python db/seed.py

# 3. Run
streamlit run app.py
```

Every command after step 2 assumes that activated environment. `uv sync` creates `.venv` but does
not enter it, so skipping the `activate` line is the one way to have each following step fail
with `command not found`.

**Done.** Streamlit opens at [http://localhost:8501](http://localhost:8501). Ask
*"Which terminal has the longest average berth wait?"*, and you should get Jebel Ali Terminal 2 at
17.46 hours as a metric card, and the SQL one click away.

Then, to reproduce the numbers below:

```bash
pytest -m "not integration"   # 654 unit tests, no database or network needed
pytest                        # all 858; needs the seeded database, and 10 of them
                              # call the live model, so they also need a funded API key
# The two configurations the published figures were measured on, named explicitly.
# --no-reading is needed for the BASELINE only: ADR-013's reading defaults on and also
# calls the verifier, so --no-verification alone no longer reproduces runs 21/23/25.
# Under --verification the reading is inert, because verification already runs the
# verifier; measured identical node set, call count and cost either way.
python eval/run_eval.py --no-verification --no-reading   # baseline, ~$1.03 (108 cases)
python eval/run_eval.py --verification                   # ADR-012 checks on, ~$1.34

# What the app actually ships: verification off, reading on. Costs more than the
# baseline above by the reading's one extra cheap call per ANSWERED question.
python eval/run_eval.py                                  # $1.26, 12.6 min (run 26)
```

One caveat on reproduction, stated rather than buried: `uv.lock` was refreshed on 2026-08-11 and
moves ten packages relative to the environment the runs in `eval/results/` were recorded on,
including `sqlglot` 30.14.0 to 30.16.0 and `litellm` 1.95.0 to 1.96.0. All 858 tests pass on the
pinned set, which is what establishes that the SQL validator behaves identically. The eval scores
are model-driven and are quoted as ranges across repeated runs for that reason.

### Configuration

Everything is environment-driven; copy [`.env.example`](.env.example) to `.env`. The defaults work
as-is except for the API key.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | **Yes**¹ | n/a | Or `OPENAI_API_KEY` / `GEMINI_API_KEY` |
| `MODEL_CHEAP` | No | `anthropic/claude-haiku-4-5` | Classify + summarise (2 of 3 calls) |
| `MODEL_STRONG` | No | `anthropic/claude-sonnet-5` | SQL generation only |
| `POSTGRES_PORT` | No | `55432` | Deliberately not 5432 |
| `POSTGRES_ANALYST_USER` | No | `analyst_ro` | The read-only role the agent uses. Fixed on the bundled database² |
| `APP_STORE_USER` | No | `app_rw` | Owns `ports_app`, the separate database holding saved chats and telemetry. The agent's role has no `CONNECT` on it (ADR-014). Fixed on the bundled database² |
| `APP_STORE_DB` | No | `ports_app` | The conversation store's database. Separate from the analytics database by design |
| `POSTGRES_ADMIN_USER` | No | `postgres` | Owner. Used **only** by `db/seed.py`. Must match `POSTGRES_USER`, the variable docker-compose creates the superuser from² |
| `STATEMENT_TIMEOUT_MS` | No | `5000` | Bounds an expensive query |
| `ROW_CAP` | No | `500` | Bounds result rows in-process. Rows, not bytes |
| `MAX_SQL_RETRIES` | No | `1` | Retries on a database error only |
| `LLM_TIMEOUT_S` | No | `45` | Per model call, so one slow provider cannot hang the UI |
| `MAX_QUESTION_CHARS` | No | `2000` | Rejects an oversized question before any model call |
| `HISTORY_TURNS` | No | `3` | Prior exchanges the follow-up rewrite may read (ADR-011). Questions and SQL only |
| `RUNTIME_VERIFICATION` | No | `false` | ADR-012's three runtime checks. Off by default because the [comparison](#runtime-verification-what-it-bought-and-what-it-cost) measured it costing more accuracy than it bought |
| `SQL_READING` | No | `true` | The "What was measured" line beside each answer (ADR-013). Runs the verifier for its description and discards its objection, so no SQL, answer or chart changes. Costs one cheap-tier call on answered questions only |

¹ Whichever provider your `MODEL_*` prefixes name. Switching provider is an env change, not a code
change, e.g. `MODEL_CHEAP=openai/gpt-5-mini`, `MODEL_STRONG=openai/gpt-5.4-mini`.

² **Role NAMES are not configurable on the bundled database, though passwords and database
names are.** The three variables above name a role; the scripts in `db/` create those roles
under literal names, so changing one of these points the application at a role nothing ever
created. `db/02_roles.sql` writes `analyst_ro` literally and `db/03_app_store.sql` writes
`app_rw` literally; only `ANALYST_RO_PASSWORD`, `APP_STORE_PASSWORD` and `APP_STORE_DB` are
threaded through `docker-compose.yml` into those scripts. `POSTGRES_ADMIN_USER` is the odd one:
the superuser IS created from a variable, but from `POSTGRES_USER`, which docker-compose passes
to the container. Set both to the same value or neither.

These three become real settings the moment you point the app at a PostgreSQL you provisioned
yourself, because then nothing in `db/` runs and the roles are whatever your DBA called them.

### Troubleshooting

The things most likely to break a first run, all of which came up while building it:

| Symptom | Cause & fix |
| --- | --- |
| `Bind for 0.0.0.0:55432 failed: port is already allocated` | Something already uses the port. Set `POSTGRES_PORT` in `.env` and re-run `docker compose up -d` |
| Container exits with *"there appears to be PostgreSQL data in /var/lib/postgresql/data"* | A stale volume from a pre-18 image. `docker compose down -v && docker compose up -d` |
| `Authentication failed` or `rejected the request` from the model | Model IDs are retired regularly. The defaults were verified 2026-08-04; check `MODEL_CHEAP` / `MODEL_STRONG` against your provider's current list |
| Sidebar shows *"Cannot reach the database"* | Not seeded yet. Run `python db/seed.py` |
| First question returns an authentication error | The API key in `.env` is still the `sk-ant-...` placeholder |
| Integration tests fail on connection | `docker compose up -d --wait`, then `python db/seed.py` |
| `password authentication failed for user "analyst_ro"` | `POSTGRES_ANALYST_PASSWORD` and `ANALYST_RO_PASSWORD` in `.env` disagree. They must match; see the note in `.env.example`. If you changed either after first start, the role already exists with the old password: `docker compose down -v && docker compose up -d --wait` |
| `password authentication failed for user "<something else>"` **shown as if the query failed** | You changed a role name. Role names are fixed on the bundled database (footnote 2 above), so the app is authenticating as a role `db/` never created. The framing is misleading and known: a connection failure is caught by the same handler as a query failure, so it arrives looking like bad SQL and consumes the model's one retry on a rewrite that cannot help. Restore the default in `.env` |

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
Jebel Ali?"* traverses four tables. Four patterns are deliberately planted into the distributions:
a congested terminal, a seasonal volume peak, an ageing low-throughput crane, and an operator with
poor punctuality, so answers are findings rather than noise.
([ADR-001](docs/ADR/ADR-001-domain-and-data-model.md))

**Not sure what to ask?** [docs/DATA.md](docs/DATA.md) is the full dataset reference: every column
with its units and allowed values, every literal you can name in a question (terminals, operators,
crane codes, date window), the measured figures behind each planted pattern, and a catalogue of
questions organised by join depth, including ones with a known correct answer, so you can check
whether the agent is right rather than merely fluent.

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
`execute`, so retried SQL is validated exactly like first-attempt SQL.

A fixed graph rather than an autonomous loop because the path here is known in advance, which makes
guardrails structural, cost a ceiling instead of a distribution, and failure modes enumerable.
The honest trade-off, that LangGraph is oversized for six nodes, is argued in
[ADR-002](docs/ADR/ADR-002-fixed-path-graph-over-agent-loop.md).

**Schema handling.** Not hard-coded and not explored per query. `src/schema.py` introspects
`information_schema` and `pg_description` once at startup (6,180 characters, roughly 1,500 tokens
for this schema) and
injects it into every SQL prompt. Column `COMMENT ON` text is functional documentation: it carries
units, enum values and grain: `berth_wait_hours` being *hours* is not recoverable from
`numeric(10,2)`, and a model that guesses wrong returns a confidently wrong number. Retrieval over
table metadata earns its place at hundreds of tables, not five.
([ADR-003](docs/ADR/ADR-003-schema-introspection.md))

---

## Guardrails: verified, not asserted

Three independent layers. Only the bottom two are load-bearing.

| # | Layer | Mechanism | Can the model affect it? |
| - | --- | --- | --- |
| 1 | **Database permissions** | `analyst_ro`: `CONNECT`, `USAGE`, `SELECT`. No write grant exists to revoke. | **No**, enforced by PostgreSQL, below the process |
| 2 | **Code validator** | sqlglot parse; one statement; SELECT-family root; **no write node anywhere in the tree**; denied functions, system schemas and system catalogs | **No**, pure code with no LLM in it |
| 3 | **Prompt hardening** | `classify` refuses hostile/out-of-scope questions before SQL is written | **Yes**, so it is not counted as a security control |

### The attack that shaped the validator

```sql
WITH d AS (DELETE FROM port_calls RETURNING *) SELECT count(*) FROM d
```

This statement's top-level node type is **`Select`**. A validator that checks "is this a SELECT?",
the obvious implementation, and what `sqlparse.get_type()` reports, **passes it, and it empties the
table.** That is why the validator walks the entire parse tree and rejects a write node at any
depth, and why `sqlparse` was rejected as a security boundary in favour of `sqlglot`.

### Proving layer 1 actually holds

The role also sets `default_transaction_read_only = on`, but that parameter is `USERSET`, so a
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
asserts exactly that, and fails if a write is ever stopped only by the transaction guard, which
would mean the boundary had silently moved to the weaker layer.

**Where this argument does not hold.** Permissions stop every *write* above, but they do not stop
system-catalog *reads*. Probing on 2026-08-08 found that this role can read `pg_roles`,
`pg_database`, `pg_tables` and `pg_class`, and can call `version()` and `inet_server_addr()`,
disclosing role names, database names, the table list, the exact server build and the server's IP.
Those catalogs are world-readable in PostgreSQL, so no `GRANT` change removes the exposure. The
block is enforced in `src/validator.py` instead, which makes this the one case where the validator
rather than the permission system is the only control. `pg_authid` stays unreadable throughout.
See [ADR-004](docs/ADR/ADR-004-defence-in-depth-sql.md), "The case where layer 1 does not hold".

**Prompts can be fooled. Permissions cannot.**
([ADR-004](docs/ADR/ADR-004-defence-in-depth-sql.md))

### Residual risk, stated plainly

- Full read access to all business data, with no row-level security. Fine for synthetic data; the first
  thing to add for real client data.
- Schema structure is discoverable (the agent needs it). Credentials in `pg_authid` are not.
- An expensive-but-valid query can burn CPU. Bounded by a 5s statement timeout and a 500-row cap,
  both verified, but the timeout is also `USERSET`, so it is a seatbelt, not a boundary.
- The 500-row cap bounds rows, not bytes. `SELECT repeat('x', 20000000) FROM generate_series(1,500)`
  is a valid read that passes every check and would pull gigabytes into the application process. The
  fix is a byte budget alongside the row cap; it is not implemented.
- The catalog deny-list in the validator is a deny-list, and deny-lists leak. Three routes around it
  are known and unclosed: a decoy CTE declared in an inner scope whitelists a catalog name used in
  the outer query, `pg_catalog.`-qualified calls escape the node-class rules that block the bare
  form, and `::regrole` or `::regclass` casts over `generate_series` enumerate role and relation
  names without naming a catalog table at all. All three disclose metadata only. None of them
  writes, because writing is blocked by the `GRANT`s underneath rather than by this list. That is
  precisely why the layer order matters.
- Nothing here prevents SQL that is safe and runs but *answers the wrong question*. That is what the
  eval harness is for.

---

## Evaluation

<!-- EVAL_RESULTS_START -->
**This block is history, measured on the retired 28-question set.** The current figures are the
108-case results in the sections below, ending with [runtime verification,
measured](#runtime-verification-what-it-bought-and-what-it-cost). It is kept because the path the
number took is the argument, not the number.

Fourteen full runs. The gold set grew twice, so read across the row and not down the column: runs 1
to 3 scored 22 answerable items, run 4 scored 25, and runs 5 onward score 28 after three
window-function questions were added. Runs 1 to 4 are **not comparable** with the rest and are
discussed below rather than tabulated.

Three code states are represented. Runs 5 to 7 predate two schema-comment fixes. Runs 8 to 10
follow them. Runs 11 onward follow a summariser change, and runs 11 and 12 are the regression it
caused, kept here because deleting them would make the number mean less.

| | Run 5 | Run 6 | Run 7 | Run 8 | Run 9 | Run 10 | Run 11 | Run 12 | Run 13 | Run 14 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Execution accuracy | 92.9% | 96.4% | 96.4% | **100%** | **100%** | **100%** | **100%** | **100%** | **100%** | **100%** |
| Answer groundedness | 89.3% | 92.9% | 96.4% | 96.4% | 92.9% | 96.4% | 89.3% | 89.3% | 92.9% | **92.9%** |
| Ambiguity handling | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100%** |
| Safety / refusals | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **100%** |
| Mean latency | 6.1s | 5.9s | 6.1s | 7.7s | 5.6s | 6.7s | 6.2s | 6.8s | 6.1s | 6.0s |
| Cost per run | $0.325 | $0.315 | $0.328 | $0.341 | $0.339 | $0.335 | $0.344 | $0.354 | $0.357 | $0.352 |

**The clearest thing this harness has done is catch a regression in a fix.** Probing beyond the
gold set found the agent answering *"project how many port calls we should expect in July 2026"*
with `SELECT ROUND(AVG(monthly_count), 2) AS projected_port_calls_july_2026`: a real historical
mean over every month on record, reported as a forward-looking figure, for a period the data does
not reach. Groundedness scored it correct, because the number genuinely was in the rows. Only the
claim about what it meant was false, and set-membership cannot see that.

The fix was a summariser rule. Runs 11 and 12 then dropped groundedness from 92.9-96.4% to 89.3%
twice, failing the same questions both times, because the new rule made the summariser more
descriptive and the extra description arrived as rounded bands: *"around 2,500 to 5,000 TEU"*,
*"crossed 100,000 in June"*. Those are new numbers wearing the clothes of description, and it is
the same defect the no-arithmetic rule already existed to prevent. Extending that rule to cover
rounding and banding returned groundedness to 92.9% on runs 13 and 14, with the forecast case
fixed. The regression was mine, the harness found it in one run, and the two bad runs stay in the
table.

**Read the 100% carefully.** It is 28 of 28 on three consecutive runs, which is 84 of 84
question-runs on a 28-question set, a genuine result, and not the same claim as "100% accurate".

**Groundedness is lower, at 92.9% to 96.4%, and one of the two flags is a false positive I chose not to
engineer around.** They differ in kind, and the difference is the interesting part:

- `q04` is a real catch. The answer said three months each exceeded "24,000 containers". That
  threshold appears in no row; the model invented it for emphasis. Exactly what this metric is for.
- `q28` is a false positive. The `LAG` column holds `-988` for a month that fell, and the answer
  says "dropped 988 containers", correct English that a signed set-membership check rejects. The
  answer is right and the checker is wrong.

The checker was **not** loosened to clear `q28`. Matching on absolute value would accept "July
increased by 3,845" against a `-3845` cell, trading a false positive for a false negative on the
sign errors that actually matter. A proximity rule keyed on decrease words would remove this case
and catch nothing new, since a sign error on a positive value already passes by exact match. So the
metric under-reports, knowingly, and both halves of that behaviour are pinned in
`tests/test_eval_scoring.py` so it cannot drift silently. The true groundedness figure is higher
than the reported one.

**What moved accuracy from ~96% to 28/28 was column comments, not model changes.** The eval caught
two failures of the same kind, both SQL that ran cleanly and answered a slightly different question:

- `q19` filtered `terminals.terminal_name = 'Jebel Ali'` and matched nothing, because every terminal
  name *begins with* its port name: the terminal is `Jebel Ali Terminal 2` and the port is
  `Jebel Ali`. Zero rows, no error.
- `q28` grouped monthly container volume by `port_calls.arrival_ts` instead of `cargo_moves.move_ts`.
  A vessel arriving on 31 January is worked in February, so the two group into different months.

Both were fixed by rewriting the `COMMENT ON` text for the columns involved, which
`src/schema.py` injects into the SQL-generation prompt. `tests/test_schema.py` now asserts those
comments reach the prompt, so deleting them fails the suite instead of quietly costing accuracy.

**On the 28-question set, execution accuracy runs 92.9% to 100% across six runs.** Quoting only the
best run would be the wrong number to trust: runs 2 and 3 were identical code and prompts and landed
nine points apart. At 28 answerable items one case is 3.6 points, so a single run cannot distinguish
93 from 96. Three consecutive clean runs is a stronger signal than any one of them, and it is still
a regression detector rather than a capability measurement.

The clearest evidence of that is which item fails. Run 5 failed `q09` and passed `q19`; run 6 did
the exact opposite, with `q19` returning zero rows from a filter on the wrong column. Same code,
same prompts, same temperature. The stable number is the one that matters: **safety has never
missed, at 19/19 in each of the six runs on the current 108-case set, 114 attempts without a
miss**, because it is enforced where the model cannot reach.

### What the window-function questions cost, and why that is the useful part

Runs 5 and 6 are the first to include `q26` to `q28`, which use `RANK() OVER (PARTITION BY ...)`,
a running total, and `LAG`. All three pass execution accuracy in run 6. Groundedness is where they
bit: it fell from 100% to 89.3% in run 5, and the cause was not the SQL but the summary.

Given a cumulative column, the model subtracted two of its values to describe a span (*"the final
two months added another 31,812 containers"*, a figure appearing nowhere in the rows). That is the
arithmetic the summariser is explicitly forbidden to do, and the harness caught it the first time
it was provoked. The fix was a clause in the summariser prompt naming the trap, not a change to the
checker. Groundedness recovered to 92.9%.

Two flags remain in run 6, and they are different kinds of thing.

`q28` is a false positive. The model reports *"the largest decrease was 3,845 containers"* against
a cell holding `-3845`. The digits are in the results and the sentence is correct English; the
checker compares signed values and calls it ungrounded. Teaching it to accept absolute values
**after** seeing it fail would be tuning the metric, so it is left failing.

`q20` is not a false positive, and it has nothing to do with the window questions. Asked for
average time at berth by terminal, the model answered *"vessels spend an average of 23-24 hours at
berth across all terminals"*, then correctly quoted 24.25 and 23.21 for the extremes. The range in
that first clause is a generalisation across six rows, and neither 23 nor 24 is a value in them.
The answer is useful and a reader would not blink at it, which is exactly why it is worth catching:
the summariser is rounding and generalising in prose, which is the same class of behaviour as the
invented annual total in run 4, even though that one was a miscomputed sum rather than a rounded
range. The prompt change made for the window questions did not address this
one, and it should not be assumed fixed.

### Groundedness was not measured until run 4, and the first measurement found a real failure

`ADR-006` had *claimed* the harness verified groundedness. It did not; no such code existed. When
it was implemented, the very first run caught this, on a question whose SQL was **correct**:

> "During 2025, container movements ranged from 10,203 in February to 29,540 in October, with the
> total annual volume reaching **228,499** containers across all twelve months."

The true total is **239,099**. The model had summed the twelve returned rows itself and got it
wrong by 10,600 containers, fluently, without hedging. **Execution accuracy scored that answer
100% correct**, because the rows *were* correct. Only the sentence describing them was false.

That is the whole argument for scoring groundedness separately, and it is why the brief lists it as
its own criterion. The root cause was a gap in the summariser prompt: it forbade *inventing*
numbers but never forbade *computing* them. It now prohibits arithmetic across rows outright:
selections ("the highest is X") are allowed, new numbers are not, because a computed figure is
indistinguishable to a reader from a retrieved one.

**~$0.0095 per question**, 3 LLM calls each, 276 to 279 calls per 108-question run. With
runtime verification on it is ~$0.0124 and 377 to 384 calls.

**Read the variance, not the best number.** Runs 2 and 3 are the same code and the same prompts, and
they differ by 9 points. Two things drive that, and they are worth separating:

- **Provider instability.** Two of run 3's four failures (`q22`, `a01`) were `error` outcomes at 58s
  and 37s. LiteLLM's own log shows an SSL handshake timeout during that run. Those are availability
  events, not wrong answers. Excluding them, run 3's execution accuracy is 90.5% and its ambiguity
  handling is 100%. The harness now reports infrastructure errors separately for exactly this
  reason, but does **not** exclude them from the headline, because a metric that quietly drops its
  own failures is worse than a noisy one.
- **Genuine model variance.** At `temperature=0`, `q09` and `q15` flipped between runs. Sampling is
  not deterministic in practice.

So the honest claim is not "95.5%". It is: **execution accuracy sits in the high-80s to mid-90s, and
a single run of 22 items cannot distinguish 86% from 95%**, because one item is 4.5 points. This is what
ADR-006 predicted about small gold sets, now measured rather than theorised. The fix is more items
and repeated runs, which is a real cost, not a footnote.

> **These three runs predate every later addition to the gold set.** They were measured against 22
> answerable items. The set grew twice after that: to 25 when a scatter-chart case and a free-text
> case were added to close coverage gaps, then to 28 when three window-function questions were
> added. The numbers above have deliberately **not** been restated against the larger set, because
> that would mean reporting results the harness never actually produced. See the table at the top
> of this section for the current figures.

**The one number that did not move: safety, 5/5 in all three runs, 15/15 attempts.** That is the
result the design is built to guarantee, and it is the one guaranteed by permissions rather than by
the model.

Known failure: `q09` returns `terminal_name, port_name` where the reference selects `terminal_name`.
The answer is correct and arguably better; strict result-set comparison calls it wrong. Relaxing the
comparison was considered and **rejected**, because loosening a metric after seeing what it fails is tuning
the metric to the result. See [ADR-006](docs/ADR/ADR-006-eval-execution-accuracy.md).
<!-- EVAL_RESULTS_END -->

**Runs 15 to 17, the first against the expanded set (2026-08-10):** 94/100, 93/100 and
94/100, at $0.88 per run, wall clock 9.7 to 18.5 minutes. The six repeat failures were
the point of the expansion: two clarify-boundary misplacements in each direction (terse
or non-native phrasing clarified when it should answer; an ambiguous duration question
answered when it should clarify), two column-shape mismatches of the kind the runtime
verifier (ADR-012) targets, and two gold-wording defects that admitted multiple
defensible readings, which were fixed by rewriting the questions (recorded in ADR-010's
addendum). A saturated 36/36 suite could not have shown any of this.

**Run 19, the settled 103-case set:** 98/103, $0.91, 9.7 minutes, no transport errors.
Both rewritten questions now pass, confirming the defects were in the wording. The five
remaining failures are the measured headroom: two clarify-boundary misplacements, and
three answers whose columns do not match the question's shape, the class the ADR-012
verifier exists to catch. (Run 18 is on disk but invalid: a local DNS outage killed name
resolution 22 items in, and the remaining 81 errored without reaching any model. It is
kept because deleting evidence is worse than annotating it.)

### Runtime verification: what it bought, and what it cost

[ADR-012](docs/ADR/ADR-012-runtime-verification.md) added three runtime checks: an LLM
that reads the question against the generated SQL and may object, a code check that every
figure in the answer appears in the rows, and three code-detected result-shape triggers.
Each can force one bounded retry. None of them can block an answer.

Six runs on the 108-case set, alternating the feature on and off so that provider drift
across the hour could not land on one configuration and be read as an effect of the
feature. Zero infrastructure errors in any of the six. Ranges, not best runs:

| | Verification ON (runs 20, 22, 24) | OFF (runs 21, 23, 25) |
| --- | --- | --- |
| Overall | 100 to 101 / 108 | 102 to 103 / 108 |
| Execution accuracy | 69 to 71 / 77 (89.6% to 92.2%) | 72 to 73 / 77 (93.5% to 94.8%) |
| Answer groundedness | 98.7% to 100% | 96.0% to 97.4% |
| Median latency | 6.74 to 7.11 s | 6.03 to 6.65 s |
| Cost per run | $1.33 to $1.36 | $1.01 to $1.04 |

**It works, and it does not pay for itself.** Groundedness is the property it was aimed
at and groundedness improved: the re-summarise loop drove the invented-figure rate to
near zero, and the only figure still flagged is `q28`, the negative-magnitude false
positive [ADR-006](docs/ADR/ADR-006-eval-execution-accuracy.md) documents and declines to
"fix". But execution accuracy fell by more than groundedness rose, and the ranges do not
overlap on either metric.

The reason codes say where it went. Across the three ON runs there were 13 verifier
objections, 8 re-summarisations and 6 quality triggers, and **every accuracy regression
carries a `verifier_objection`**: `q66` in all three runs, `q14`, `q18` and `q65` in one
each. The groundedness check and the code triggers cost nothing measurable.

`q66` ("How many port calls were there in 2020?", correct answer: none, the data starts in
2025) is the clearest case, and it fails the same way in all three ON runs. An earlier
version of this section said the cause was unresolved, and guessed that the first SQL
attempt somehow differed by configuration. **A probe settled it, and that guess was
wrong.** Instrumenting every SQL attempt shows the first attempt is **identical in both
configurations and correct**, six times out of six. **The verifier then objects to that
correct query, in 4 of 4 trials**, on the grounds that the data holds nothing for 2020.
True about the data, false about the query, because returning zero rows is the right
answer. The regeneration is a coin flip from there: in 3 of those 4 trials the model
"corrects" to 2025 and reports a confidently wrong 1,044; in the fourth it held its ground
and answered zero.

So the cost is not the architecture and not the retry. It is a defect in the verifier's own
prompt, which never tells it that an empty result can be correct, and it is the second
defect of that kind alongside the column-shape rule that broke `q65`. Both are one-line
fixes, and both are deliberately **not applied**, because applying them would invalidate the
six runs above and the comparison would have to be re-measured before this section could be
rewritten. See [ADR-012](docs/ADR/ADR-012-runtime-verification.md)'s addendum.

So the feature ships **off by default** (`RUNTIME_VERIFICATION=false`), with the switch and
the evidence both in the repo. Turning it on is defensible when an invented figure costs
more than a wrong row, which is a judgement about the deployment rather than about the code.
The honest summary is that a plausible idea, built as specified and measured properly, made
the headline number worse.

**What the shipped configuration actually costs.** Both tables above measure the two extreme
settings. What ships is neither: verification off, ADR-013's reading on. Run 26, on 2026-08-12,
is the first full 108-case run of it, and the first artefact carrying its own provenance, so
`eval/results/run26.meta.json` records the prompt digest, both model ids and the commit it ran
from rather than leaving them to be recalled later.

| | Both off (21, 23, 25) | **Shipped (26)** | Verification on (20, 22, 24) |
| --- | --- | --- | --- |
| Overall | 102 to 103 / 108 | **102 / 108** | 100 to 101 / 108 |
| Cost per run | $1.014 to $1.036 | **$1.257** | $1.330 to $1.355 |
| Cost per answered question | $0.01229 to $0.01245 | **$0.01530** | $0.01621 to $0.01661 |
| LLM calls | 276 to 279 | **361** | 377 to 384 |
| Median latency | 6.03 to 6.65 s | **6.91 s** | 6.74 to 7.11 s |

The cost columns are arithmetic over 108 records and can be trusted. The latency column is one
run against three, and the variance section below is the reason it is quoted as a direction
rather than a measurement: the reading's unabsorbed cost, collected at the `review` node, was
36.8s across the run, or 0.48s per answered question.

Run 26's six failures were `q49`, `q62`, `q64` and `q70`, which fail in every both-off run,
plus `a10` and `q30`. `q30` is new and is the same class as `q62` and `q64`: asked which
terminals have "Terminal" in the name, it returned the right three rows with an extra
`port_name` column the question never asked for. Three answers carried a grounding flag, and
two of them, `q23` and `q27`, are worth naming because `SUMMARIZE_SYSTEM` already forbids
exactly what they did. It bans banding as arithmetic and gives "the 21,000 to 22,000 range" and
"crossed 100,000 in June" as its own examples of the mistake; the flagged figures were 21,000
and 100,000. The prompt naming a failure is not the same as the prompt preventing it, which is
the argument for keeping the checks outside the prompt.

The gold set has 108 items in three categories (expanded from 36 on 2026-08-10, ADR-010:
every case now carries a syllabus-topic tag and a behaviour tag; five conversational
cases followed on 2026-08-11 with ADR-011), because a system that answers well but
cannot say no is not deployable:

| Category | Items | What it asserts |
| --- | --- | --- |
| **answerable** | 77 | Agent SQL returns the same rows as hand-verified reference SQL |
| **ambiguous** | 12 | Agent asks a clarifying question instead of guessing |
| **adversarial** | 19 | Injection / destructive / out-of-scope / write requests are refused |

Five of those are two-turn conversational cases (ADR-011). Their setup turn is replayed
through the agent so the history the rewrite node reads carries the SQL the agent itself
wrote; only the final turn is scored. One of them, `s19`, is a follow-up whose correct
answer is a refusal: the premise is true, since June 2026 moved 16,624 containers against
May's 20,701, and the question still asks for causation the data does not record.

Correctness compares **result sets, not SQL text.** The same question has many correct SQL
formulations (join order, CTE versus subquery, `COUNT(*)` versus `COUNT(1)`), so string comparison
would measure stylistic conformance rather than whether the user got the right numbers. Comparison
is order-insensitive unless the question implies a ranking, with float tolerance for aggregates.

**Ambiguity is scored as a behaviour.** *"Which is the busiest terminal?"* By port calls, or by
containers moved? Those are different queries with different answers, so the correct response is a
question back. A confidently wrong number is worse for a client than no number, because wrong
numbers end up in decks and then in decisions.

One ambiguity case arose naturally from the data rather than being contrived: two operators are
named `Meridian Lines` and `Blue Meridian Shipping`, so *"how is Meridian performing?"* is genuinely
under-specified.

**Honest limitations.** At 77 scored answerable items one case is worth 1.3 percentage points,
against 3.6 points on the retired 28-question set. The interval is narrower, but the headline still
carries one: it is a regression detector and a smoke test, not a precise measure of general
capability. Result-set comparison also passes if the *reference*
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
| **Streaming** | Perceived latency, not latency. With three calls the honest fix is fewer or faster calls. |
| **Deployment** | Runs locally. Containerising demonstrates a skill this brief does not assess. |
| **Semantic layer** | The most consequential omission. See below. |
| **Exported traces and alerting** | Cost and latency are stored per turn and aggregated on the Observability page, but nothing exports them and nothing pages anyone. |

**Multi-turn memory was on this list and is not any more.**
[ADR-008](docs/ADR/ADR-008-ui-and-scope-boundary.md) deferred it as scope control and predicted it
was the omission a live demo would expose.
[ADR-011](docs/ADR/ADR-011-bounded-multi-turn.md) built it when that became the remaining
deliverable: one rewrite node at the edge of the graph, `HISTORY_TURNS` prior exchanges carrying
questions and SQL but never answer text, and a core that stays single-turn.

**On the semantic layer**, because it is the one worth raising before a reviewer does: without
governed metric definitions, "utilisation" resolves to whatever the model infers that day, and the
same question yields different SQL and different numbers across sessions. At this scale, column
comments are a partial substitute. At client scale they are not, and this, not model capability, is
the usual reason agent-analytics deployments stall.

---

## Path to production

1. **Row-level security** per tenant role, so each user sees only their data.
2. **A semantic layer** for governed metric definitions.
3. **The eval suite in CI** as a regression gate on every prompt or model change.
4. **Exported traces and alerting.** Per-query spend, latency percentiles and failure
   attribution are stored and aggregated on the Observability page, so what remains is getting
   them out of this application: an OpenTelemetry exporter over the per-turn `stage_timings`
   map, and thresholds that page someone. Per-user attribution needs authentication first.
5. **Retrieval over table metadata** once the schema outgrows the context window
   ([ADR-003](docs/ADR/ADR-003-schema-introspection.md)).
6. **A different storage and ingestion layer** once query volume or continuous data arrival
   outgrows single-node PostgreSQL. The two thresholds that trigger it, and what each one changes
   about the agent itself, are named in
   [ADR-001](docs/ADR/ADR-001-domain-and-data-model.md#where-this-model-stops-holding).

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
| [009](docs/ADR/ADR-009-withheld-runtime-capabilities.md) | Withheld runtime capabilities: docs retrieval, web search, MCP, LLM validation |
| [010](docs/ADR/ADR-010-syllabus-mapped-eval-expansion.md) | Syllabus-mapped eval expansion, 36 to 103 cases on two tag dimensions |
| [011](docs/ADR/ADR-011-bounded-multi-turn.md) | Bounded multi-turn at the edge, single-turn core |
| [012](docs/ADR/ADR-012-runtime-verification.md) | Runtime verification, each property gets its instrument (measured; shipped off by default) |
| [013](docs/ADR/ADR-013-the-reading-without-the-verdict.md) | The verifier's reading without its verdict, so an answer says what was measured (shipped on) |
| [014](docs/ADR/ADR-014-conversation-store.md) | Conversations and telemetry in a separate database the agent cannot connect to |

---

## Layout

```
├── app.py                  Entrypoint: page config and navigation
├── views/
│   ├── chat.py             The chat page
│   ├── observability.py    The panel: live traffic and the committed eval runs
│   └── state.py            The store handle and chat session both pages share
├── docker-compose.yml      PostgreSQL 18
├── db/
│   ├── 01_schema.sql       Tables, constraints, indexes, COMMENT ON (prompt context)
│   ├── 02_roles.sql        The read-only analyst_ro role
│   ├── 03_app_store.sql    The separate database for saved chats (ADR-014)
│   ├── seed.py             Deterministic synthetic data generator
│   └── verify_seed.sql     Sanity checks that the planted patterns are detectable
├── src/
│   ├── agent.py            LangGraph pipeline
│   ├── validator.py        The SQL safety gate
│   ├── executor.py         Read-only execution, timeout, row cap
│   ├── schema.py           Introspection and prompt context
│   ├── charts.py           Rule-based chart selection
│   ├── notices.py          Which captions and warnings sit beside an answer, and in what order
│   ├── conversations.py    Which chat is open, and the order a turn is saved and shown
│   ├── store.py            Conversations and telemetry, in their own database (ADR-014)
│   ├── telemetry.py        Reads the committed eval runs for the panel
│   ├── grounding.py        Whether every figure in an answer appears in the rows
│   ├── quality.py          Code-detected result-shape triggers (ADR-012)
│   ├── prompts.py          Prompt templates
│   ├── provenance.py       What produced an eval run: prompt hashes, models, commit
│   ├── llm.py              Two-tier LiteLLM wrapper
│   ├── models.py           Typed state and results
│   └── config.py           Settings; separates admin and read-only identities
├── eval/
│   ├── gold_questions.yaml   108 scored cases, topic- and behaviour-tagged
│   ├── gold.py               Gold-set schema; validated at load
│   ├── run_eval.py           The harness
│   └── results/              Committed raw output, evidence for the numbers above.
│                             `runNN.json` is the records, `runNN.meta.json` what produced them
└── tests/                  858 tests
```

---

## Testing

858 tests. They exist to catch regressions, not to raise a coverage number, so the suite is
weighted heavily toward the parts where a silent failure would be expensive.

| File | Tests | What it protects |
| --- | --- | --- |
| `test_gold_set.py` | 337 | The gold set's schema and tag guards, parametrized over all 108 cases |
| `test_validator.py` | 93 | The security gate: the write-blocking rules, their evasions, and fail-closed parsing |
| `test_eval_scoring.py` | 35 | The comparison logic, i.e. the definition of "correct" |
| `test_charts.py` | 30 | Every chart rule, at its boundaries |
| `test_provenance.py` | 37 | That a run records what produced it, and that no DSN password reaches the committed artefact |
| `test_quality_triggers.py` | 28 | The code-detected result-shape triggers (ADR-012), weighted toward the cases that must NOT fire |
| `test_agent_routing.py` | 25 | Graph topology with a stubbed LLM: unskippable validation, bounded retry |
| `test_runtime_verification.py` | 32 | That runtime verification stays advisory: it cannot block, exceed one retry, or approve (ADR-012), and that the reading-only default adds a description and nothing else (ADR-013) |
| `test_config_defaults.py` | 36 | That `RUNTIME_VERIFICATION` and `SQL_READING` parse to their intended defaults, since a sign error in either ships a configuration nobody chose |
| `test_security_boundary.py` | 19 | That `GRANT`s hold with the read-only guard disabled |
| `test_llm_extraction.py` | 18 | Parsing model output; functions that raise rather than half-parse |
| `test_multi_turn.py` | 15 | That a first turn pays nothing, history carries no answer text, and a rewrite is still untrusted (ADR-011) |
| `test_executor.py` | 13 | Row cap and its boundary, statement timeout, verbatim execution, errors |
| `test_schema.py` | 12 | Catalog introspection, and that composed identifiers are quoted |
| `test_schema_labels.py` | 12 | Turning a table's `COMMENT ON` into a sidebar label, including the split it gets wrong |
| `test_notices.py` | 9 | Which captions and warnings sit beside an answer, and the order they arrive in |
| `test_telemetry.py` | 18 | What the Observability page aggregates: the SQL over stored turns, and reading the committed eval runs |
| `test_conversations.py` | 31 | That a turn is saved before it is shown, and what New chat, reopen and delete do to the open one |
| `test_app_smoke.py` | 10 | Both pages rendered headlessly: that a saved chat reopens with its table and chart, that a missing store degrades to a caption, and that the panel's figures match the store |
| `test_store.py` | 17 | Conversation persistence: round trip, ordering, cascade delete, concurrent appends |
| `test_store_isolation.py` | 6 | That the agent's role cannot connect to the conversation store (ADR-014) |
| `test_store_titles.py` | 5 | Deriving a chat title from its first question |
| `test_seed_characterization.py` | 7 | Data digests, planted patterns, the crane/terminal invariant |
| `test_second_order_injection.py` | 5 | Injection arriving through query results, not the chat box. Two of the five call a live model and skip without an API key |
| `test_forecast_grounding.py` | 5 | That a historical figure is never reported as a forecast. Live model calls; skipped without an API key |
| `test_data_coverage.py` | 3 | That the sidebar's date range is derived from the data, not written down |

```bash
pytest -m "not integration"   # no database, no network
pytest                        # everything (needs a seeded DB; injection tests need an API key)
ruff check src/ tests/ eval/ db/ app.py
```

Three tests are worth reading rather than just running, because each encodes a finding:

- `test_data_modifying_cte_is_blocked`: a `DELETE` hidden in a CTE has top-level type `SELECT`.
  The obvious validator executes it.
- `test_writes_fail_on_permissions_even_with_the_guard_disabled`: disables the bypassable
  read-only flag *first*, then asserts writes still fail on privileges.
- `test_generated_data_is_byte_identical`: a shifted RNG stream produces data that is wrong in no
  visible way; nothing else in the suite would notice.
