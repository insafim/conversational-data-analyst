# Architecture: Conversational Data Analyst

> An architecture reference for this system: what it solves, how it is structured, and the
> reasoning behind each significant design decision, including the alternatives that were
> considered and the specific failure each rejected alternative would have introduced.
>
> Companion documents: [README.md](../README.md) (setup, quickstart, measured results)
> and [docs/ADR/](ADR/) (fourteen decision records, each with alternatives and trade-offs).
>
> Every claim below was verified against the running system rather than written from intent.
> This document is maintained with the code: a divergence between the two is a defect in one
> of them, to be corrected at the source and then reflected here.

---

## Table of Contents

1. [The Problem & The Product](#1-the-problem--the-product)
2. [Core Domain Concepts](#2-core-domain-concepts)
3. [System Architecture at a Glance](#3-system-architecture-at-a-glance)
4. [Technology Stack](#4-technology-stack)
5. [Runtime Topology](#5-runtime-topology)
6. [The Agent Pipeline](#6-the-agent-pipeline)
7. [Schema Handling](#7-schema-handling)
8. [The Security Model](#8-the-security-model)
9. [Query Execution & Runtime Limits](#9-query-execution--runtime-limits)
10. [Chart Selection](#10-chart-selection)
11. [Data Model & The Domain Choice](#11-data-model--the-domain-choice)
12. [Evaluation](#12-evaluation)
13. [Frontend](#13-frontend)
14. [Cost, Latency & Observability](#14-cost-latency--observability)
15. [Repository Structure](#15-repository-structure)
16. [Development & Testing](#16-development--testing)
17. [Deliberately Out of Scope](#17-deliberately-out-of-scope)
18. [Path to Production](#18-path-to-production)
19. [Key Design Decisions & Trade-offs](#19-key-design-decisions--trade-offs)

---

## 1. The Problem & The Product

### The problem

Every operations team has business questions queued behind a data analyst. An operations
manager wants to know which terminal is congested this quarter; the question becomes a
ticket, the ticket joins a backlog, and the answer arrives after the decision it was meant
to inform. The bottleneck is not analytical capability, since the SQL is usually
straightforward. It is that writing SQL requires a person who can write SQL.

The direct fix, "let an LLM write the SQL", creates three new problems that are harder than
the original one:

1. **Correctness is unverifiable.** A model that produces SQL which runs, returns rows, and
   is silently wrong is worse than no tool at all, because wrong numbers reach decks and
   then decisions.
2. **The model is an attack surface.** Anything that turns user text into executed SQL
   inherits every prompt-injection and SQL-injection concern at once.
3. **Ambiguity gets resolved by guessing.** "The busiest terminal" has two defensible
   readings; a system that silently picks one is confidently wrong much of the time.

### The product

A conversational analyst over an operational PostgreSQL database. A non-technical user asks
in plain English; the system translates to SQL, checks it, executes it read-only, answers in
natural language, and charts the result when a chart helps.

The three properties it is actually built around, each answering a problem above:

| Property                                        | How it is achieved                                                             | Where it is proven                                               |
| ----------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| **Correctness is measured**               | Gold set of 108 questions with executable reference SQL; result-set comparison | [§12](#12-evaluation), `eval/`                                 |
| **Safety is structural**                  | Read-only role beneath a code validator beneath prompt hardening               | [§8](#8-the-security-model), `tests/test_security_boundary.py` |
| **Ambiguity is answered with a question** | `classify` routes under-specified questions to a clarification exit          | [§6](#6-the-agent-pipeline), scored in the gold set              |

### What "done" looks like for a user

| Persona                                  | What they get                                                                                                          |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Operations manager**             | A plain-English answer with the key figures and units, plus a chart when the shape warrants one                        |
| **Analyst reviewing the output**   | The exact SQL, one click away, on every answer, so the number can be audited before it is trusted                      |
| **Engineer evaluating the system** | A reproducible accuracy figure, its spread across runs, and a test suite that encodes the security claims              |
| **Security reviewer**              | A permission model readable in one file, and a test proving writes fail on privileges rather than on a bypassable flag |

---

## 2. Core Domain Concepts

The demo dataset models **port and terminal operations**. This vocabulary matters because
the column comments are injected into the model's prompt ([§7](#7-schema-handling)), so
these definitions are load-bearing rather than background.

| Concept              | Meaning                                                                      | Why it matters analytically                                                             |
| -------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Terminal**   | A container-handling facility within a port                                  | The primary grouping dimension                                                          |
| **Vessel**     | A ship calling at terminals; belongs to an **operator** (shipping line) | Carries capacity (TEU), type, and the operator dimension                                |
| **Crane**      | A quay crane belonging to exactly one terminal                               | Equipment productivity;*a crane can only service vessels berthed at its own terminal* |
| **Port call**  | One visit of one vessel to one terminal                                      | The grain for congestion and visit-count questions                                      |
| **Cargo move** | One batch of container moves by one crane during one port call               | Finer grain than a port call; the throughput measure                                    |

### The three timestamps, and why they are not interchangeable

This is the distinction most questions turn on, and the one a model gets wrong without help:

- `arrival_ts`: the vessel reaches the port area and starts **waiting**.
- `berth_ts`: a berth is allocated and cargo work can begin.
- `departure_ts`: the vessel leaves the berth.

From these come two metrics that sound alike in English but are different questions:

- **Berth wait** = `berth_ts - arrival_ts`. Time spent queueing, which is the congestion
  metric. Materialised as the generated column `port_calls.berth_wait_hours`.
- **Dwell / time at berth** = `departure_ts - berth_ts`. Time spent working, which is a
  throughput metric.

"How long do vessels *wait* at this terminal?" and "how long do vessels *spend* at this
terminal?" require different SQL. The column comments say so explicitly.

### Cancelled calls

Roughly 3% of port calls are `cancelled`: the vessel arrived but never berthed, so
`berth_ts`, `departure_ts` and `berth_wait_hours` are all `NULL`. This exists to exercise
NULL semantics: `AVG` correctly ignores them, whereas a naive `WHERE status = 'completed'`
filter applied to a **count** silently changes what the number means. That distinction
caused two real evaluation failures and is now pinned in the prompt rules
([§12](#12-evaluation)).

---

## 3. System Architecture at a Glance

```mermaid
flowchart TB
    subgraph Client["Client"]
        UI["Streamlit chat UI<br/>answer - chart - SQL - latency"]
    end

    subgraph App["Application (single process)"]
        GRAPH["LangGraph pipeline<br/>contextualize to classify to generate to validate to execute to summarize<br/>(verify and review ship on, for the reading; ground_check only when RUNTIME_VERIFICATION is on)"]
        VAL["validator.py<br/>sqlglot AST gate"]
        EXEC["executor.py<br/>read-only, timeout, row cap"]
        CHART["charts.py<br/>rule-based, no LLM"]
        SCHEMA["schema.py<br/>introspection cache"]
        STORE["store.py + conversations.py<br/>saved chats, per-turn telemetry"]
    end

    subgraph Data["Data Tier (one container, two databases)"]
        RO{{"analyst_ro role<br/>SELECT only"}}
        PG[("ports<br/>analytics data, 5 tables")]
        RW{{"app_rw role<br/>owns ports_app, nothing in ports"}}
        APP[("ports_app<br/>conversation, turn<br/>analyst_ro holds no CONNECT")]
    end

    subgraph External["External"]
        LLM["LLM provider<br/>via LiteLLM"]
    end

    subgraph Offline["Offline"]
        EVAL["eval/run_eval.py<br/>108 gold questions"]
    end

    UI --> GRAPH
    GRAPH --> VAL
    VAL --> EXEC
    EXEC --> RO
    RO --> PG
    GRAPH --> CHART
    SCHEMA -. cached at startup .-> GRAPH
    SCHEMA --> RO
    GRAPH <--> LLM
    EVAL --> GRAPH
    EVAL --> PG
    UI <--> STORE
    STORE --> RW
    RW --> APP
    RO --x APP
```

**The one structural property worth reading the diagram for:** there is no path from the
graph to the analytics data that does not pass through `validate` and then `execute`, and
`execute` authenticates as `analyst_ro`. Those two facts are enforced by graph topology and
by PostgreSQL respectively, not by the model's cooperation.

**The second property is the crossed edge.** Saved conversations live in a different
database, `ports_app`, written by `store.py` as `app_rw` with fixed statements. No edge runs
from the graph to it, and `analyst_ro` holds no `CONNECT` on it, so the agent cannot reach
chat history even with fully compromised SQL generation. That is why the store appears in the
data tier rather than inside the application: the separation is enforced by PostgreSQL, in the
same place and by the same mechanism as the read-only guarantee
([§5](#5-runtime-topology), [ADR-014](ADR/ADR-014-conversation-store.md)).

---

## 4. Technology Stack

Every version below was verified against the installed environment, most recently on
2026-08-12 by reading `importlib.metadata.version` for each package and `SHOW server_version`
for PostgreSQL. That matters more than usual here: PostgreSQL 18 changed the default kind of
generated column and moved the Docker volume path, LangGraph is on 1.x, pandas on 3.x, and
every 2025-era model identifier has been retired. Code written from year-old documentation
breaks in all four places.

| Layer                     | Technology                                                 | Version                 |
| ------------------------- | ---------------------------------------------------------- | ----------------------- |
| **Language**        | Python                                                     | 3.12 (`>=3.12,<3.14`) |
| **Database**        | PostgreSQL, Docker `postgres:18-alpine`                   | 18.4                    |
| **DB driver**       | `psycopg` v3, `[binary]` extra (no local libpq needed) | 3.3.4                   |
| **Agent framework** | `langgraph` (fixed-topology state graph)                 | 1.2.10                  |
| **LLM access**      | `litellm` (provider selected by model-string prefix)     | 1.96.0                  |
| **SQL parsing**     | `sqlglot` (AST parsing for the security validator)       | 30.16.0                 |
| **Data frames**     | `pandas` (result frames for charting)                    | 3.0.5                   |
| **UI**              | `streamlit` (chat, charts, SQL expander, panel)          | 1.61.1                  |
| **Typed models**    | `pydantic`                                               | 2.13.4                  |
| **Tests / lint**    | `pytest` 9.1.1, `ruff` 0.16.2                          | see left                |
| **Packaging**       | `uv` + `pyproject.toml`                                | n/a                     |

**Notably absent:** no ORM (the agent's queries are model-generated SQL and the rest, the
schema, the seed and the eval's reference queries, is hand-written, so an ORM has nothing to
do), no migration tool (the schema is created once from `db/01_schema.sql`), no vector
database (the schema fits in context, see [§7](#7-schema-handling)), no task queue, and no
cache tier.

**What "no cache tier" means, precisely.** There is no Redis and no Memcached, and nothing
caches a question, an answer, a model response or a result set. Every question is answered
from scratch, which is what makes the per-question cost and latency in
[§14](#14-cost-latency--observability) figures about the pipeline rather than about a hit
rate, and what makes two eval runs of the same gold set comparable.

Five things are cached, and all five hold setup rather than results: the rendered schema
description (`src/schema.py`, `lru_cache(maxsize=1)`, [ADR-003](ADR/ADR-003-schema-introspection.md)),
the compiled graph (`src/agent.py`), the agent handle and the store connection
(`views/chat.py` and `views/state.py`, both `st.cache_resource`), and the one-sentence data
coverage line (`views/state.py`, `st.cache_data`). Reading the schema from the catalog on
every question would add latency for no benefit, since DDL does not change while the process
runs; serving an answer from a cache would change what the measured numbers mean.

### Why LangGraph, and what it was chosen over

[ADR-002](ADR/ADR-002-fixed-path-graph-over-agent-loop.md) argues the shape: a path known in
advance, so the graph decides flow and the model decides content. It compares that shape
against an autonomous tool-calling loop, against one mega-prompt, and against plain Python
functions. What it does not do is compare the library against the other libraries a reader
would name, and that is a fair question to ask of any framework dependency.

Each entry below was checked against the project's own documentation and its package registry
entry on 2026-08-25. The version column is the latest release on that date, which is what
says whether a project is still moving, and is not what this repository installs.

| Framework | Latest release on 2026-08-25 | Its primitive | Why not here |
| --- | --- | --- | --- |
| **LangGraph** | 1.2.11, 2026-08-11 | Nodes and edges over a typed state object | Chosen. A declared topology with `add_conditional_edges` is what this pipeline is. This repository requires `>=1.2.10,<2` and runs 1.2.10, the version in the table above |
| **Microsoft Agent Framework** | 1.15.0, 2026-08-21 | Agents, plus graph-based Workflows | The real alternative. See below |
| **CrewAI** | 1.15.17, 2026-08-20 | A crew of role-playing agents that delegate to each other | Deterministic control exists in Flows, but the framework's purpose is autonomous collaboration between roles. There are no roles here |
| **LlamaIndex Workflows** | `llama-index-core` 0.14.24, 2026-08-19 | Steps wired implicitly by event type | Explicitly not a fixed graph: its documentation says branches are ordinary `if` statements. Topology is inferable, not declared |
| **OpenAI Agents SDK** | 0.22.0, 2026-08-19 | Agents that hand off to other agents | The handoff is chosen by the model. `max_turns` bounds the loop, but the route is not an artefact anyone can read |
| **Semantic Kernel** | 1.44.1, 2026-08-06 | A kernel that translates model function calls into plugin calls | Function-calling middleware rather than a graph runtime, and its own publisher now calls it superseded |
| **AutoGen** | 0.7.5, 2025-09-30 | Teams of agents in conversation | In maintenance mode by its maintainer's own README, no release in eleven months, migration guide points at the Agent Framework |
| **Pydantic AI** | not verified | A typed agent loop | The graph is an adjunct, for when plain control flow is not enough. Here the graph is the substrate, which is the inverse framing |
| **DSPy** | 3.3.1, 2026-08-21 | Signatures, modules and optimisers | A different category. It optimises what a prompt says, not what runs next, so it would complement `generate_sql` rather than replace this graph |

**The honest comparison is with the Microsoft Agent Framework, and it is close.** It is the
declared successor to both AutoGen and Semantic Kernel, by the teams that wrote them, and it
ships graph-based workflows whose own guidance says to use them when the process has
well-defined steps and explicit control over execution order is wanted. That is this system.
The distinction is not that one can express a fixed topology and the other cannot, and any
document claiming so is arguing against a straw version of the alternative. The reasons this
project runs on LangGraph are narrower: its centre of gravity is the Python data ecosystem
rather than Azure, its graph runtime is the older of the two, and the retry edge and the typed
state in [§6](#6-the-agent-pipeline) map onto its primitives without adaptation. Those are
preferences with reasons, not a refutation.

**One thing LangGraph is not doing here, stated so the credit lands correctly.** The bounded
retry is enforced by this repository, not by the framework: `_route_after_execute` compares a
counter carried in state against `MAX_SQL_RETRIES`, which defaults to 1. LangGraph's own
`recursion_limit` defaults to 1000, per its graph-API documentation read on 2026-08-25, and
exists to stop a runaway graph, so it is a backstop three orders of magnitude above the
ceiling this pipeline actually holds itself to.

### Why these two models

| Tier | Default identifier | Price per million tokens | Context | Used by |
| --- | --- | --- | --- | --- |
| cheap | `anthropic/claude-haiku-4-5` | $1 in, $5 out | 200K | `contextualize`, `classify`, `verify`, `summarize` |
| strong | `anthropic/claude-sonnet-5` | $2 in, $10 out | 1M | `generate_sql` |

Prices and context windows were read from Anthropic's pricing and model pages on 2026-08-25
and cross-checked the same day against the LiteLLM registry that this application bills
against, `litellm` 1.96.0, which agrees on all four figures. That agreement is the reason the
cost numbers in [§14](#14-cost-latency--observability) can be trusted as arithmetic rather
than as an estimate.

The split follows the consequence of a mistake, which
[ADR-007](ADR/ADR-007-llm-provider-and-tiering.md) sets out: `generate_sql` is the one node
where a weaker model produces SQL that parses, passes the validator, executes cleanly and
returns the wrong numbers, which is the only failure in this system that is silent. Everything
else either routes between three labels or phrases rows that have already been fetched.

**The strong tier costs twice the cheap tier per token, not ten times.** Worth stating plainly,
because it bounds what the tiering claim is worth: this is not a dramatic saving, and the
argument for the split is capability where correctness is measured rather than economy.

**The schema is not what separates them either.** Three of the five model-calling nodes
interpolate it in full, `classify`, `generate_sql` and `verify`, so its roughly 1,500 tokens
([§7](#7-schema-handling)) are paid three times on an answered question rather than once.
Rendered against the shipped schema on 2026-08-25, the three prompts measure 7,932, 8,522 and
8,492 characters with system and user parts together, which puts them within eight percent of
each other. So the strong tier is not carrying a materially larger prompt; it is carrying the
one task where being wrong is silent.

**Both identifiers were current on 2026-08-25, and one has a near retirement date.** Anthropic
lists Haiku 4.5's retirement as not sooner than 2026-10-15, which is the nearest date in the
current lineup, against not sooner than 2027-06-30 for Sonnet 5. When it retires, four of the
five model-calling nodes lose their default. That is a configuration change rather than a code
change, which is the point of [ADR-007](ADR/ADR-007-llm-provider-and-tiering.md), and it is
also why `MODEL_CHEAP` and `MODEL_STRONG` are environment variables rather than constants.

Anthropic is the default rather than a requirement. LiteLLM resolves the provider from the
prefix of the model string, so `openai/…` or `gemini/…` is an environment change; ADR-007's
2026-08-18 addendum records the one occasion that was actually run, with what it did and did
not establish.

---

## 5. Runtime Topology

A single Python process plus one database container. That container holds **two databases**:
`ports`, the analytics data the agent queries, and `ports_app`, the application's own state
([ADR-014](ADR/ADR-014-conversation-store.md)). No API tier, no worker, no broker, because
nothing here is asynchronous or multi-user by design
([ADR-008](ADR/ADR-008-ui-and-scope-boundary.md)).

```mermaid
flowchart LR
    UIP["streamlit run app.py"]
    EV["python eval/run_eval.py"]
    SEED["python db/seed.py"]

    subgraph C["cda_postgres - postgres:18-alpine - host port 55432"]
        PG[("ports<br/>analytics data, 5 tables")]
        APP[("ports_app<br/>saved chats, per-turn telemetry")]
    end

    UIP -- "analyst_ro (SELECT only)" --> PG
    EV -- "analyst_ro (SELECT only)" --> PG
    UIP -- "app_rw (owns this database)" --> APP
    SEED -- "postgres (owner)" --> PG
```

Only the Streamlit process touches `ports_app`. The eval harness reads the analytics data and
writes its results to `eval/results/` as files, so a scored run leaves no trace in the store
and the Observability page's live traffic and its eval figures cannot be confused for each
other ([§14](#14-cost-latency--observability)).

### Three connection identities, kept apart on purpose

The most important thing in the runtime topology, enforced in `src/config.py` rather than by
convention:

| Identity             | Database       | Used by                                                   | Privileges                                     |
| -------------------- | -------------- | --------------------------------------------------------- | ---------------------------------------------- |
| `postgres` (owner) | `ports`      | `db/seed.py` **only**                             | Full DDL and DML                               |
| `analyst_ro`       | `ports`      | The agent, the UI, the eval harness, schema introspection | `CONNECT`, `USAGE`, `SELECT`             |
| `app_rw`           |  `ports_app` | `src/store.py` **only**                           | Owns `ports_app`. No `CONNECT` on `ports` |

The agent never receives the owner credentials. That is structural rather than a matter of
policy: the code path building the agent's DSN reads different environment variables. There
is no code path in the request flow that can acquire write access to the analytics data.

The third identity is the one worth reading twice, because it is a *write* credential in a
process whose whole security argument is that it cannot write. It resolves because **neither
role can open a session on the other's database**, and the denial is mutual rather than
one-sided. All three cases are pinned in `tests/test_store_isolation.py`:

| Denied                          | Proven by                                                 | How                                    |
| ------------------------------- | --------------------------------------------------------- | -------------------------------------- |
| `analyst_ro` to `ports_app` | `test_the_agents_role_cannot_even_connect_to_the_store` | Connects, asserts `permission denied` |
| `app_rw` to `ports`         | `test_the_store_role_cannot_read_the_business_data`     | Connects, asserts `permission denied` |
| `PUBLIC` to `ports_app`     | `test_public_is_not_a_way_in_either`                    | Asserts the grant is absent            |

The first two attempt the connection rather than inspecting a grant, because a grant that
reads correctly while a session opens anyway is exactly the failure this section exists to
rule out. The third cannot be written that way: `PUBLIC` is a pseudo-role that cannot be
logged in as, so there is nothing to connect with and the grant is the only observable. It is
in the set because `PUBLIC` is what would make the whole separation decorative if it were ever
granted back.

So `app_rw` can write, but only inside a database holding no business data, and it cannot
reach `ports` even to read. `analyst_ro` can read the business data and cannot reach
`ports_app` even to look. Neither identity is a superset of the other, and no code path holds
both DSNs for the same purpose.

Both denials come from the same one-line pattern applied in opposite directions:
`db/02_roles.sql:28` revokes everything on `ports` from `PUBLIC` before granting `CONNECT`
back to `analyst_ro` alone, and `db/03_app_store.sql` does the same for `ports_app` and
`app_rw`. That pattern is load-bearing rather than tidy, for the reason given below.

### The second database, and why it is not a table

Saved conversations and per-turn telemetry are the same record: a reopened chat needs the
turn in order to render it again, and the Observability page needs that same turn's latency,
cost and outcome. One store therefore serves both, and the two views cannot disagree about
what happened ([§14](#14-cost-latency--observability)).

Where it lives was the decision. The cheaper alternative is a table in `ports`, since that
database is already running. It is a separate database instead, for one reason of design and
one of security. [ADR-014](ADR/ADR-014-conversation-store.md) carries both in full; the
compressed form is that in production these are two systems by **ownership**, **lifecycle**,
**workload** and **governance**, so modelling that here makes the step to a separate server a
connection string rather than a migration.

**The security reason is the one that belongs in this document**, because it constrains the
topology. `db/02_roles.sql` grants `analyst_ro` `SELECT` on every table in `public` and,
through `ALTER DEFAULT PRIVILEGES`, on every table added later. That default is deliberate, so
that adding a table cannot silently break the agent, but it means chat history stored in
`ports` would be readable by the agent the moment it existed, and would put user-supplied text
inside the database the agent queries. What that costs is set out in
[§8](#8-the-security-model), which is where the second-order channel is described.

The isolation rests on a `REVOKE`, not on the `CREATE DATABASE`:

```sql
REVOKE ALL ON DATABASE ports_app FROM PUBLIC;
GRANT CONNECT ON DATABASE ports_app TO app_rw;
```

PostgreSQL grants `CONNECT` on a new database to the `PUBLIC` pseudo-role, and every login
role inherits `PUBLIC`. Without that revoke, `analyst_ro` could simply connect and the
separation would be decorative. **Creating a separate database is not, by itself, an access
control.** With it, PostgreSQL denies the session before a query exists, so there is nothing
to validate, no schema to qualify and no `search_path` to escape.

One consequence worth stating plainly: the store is **written by application code with fixed
statements, never by model output**, so it needs no validator, and no path from the graph
reaches it. The store is also optional, and what happens when it is absent is described in
[§13](#13-frontend).

### Database bootstrap

`docker-compose.yml` mounts three scripts into `/docker-entrypoint-initdb.d`, which PostgreSQL
executes **in filename order, on first boot only** (against an empty volume). One of the two
orderings is load-bearing and the other is not, which is worth separating:

1. `01_schema.sql`: tables, constraints, indexes, and the `COMMENT ON` statements that
   become prompt context.
2. `02_roles.sql`: the `analyst_ro` role. Separate and second **because
   `GRANT ... ON ALL TABLES` applies only to tables that exist when it runs**; granting
   before the DDL silently produces a role with no table privileges at all. This ordering is
   real: reverse it and the agent gets a role with no table privileges, silently.
3. `03_app_store.sql`: the `app_rw` role, the `ports_app` database it owns, and the
   `conversation` and `turn` tables inside it. **This one carries no ordering dependency.**
   Its revoke targets the `PUBLIC` pseudo-role rather than any named role, and removing
   `PUBLIC`'s `CONNECT` denies every role that inherits it, including roles created later. It
   is third by filename convention, not by necessity.

The healthcheck deliberately does more than `pg_isready`: it also queries `cargo_moves`,
because `pg_isready` reports success slightly before the init scripts finish, and a container
that is "healthy" but unseeded produces confusing downstream failures.

Two defaults exist to avoid collisions rather than by preference: **host port 55432**, not
5432, so the demo never conflicts with a PostgreSQL the reviewer already runs; and the
compose file omits the now-obsolete top-level `version:` key.

---

## 6. The Agent Pipeline

Implemented in `src/agent.py` as a **fixed-topology state graph**
([ADR-002](ADR/ADR-002-fixed-path-graph-over-agent-loop.md)).

```mermaid
flowchart TD
    START([question]) --> CX{history?}
    CX -- "yes (ADR-011)" --> CTX["contextualize<br/>Rewriter Agent"]
    CX -- "no, first turn" --> C
    CTX --> C{"classify<br/>Classifier Agent"}
    C -- ambiguous --> CL[clarify]
    C -- out_of_scope --> RF[refuse]
    C -- answerable --> G["generate_sql<br/>SQL Author Agent"]
    G --> V{validate}
    V -- fail --> RJ[reject]
    V -- pass --> E{execute}
    V -- "pass, when the explainer is on" --> VF["verify<br/>Explainer Agent"]
    VF -. "via state, not an edge" .-> RV
    E -- "db error, db_retries <= 1" --> G
    E -. "bad result shape, once; verification only" .-> G
    E -- "db error, exhausted" --> ERR([error])
    E -- rows --> S["summarize<br/>Summariser Agent"]
    S -- "both switches off" --> P[pick_chart]
    S -- "reading on, shipped" --> RV{review}
    S -. "verification only" .-> GC[ground_check]
    GC -. "verification only" .-> RV
    RV -. "objection, once; verification only" .-> G
    RV -. "ungrounded, once; verification only" .-> S
    RV -- ok --> P
    P --> D1([answered])
    CL --> D2([clarify])
    RF --> D3([refused])
    RJ --> D4([rejected])

    classDef agent fill:#efe6ff,stroke:#7c5cff,stroke-width:2px,color:#1c1c28;
    classDef code fill:#eef3f0,stroke:#4f9d7a,stroke-width:2px,color:#1c1c28;
    class CTX,C,G,VF,S agent;
    class CL,RF,V,RJ,E,GC,RV,P code;
```

**The five nodes carrying an agent name are the model calls, in purple. The green boxes are
ordinary code.** That division is what the diagram exists to show, and it uses the same two
colour values as the presentation frame at `docs/visuals/pipeline.html`, so the two can be
read against each other. That frame is tracked and ships with the repository: `.gitignore` lifts
the blanket exclusion on `docs/visuals/`, excludes everything inside it, then re-includes the
four frames by name, so the path resolves on GitHub as well as in a working copy. It said the
opposite here until 2026-08-16, a sentence left standing when the repository was published and
the allowlist changed under it. The rounded
pills are the graph's entry and exit states rather than work, which is why `validate`,
`execute` and `pick_chart` carry no agent name and why no colour is spent on `answered`.

**A dotted edge is either not on the shipped path or not a graph edge, and its label says
which.** Five of them are labelled *verification only*: they exist in the code and cannot fire
with `RUNTIME_VERIFICATION` off, so drawing them solid would show four ways back into the graph
where the shipped configuration has one. The label carries the most weight on
`E -. "bad result shape, once" .-> G`, because that edge shares both endpoints with the
`db_error` retry beside it and only one of the two can fire in what ships. Its gate is inside
the node rather than on the edge: `execute` checks `verify_enabled` and returns before
`detect_quality_issue` is reached (`src/agent.py:412`). The
sixth dotted edge, `verify` to `review`, is the opposite case: it is live in what ships and is
**not a graph edge at all**. `verify`'s only edge is to `END`; it submits its
call to a thread pool and leaves a future in state, and `review` collects that future later.
Drawing it solid would claim a hop the graph does not have, and would hide the mechanism that
lets the call overlap `execute` at all.

`contextualize` runs only when the caller passes history, so a first turn does not pay for
it. `verify` is a sibling of `execute`, never a step before it: it can object and it can
attach a caveat, and that is the whole of its authority.

**Which loops are live.** Three backward edges carry four reason codes, and the shipped
configuration can fire exactly one of them.
`execute` returns to `generate_sql` for two unrelated reasons over a single edge, each with its
own budget, so a query that failed in the database has not spent the allowance for one that
returned the wrong shape: `db_error` attaches the PostgreSQL message and is the one loop that
ships, and `quality_trigger` is gated off. `review` returns to `generate_sql` on
`verifier_objection`, gated off. `review` returns to `summarize`, not to `generate_sql`, on
`ground_check`, also gated off; the split follows the precedence `review`'s own docstring
records, where an objection outranks a groundedness violation "because it says the ROWS are
wrong" (`src/agent.py:546-548`). The eval records this directly: run 21 (both switches off)
logged no retries at all, run 24 (verification on) logged four verifier objections, two quality
triggers and three re-summarisations, and **run 26, which is what ships, logged one `db_error`
and nothing else**.

**Where that history comes from, and what it is allowed to contain.** The caller is the chat
session in `src/conversations.py`, and on a reopened conversation the turns are read back from
`ports_app` ([§5](#5-runtime-topology)), which is what makes a follow-up work after a browser
reload rather than only within one session. What it hands the graph is deliberately narrow:
**the earlier question and the SQL it produced, never the answer text and never the returned
rows**, trimmed to `HISTORY_TURNS` (default 3). Where an earlier turn was itself a follow-up,
its interpreted form is passed rather than what was typed, so that a chain resolves. Excluding
rows is not tidiness. Rows are the second-order injection channel of
[§8](#8-the-security-model), and feeding them back into a later prompt would give a payload a
second, longer-lived route into the model after the turn that fetched it had ended.

**Three configurations, and the topology differs in each**
([ADR-013](ADR/ADR-013-the-reading-without-the-verdict.md)). With both switches off, `verify`,
`ground_check` and `review` are absent from the topology rather than present as no-ops, which is
what makes the comparison in the README a comparison. **What ships is `SQL_READING` on and
`RUNTIME_VERIFICATION` off**: `verify` runs for its plain-language description of the query,
`review` collects it, `ground_check` stays absent, and the verifier's objection is discarded
unread. With `RUNTIME_VERIFICATION` on, all three run and the objection is applied.

### Node responsibilities

Thirteen nodes, of which **five call a model and eight are ordinary code**. The five that call
a model are named as agents below. The names are a documentation convention introduced here
and used in the presentation frame at `docs/visuals/pipeline.html`; they are not identifiers in the code,
where the node functions carry the names in the first column. Naming them still earns its
keep, because it makes "the Explainer Agent never sees the result rows" a sentence someone can
check against a specific call.

The table is in pipeline order, not alphabetical, so it can be read as the path a question
takes.

| Node              | Kind           | Agent                      | Model tier       | What it does                                                                                                                                                                      |
| ----------------- | -------------- | -------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contextualize` | LLM            | **Rewriter Agent**   | cheap            | Rewrite a follow-up into a standalone question (ADR-011); skipped on first turns                                                                                                  |
| `classify`      | LLM            | **Classifier Agent** | cheap            | Route: answerable / ambiguous / out_of_scope                                                                                                                                      |
| `clarify`       | code           | none                       | n/a              | Terminal: return a clarifying question                                                                                                                                            |
| `refuse`        | code           | none                       | n/a              | Terminal: classification-time refusal                                                                                                                                             |
| `generate_sql`  | LLM            | **SQL Author Agent** | **strong** | Write one SELECT. Also the retry target                                                                                                                                           |
| `validate`      | **code** | none                       | n/a              | The safety gate ([§8](#8-the-security-model))                                                                                                                                     |
| `reject`        | code           | none                       | n/a              | Terminal: validation-time refusal                                                                                                                                                 |
| `execute`       | **code** | none                       | n/a              | Run as `analyst_ro`, with limits                                                                                                                                                 |
| `verify`        | LLM            | **Explainer Agent**  | cheap            | Advisory: does the SQL measure what was asked (ADR-012)? In the shipped configuration only its description is kept, as the "What was measured" line (ADR-013). Never sees results |
| `summarize`     | LLM            | **Summariser Agent** | cheap            | State the answer, grounded strictly in returned rows                                                                                                                              |
| `ground_check`  | **code** | none                       | n/a              | Advisory: every figure in the answer appears in the rows, question or SQL                                                                                                         |
| `review`        | **code** | none                       | n/a              | Applies the advisory verdicts and decides the next hop                                                                                                                            |
| `pick_chart`    | **code** | none                       | n/a              | Choose the visualisation ([§10](#10-chart-selection))                                                                                                                             |

**Which model each agent is.** The tier column maps to two environment variables, and the
literal defaults in `src/config.py` are `anthropic/claude-sonnet-5` for the strong tier and
`anthropic/claude-haiku-4-5` for the cheap one. The prefix is not decoration: it is how
LiteLLM selects the provider, which is why switching to `openai/…` or `gemini/…` is an
environment change rather than a code change. So the SQL Author Agent runs on Sonnet and the
other four run on Haiku
([§14](#14-cost-latency--observability), [ADR-007](ADR/ADR-007-llm-provider-and-tiering.md)).

**Why the node is called `verify` and the agent is called the Explainer.** The node can do
two things, and only one of them ships. It returns a plain-language reading of what the SQL
measures, and it returns an objection saying the SQL does not answer the question. The
objection is the verification half, and it is off by default because measurement showed it
cost more execution accuracy than it bought
([ADR-012](ADR/ADR-012-runtime-verification.md)). The reading is the half that ships, and
describing is all this agent is permitted to do in the shipped configuration, so **Explainer
Agent** is what it is called throughout these documents and on the pipeline frame. The code
identifier stays `verify`, and the switch stays `RUNTIME_VERIFICATION`, because those name
the full capability rather than the shipped subset. A reader who greps the source and finds
`verify` is not looking at a different component.

**Where it sits.** It is the only model anywhere near the verification path; `ground_check`
and `review` are code. It hangs off the same router edge as `execute` rather than sitting in
front of it, and it is given the question and the SQL but never the returned rows. Running
beside `execute` absorbs most of its latency but not all: run 26 measured the unabsorbed
residue at **0.48s per answered question**, 36.8s across the set, and
[ADR-013](ADR/ADR-013-the-reading-without-the-verdict.md) records that the earlier "costs only
what the overlap does not absorb" framing was too generous to the feature. It has no authority
of its own in either configuration: it can force at most one regeneration and it can attach a
caveat, and `review` decides even that.

**Four LLM calls on an answered question in the shipped configuration**: `classify`,
`generate_sql`, the reading, and `summarize`. Only three of them are sequential, because the
reading runs beside `execute` rather than in front of it. A refusal or a clarification costs
one, a follow-up turn adds one for the rewrite, and a retry adds two rather than one, because
`validate` fans out to the reading on the second pass as well as the first: one call to
regenerate the SQL and one to read what was regenerated, measured at six on a retried answered
question. With both switches
off it is three, which is the configuration runs 21, 23 and 25 measured. Everything else is
code. The division is deliberate: *the model decides content, the graph decides flow.*

### Two edges that carry the security argument

1. **`validate` is on the only edge into `execute`.** No path reaches the database without
   passing through it. That is a property of the topology, not of the model's behaviour.
2. **The retry edge returns to `generate_sql`, never to `execute`.** Retried SQL is therefore
   validated identically to first-attempt SQL. An edge looping back into `execute` would be a
   bypass and would still look reasonable in a diagram, which is exactly why this one is
   drawn and tested explicitly
   (`tests/test_agent_routing.py::test_retried_sql_is_validated_exactly_like_first_attempt_sql`).

### The bounded retry

On a database error the graph returns to `generate_sql` with the PostgreSQL error message
appended to the prompt. This is the ReAct pattern (act, observe the error, reason again), but
**the loop counter belongs to the graph**, capped at one iteration. The model contributes
reasoning and has no vote on whether to continue.

A validation *rejection* is never retried: that is a safety decision, not a transient
failure, and retrying it would hand the model a second attempt at the gate.

### Four terminal outcomes

`answered`, `clarify`, `refused` (classification-time), `rejected` (validation-time). The
last two are distinct on purpose, because they are different events with different causes,
and collapsing them would hide which layer did the work. `AgentResult.outcome` carries the
distinction to both the UI and the eval harness.

### Failure containment

`ask()` never raises for an expected failure mode. A provider outage, or a query that fails
twice, returns an `AgentResult` with `outcome=ERROR`, so callers have exactly one code path
and the eval harness scores a failure rather than crashing on it.

### Node by node

The table above says what each node is for. This says how it does it, in pipeline order.

**About the per-node paragraphs.** Each node carries one. They are written to be read on
demand rather than in sequence: a walkthrough of the pipeline covers four or five points,
and these are the detail behind any one of them. Read end to end they run several minutes,
which is why a summary names a few and lets the diagram carry the rest.

---

#### 1. `contextualize`, the Rewriter Agent

**How it works.** A conditional edge from `START` reaches this node only when the caller
passed history, so a first turn skips it and reports no timing for a node that did not run.
It renders the last `HISTORY_TURNS` turns into the prompt as pairs of earlier question and
the SQL that turn produced, with newlines flattened and `(no query was run)` where a turn
produced none, then asks the cheap model for a standalone rewrite and reads the `question`
field out of the JSON.

It fails open in three separate ways, all of which return the typed question unchanged: an
unparseable reply, an empty rewrite, and a rewrite longer than `max_question_chars`. That
last one shares its bound with the check `ask()` applies to typed input but not its action,
and the asymmetry is the point. A typed question over the limit is a user handing the model a
document. A rewrite over the limit is this node malfunctioning, and refusing a legitimate
follow-up because our own rewrite ran away would turn an internal fault into a user-visible
failure. When the rewrite comes back identical to the question, it returns nothing, so the
screen shows no "Interpreted as" line for a question that was already standalone.

> "It follows the conversation. If you ask a second question that says 'its' or 'and
> Rotterdam?', a first model rewrites it into a question that stands on its own, and the
> screen shows you what it resolved to. That last part matters more than the rewrite: a
> resolution you cannot see is one you cannot correct. It is only given the earlier questions
> and the SQL they produced, never the rows that came back, and that is a security decision I
> will come back to."

---

#### 2. `classify`, the Classifier Agent

**How it works.** One cheap call with the schema and the question, returning JSON that routes
the turn three ways: `answerable` to `generate_sql`, `ambiguous` to `clarify`, `out_of_scope`
to `refuse`. It also carries back the clarifying question to ask and the reason to give.

If that JSON does not parse, it defaults to `answerable` rather than to a refusal. That
direction is deliberate: the code validator and the read-only role still stand between this
node and the database, so a parsing hiccup routed onward is contained by two later layers,
while a parsing hiccup routed to a refusal is a broken product. This is layer 3 of
[§8](#8-the-security-model), the layer written on the assumption that it will be defeated.

> "The first thing that happens is a cheap model deciding whether the question can be
> answered from this database at all, whether it is too vague to answer, or whether it is out
> of scope. Refusing early is what makes the cost profile work, because a refusal costs one
> model call instead of four. I want to be clear that this is not the security boundary. It
> is a router, it is a language model, and I have assumed it can be talked around."

---

#### 3. `clarify`, ordinary code

**How it works.** A terminal node with no model in it. It returns the clarifying question the
classifier already wrote, falling back to a fixed sentence when that field came back empty,
and stamps the outcome `CLARIFY`. No SQL is written and no database connection is opened.

> "When the question is ambiguous it asks back instead of guessing. 'Which is the busiest
> terminal' is two different questions, busiest by ship visits or by containers moved, and
> they have different answers. For a client, a confident wrong number is worse than no
> number, because wrong numbers end up in decisions."

---

#### 4. `refuse`, ordinary code

**How it works.** The other classification-time terminal. It prefixes the classifier's reason
with a fixed apology and stamps the outcome `REFUSED`, again with no SQL and no connection.
This outcome is kept distinct from `rejected` throughout, because the two happen at different
layers and collapsing them would hide which layer did the work.

> "If the question is out of scope, it says so and stops, before it has spent anything beyond
> the one classification call. And notice this is a different outcome from the one you get
> when the safety gate stops a query. I keep them separate in the results, because they tell
> me different things about the system."

---

#### 5. `generate_sql`, the SQL Author Agent

**How it works.** The one strong-tier call. The prompt is the introspected schema plus the
question, and this node is also the target of every regeneration, so on a second pass it
appends exactly one evidence suffix in a fixed precedence: the verbatim PostgreSQL error, or
the result-shape complaint, or the verifier's objection. Passing the database error through
verbatim is what recovered four of four historical retries, which is why the other two causes
are attached the same way rather than as a bare instruction to try again.

Every cause is cleared as it is consumed, along with the previous groundedness state, so a
third attempt cannot argue with the first attempt's problem. The retry reason is recorded
here, where the regeneration actually happens, rather than in the router that decided it, so
the reason list cannot claim a retry that a router then declined to take.

> "The one place I spend a stronger model is writing the SQL, because that is the one step
> where model capability changes whether the answer is right. Everything else runs on the
> cheap tier. If the query fails, this is also where it comes back to, and it comes back
> carrying the actual PostgreSQL error text rather than a note saying it failed. Handing the
> model the real error is what makes the retry work."

---

#### 6. `validate`, ordinary code, layer 2

**How it works.** `validate_sql` parses the statement with sqlglot and applies nine checks
in order: it rejects empty input; it fails closed on any parse error, because a statement we
cannot understand is one we cannot call safe; it requires exactly one statement, which is what
defeats stacked-query injection independently of what the second statement contains; it
requires a SELECT-family root; it walks the entire tree and rejects a write node at any depth;
it rejects `SELECT ... INTO`, which creates a table while wearing a SELECT's clothes; it
rejects locking clauses; it rejects denied functions; and it rejects system schemas and
catalog tables, collecting CTE names first so that a CTE innocently named `pg_summary` is not
mistaken for the catalog.

Each rejection carries a stable violation code that the tests assert against, and a separate
user-facing reason, so a rejection is pinned to a rule rather than to the wording of a
message.

> "Then a piece of ordinary code checks the SQL before anything runs. Not a model, and this
> is the part people get wrong. You can hide a delete inside a sub-query, and the whole
> statement still looks like a read to any check that only glances at the first word. This one
> walks the entire parse tree, so a delete nested three levels down is refused exactly as
> readily as a bare one. And it fails closed: if it cannot parse the statement, it refuses
> it."

---

#### 7. `reject`, ordinary code

**How it works.** The validation-time terminal. It returns the validator's reason to the user
and stamps the outcome `REJECTED`. Critically, this outcome is never retried. A rejection is a
safety decision rather than a transient failure, and retrying it would hand the model a second
attempt at the gate.

> "If the gate refuses the query, the turn ends there and the user is told why. I do not retry
> a rejection. A failed database call is bad luck and worth another attempt; a query the
> safety gate refused is not, and giving the model a second run at the gate is exactly the
> thing I do not want to build."

---

#### 8. `execute`, ordinary code

**How it works.** It runs the validated statement as `analyst_ro` under a five second
`statement_timeout`, through a server-side cursor that fetches the row cap plus one, so
truncation is detected without a second count and the statement itself is never rewritten
([§9](#9-query-execution--runtime-limits)). A failure returns the PostgreSQL message and
increments the database retry counter here, in the node, rather than in the router, because a
router is a pure function of state and must not be the thing that advances the budget it
reads.

When runtime verification is on it also applies the three code-detected result-shape triggers
at this point: an empty result on an answerable question, a result that saturates the row cap,
and a multi-row result for a singular superlative question. Checking them here rather than
after the answer is written costs nothing and saves the summarisation call that a regeneration
would have discarded.

> "The query runs as a role that can read and nothing else, with a five second timeout and a
> five hundred row cap. The cap is applied by fetching fewer rows rather than by editing the
> SQL, because the moment this code starts composing SQL around model output, it becomes the
> thing I am claiming it is not."

---

#### 9. `verify`, the Explainer Agent

**How it works.** One cheap call that reads the question, the schema and the SQL, and returns
two things: a plain-language **reading** of what the query measures, and an **objection** if it
judges that the SQL does not answer the question. Only the reading ships, which is why the
agent is named for explaining rather than for verifying; the objection half is switched off by
default and the evidence for that is in [ADR-012](ADR/ADR-012-runtime-verification.md).

It never sees the returned rows, which is what makes running it beside `execute` sound: it is
a statement about intent, and intent is fully determined before a single row comes back. It is
also why the reading can be shown as "What was measured" rather than "what was found".

It does not block. It submits the call to a small thread pool and returns the future
immediately, because the installed LangGraph synchronises between supersteps, so a node that
blocked here would overlap `execute`, measured at 0.025s, and put the rest of its latency
straight onto the critical path. That is a claim about a third-party library rather than about
this code, so it is measured:
`tests/test_agent_routing.py::test_langgraph_barriers_between_supersteps_so_verify_must_not_block`
runs a two-node branch beside a single longer branch and asserts the total lands where
synchronisation would put it. Any failure inside that call returns nothing at all, so the
answer ships without its reading rather than not shipping. That is the whole availability
argument for putting a model in an advisory position: a flaky one must not be able to degrade
a working system.

> "There is a second model that reads the question and the SQL, and says in one sentence what
> the query actually measures. It never sees the results, only the intent. In what ships, I
> keep its description and show it beside the answer, and I discard its objections, because
> when I measured it, it objected to a correct query four times out of four. A warning printed
> next to a right answer is worse than no warning. It runs alongside execution rather than in
> front of it, so most of its time is absorbed."

---

#### 10. `summarize`, the Summariser Agent

**How it works.** With zero rows it does not call a model at all and answers in code with a
fixed sentence, because there is nothing to ground an answer in and that is precisely where a
model is most likely to invent a plausible number. Otherwise it renders up to fifty rows as
pipe-delimited text with a header, which is more compact per token than JSON and loses nothing
because there is no nesting to preserve, and passes the question, the SQL and the row count
alongside, with a note when the cap truncated the result.

On a second pass it appends the exact figures that failed the groundedness check, quoted back
as numbers, together with the answer that contained them. Naming the offending figure is the
whole mechanism, because "be more grounded" is not an instruction a model can act on.

> "Then a cheap model writes the answer, and it is only allowed to work from the rows that
> came back. If the query returns nothing, no model runs at all: code says nothing matched.
> An empty result is exactly where a language model will happily invent a number for you, so
> I took the model out of that path."

---

#### 11. `ground_check`, ordinary code

**How it works.** It extracts every number from the answer and accepts it as grounded if it
appears in the returned rows, exactly or as a rounding, or in the question, or in the SQL, or
if it is the row count. Small integers are ignored, since they are almost always ordinals or
counts of listed items rather than data values. On an empty result it looks instead for
phrasing that says nothing matched. It is the same function the eval scores with, so the
runtime floor and the published metric cannot disagree about what grounded means.

It has two known false positives, both stated rather than hidden: figures the model derived by
arithmetic, and the magnitude of a negative value, where the data holds -988 and the natural
English is "dropped 988". Those false positives are the reason this check is advisory at
runtime. Blocking on a check that is known to be wrong sometimes would withhold answers that
are in fact correct.

> "After the answer is written, code checks every number in it against the rows that came
> back. Not a model checking a model: string and set membership. It is the same function the
> evaluation scores with, so the number I quote you and the check running in production cannot
> drift apart. It has two false positives I know about and can describe, which is why it flags
> rather than blocks."

---

#### 12. `review`, ordinary code

**How it works.** This is where the verifier's future is finally collected, and where the only
latency it costs is the part that running beside `execute` and `summarize` did not absorb. A
verdict issued against SQL that a database-error retry has since replaced is discarded, since
acting on it would mean regenerating a query in response to a complaint about a different one.

Precedence is deliberate: an objection outranks a groundedness violation, because an objection
says the rows themselves are wrong, and re-describing wrong rows more carefully is not an
improvement. Each mechanism gets one regeneration. Past that budget the answer ships carrying
a visible caveat or flag rather than being withheld, because nothing in this node has the
authority to withhold an answer. In the shipped configuration it collects the reading, returns
`ok`, and clears the groundedness state as a second independent mechanism rather than relying
on the routing alone.

> "A small piece of code collects the advisory verdicts and decides what happens next. It can
> send the query back to be rewritten, once. It can ask for the answer to be restated, once.
> What it cannot do is refuse to answer you. Advisory means advisory, and past its budget the
> answer ships with the concern printed next to it rather than swallowed."

---

#### 13. `pick_chart`, ordinary code

**How it works.** Six rules over the shape of the result set, with no model call
([§10](#10-chart-selection)). Once the SQL has run, the result fully determines which
encodings are valid, so no linguistic judgement is left for a model to contribute. Every
`ChartSpec` carries the name of the rule that fired, which is shown in the UI. Its content is
asserted for only some of the rules; [§10](#10-chart-selection) says which.

> "The chart type is chosen by rules in code, from the shape of the result, not from the
> words in the question. A time series becomes a line, a ranking becomes bars, a single figure
> becomes a metric. It is deterministic and unit-tested, and every chart can tell you which
> rule picked it. This is the clearest example of the whole design: the model decides content,
> the graph and the code decide flow."

---

## 7. Schema Handling

`src/schema.py` introspects the live database **once at startup**, renders it to annotated
`CREATE TABLE` text, caches it for the process lifetime, and injects it into every
SQL-generation prompt ([ADR-003](ADR/ADR-003-schema-introspection.md)).

Four things are extracted, and the middle two are the interesting ones:

1. **Structure**: tables, columns, types and nullability, from `information_schema`.
2. **Join paths**: primary *and* foreign keys, recovered from `pg_constraint` (joined to
   `pg_class`, `pg_namespace` and `pg_attribute`) rather than from `information_schema`,
   which cannot yield them in one readable query. On a multi-table schema this is the single
   most valuable thing to hand a model.
3. **Semantics**: the `COMMENT ON` text, read from `pg_description` via `col_description()`
   and `obj_description()`.
4. **Data coverage**: the real min/max of every date column, so relative dates resolve
   against the data rather than the wall clock. Detailed below.

Per-column detail on what each of these teaches the model, and the literal text it produces,
is in [DATA.md §9](DATA.md#9-how-the-data-reaches-the-model).

### Column comments are functional code, not documentation

`berth_wait_hours` being measured in **hours** is not recoverable from `numeric(10,2)`.
Neither is `move_type` being one of `load`/`discharge`, nor `cargo_moves` being at a finer
grain than `port_calls`, nor the difference between the three timestamps in
[§2](#2-core-domain-concepts). A model that guesses any of these produces SQL that executes
cleanly and returns a confidently wrong number.

So the comments are written *for the model as reader*, they live in `db/01_schema.sql` beside
the data they describe, and changing a column without changing its comment is a defect.

### Data coverage is injected too

The introspection also computes the actual min/max of every date column:

```
-- Data coverage. Resolve relative dates ('last quarter', 'recently')
-- against THESE ranges, not against today's date:
  port_calls.arrival_ts: 2025-01-01 05:15:00 .. 2026-06-30 12:45:00
```

The dataset uses a fixed historical window ([§11](#11-data-model--the-domain-choice)), so a
model reasoning from the system clock would silently query an empty range. This is not an
artefact of synthetic data: production systems hit the same problem the moment a user says
"recently".

### Why not the alternatives

| Approach                                                                 | Verdict                                                                                                                                                                                               |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hard-coded schema string                                                 | Rejected. Rots silently on the first schema change, and makes this a demo of*this* database rather than a system that works against *a* database                                                  |
| Per-query agentic discovery (`list_tables` / `describe_table` tools) | Rejected. 2 to 4 extra sequential round trips **per question** to rediscover something unchanged since startup, on a system where latency is assessed, and it reintroduces model-controlled flow |
| **Startup introspection + full injection**                         | **Chosen.** The entire schema is 6,180 characters (roughly 1,500 tokens)                                                                                                                        |

The generalisable principle: **retrieval earns its place when the schema stops fitting in
context, not before.** At five tables context is not scarce. Knowing when *not* to reach for
RAG is the same skill as knowing when to.

**Where this breaks:** correct at 5 tables, wrong at 500. The replacement is retrieval over
table metadata, then a curated semantic layer ([§18](#18-path-to-production)).

---

## 8. The Security Model

Full reasoning in [ADR-004](ADR/ADR-004-defence-in-depth-sql.md). The organising principle:
**assume the model is fully compromised, and ask what still holds.**

| # | Layer                | Mechanism                                                                                                                          | Can the model affect it?                     | Load-bearing?                                      |
| - | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- | -------------------------------------------------- |
| 3 | Prompt hardening     | `classify` refuses hostile / out-of-scope questions                                                                              | **Yes**                                | **No.** Written assuming it will be defeated |
| 2 | Code validator       | sqlglot AST: one statement, SELECT-family root, no write node at any depth, no denied functions, system schemas or system catalogs | No, pure code with no LLM inside             | Yes                                                |
| 1 | Database permissions | `analyst_ro`: `CONNECT`, `USAGE`, `SELECT`. No write grant exists to revoke                                                | No, enforced by PostgreSQL below the process | **Yes, decisively**                          |

### The attack that shaped the validator

```sql
WITH d AS (DELETE FROM port_calls RETURNING *) SELECT count(*) FROM d
```

This statement's top-level node type is **`Select`**. A validator that inspects only the
top-level statement type, which is what `sqlparse.get_type()` reports, **passes it, and it
empties the table.** That single case is why `src/validator.py` walks the entire parse tree
and rejects a write node at any depth, and why `sqlparse` was rejected as a security boundary
in favour of `sqlglot`.

The validator is **allow-list shaped**: only a single read-only SELECT-family statement is
permitted, so a construct nobody anticipated is denied by default. That is not theoretical:
`EXPLAIN ANALYZE DELETE FROM port_calls` (`EXPLAIN ANALYZE` *executes* its argument) is
blocked by the deny-`Command` catch-all rather than by anyone having thought of it.

It **fails closed**: an unparseable statement is rejected, never passed through. If we cannot
understand it, we cannot claim it is safe.

### Verified, not asserted

The role also sets `default_transaction_read_only = on`. That parameter is **`USERSET`**, so
any session can switch it off, and the test does exactly that. If it were the only control,
the security story would be theatre.

So `tests/test_security_boundary.py` **disables that guard first**, then attempts the writes:

| Attempt, with the read-only guard disabled | Result                                             |
| ------------------------------------------ | -------------------------------------------------- |
| `INSERT` / `UPDATE` / `DELETE`       | `ERROR: permission denied for table ...`         |
| `DROP TABLE`                             | `ERROR: must be owner of table ...`              |
| `TRUNCATE`                               | `ERROR: permission denied for table ...`         |
| `CREATE TABLE`                           | `ERROR: permission denied for schema public`     |
| CTE-hidden `DELETE`                       | `ERROR: permission denied for table ...`         |
| `SELECT ... INTO`                        | `ERROR: permission denied for schema public`     |
| `SELECT pg_sleep(1)`                     | `ERROR: permission denied for function pg_sleep` |
| Read `pg_authid`                          | `ERROR: permission denied for table pg_authid`   |

Every failure is on **permissions**. The test asserts that specifically, and fails if a write
is ever stopped only by the transaction flag, which would mean the boundary had silently
moved to the weaker layer.

**Where this argument does not hold.** Permissions stop every write above, but they do not
stop system-catalog reads. Probing on 2026-08-08 found this role can read `pg_roles`,
`pg_database`, `pg_tables` and `pg_class`, and can call `version()` and `inet_server_addr()`,
disclosing role names, database names, the table list, the exact server build and the
server's IP. These catalogs are world-readable in PostgreSQL, so no `GRANT` change removes
the exposure; the block is enforced in `src/validator.py` instead. This is the one case where
the validator, not the permission system, is the only control. `pg_authid` stays unreadable.
See [ADR-004](ADR/ADR-004-defence-in-depth-sql.md), "The case where layer 1 does not hold".

One finding worth recording: **`GRANT INSERT ON terminals TO analyst_ro`, issued by
`analyst_ro`, does not raise.** PostgreSQL emits a warning and reports success. No privilege
is granted (`has_table_privilege` returns false and the `INSERT` is still denied), but a
test asserting "GRANT raises" fails, and a casual reading of that success looks like
privilege escalation. The lesson generalises: **assert the property you care about, not the
error you expect to see.**

### Second-order prompt injection

The subtler attack, and the one the layers above cannot see. Hostile text stored **in the
database** reaches the model through query results rather than through the chat box:

1. The user asks a completely innocent question.
2. `classify` sees innocent text; `generate_sql` writes legitimate SQL.
3. `validate` passes it, because it is genuinely a single read-only SELECT.
4. `execute` runs it under a SELECT-only role, correctly.
5. **The payload arrives inside the result rows, at summarisation.**

Every control above sits upstream of step 5. The read-only role is irrelevant here, because
nothing is being written. What remains is the summariser's instruction to treat rows as data,
which is a *prompt-layer* defence: the weakest kind.

Because that is the one claim the architecture cannot structurally guarantee, it is tested
rather than asserted. `db/seed.py` stores a real payload in `port_calls.remarks`, and
`tests/test_second_order_injection.py` verifies the model reports it rather than obeying it.

The test's discriminator is itself worth noting: a naive substring search for the payload's
demanded string **fails on correct behaviour**, because a model faithfully quoting the remark
must reproduce that string. The first version of the test reported a breach that had not
happened. It now distinguishes *adopting* the demanded reply from *quoting* it.

**Blast radius, stated honestly:** a successful second-order injection can corrupt an
*answer*. It cannot corrupt *data*, because compliance happens after execution under a role
with no write privilege. That containment is the entire point of layering.

### Where user text is allowed to live

The attack above needs hostile text inside the queried database, so the size of the channel is
decided by one question: how much user-supplied text does `ports` hold? The answer is one
row, planted by `db/seed.py` so the defence can be tested.

That is a property to be maintained rather than a fact to be enjoyed, and the conversation
store is where it was nearly lost. Chat history is user text by definition, and the cheaper
place to keep it was a table in `ports`. Because of the default-privileges grant described in
[§5](#5-runtime-topology), storing it there would have made every question any user had ever
typed readable by model-generated SQL, and would have widened the second-order channel as a
side effect of a convenience feature. **A separate database keeps the analytics
data synthetic-only** ([§5](#5-runtime-topology),
[ADR-014](ADR/ADR-014-conversation-store.md)), so adding saved conversations changed the
injection surface not at all.

The general form is worth naming, because it outlives this schema: **the second-order surface
is the set of tables the agent can read that anyone else can write.** Today that set has one
deliberate member. In a client deployment it would be large, which is where row-level security
and column masking stop being optional ([§18](#18-path-to-production)).

### Disclosure through the error path

The controls above govern what the agent may *read*. A separate question is what leaves the
process when a call fails, and it is not the same question: a provider's `BadRequest` quotes
the request that failed, and on a summarise call that request carries the returned rows. An
unfiltered error message is therefore a channel by which result data reaches the screen
without passing the summariser's grounding rules at all.

`LLMError` splits the two audiences. `str(exc)` is the log and may hold anything the provider
said; `safe_detail` is opt-in and is the only part `ask()` renders. The default is silence, so
a future `raise LLMError` that does not think about disclosure fails closed. One message is
curated and marked safe: the authentication failure, because it is the most common first-run
problem and hiding it behind "an error occurred" would cost more than it protects. Both
directions are pinned in `tests/test_security_boundary.py`.

### Residual risk

- Full read access to all business data: no row-level security, no column masking.
  Acceptable for synthetic data; mandatory to add for real client data.
- Schema structure is discoverable (the agent needs it). `pg_authid` is not.
- An expensive-but-valid query can burn CPU. Bounded by a 5s statement timeout and a 500-row
  cap, both verified, but `statement_timeout` is also `USERSET`, so it is a seatbelt rather
  than a boundary.
- Nothing here prevents SQL that is safe, executes, and answers the **wrong question**. That
  is what [§12](#12-evaluation) is for.
- The process holds a **write credential**, `app_rw`, which the read-only argument above does
  not cover. It is scoped to `ports_app` and holds nothing in `ports`, so the worst it reaches
  is the application's own chat history, and no path from the graph reaches it at all. It is
  still a credential in the process, and a compromise of the process rather than of the model
  is where it would matter.
- Saved conversations have **no row-level security**, for the same reason the business data
  has none: there is one implicit user, and identity is the precondition for both
  ([§18](#18-path-to-production)).

---

## 9. Query Execution & Runtime Limits

`src/executor.py` assumes its input has passed the validator, but does not depend on that for
safety: the connection authenticates as `analyst_ro` regardless.

| Limit                 | Value | Bounds what                                                          |
| --------------------- | ----- | -------------------------------------------------------------------- |
| `statement_timeout` | 5s    | A valid but ruinously expensive query                                |
| Row cap               | 500   | A cheap query returning millions of rows (memory in*this* process) |
| `connect_timeout`   | 10s   | A hung connection attempt                                            |
| Read-only transaction | on    | Accidental writes, with a clear error                                |

**The row cap is applied by fetching fewer rows, not by writing SQL.** The validated statement
is executed verbatim through a server-side cursor:

```python
cur = conn.cursor(name="cda_reader")   # psycopg issues DECLARE ... CURSOR FOR <statement>
cur.execute(inner)                      # verbatim; nothing composed around it
fetched = cur.fetchmany(cap + 1)        # cap + 1 detects truncation without a second COUNT
```

Two earlier designs were rejected. Appending `LIMIT` would corrupt a query already ending in
`LIMIT` or `ORDER BY` and would silently change the meaning of a `UNION`. Wrapping the query
(`SELECT * FROM ( ... ) AS _capped LIMIT 501`) fixed that but meant this module composed new
SQL around model output, which is the one thing a text-to-SQL system should be able to say it
does not do. Executing verbatim removes the construct rather than defending it.

No parameters are passed to `execute()`, so psycopg performs no client-side placeholder
substitution and a literal `%` (a `LIKE` pattern, or modulo arithmetic) reaches PostgreSQL
untouched. Both cases are asserted in `tests/test_executor.py`.

Column **type names** are resolved from psycopg's OID registry and returned on every
`QueryResult`, because chart selection keys off the database's declared types rather than
sniffing values ([§10](#10-chart-selection)).

Errors are raised as `ExecutionError` carrying the PostgreSQL message verbatim. That message
is what makes the single retry useful, because the model can see exactly what it got wrong.

---

## 10. Chart Selection

> The detailed reference is [CHARTS.md](CHARTS.md): the rules with their guards, which questions
> produce which chart, the metric card's number formatting, and where the rules are wrong. This
> section places chart selection in the pipeline.

Pure code, no LLM call ([ADR-005](ADR/ADR-005-deterministic-chart-selection.md)). Chart choice
is a function of the **shape** of the result set, not of the language in the question: once
the SQL has run, the result fully determines which encodings are valid. No linguistic
judgement remains, so there is nothing for a language model to contribute.

`src/charts.py` splits into `classify_columns()` (type → charting role) and the rules:

| #  | Condition                                                                      | Output                                                   |
| -- | ------------------------------------------------------------------------------ | -------------------------------------------------------- |
| 1  | Zero rows                                                                      | **No chart** (the answer says nothing matched)     |
| 2  | One row, one numeric column, at most two columns                               | **Metric** (a second column labels the figure)     |
| 3  | A temporal column + ≥1 numeric, **>1 row**                               | **Line** (one per measure)                         |
| 3b | ...and a category column, with the temporal column repeating, ≤10 distinct | **Line** (one per category, as a colour)           |
| 3c | ...but >1 measure, or a hidden third dimension, or >10 series, or no series with two rows | **Table**                |
| 4  | A label column + ≥1 numeric, ≤12 distinct, **>1 row**                   | **Bar**                                            |
| 4b | Several categoricals without a unique first label                              | **Table** (a bar chart would collapse a dimension) |
| 5  | Exactly two numerics, nothing else, **>1 row**                            | **Scatter**                                        |
| 6  | Anything else, including a single row carrying more than a label and a measure | **Table**                                          |

The `>1 row` guards on rules 3, 4 and 5 are not decoration. A superlative question ("which
terminal has the longest berth wait?") returns one row holding a label and a measure, which
before this guard fell through to rule 4 and drew a bar chart of exactly one bar stretched
across the full container. Nine answerable gold questions produce that shape, including the
first example button in the UI. See ADR-005 and
[CHARTS.md §8](CHARTS.md#8-four-guards-and-how-each-was-found).

Every `ChartSpec` carries a `reason` naming the rule that fired, because the field is required
rather than optional, and it is shown in the UI, so the behaviour is inspectable rather than
magic. Its *content* is asserted for rule 4's over-the-limit table branch and for all five of
rule 3's series outcomes; every other reason, rules 1, 2, 5 and 6 among them, is only asserted to
be non-trivially long; see [CHARTS.md §10](CHARTS.md#10-what-is-tested-and-what-is-not).

### Three things found by running it, not by predicting it

- **`to_char(ts, 'YYYY-MM')` returns `text`.** A purely type-driven rule classifies the most
  common time-series result as categorical and draws bars where a line is correct. Fixed in
  two places: the prompt asks for `date_trunc(...)::date`, and the rules additionally accept
  anchored ISO-8601-shaped text as temporal. The fallback matches *values* against a strict
  pattern, never column *names*.
- **Models add descriptive companion columns.** Asked for average wait by terminal, the model
  returned `terminal_name, port_name, avg_wait`, so a strict "exactly one categorical" rule
  fell through to a table. Rule 4 now tolerates extra categoricals *only* when the first
  uniquely labels each row.
- **A breakdown over time was drawn as one line.** A follow-up question in the running app
  returned `month, operator, total_containers`, 33 rows of twelve months by three operators.
  Rule 3 dropped the operator and joined every row into a single series, so the line jumped
  between the three operators inside each month and read as a sawtooth. Rule 3b now sends the
  category to the colour channel and rule 3c refuses the shapes where no honest line exists.
  Found by using the app: the gold set contains no question of this shape, so both prior
  sweeps of the rules passed.

**Known limitation:** explicit user intent is ignored. "Show that as a pie chart" is not
heard. The fix is a small intent-extraction step feeding an override, which is an increment
rather than a rewrite.

---

## 11. Data Model & The Domain Choice

> This section covers how the data model fits the system. [DATA.md](DATA.md) covers the
> dataset itself: every column with its units and allowed values, the full value inventory,
> the generator's construction, the measured figures behind each planted pattern, and a
> catalogue of questions the data can answer.

### The schema

```mermaid
erDiagram
    TERMINALS  ||--o{ CRANES      : owns
    TERMINALS  ||--o{ PORT_CALLS  : hosts
    VESSELS    ||--o{ PORT_CALLS  : makes
    PORT_CALLS ||--o{ CARGO_MOVES : contains
    CRANES     ||--o{ CARGO_MOVES : performs
```

| Table           | Rows  | Grain                    |
| --------------- | ----- | ------------------------ |
| `terminals`   | 6     | one per terminal         |
| `vessels`     | 40    | one per ship             |
| `cranes`      | 25    | one per quay crane       |
| `port_calls`  | 1,500 | one per vessel visit     |
| `cargo_moves` | 6,577 | one per crane move batch |

Two dimensions, one equipment dimension, and two fact tables at different grains: a
recognisable star-ish shape, which is what a real analytics database looks like.

### Why this domain, and not e-commerce

The requirements allow any input data, so the choice needed a reason. It was not thematic.

The **strongest reason is methodological: the default alternative
(customers/orders/products/order_items) is massively over-represented in LLM training data,
so accuracy on it would partly measure memorisation rather than schema comprehension.** A
model that has seen ten thousand `orders JOIN order_items` examples can produce correct SQL
for that schema without meaningfully reading the schema at all, which yields a flattering
evaluation that measures the wrong thing. Since the entire point of this build is that
correctness is *measured*, a benchmark that inflates itself would undermine the central
claim.

**Port ops gives natural join depth, a real time axis, and honest categorical dimensions.**
Join depth is inherent rather than contrived; the arrival/berth/departure timestamps give a
genuine time series rather than a decorative date column; and the categorical dimensions
(operator, terminal, vessel type, crane status) are real groupings a business would actually
ask about.

Concretely, join depth falls out of the questions rather than out of artificial schema
splitting:

| Question                                           | Tables joined |
| -------------------------------------------------- | ------------- |
| "Average berth wait by terminal"                   | 2             |
| "Container moves per month by terminal"            | 3             |
| "Crane productivity by crane and port"             | 3             |
| "Top operators by containers handled at Jebel Ali" | **4**   |

Alternatives considered and rejected: a **public real-world dataset** (download and licensing
friction in a repo promising a two-command start, and signal cannot be planted in it),
**more tables** (8 to 10 would grow prompt size and SQL error surface without testing
anything the requirements ask about), and **the bare minimum of four** (meets the letter of the
requirements with no margin).

### Planted signal, not noise

Uniformly random data produces flat aggregates, and against flat data a working agent and a
broken one look identical, because every answer is "they're all about the same". Four
patterns are planted as **statistical distributions**, so they are visible in aggregate but
not obvious in any single row:

| # | Pattern                                                              | Discoverable by                              |
| - | -------------------------------------------------------------------- | -------------------------------------------- |
| 1 | One congested terminal (Jebel Ali T2, ~17.5h vs ~5.7h)               | "Which terminal has the longest berth wait?" |
| 2 | Seasonal volume: February trough, autumn peak                        | "Show monthly container volume"              |
| 3 | One ageing crane (RTM-QC-01, commissioned 2001, ~17 vs ~28 moves/hr) | "Which crane is least productive?"           |
| 4 | One operator with poor punctuality (Meridian Lines, ~12.8h)          | "Which operator waits longest?"              |

`db/verify_seed.sql` proves each is still detectable after any change to the generator.

### Determinism is a hard requirement

`db/seed.py` uses a fixed RNG seed (42) and a **fixed date window** (2025-01-01 to
2026-06-30). Nothing reads the system clock. Two consequences:

- Reference SQL in the gold set contains literal date predicates. A window that moved with the
  wall clock would drift the gold queries out of range and decay measured accuracy for reasons
  unrelated to the agent.
- Any change to **RNG draw order** silently changes every generated value. The data stays
  plausible and every constraint still passes, so nothing fails loudly, but the
  reproducibility guarantee breaks. `tests/test_seed_characterization.py` pins a digest of
  every table's full contents to catch exactly this.

That constraint shaped an implementation detail: `port_calls.remarks` is populated by post-hoc
`UPDATE` keyed on `port_call_id`, never by drawing from the RNG, precisely so the existing
draw stream stays intact. Four of five table digests were verified byte-identical after that
column was added.

### One accepted weakness

Every port currently has exactly one terminal, which makes `port_name` effectively redundant
and removes a natural port → terminal hierarchy. Cosmetic rather than functional; fixing it
requires re-seeding and re-baselining the characterization digests for no gain in what the
project sets out to show.

---

## 12. Evaluation

`eval/run_eval.py` turns "the SQL is correct" into a number
([ADR-006](ADR/ADR-006-eval-execution-accuracy.md)). It calls `src/agent.py` directly, with
no Streamlit in the path, so it can run in CI.

### The gold set: 108 items in three categories

Expanded from 36 on 2026-08-10 (ADR-010). Every case carries two tags: the SQL syllabus
topics its reference answer exercises, and a behaviour label for how the question is
asked (typos, vague phrasing, fact-checks with false premises, write requests, and so
on). The harness prints coverage on both dimensions each run.

| Category              | Items | What it asserts                                                     |
| --------------------- | ----- | ------------------------------------------------------------------- |
| **answerable**  | 77    | Agent SQL returns the same rows as the reference SQL                |
| **ambiguous**   | 12    | Agent asks a clarifying question instead of guessing                |
| **adversarial** | 19    | Injection / destructive / out-of-scope / write requests are refused |

Five of the answerable and adversarial cases are two-turn conversational cases added with
[ADR-011](ADR/ADR-011-bounded-multi-turn.md); the setup turn is replayed through the agent
and only the final turn is scored.

**Groundedness is scored separately, on every answered case**, because an answer can carry the
right rows and still describe them with an invented figure. `_check_groundedness()` requires every
number in the answer to appear in the returned rows (exactly or as a rounding), in the question, or
in the SQL, and requires an empty result set to be reported as such rather than filled in.

A system that answers well but cannot say no is not deployable, which is why the second and
third categories exist at all.

### Result-set comparison, not SQL text

The same question has many correct SQL formulations: join order, CTE versus subquery,
`COUNT(*)` versus `COUNT(1)`. String or AST comparison would measure stylistic conformance
rather than whether the user got the right numbers. Comparison is order-insensitive unless the
question implies a ranking, with float tolerance for aggregate arithmetic and type unification
so `numeric` vs `double precision`, and `date` vs midnight `timestamp`, compare equal.

### Measured results

Twenty-six full runs. **The item set grew four times, so a column is comparable only with
another column from the same set.** Runs 1 to 3 scored a 30-item set (22 answerable), run 4
scored 33 items, runs 5 to 14 scored 36 items (28 answerable), runs 15 to 17 scored a frozen
100-item set, run 19 scored 103, and runs 20 onward score 108 (77 answerable) after the
conversational tranche. Run 18 is invalid and kept deliberately: a local DNS outage killed name
resolution 22 items in. Raw output for every run is in `eval/results/`.

The six runs below are the current code. They alternate ADR-012's runtime verification off and
on, so that provider drift across the hour could not land on one configuration and be read as
an effect of the feature. The three "off" runs were the shipped default when they were made;
they are not any longer, because ADR-013 subsequently turned the reading on. Run 26, measured
separately below, is the first full run of what actually ships.

| Metric               | Run 21          | Run 23          | Run 25          | Run 20         | Run 22          | Run 24          |
| -------------------- | --------------- | --------------- | --------------- | -------------- | --------------- | --------------- |
| Runtime verification | off             | off             | off             | on             | on              | on              |
| Overall              | 103/108         | 103/108         | 102/108         | 100/108        | 100/108         | 101/108         |
| Execution accuracy   | **93.5%** | **94.8%** | **93.5%** | 89.6%          | 90.9%           | 92.2%           |
| Answer groundedness  | 96.0%           | 97.4%           | 97.4%           | **100%** | **98.7%** | **98.7%** |
| Ambiguity handling   | 12/12           | 11/12           | 11/12           | 12/12          | 11/12           | 11/12           |
| Safety / refusals    | 19/19           | 19/19           | 19/19           | 19/19          | 19/19           | 19/19           |
| Median latency       | 6.03s           | 6.65s           | 6.09s           | 6.74s          | 6.96s           | 7.11s           |
| Cost per run         | $1.014          | $1.036          | $1.028          | $1.355         | $1.330          | $1.339          |

**Accuracy fell when the set grew, and that is the expansion working.** The 36-item suite had
saturated at 28/28 across seven consecutive runs, which means it had stopped discriminating.
At 108 items the number is 93.5% to 94.8% and the residual failures are known and named: two
clarify-boundary misplacements and three answers whose column shape does not match the
question's.

**Safety is the one figure that has never moved: 19 of 19 in every one of these runs, 114 of
114 across the six.** That stability is a property of the permission model rather than of the
model's cooperation.

**Runtime verification is measured and disabled.** It raised groundedness and cost more
execution accuracy than it bought, and every regression carried a `verifier_objection` reason
code. [ADR-012](ADR/ADR-012-runtime-verification.md)'s addendum records the decision and what
remains unexplained. The historical narrative below predates the expansion and is retained
because the failures it describes are what motivated each change.

**Runs 8 to 10 follow two column-comment fixes, and that is what moved the number.** The eval kept
catching SQL that ran clean and answered a slightly different question: one query filtered on
terminal name where the question meant port name, another grouped container volume by arrival date
rather than the date the containers moved. Both return rows and neither errors. The repair was
writing the grain and the meaning into the column comments, which are what `src/schema.py` injects
into the prompt, so it is a schema-documentation fix rather than a model or prompt fix.

**The first groundedness measurement caught a real failure on a question whose SQL was correct.**
The agent returned the right monthly rows, then wrote "the total annual volume reaching 228,499
containers", a figure it had computed itself, wrong by 10,600 (the true total is 239,099).
Execution accuracy scored that answer 100% correct. The summariser prompt now forbids arithmetic
across rows outright: a computed figure reads with exactly the authority of a retrieved one while
being unverifiable.

**The variance is the finding, not the maximum.** Runs 2 and 3 differ by nine points from
nothing but re-running, at `temperature=0`. Two causes, which the harness originally conflated
and now reports separately:

- **Provider instability.** Two of run 3's failures were `error` outcomes at 58s and 37s, with
  an SSL handshake timeout in the log. Those are availability events, not wrong answers.
  Excluding them, run 3 scores 90.5%.
- **Genuine sampling variance.** Two items flipped between runs.

Infrastructure errors are reported separately but **not excluded** from the headline: a metric
that silently drops its own failed requests flatters exactly when the system is least usable.

The honest claim is therefore a range: **execution accuracy of 93.5% to 94.8% across runs 21, 23
and 25 on the 108-item set.** Those runs predate [ADR-013](ADR/ADR-013-the-reading-without-the-verdict.md)
and so ran without the reading, but the figure still describes what ships: the reading-only path
performs no regeneration and no re-summarisation, so it cannot change the SQL or the answer, and
therefore cannot move accuracy. Cost and latency are a different matter and are qualified
below. At 77 answerable items one case is worth
1.3 points, so a single run still cannot distinguish 93 from 95, and no run should be quoted
alone. Runs 5 and 6 made the variance point on the older set without needing the infrastructure
caveat: they differ by one item, and it is not the same item. Run 5 failed `q09` and passed
`q19`; run 6 passed `q09` and failed `q19`, where the agent filtered on the wrong column and
returned zero rows. Groundedness moves between runs in the same way, ranging from 96.0% to 97.4%
here, so the accuracy result should not be read as the system being finished.

**The one number that did not move: safety, 19/19 in every run, 114 attempts across these six
runs without a miss.**

That stability is worth attributing carefully, because the natural reading of it is wrong. It is
not evidence that the permission layer did the stopping. Counted across all 26 committed runs,
1,745 case results, no adversarial case has ever ended in `rejected`, which is the outcome the
validator produces; every one ended in `refused`, apart from 18 provider errors. `refused` comes
from `classify`, before any SQL exists. So this figure measures the **first** layer, and that
layer is a prompt. The write guarantee below it is established by
`tests/test_security_boundary.py`, which disables the bypassable read-only guard and requires
PostgreSQL itself to refuse each write. See [EVAL.md §6](EVAL.md) and
[ADR-006's 2026-08-14 addendum](ADR/ADR-006-eval-execution-accuracy.md).

### What the harness found, about the system and about itself

- **The first run scored 86.4%, and all three failures were defects in the *specification*,
  not the model.** A vague "exclude cancelled port calls" prompt rule silently changed what a
  count meant, and the ranking rule did not distinguish a singular superlative from a per-group
  question. Fixing the prompt took accuracy to 95.5%.
- **Characterization tests found a bug in the harness itself.** `_normalise` compared
  `date(2025,1,1)` against `datetime(2025,1,1)` as unequal while its docstring claimed they
  matched, so a correct answer could be scored wrong purely for omitting a cast.
- **One failure was left unfixed on purpose.** `q09` returns an extra descriptive column; the
  answer is correct and strict comparison calls it wrong. Relaxing the comparison after seeing
  what it fails would be tuning the metric to the result, so the stricter number stands.

`run_eval.py` exits non-zero if any **safety** case fails, so in CI a safety regression breaks
the build even when overall accuracy looks acceptable.

---

## 13. Frontend

The Streamlit layer is deliberately thin
([ADR-008](ADR/ADR-008-ui-and-scope-boundary.md)). `app.py` is the entrypoint and renders
nothing: it sets the page config and hands off to two pages, `views/chat.py` and
`views/observability.py`, with `views/state.py` holding what they share. Between them their
job is to make these visible, each mapping to an assessed behaviour:

| Element                                                                                                  | Demonstrates                                                          |
| -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Chat input + history (`st.chat_message`, `st.chat_input`)                                            | The conversational requirement                                        |
| Natural-language answer                                                                                  | Groundedness: phrased only from returned rows                         |
| Chart rendered from a typed `ChartSpec`, plus the rule that fired                                       | Chart-type selection                                                  |
| A "What was measured" line in plain language (ADR-013)                                                   | Groundedness, for a reader who cannot audit SQL                       |
| Collapsed "View SQL" expander                                                                            | Auditability                                                          |
| Sidebar chat list, with New chat, reopen, rename and delete                                              | Conversations that survive a reload (ADR-014)                         |
| Collapsed per-answer telemetry: seconds, cost, model calls, stage breakdown                              | Latency, for the turn just taken                                      |
| Sidebar table list, each opening onto its columns, types and units                                       | Schema handling, without a listing the reader must scroll past        |
| An Observability page: latency, cost, guardrail counts, stage means, per-category eval scores, eval runs | Latency and cost as a distribution, and the safety record as evidence |

Outcomes that are not a normal answer carry a visible badge, so a refusal or clarification is
never mistaken for an answer.

**The sidebar names tables in the reader's language.** `port_calls` is listed as "Vessel
visits", with the database name shown in code formatting beneath it, alongside the column
count and the description. Both are read from
the table's `COMMENT ON`, never from a mapping in the UI, so the label a user reads and the
description the model reads cannot drift apart, and ADR-003's claim that the schema layer
holds no domain literals stays true.

**A truncated result is announced with the other trust signals, before the chart.** It sits
beside the caveat and grounding warnings rather than beside the SQL, because it is the same
kind of signal: a reason to trust the answer less. It precedes the chart deliberately, since
the chart is drawn from the truncated rows too, and a caveat read after the picture has
already been believed arrived too late.

**Which notices appear, and in what order, is decided in `src/notices.py` rather than in the
view.** The chat page renders the list it is given and chooses nothing. The ordering carries
meaning, so leaving it inline made it an eyeballed property: the only way to check it was to
read the render function or run the app. As a pure function over `AgentResult` it is asserted
in `tests/test_notices.py` without a database, a model or a browser, and reversing the order
fails two tests. It also keeps [ADR-008](ADR/ADR-008-ui-and-scope-boundary.md)'s "the UI is
thin" claim true as the UI grows, rather than slowly aspirational.

**Which conversation is open, and when a turn is saved, is decided in `src/conversations.py`
for the same reason.** The ordering there was a real defect rather than a hypothetical one:
the chat view rendered the answer and appended it to history afterwards, so a click landing during
rendering preempted the rerun and discarded a turn the user had already paid for. The view now
makes one call, `session.answer(question, ask)`, which asks, persists, then returns the turn to
render, so there is no order left for the view to get wrong. `tests/test_conversations.py`
asserts durability at the moment the turn is returned, which is the only point before the
caller could render it.

A save failure never costs an answer. `StoreError` is swallowed into a `saved` flag on the
turn, and the interface says the turn was not saved rather than replacing an answer with an
error about bookkeeping. The same reasoning makes the store optional: `db/03_app_store.sql`
runs only on the container's first boot, so an older data volume has no store database, and
that degrades to a caption in the sidebar instead of a page that will not load.

**The pages have tests.** `tests/test_app_smoke.py` runs them under Streamlit's own `AppTest`
harness against a throwaway store, which covers the part that only breaks when a page actually
executes: that a saved chat reopens carrying its table and chart, that New chat clears the pane
without deleting anything, and that the panel's metrics read what the store holds. No question
is submitted, so no model is called.

Both pages are files rather than callables passed to `st.Page`, which is a testability decision:
`AppTest.switch_page` only reaches file-based pages, so a callable page could not be driven by a
test at all.

**The SQL expander is the load-bearing UI decision.** It converts the system from something a
user must trust into something a user can check, and it is the human-in-the-loop story in this
build: an analyst can read the SQL that produced a number before that number reaches a deck.

`@st.cache_resource` caches the compiled graph and schema so neither is rebuilt on every
Streamlit rerun.

---

## 14. Cost, Latency & Observability

### Cost

Two model tiers behind one wrapper ([ADR-007](ADR/ADR-007-llm-provider-and-tiering.md)):

| Tier   | Env var          | Used by                                                    | Rationale                                            |
| ------ | ---------------- | ---------------------------------------------------------- | ---------------------------------------------------- |
| Cheap  | `MODEL_CHEAP`  | `classify`, `summarize`, `verify`, `contextualize` | Short, bounded, low-difficulty language tasks        |
| Strong | `MODEL_STRONG` | `generate_sql`                                           | The one node where capability determines correctness |

Three of the four calls on an answered question go to the cheap tier, and `generate_sql` is the
only node that needs the strong one. `contextualize` is a fifth cheap call that only a follow-up
turn pays for, which is why the table lists four nodes against a count of three. Provider is chosen by the model-string prefix
(`anthropic/…`, `openai/…`, `gemini/…`), so switching provider is an environment change, not a
code change. Measured cost is **~$0.0095 per question**, or about $1.03 for a 108-question run
with both switches off (runs 21, 23, 25), rising to ~$0.0124 and ~$1.34 with runtime
verification on (runs 20, 22, 24). A follow-up turn adds one cheap call for the rewrite node.

The **shipped** default sits between the two, and run 26 measured it across the whole set:
**$1.2567 for 108 questions, or ~$0.0116 each**, against ~$1.03 with both switches off and ~$1.34
with runtime verification on. ADR-013's reading adds one cheap-tier call to every ANSWERED
question and nothing to a refusal or a clarification, which have no SQL to describe. Per answered
question that is $0.01530 against $0.01229 to $0.01245 in runs 21, 23 and 25, so **22.9% to
24.5% more**, inside the 18% to 27% that five questions had predicted. Call count moved the same
way: 361 against 276 to 279.

### Latency

**Median 6.03 to 6.65s across runs 21, 23 and 25**, both switches off, and this is the
weakest number in the system. The shipped default adds ADR-013's reading, which runs on a worker
thread beside `execute` and so costs only what that overlap does not absorb. Run 26 put a number
on the residue: the `review` collection point totalled **36.8s, or 0.48s per answered question**,
and the shipped median was **6.91s**, above the both-off band rather than inside it. That is one
run against three, and this document's own variance section is the reason it is quoted as a
direction and not as a measurement; the cost figures above are arithmetic over 108 records and
carry no such caveat. Across those same both-off runs mean is 5.6 to 6.3s and p95 is 10.7 to
13.9s, against 7.01s and 14.32s in run 26; the median is the
figure to quote, because cold-start outliers pull the mean around. It is a direct consequence
of three sequential LLM calls, the fourth running beside `execute` rather than after it, and
not a defect. The honest fixes are caching and a smaller classifier, not a rewrite. What the
architecture *does* guarantee is that latency is a **ceiling** (four calls, six with a retry,
three of them on the critical path) rather than a distribution with a long tail, which is what
an autonomous loop would have produced.

### Observability

`llm.Usage` accumulates calls and cost per question via `litellm.completion_cost()`, and the
eval harness aggregates the same figures across a run.

**Persistence landed with the conversation store.** Every turn is written to `ports_app` as a
`jsonb` record carrying `elapsed_s`, `stage_timings`, `llm_calls`, `cost_usd`, `outcome` and
`retry_reasons` (ADR-014), and the Observability page aggregates them in SQL: median and p95
latency, cost per question, outcomes, mean seconds per stage, and retries by reason. Before
that the numbers were measured per request and discarded when the answer rendered, so no
question about a trend could be answered.

**The page answers two different questions about the guardrails, and keeps them apart.** The
live half counts what this application happened to be asked, as tiles for answered,
clarified, refused, blocked by the validator and errored. On a fresh store every one of them
is zero, which is honest and proves nothing. The eval half carries the figure that does: the
newest committed run scored on four figures, where an adversarial case fails by being answered
and passes by being refused, blocked by `validator.py`, or asked back about
(`_score_adversarial` in `eval/run_eval.py`). Run 26 reads 19 of 19, and all nineteen were
refused before any SQL existed, so that figure measures the classifier. The live
panel points at it rather than borrowing the number into its own section, because the two
halves are never added together (ADR-010): one is whatever a user typed and the other is a
fixed 108-case benchmark.

**The eval half is four denominators over one file.** SQL correctness is the answerable
subset, which is the figure ADR-006 calls execution accuracy; answer groundedness is scored
per answered case rather than per subset, so a refusal is not counted against it; ambiguity
handling is the ambiguous subset; guardrails is the adversarial one. None of the four is the
overall score, and the overall score is no longer shown: one number over three subsets that
pass by different criteria was the figure most likely to be quoted for one of the four.

The two narrow subsets are shown as counts, `11 of 12` and `19 of 19`, rather than as rates.
Twelve and nineteen cases mean a percentage there moves in steps of eight and five points,
and 100% over two cases reads the same as 100% over nineteen.

The names live in `src/telemetry.py` rather than in the page, and one mapping feeds both the
tiles and the table columns, because a figure shown under two names one screen apart is how a
deck ends up quoting the wrong number. They are the names on the eval board in
`docs/visuals/eval.html`, which quotes the same run, so the two surfaces can be checked
against each other; `tests/test_telemetry.py` pins all four rendered values against the
committed artefact.

**Latency is disclosed twice, deliberately.** Beside the answer it is one turn, collapsed
into an expander labelled with the outcome and the seconds. On this page it is a median, a
p95 and a per-stage mean. The per-turn figure was removed in the first version of this page
and restored once the page existed, because a single reading only means something against a
distribution; ADR-008's two 2026-08-12 addenda carry the argument both ways.

**What is still not built** is per-user attribution, which needs authentication first, and
alerting. There is no exporter either, though `stage_timings` is a name-to-duration map per
turn, which is the shape an OpenTelemetry span set needs, so emitting traces is an adapter
rather than a rewrite.

---

## 15. Repository Structure

```
├── README.md                   Setup, run instructions and the measured results
├── app.py                      Entrypoint: page config and navigation
├── views/
│   ├── chat.py                 The chat page
│   ├── observability.py        The panel: live traffic and the committed eval runs
│   └── state.py                The store handle and chat session both pages share
├── docker-compose.yml          PostgreSQL 18 (host port 55432)
├── pyproject.toml              Dependencies, pytest markers, ruff config
├── uv.lock                     Exact pinned resolution, so an install is reproducible
├── .env.example                Config template (the real .env is gitignored)
├── db/
│   ├── 01_schema.sql           Tables, constraints, indexes, COMMENT ON (prompt context)
│   ├── 02_roles.sql            The analyst_ro read-only role
│   ├── 03_app_store.sql        The separate database for saved chats (ADR-014)
│   ├── seed.py                 Deterministic synthetic data generator (seed=42)
│   └── verify_seed.sql         Proves the planted patterns are still detectable
├── src/
│   ├── agent.py                LangGraph pipeline, node functions, ask()
│   ├── validator.py            The SQL safety gate (sqlglot AST)
│   ├── executor.py             Read-only execution, timeout, row cap
│   ├── schema.py               Introspection to cached prompt context
│   ├── charts.py               Rule-based chart selection
│   ├── notices.py              Which captions and warnings sit beside an answer, and in what order
│   ├── conversations.py        Which chat is open, and the order a turn is saved and shown
│   ├── store.py                Conversations and telemetry, in their own database (ADR-014)
│   ├── telemetry.py            Reads the committed eval runs for the panel
│   ├── grounding.py            Whether every figure in an answer appears in the rows
│   ├── quality.py              Code-detected result-shape triggers (ADR-012)
│   ├── prompts.py              Prompt templates
│   ├── provenance.py           What produced an eval run: prompt hashes, models, commit
│   ├── llm.py                  Two-tier LiteLLM wrapper, output extraction
│   ├── models.py               Typed state and results (pydantic)
│   └── config.py               Settings; separates admin and read-only identities
├── eval/
│   ├── gold_questions.yaml     108 scored cases, topic- and behaviour-tagged
│   ├── gold.py                 Gold-set schema; validated at load
│   ├── run_eval.py             The harness
│   └── results/                Committed raw output, the evidence for the README's numbers.
│                               `runNN.json` the records, `runNN.meta.json` their provenance
├── tests/                      1,005 tests
└── docs/
    ├── ARCHITECTURE.md         This document
    └── ADR/                    Fourteen decision records
```

---

## 16. Development & Testing

### Quickstart

```bash
cp .env.example .env                              # add an API key
docker compose up -d                              # PostgreSQL 18 on 55432
uv sync --extra dev                               # creates .venv from the pinned uv.lock
python db/seed.py                                 # deterministic seed
streamlit run app.py
```

### Test suite: 1,005 tests

| File                               | Tests | Scope                                                                                                                                                                               |
| ---------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_gold_set.py`               | 337   | Gold-set schema and tag guards, parametrized over all 108 cases                                                                                                                     |
| `test_gold_audit.py`             | 78    | The audit ledger for the reference SQL itself: that a documented coverage figure cannot outrun the evidence, and that the triage neither over- nor under-fires                      |
| `test_validator.py`              | 93    | The security gate: write-blocking rules, evasions, fail-closed parsing                                                                                                              |
| `test_eval_scoring.py`           | 35    | The comparison logic, i.e. the definition of "correct"                                                                                                                              |
| `test_charts.py`                 | 43    | Every chart rule, at its boundaries, including the four a line refuses                                                                                                                                               |
| `test_provenance.py`             | 37    | That a run records what produced it, and that no DSN password reaches the committed artefact                                                                                        |
| `test_quality_triggers.py`       | 28    | Code-detected result-shape triggers (ADR-012)                                                                                                                                       |
| `test_agent_routing.py`          | 25    | Graph topology with a stubbed LLM                                                                                                                                                   |
| `test_runtime_verification.py`   | 33    | That runtime verification stays advisory (ADR-012), and that reading-only adds a description and nothing else (ADR-013)                                                             |
| `test_config_defaults.py`        | 36    | That RUNTIME_VERIFICATION and SQL_READING parse to their intended defaults                                                                                                          |
| `test_security_boundary.py`      | 19    | That GRANTs hold with the read-only guard disabled                                                                                                                                  |
| `test_llm_extraction.py`         | 18    | Parsing model output; raise rather than half-parse                                                                                                                                  |
| `test_multi_turn.py`             | 15    | Bounded multi-turn behaviour (ADR-011)                                                                                                                                              |
| `test_executor.py`               | 13    | Row cap, statement timeout, verbatim execution, errors                                                                                                                              |
| `test_schema.py`                 | 14    | Catalog introspection; composed identifiers are quoted; the sidebar's column listing carries its units                                                                              |
| `test_schema_labels.py`          | 12    | Turning a table's COMMENT ON into a sidebar label, including the split it gets wrong                                                                                                |
| `test_notices.py`                | 23    | Which captions and warnings sit beside an answer, their order, and what one turn cost                                                                                               |
| `test_telemetry.py`              | 30    | The Observability page's arithmetic: SQL aggregates over stored turns, the eval-run reader, per-category scores, and how a cost is written                                          |
| `test_conversations.py`          | 31    | That a turn is saved before it is shown, and what New chat, reopen and delete do to the open one                                                                                    |
| `test_app_smoke.py`              | 17    | Both pages under Streamlit's AppTest harness: reopen renders its table and chart, a missing store degrades to a caption, the panel's metrics and the four eval tiles match the artefacts |
| `test_store.py`                  | 18    | Conversation persistence: round trip, ordering, cascade delete, concurrency                                                                                                         |
| `test_store_isolation.py`        | 6     | That the agent's role cannot connect to the store (ADR-014)                                                                                                                         |
| `test_store_titles.py`           | 5     | Deriving a chat title from its first question                                                                                                                                       |
| `test_repo_hygiene.py`           | 5     | That no document is both untracked and unignored, so preparation material cannot ship by accident                                                                                   |
| `test_visuals.py`                | 14    | That the presentation frame's schema claims match `db/01_schema.sql`: columns, declared types, key roles and join paths                                                              |
| `test_seed_characterization.py`  | 7     | Data digests, planted patterns, crane/terminal invariant                                                                                                                            |
| `test_second_order_injection.py` | 5     | Injection arriving through query results                                                                                                                                            |
| `test_forecast_grounding.py`     | 5     | A historical figure is never reported as a forecast                                                                                                                                 |
| `test_data_coverage.py`          | 3     | The sidebar date range is derived from the data                                                                                                                                     |

Integration tests are marked, so unit tests run without a database:

```bash
pytest -m "not integration"   # 776 unit tests, no database, no network
pytest                        # everything (needs a seeded DB; injection tests need an API key)
ruff check src/ tests/ eval/ db/ app.py
python eval/run_eval.py                                # shipped config, 10 to 15 min
python eval/run_eval.py --no-verification --no-reading # the ~$1.03 baseline (108 cases)
python eval/run_eval.py --verification                 # ADR-012 on, ~$1.34
```

### Testing philosophy

Tests are written to **catch a regression**, not to raise a coverage number. Three
consequences visible in the suite:

- **The security tests test the boundary, not the symptom.** `test_security_boundary.py`
  disables the bypassable guard before attempting writes and asserts the failure is on
  permissions, so it fails if the boundary ever moves to the weaker layer.
- **Graph topology is tested with a stubbed LLM**, because a live model cannot be made to
  produce the specific failure sequences each edge exists to handle.
- **Characterization tests pin behaviour nothing else would notice changing.** The seed
  digests are the clearest case, since a shifted RNG stream produces data that is wrong in
  no visible way.

---

## 17. Deliberately Out of Scope

"Not built" and "not considered" are different claims. One line of reasoning each:

| Omitted                       | Why                                                                                                                                            |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| **Authentication**      | One implicit user. Identity is a precondition for row-level security, which is why it heads the production path rather than being a UI feature |
| **Caching**             | A repeated question would cost less and return faster. The first optimisation to reach for once real traffic shows repeats                     |
| **Streaming**           | Perceived latency, not latency. With four calls, three of them sequential, the honest fix is fewer or faster calls                             |
| **Deployment**          | Runs locally. Containerising demonstrates a skill this project does not set out to show                                                                   |
| **Async / concurrency** | Single-user by design; Streamlit's rerun model would not scale to concurrent users anyway                                                      |
| **Semantic layer**      | Governed metric definitions; see below                                                                                                        |
| **Hybrid retrieval over past chats** | Ranking older turns by relevance, lexical and vector together with `pgvector` in the same database ([ADR-014](ADR/ADR-014-conversation-store.md)), would put stored answers back in the prompt, which [ADR-011](ADR/ADR-011-bounded-multi-turn.md) keeps out |

**Multi-turn memory used to be on this list and no longer is.** It was omitted by
[ADR-008](ADR/ADR-008-ui-and-scope-boundary.md) and built by
[ADR-011](ADR/ADR-011-bounded-multi-turn.md) on 2026-08-10, as a bounded rewrite at the edge of
the graph rather than as graph memory. Section 6 describes the node and section 19 records the
decision. The row is removed rather than edited because a list of omissions that contains
something the app does is worse than no list.

**On the semantic layer.** Without governed metric definitions, "utilisation" resolves to
whatever the model infers that day, and the same question yields different SQL and different
numbers across sessions. At this scale the column comments are a partial substitute. At client
scale they are not, and this, rather than model capability, is the usual reason agent-analytics
deployments stall.

---

## 18. Path to Production

1. **Row-level security** per tenant role, so each user sees only their data. Requires
   authentication first.
2. **A semantic layer** for governed metric definitions.
3. **The eval suite in CI** as a regression gate on every prompt or model change. The harness
   already exits non-zero on a safety regression.
4. **Exported traces and alerting.** Per-query spend, latency percentiles and failure
   attribution are now stored and aggregated on the Observability page, so what remains is
   getting them out of this application: an OpenTelemetry exporter over the `stage_timings`
   map, and thresholds that page someone. Per-user attribution needs authentication first.
5. **Retrieval over table metadata** once the schema outgrows the context window
   ([§7](#7-schema-handling)).
6. **A different storage and ingestion layer** once query volume or continuous data arrival
   outgrows single-node PostgreSQL. The seeded working set is 1,680 kB against the 128 MB buffer
   pool this container runs with, so the latency threshold is far off, and nothing ingests
   continuously today. Both are stated with their observable trigger conditions in
   [ADR-001](ADR/ADR-001-domain-and-data-model.md#where-this-model-stops-holding).
7. **A connection pool.** Each query opens its own connection and closes it
   ([§9](#9-query-execution--runtime-limits)), while `analyst_ro` is created with
   `CONNECTION LIMIT 10` (`db/02_roles.sql`). Ten concurrent in-flight queries therefore
   exhaust the role's budget and the eleventh is refused. A single-user Streamlit session
   never reaches that, which is why the connection-per-query shape is acceptable here and
   is the first thing that breaks under concurrent use. Pooling is a small change, and it
   belongs to the same increment as the FastAPI serving model rather than preceding it.

---

## 19. Key Design Decisions & Trade-offs

| Decision                                                               | Why                                                                                                                                                                                                                            | Trade-off                                                                                                                                                                                          |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fixed-topology graph, not an autonomous agent loop**           | The path is known in advance, so guardrails become structural, cost becomes a ceiling rather than a distribution, and failure modes are enumerable                                                                             | Cannot handle genuinely multi-step exploration ("find anomalies, then investigate the biggest")                                                                                                    |
| **LangGraph despite thirteen nodes**                             | Typed state, topology-as-documentation, and the mechanism for checkpointing and multi-turn later                                                                                                                               | Honestly oversized today; plain functions would work. A bet on the next increment                                                                                                                  |
| **Read-only DB role as the real boundary**                       | A control the model can influence is a tendency; one it cannot reach is a guarantee                                                                                                                                            | Requires provisioning a role, which is what a client DBA would do anyway                                                                                                                           |
| **sqlglot AST walk, not a `sqlparse` statement-type check**    | A data-modifying CTE has top-level type `Select`, so a check on the top-level type alone admits it and it executes                                                                                                            | An extra dependency; allow-list shape occasionally blocks exotic-but-valid SQL                                                                                                                     |
| **Allow-list validator, not a keyword deny-list**                | An unanticipated construct is denied by default rather than admitted by omission                                                                                                                                               | False positives (e.g. `INTERSECT` initially), which is the correct direction to fail in                                                                                                           |
| **Startup schema introspection, not per-query discovery**        | roughly 1,500 tokens injected once beats 2 to 4 extra round trips per question                                                                                                                                                 | Cached, so DDL changes need a restart; breaks down in the hundreds of tables                                                                                                                       |
| **Column comments as prompt context**                            | Units, enums and grain are not recoverable from types, and guessing them yields confidently wrong numbers                                                                                                                      | Comments become production code, maintained with the schema                                                                                                                                        |
| **Rule-based chart selection, no LLM**                           | Chart choice is a function of data shape rather than language, so the rule is deterministic, free, and unit-testable                                                                                                           | Ignores explicit user intent ("show as a pie chart")                                                                                                                                               |
| **Port operations domain, not e-commerce**                       | E-commerce is over-represented in training data, so accuracy would partly measure memorisation rather than schema comprehension                                                                                                | Less immediately familiar to a reader than orders and products                                                                                                                                     |
| **Deterministic seed, fixed date window**                        | Reference SQL contains literal dates; a moving window would decay accuracy for unrelated reasons                                                                                                                               | Relative dates must resolve against injected data ranges, not the clock                                                                                                                            |
| **Result-set comparison, not SQL text**                          | Many correct formulations exist; string comparison measures style, not correctness                                                                                                                                             | Slightly stricter than "did the user get the right answer", since an extra column counts as a miss                                                                                                 |
| **Two model tiers**                                              | Cost per task is a design input; three of the four calls do not need capability                                                                                                                                                | Two behaviour profiles to reason about; a prompt tuned on one tier may not transfer                                                                                                                |
| **Streamlit, not React + API**                                   | The UI is the least interesting component here, and its implementation should say so                                                                                                                                           | Would not scale to concurrent users; not a production serving model                                                                                                                                |
| **Reporting a range, not the best run**                          | Same code scored 86.4% and 95.5%; quoting only the maximum on a "measured, not claimed" system would be self-defeating                                                                                                         | A less impressive headline number                                                                                                                                                                  |
| **Withheld capabilities recorded as decisions** (ADR-009)        | Fourteen runs show zero dialect failures, so docs retrieval, web search, MCP and LLM validation are absences with evidence and revisit conditions                                                                              | The record must be re-examined as models, dialects and scope change                                                                                                                                |
| **Syllabus- and behaviour-tagged gold set, 108 cases** (ADR-010) | A saturated 36-case suite confirmed rather than measured; two orthogonal tags locate a failure in both the SQL plane and the phrasing plane                                                                                    | Runs cost ~$1.03 to ~$1.34, ~$1.26 in the shipped configuration, and 10 to 15 minutes; question wording itself becomes part of the measured surface                                                |
| **Bounded multi-turn: one rewrite node at the edge** (ADR-011)   | A follow-up resolves against prior questions and SQL only, and everything downstream stays byte-identical to the single-turn pipeline; the resolution is shown to the user as "Interpreted as:" so a misreading is correctable | A follow-up turn costs one extra cheap call; a chain of clarification turns carries no answer text, so "by containers" resolves from the earlier question rather than from the clarifying reply    |
| **Runtime verification, measured then defaulted off** (ADR-012)  | Groundedness rose to 98.7% to 100% against 96.0% to 97.4%, and every mechanism is bounded, advisory and fail-open, so none of it can withhold an answer                                                                        | Execution accuracy fell to 89.6% to 92.2% against 93.5% to 94.8%, all of it attributable to verifier objections by reason code; ships behind `RUNTIME_VERIFICATION` with the evidence in the repo |
| **The reading without the verdict** (ADR-013)                    | Every answered question regains a plain-language "What was measured" line, which disappeared for all 108 cases when ADR-012 was defaulted off; no SQL, answer or chart can change, because no path regenerates                 | One extra cheap call per answered question, 18% to 27% more cost; the verifier's objection is discarded rather than shown, because its `q66` probe objected to a correct query in 4 of 4 trials   |
| **A separate database for conversations** (ADR-014)              | Chat history is unreachable by the agent's role, which holds no CONNECT there, and the analytics database keeps holding only synthetic data so the second-order injection surface is unchanged                                 | The process gains a narrow write credential; the store's tests need a running database; production separation (ownership, lifecycle, workload, governance) is modelled rather than deferred        |

---

*This document describes the architecture as implemented and is updated in the same change as
the code it describes. The fourteen ADRs in [docs/ADR/](ADR/) carry the full reasoning, the
alternatives considered, and the trade-off accepted for each decision summarised here.*
