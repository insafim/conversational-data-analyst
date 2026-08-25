# ADR-002 — A Fixed-Path Graph, Not an Autonomous Agent Loop

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-004](ADR-004-defence-in-depth-sql.md), [ADR-005](ADR-005-deterministic-chart-selection.md), [ADR-007](ADR-007-llm-provider-and-tiering.md), [ADR-008](ADR-008-ui-and-scope-boundary.md)

## Context

The requirements ask for "an agent". That word covers a wide range of designs, from a fully autonomous
tool-calling loop that decides its own next step, to a fixed pipeline where a model fills in
specific slots. The choice is the central architectural decision in this build, and it determines
whether the security and latency properties are guarantees or merely tendencies.

The defining question is: **is the path from question to answer known in advance?**

For this problem it is. Every request follows the same route: understand the question, write SQL,
check the SQL is safe, run it, describe the result. There is no branch of the problem space that
requires the model to invent a novel sequence of steps.

## Decision

Implement the pipeline as an **explicit state graph with a fixed topology**, using LangGraph:

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

**Read the retry edge carefully — it carries the security argument.** The retry returns to
`generate_sql`, never to `execute`. Because the only outgoing edge from `generate_sql` is
`validate`, retried SQL is validated exactly like first-attempt SQL. The diagram is drawn this way
deliberately: a diagram that looped the retry straight back into `execute` would be describing a
bypass, and the diagram is the claim.

Two distinct terminal refusals exist and are named differently on purpose. `refuse` is a
classification-time exit, taken before any SQL is generated. `reject` is a validation-time exit,
taken after generation when the SQL fails the code validator. They are different events with
different causes, and collapsing them would hide which layer did the work
([ADR-004](ADR-004-defence-in-depth-sql.md)).

The division of labour is deliberate: **the model decides content, the graph decides flow.**

| Node | Implementation | Why |
| --- | --- | --- |
| `classify` | LLM (cheap tier) | Intent judgement is genuinely linguistic |
| `generate_sql` | LLM (strong tier) | The one task that needs real capability |
| `validate` | **Pure code** | A safety boundary must not be a prompt |
| `execute` | **Pure code** | Connection policy, timeout, row cap |
| `summarize` | LLM (cheap tier) | Grounded phrasing of returned rows |
| `pick_chart` | **Pure code** | Deterministic and unit-testable ([ADR-005](ADR-005-deterministic-chart-selection.md)) |

Three LLM calls per question; four if the single retry fires. Everything else is code.

## Why not an autonomous loop

An autonomous agent with `run_sql` and `inspect_schema` tools would work, and would be less code.
It was rejected for three reasons, in order of importance:

**1. Guardrails become structural rather than behavioural.** In this graph, `validate` sits on the
only edge into `execute`. There is no path to the database that does not pass through it — that is
a property of the topology, not of the model's cooperation. In a tool-calling loop, the model
decides when to call `run_sql`, so every safety property must be enforced inside the tool anyway;
the loop adds a layer that can be talked to but cannot be trusted, without removing the need for
the layer that can. The graph makes the trust boundary visible in the diagram.

**2. Cost and latency become a ceiling, not a distribution.** A loop's step count is a random
variable with a tail. A fixed graph's is bounded by construction: 3 calls, 4 with a retry. Latency
is a scored dimension of this exercise, and "p99 is whatever the model felt like" is not an
acceptable answer for a client-facing system.

**3. Failure modes are enumerable.** With six processing nodes and four terminal paths (`clarify`,
`refuse`, `reject`, and the answered path through `pick_chart`), every route can be reasoned about
and tested. A loop's failure modes include ones no one has thought of yet, which is
precisely what makes them expensive in production.

The retry edge is worth naming explicitly: **it is the ReAct pattern** — act, observe the error,
reason again — but the graph owns the loop counter, capped at one iteration. The model contributes
the reasoning; it has no vote on whether to continue. That is the useful half of ReAct without the
unbounded half.

## Honest assessment of the framework choice

For a graph this small, **plain Python functions with an `if` statement would work**, and would add
no dependency. LangGraph is not load-bearing at six nodes, and it would be dishonest to claim
otherwise.

