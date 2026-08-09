# Conversational Data Analyst

Ask questions about port and terminal operations in plain English. The agent translates the
question to SQL, checks it, runs it read-only against PostgreSQL, answers in natural language, and
picks a chart when one helps.

Built as a take-home exercise. Three things it tries to do properly rather than broadly:

- **Safety is structural, not prompted.** The agent connects as a PostgreSQL role holding `SELECT`
  and nothing else. A fully jailbroken model still cannot write — [verified, not
  asserted](#guardrails-verified-not-asserted).
- **Correctness is measured, not claimed.** A gold set of 36 questions with hand-verified reference
  SQL produces a reproducible accuracy number, including for refusals and ambiguity.
- **Scope is controlled on purpose.** [What was left out, and
  why](docs/ADR/ADR-008-ui-and-scope-boundary.md).

> **Going deeper:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the full architecture
> reference — pipeline internals, the security model, schema handling, the evaluation
> method, and a consolidated table of design decisions with their trade-offs.
> [docs/DATA.md](docs/DATA.md) is the dataset reference — what is in it, how it was built,
> and **what you can ask it**, with a value inventory and a question catalogue.
> [docs/ADR/](docs/ADR/) holds the eight decision records behind them.

---

## Prerequisites

| Requirement | Why |
| --- | --- |
| **Docker** (running) | PostgreSQL 18 runs in a container; nothing else needs installing |
| **Python 3.12+** (`<3.14`) | `pandas` 3.x needs ≥3.11, `litellm` needs `<3.15` |
| [**uv**](https://docs.astral.sh/uv/) | Creates the venv and resolves dependencies |
| **One LLM API key** | Anthropic by default; any [LiteLLM-supported](https://docs.litellm.ai/docs/providers) provider works |

Nothing else — no local PostgreSQL, no libpq (the `psycopg[binary]` wheel bundles it).

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

# 2. Install and seed
uv venv --python 3.12
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"
python db/seed.py

# 3. Run
streamlit run app.py
```

Every command after step 2 assumes that activated environment. `uv venv` creates it but does
not enter it, so skipping the `activate` line is the one way to have each following step fail
with `command not found`.

**Done.** Streamlit opens at [http://localhost:8501](http://localhost:8501). Ask
*"Which terminal has the longest average berth wait?"* — you should get Jebel Ali Terminal 2 at
17.46 hours as a metric card, and the SQL one click away.

Then, to reproduce the numbers below:

```bash
pytest -m "not integration"   # 293 unit tests, no database or network needed
pytest                        # all 373, needs the seeded database
python eval/run_eval.py       # ~4 min, ~$0.34 of tokens
```

### Configuration

Everything is environment-driven; copy [`.env.example`](.env.example) to `.env`. The defaults work
as-is except for the API key.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | **Yes**¹ | — | Or `OPENAI_API_KEY` / `GEMINI_API_KEY` |
| `MODEL_CHEAP` | No | `anthropic/claude-haiku-4-5` | Classify + summarise (2 of 3 calls) |
| `MODEL_STRONG` | No | `anthropic/claude-sonnet-5` | SQL generation only |
| `POSTGRES_PORT` | No | `55432` | Deliberately not 5432 |
| `POSTGRES_ANALYST_USER` | No | `analyst_ro` | The read-only role the agent uses |
| `POSTGRES_ADMIN_USER` | No | `postgres` | Owner. Used **only** by `db/seed.py` |
| `STATEMENT_TIMEOUT_MS` | No | `5000` | Bounds an expensive query |
| `ROW_CAP` | No | `500` | Bounds result rows in-process. Rows, not bytes |
| `MAX_SQL_RETRIES` | No | `1` | Retries on a database error only |
| `LLM_TIMEOUT_S` | No | `45` | Per model call, so one slow provider cannot hang the UI |
| `MAX_QUESTION_CHARS` | No | `2000` | Rejects an oversized question before any model call |

¹ Whichever provider your `MODEL_*` prefixes name. Switching provider is an env change, not a code
change — e.g. `MODEL_CHEAP=openai/gpt-5-mini`, `MODEL_STRONG=openai/gpt-5.4-mini`.

### Troubleshooting

The things most likely to break a first run, all of which came up while building it:

| Symptom | Cause & fix |
| --- | --- |
| `Bind for 0.0.0.0:55432 failed: port is already allocated` | Something already uses the port. Set `POSTGRES_PORT` in `.env` and re-run `docker compose up -d` |
| Container exits with *"there appears to be PostgreSQL data in /var/lib/postgresql/data"* | A stale volume from a pre-18 image. `docker compose down -v && docker compose up -d` |
| `Authentication failed` or `rejected the request` from the model | Model IDs are retired regularly. The defaults were verified 2026-08-04; check `MODEL_CHEAP` / `MODEL_STRONG` against your provider's current list |
| Sidebar shows *"Cannot reach the database"* | Not seeded yet — run `python db/seed.py` |
| First question returns an authentication error | The API key in `.env` is still the `sk-ant-...` placeholder |
| Integration tests fail on connection | `docker compose up -d --wait`, then `python db/seed.py` |
| `password authentication failed for user "analyst_ro"` | `POSTGRES_ANALYST_PASSWORD` and `ANALYST_RO_PASSWORD` in `.env` disagree. They must match; see the note in `.env.example`. If you changed either after first start, the role already exists with the old password: `docker compose down -v && docker compose up -d --wait` |

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

**Not sure what to ask?** [docs/DATA.md](docs/DATA.md) is the full dataset reference: every column
with its units and allowed values, every literal you can name in a question (terminals, operators,
crane codes, date window), the measured figures behind each planted pattern, and a catalogue of
questions organised by join depth — including ones with a known correct answer, so you can check
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
`execute` — so retried SQL is validated exactly like first-attempt SQL.

A fixed graph rather than an autonomous loop because the path here is known in advance, which makes
guardrails structural, cost a ceiling instead of a distribution, and failure modes enumerable.
The honest trade-off — LangGraph is oversized for six nodes — is argued in
[ADR-002](docs/ADR/ADR-002-fixed-path-graph-over-agent-loop.md).

**Schema handling.** Not hard-coded and not explored per query. `src/schema.py` introspects
`information_schema` and `pg_description` once at startup (6,180 characters, roughly 1,500 tokens
for this schema) and
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
| 2 | **Code validator** | sqlglot parse; one statement; SELECT-family root; **no write node anywhere in the tree**; denied functions, system schemas and system catalogs | **No**, pure code with no LLM in it |
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

- Full read access to all business data — no row-level security. Fine for synthetic data; the first
  thing to add for real client data.
- Schema structure is discoverable (the agent needs it). Credentials in `pg_authid` are not.
- An expensive-but-valid query can burn CPU. Bounded by a 5s statement timeout and a 500-row cap —
  both verified — but the timeout is also `USERSET`, so it is a seatbelt, not a boundary.
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
| Gold items | 36 | 36 | 36 | 36 | 36 | 36 |

**Read the 100% carefully.** It is 28 of 28 on three consecutive runs, which is 84 of 84
question-runs on a 28-question set — a genuine result, and not the same claim as "100% accurate".

**Groundedness is lower, at 92.9–96.4%, and one of the two flags is a false positive I chose not to
engineer around.** They differ in kind, and the difference is the interesting part:

- `q04` is a real catch. The answer said three months each exceeded "24,000 containers". That
  threshold appears in no row; the model invented it for emphasis. Exactly what this metric is for.
- `q28` is a false positive. The `LAG` column holds `-988` for a month that fell, and the answer
  says "dropped 988 containers" — correct English that a signed set-membership check rejects. The
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
  name *begins with* its port name — the terminal is `Jebel Ali Terminal 2` and the port is
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
same prompts, same temperature. The stable number is the one that matters: **safety has been 5/5
in every run, 70 attempts across fourteen runs without a miss**, because it is enforced where the
model cannot reach.

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

### Groundedness was not measured until run 4 — and the first measurement found a real failure

`ADR-006` had *claimed* the harness verified groundedness. It did not; no such code existed. When
it was implemented, the very first run caught this, on a question whose SQL was **correct**:

> "During 2025, container movements ranged from 10,203 in February to 29,540 in October, with the
> total annual volume reaching **228,499** containers across all twelve months."

The true total is **239,099**. The model had summed the twelve returned rows itself and got it
wrong by 10,600 containers — fluently, without hedging. **Execution accuracy scored that answer
100% correct**, because the rows *were* correct. Only the sentence describing them was false.

That is the whole argument for scoring groundedness separately, and it is why the brief lists it as
its own criterion. The root cause was a gap in the summariser prompt: it forbade *inventing*
numbers but never forbade *computing* them. It now prohibits arithmetic across rows outright —
selections ("the highest is X") are allowed, new numbers are not — because a computed figure is
indistinguishable to a reader from a retrieved one.

**~$0.009 per question**, 3 LLM calls each, 91 calls per 36-question run.

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

> **These three runs predate every later addition to the gold set.** They were measured against 22
> answerable items. The set grew twice after that: to 25 when a scatter-chart case and a free-text
> case were added to close coverage gaps, then to 28 when three window-function questions were
> added. The numbers above have deliberately **not** been restated against the larger set, because
> that would mean reporting results the harness never actually produced. See the table at the top
> of this section for the current figures.

**The one number that did not move: safety — 5/5 in all three runs, 15/15 attempts.** That is the
result the design is built to guarantee, and it is the one guaranteed by permissions rather than by
the model.

Known failure: `q09` returns `terminal_name, port_name` where the reference selects `terminal_name`.
The answer is correct and arguably better; strict result-set comparison calls it wrong. Relaxing the
comparison was considered and **rejected** — loosening a metric after seeing what it fails is tuning
the metric to the result. See [ADR-006](docs/ADR/ADR-006-eval-execution-accuracy.md).
<!-- EVAL_RESULTS_END -->

The gold set has 36 items in three categories, because a system that answers well but cannot say no
is not deployable:

| Category | Items | What it asserts |
| --- | --- | --- |
| **answerable** | 28 | Agent SQL returns the same rows as hand-verified reference SQL |
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

**Honest limitations.** At 28 scored answerable items, one case is worth 3.6 percentage points, so
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
│   ├── gold_questions.yaml   36 scored cases
│   ├── gold.py               Gold-set schema; validated at load
│   ├── run_eval.py           The harness
│   └── results/              Committed raw output — evidence for the numbers above
└── tests/                  373 tests
```

---

## Testing

373 tests. They exist to catch regressions, not to raise a coverage number, so the suite is
weighted heavily toward the parts where a silent failure would be expensive.

| File | Tests | What it protects |
| --- | --- | --- |
| `test_gold_set.py` | 120 | The gold set's schema, parametrized over all 36 cases |
| `test_validator.py` | 93 | The security gate: the write-blocking rules, their evasions, and fail-closed parsing |
| `test_eval_scoring.py` | 35 | The comparison logic, i.e. the definition of "correct" |
| `test_security_boundary.py` | 17 | That `GRANT`s hold with the read-only guard disabled |
| `test_llm_extraction.py` | 15 | Parsing model output; functions that raise rather than half-parse |
| `test_agent_routing.py` | 22 | Graph topology with a stubbed LLM: unskippable validation, bounded retry |
| `test_charts.py` | 30 | Every chart rule, at its boundaries |
| `test_executor.py` | 13 | Row cap and its boundary, statement timeout, verbatim execution, errors |
| `test_schema.py` | 11 | Catalog introspection, and that composed identifiers are quoted |
| `test_seed_characterization.py` | 7 | Data digests, planted patterns, the crane/terminal invariant |
| `test_second_order_injection.py` | 5 | Injection arriving through query results, not the chat box. Two of the five call a live model and skip without an API key |
| `test_forecast_grounding.py` | 5 | That a historical figure is never reported as a forecast. Live model calls; skipped without an API key |

```bash
pytest -m "not integration"   # no database, no network
pytest                        # everything (needs a seeded DB; injection tests need an API key)
ruff check src/ tests/ eval/ db/ app.py
```

Three tests are worth reading rather than just running, because each encodes a finding:

- `test_data_modifying_cte_is_blocked` — a `DELETE` hidden in a CTE has top-level type `SELECT`.
  The obvious validator executes it.
- `test_writes_fail_on_permissions_even_with_the_guard_disabled` — disables the bypassable
  read-only flag *first*, then asserts writes still fail on privileges.
- `test_generated_data_is_byte_identical` — a shifted RNG stream produces data that is wrong in no
  visible way; nothing else in the suite would notice.
