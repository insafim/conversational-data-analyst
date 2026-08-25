# Conversational Data Analyst

Ask questions about port and terminal operations in plain English. The agent turns the question
into SQL, checks the SQL, runs it read-only against PostgreSQL, answers in a sentence, and draws a
chart when one helps.

The data is synthetic: five tables covering terminals, vessels, cranes, port calls and cargo moves,
so most real questions need a join or three.

## Stack

- **Python**, packaged with [uv](https://docs.astral.sh/uv/).
- **PostgreSQL** in Docker, reached through `psycopg`. No ORM anywhere: generated or
  hand-written, everything that reaches the database is SQL.
- **LangGraph** for the pipeline: a fixed set of nodes and a fixed set of edges, rather than a
  model deciding what to call next.
- **LiteLLM** for model access, which is why the provider is a model string rather than a code
  change.
- **sqlglot** to parse generated SQL before it runs.
- **Pydantic** for the models the pipeline passes between nodes, **pandas** for the result
  frames the charts read.
- **Streamlit** for the UI, using its built-in charts.
- **pytest** and **ruff**.

Versions are pinned in [`pyproject.toml`](pyproject.toml) and tabulated in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#4-technology-stack).

---

## What you need

- **Docker**, running. PostgreSQL 18 comes up in a container, so nothing else is installed.
- **Python 3.12 or newer**, below 3.14.
- **[uv](https://docs.astral.sh/uv/)** to create the environment.
- **One LLM API key.** Anthropic by default; any
  [LiteLLM provider](https://docs.litellm.ai/docs/providers) works by setting its key *and* the two
  model strings, because the provider is read from the model string rather than from which key is
  present.

## Setup

```bash
cp .env.example .env
```

Open `.env` and replace `sk-ant-...` with a real API key. This is the one step the commands below
cannot do for you, and skipping it gives you a database and a UI that both look healthy and then
fail on the first question with an authentication error.

```bash
# 1. Database. PostgreSQL 18 on port 55432, so it will not collide with a local one.
#    --wait blocks until the healthcheck passes; without it the seed below can run
#    before the schema exists.
docker compose up -d --wait

# 2. Environment and data.
uv sync --extra dev
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python db/seed.py

# 3. Run.
streamlit run app.py
```

Every command from here on assumes that activated environment.

## Try it

Streamlit opens at [http://localhost:8501](http://localhost:8501). Ask:

> Which terminal has the longest average berth wait?

You should get **Jebel Ali Terminal 2 at 17.46 hours**, as a metric card, with the SQL one click
away. Then try *"which three operators moved the most containers at Jebel Ali?"*, which crosses
four tables, and *"which is the busiest terminal?"*, which is genuinely ambiguous and should get
you a question back rather than a number.

[docs/DATA.md](docs/DATA.md) lists every column, every name you can mention in a question, and a
catalogue of questions with known answers so you can check whether the agent is right rather than
just fluent.

## Configuration

Everything is environment-driven and the defaults work as they are. The ones worth knowing:

| Variable | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | none | Required. Or `OPENAI_API_KEY` / `GEMINI_API_KEY`, in which case change the two model strings below to match |
| `MODEL_CHEAP` | `anthropic/claude-haiku-4-5` | Classification, summarising, the reading beside each answer |
| `MODEL_STRONG` | `anthropic/claude-sonnet-5` | SQL generation, the one task where capability changes the answer |
| `POSTGRES_PORT` | `55432` | Deliberately not 5432 |
| `STATEMENT_TIMEOUT_MS` | `5000` | Bounds an expensive query |
| `ROW_CAP` | `500` | Bounds result rows |
| `MAX_SQL_RETRIES` | `1` | Retries on a database error only |

The rest are in [`.env.example`](.env.example), each with a comment. Two things there are easy to
trip over: `POSTGRES_ANALYST_PASSWORD` and `ANALYST_RO_PASSWORD` must agree, because the first is
what the app reads and the second is what creates the role, and the role *names* are fixed by the
scripts in `db/`, so renaming one points the app at a role nothing ever created.

## If something breaks

| Symptom | Fix |
| --- | --- |
| `port is already allocated` | Set `POSTGRES_PORT` in `.env`, then `docker compose up -d` again |
| `container name "/cda_postgres" is already in use` | Another copy of this repository has it running. `docker compose down` in that copy, or `docker rm -f cda_postgres`. The name is fixed, so two copies cannot run at once |
| Sidebar says *"Cannot reach the database"* | Not seeded yet. Run `python db/seed.py` |
| First question returns an authentication error | The key in `.env` is still the placeholder |
| Authentication error naming a model you did not choose | The key is for one provider and `MODEL_CHEAP` / `MODEL_STRONG` still name another. The message names the model string, which is the tell |
| `password authentication failed for user "analyst_ro"` | The two password variables disagree. Fix them, then `docker compose down -v && docker compose up -d --wait` |
| Model replies *"rejected the request"* | Model IDs get retired. Check `MODEL_CHEAP` and `MODEL_STRONG` against your provider's current list |

---

## The data

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

Generated from a fixed random seed and a fixed date window, so the same questions give the same
answers on every machine and the evaluation below is reproducible. Four patterns are planted into
the distributions: a congested terminal, a seasonal peak, an ageing low-throughput crane, and an
operator with poor punctuality. That way an answer is a finding rather than noise.

## How it works

```
classify ──ambiguous─────► ask a clarifying question
   │     └─out of scope──► refuse
   │ answerable
   ▼
generate SQL ◄────────────────────┐
   │                              │ one retry, on a database error
   ▼                              │
validate ──fail──► refuse         │
   │ pass                         │
   ▼                              │
execute ──────────────────────────┘
   │
   ▼
summarize ──► pick a chart
```

The model decides content, the graph decides flow. Classification, SQL generation and the summary
are model calls. Validation, execution and chart selection are plain code, because a safety
boundary should not be a prompt and a chart rule should be testable.

An answered question costs **four model calls**: classify, generate SQL, a short reading of what
the SQL measured, and the summary. A refusal or a clarification costs one.

The schema is not hard-coded and not explored per query. It is introspected once at startup,
including the `COMMENT ON` text, which carries units and grain that the column types cannot:
`berth_wait_hours` being hours is not recoverable from `numeric(10,2)`, and a model guessing wrong
returns a confidently wrong number.

Charts are chosen in code from the *shape* of the result set rather than from the wording of the
question, so the same rows always draw the same chart. Six rules, first match wins, from no rows
through metric card, line, bars and scatter, ending at a plain table.
([docs/CHARTS.md](docs/CHARTS.md))

Latency is model latency. Run 26 measured a median of **6.91s** per question. Of the time spent,
SQL generation is 42%, classification 34% and the summary 18%, while the database itself is 0.3%.
So the levers that matter are fewer calls or faster models, not query tuning.

## Safety

Three layers, and only the bottom two are load-bearing.

1. **Database permissions.** The agent connects as a role holding `CONNECT`, `USAGE` and `SELECT`.
   There is no write grant to revoke. A fully jailbroken model still cannot write.
2. **A code validator.** The SQL is parsed with sqlglot and rejected if it is more than one
   statement, is not a SELECT at the root, contains a write node *anywhere* in the tree, or touches
   system catalogs and denied functions.
3. **Prompt hardening.** The classifier turns hostile and out-of-scope questions away. This one can
   be talked around, so it is not counted as a security control.

The statement that shaped layer 2:

```sql
WITH d AS (DELETE FROM port_calls RETURNING *) SELECT count(*) FROM d
```

Its top-level node type is `Select`. A validator that asks "is this a SELECT?" passes it, and it
empties the table.

Layer 1 is proven rather than asserted. The role also sets `default_transaction_read_only`, but
that setting is bypassable from inside the session, so `tests/test_security_boundary.py` **turns it
off first** and then attempts `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `CREATE`, a
CTE-hidden `DELETE`, `SELECT ... INTO`, `pg_sleep` and a read of `pg_authid`. Every one fails on
permissions.

What this does not stop: full read access to all business data, with no row-level security; a valid
but expensive query, which is bounded but not prevented; and SQL that is safe, runs, and answers the
wrong question. The last one is what the evaluation is for. The security model, including the routes
around the catalog deny-list that are known and unclosed, is in
[docs/GUARDRAILS.md](docs/GUARDRAILS.md).

## Evaluation

108 questions with executable reference SQL: **77 answerable**, **12 ambiguous**, **19
adversarial**. Correctness compares result sets rather than SQL text, because the same question has
many correct queries.

The most recent full run:

| | Run 26 |
| --- | --- |
| Execution accuracy | 72/77 (93.5%) |
| Ambiguity handling | 11/12 (91.7%) |
| Safety, refusals | 19/19 (100%) |
| Answer groundedness | 73/76 (96.1%) |
| **Overall** | **102/108 (94.4%)** |
| Cost, median latency | $1.26 for the run, 6.91s per question |

```bash
python eval/run_eval.py     # about 13 minutes and $1.26 against the live model
```

Groundedness is scored separately from correctness because the first time it was measured it caught
an answer whose SQL was right and whose sentence was not: the model summed twelve returned rows
itself, reported 228,499 where the true total is 239,099, and did it fluently. Correct rows, false
sentence.

Two honest limits. At 77 answerable items one case is worth 1.3 percentage points, so this is a
regression detector rather than a precise measure of capability. And result-set comparison also
passes when the reference SQL itself is wrong, since the reference and the agent encode the same
reading of the same question. [docs/EVAL.md](docs/EVAL.md) is the method, including what each
metric cannot see. Raw output for every run is committed under `eval/results/`.

## Tests

```bash
pytest -m "not integration"   # 797 tests, no database and no network
pytest                        # all 1,013; needs the seeded database, and a few call the model
ruff check src/ tests/ eval/ db/ app.py views/
```

Much of that count is parametrization over the 108 gold questions rather than 1,013 hand-written
cases, so read it as a regression net rather than as breadth of coverage.

Three are worth reading rather than just running, because each one records something that was
found the hard way:

- `test_data_modifying_cte_is_blocked`: the `DELETE` hidden in a CTE above.
- `test_writes_fail_on_permissions_even_with_the_guard_disabled`: the read-only flag is switched
  off before the writes are attempted.
- `test_generated_data_is_byte_identical`: a shifted random stream produces data that is wrong in no
  visible way, and nothing else in the suite would notice.

## What is not here

Each of these was considered and left out.

- **Authentication.** One implicit user. Identity is the precondition for row-level security,
  which is why it heads the production path rather than sitting in the UI.
- **Caching.** A repeated question would cost less and return faster. It is the first
  optimisation to reach for once real traffic shows repeats.
- **Streaming.** That improves perceived latency. With three sequential model calls, the honest
  fix is fewer or faster calls.
- **Deployment.** It runs locally on purpose.
- **Hybrid retrieval over past chats.** The follow-up rewrite reads the last three exchanges in
  order, carrying the questions and the SQL they produced but never the answer text. Ranking
  older turns by relevance instead, lexical search and vector similarity together, with
  `pgvector` in the same PostgreSQL rather than a second store, would carry more of a long
  session forward. It is not built because retrieval over stored answers reopens the injection
  path [ADR-011](docs/ADR/ADR-011-bounded-multi-turn.md) closes by keeping answers out of the
  prompt.

## Layout

```
app.py             Entry point: page config and navigation
views/             The chat page, the observability page, shared state
db/                Schema with COMMENT ON, roles, the conversation store, the seed generator
src/               agent, validator, executor, schema, charts, store, telemetry, prompts, llm
eval/              The gold questions, the harness, and committed raw results
tests/             1,013 tests
docs/              Architecture, data, guardrails, charts, evaluation
```

## More detail

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the full picture: pipeline internals, the security
  model, schema handling, and the trade-off behind each design choice.
- [docs/DATA.md](docs/DATA.md), the dataset and what you can ask it.
- [docs/GUARDRAILS.md](docs/GUARDRAILS.md), the security model by threat.
- [docs/CHARTS.md](docs/CHARTS.md), the chart rules and where they are wrong.
- [docs/EVAL.md](docs/EVAL.md), how the numbers above are produced.
- [docs/ADR/](docs/ADR/) holds the decision records behind all of it, one per choice.

## License

MIT. See [LICENSE](LICENSE).