It is used anyway, for reasons that are about the second version rather than this one:

- The typed state object makes what flows between stages explicit and self-documenting, which is
  where hand-rolled pipelines usually rot first.
- Conditional edges and a bounded retry loop are declared as topology rather than buried in control
  flow, so the diagram above and the code cannot drift apart.
- Checkpointing, streaming, and human-in-the-loop interrupts are the features this system would
  need next (see [ADR-008](ADR-008-ui-and-scope-boundary.md) on deferred multi-turn memory), and
  retro-fitting them onto hand-rolled control flow is the expensive path.

The trade-off in one line: **a dependency that is mildly oversized today, chosen because it is the
shape the production version takes.** If the reviewer's judgement is that this is overhead at this
scale, that judgement is correct — it is a deliberate bet on the next increment, not an oversight.

## Alternatives considered

**Autonomous tool-calling loop (ReAct).** Rejected — see above. It is the right choice when the
question shapes are unpredictable and the path must be discovered; here the path is known, so the
autonomy buys nothing and costs bounded cost, bounded latency, and structural guardrails.

**A single mega-prompt** producing SQL, answer and chart choice in one call. Rejected: it is the
cheapest option and the least defensible. There would be no point at which SQL can be inspected
before execution, no way to make chart selection deterministic, and no way to distinguish "the SQL
was wrong" from "the summary was wrong" during evaluation. Collapsing the stages destroys exactly
the observability the eval harness depends on.

**Plain Python functions, no framework.** A legitimate choice, and genuinely simpler for this
scope. Rejected on the second-version argument above, with the trade-off acknowledged rather than
hidden.

## Consequences

**Positive**

- Validation is unskippable by construction; the security story is architectural.
- Hard ceiling on cost and latency per question.
- Each node is independently testable; the eval harness can attribute failures to a stage.
- The graph topology is the documentation.

**Negative / accepted**

- A dependency heavier than this node count justifies. Accepted knowingly.
- Fixed topology cannot handle questions requiring genuinely multi-step exploration (e.g. "find
  anomalies, then investigate the biggest one"). Out of scope for this project; would need either a
  planner node or a supervised loop.
- Three sequential LLM calls set a latency floor that a single call would not have. Mitigated by
  routing two of the three to a fast, cheap model tier ([ADR-007](ADR-007-llm-provider-and-tiering.md)).

## Addendum, 2026-08-25: the node count, the call count, and the libraries this never named

Two figures in the record above are historical and are left in place, because an ADR records
what was decided when. Both have moved.

**Six nodes are now thirteen**, five of which call a model, after ADR-011 added `contextualize`
and ADR-012 and ADR-013 added `verify`, `ground_check` and `review`. The "honest assessment of
the framework choice" section above should be read against thirteen rather than six. It argued
that plain Python functions would work at six nodes, and that argument gets weaker with each
conditional router added; there are five of them now, plus a fan-out where `verify` runs beside
`execute`. The decision is unchanged and the reasoning is not restated, because what the
document records is what was known on 2026-08-04.

**Three LLM calls per question is four in the shipped configuration**, and six on a retried
answered question, since ADR-013's reading runs on every answered question and the retry pays
for a second reading. Both switches off is still three, which is what runs 21, 23 and 25
measured. [ARCHITECTURE.md §6](../ARCHITECTURE.md#6-the-agent-pipeline) carries the current
figure.

**The alternatives section above compares shapes, not libraries.** It weighs an autonomous
loop, a single mega-prompt and plain Python functions, which are the three shapes this pipeline
could have taken, and it names no competing framework. That comparison now exists in
[ARCHITECTURE.md §4](../ARCHITECTURE.md#4-technology-stack), checked against each project's own
documentation on 2026-08-25. The one worth knowing about here is the Microsoft Agent Framework,
the declared successor to both AutoGen and Semantic Kernel, which ships graph-based workflows
with explicit execution paths and is therefore a real alternative rather than a straw one. The
grounds for preferring LangGraph are narrower than this ADR's argument against autonomous loops
and are stated as preferences there.

