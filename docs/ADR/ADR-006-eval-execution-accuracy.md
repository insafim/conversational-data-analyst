# ADR-006 — Evaluation by Execution Accuracy on a Gold Set

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-001](ADR-001-domain-and-data-model.md), [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)

## Context

The brief names three things to be evaluated: **SQL correctness, answer groundedness, and ambiguous
query handling**. Every text-to-SQL demo asserts the first. Almost none measure it.

The distinction matters commercially, not just academically. A client cannot deploy a system whose
correctness is a vibe, and the first question any serious buyer asks is "how do you know it works,
and how will you know it still works after you change the prompt?" A number that can be regenerated
on every change is a different kind of artefact from a demo that worked once on camera.

## Decision

**A gold question set plus an execution-accuracy harness: run the agent's SQL and the reference SQL
against the same database, and compare the result sets.**

The gold set (`eval/gold_questions.jsonl`) contains three categories of case, because the brief
names three behaviours:

| Category | What the item asserts | How it is scored |
| --- | --- | --- |
| **Answerable** | Reference SQL, hand-written and hand-verified | Result-set equivalence with the agent's SQL |
| **Ambiguous** | The question is genuinely under-specified | Agent must return a clarifying question, not SQL |
| **Adversarial** | Injection / destructive / out-of-scope requests | Agent must refuse, and nothing may execute |

Reported metrics: execution accuracy, ambiguity handling rate, refusal rate, mean and p95 latency,
and per-item failure diffs.

### Why compare results, not SQL text

The same question has many correct SQL answers. `JOIN` order, `HAVING` versus a filtered subquery,
`COUNT(*)` versus `COUNT(1)`, aliasing, CTE versus inline — all semantically identical, all
textually different. String comparison, and even AST comparison, would fail correct queries and
produce a metric that measures stylistic conformance rather than correctness.

Comparing what the queries *return* measures the thing that actually matters: **did the user get
the right numbers?** Comparison is order-insensitive unless the question implies an ordering
(top-N, ranking), with float tolerance for aggregate arithmetic, since averages computed by
different-but-equivalent paths can differ in the last bits.

### Ambiguity is a scored behaviour, not an anecdote

"What was the busiest month?" is not answerable: busiest by port calls, or by container volume?
Those are different queries with potentially different answers. A system that silently picks one
produces a confidently wrong answer, which for a client is strictly worse than no answer — wrong
numbers get into decks and then into decisions.

So the harness scores *asking back* as the correct behaviour, and treats confidently answering an
ambiguous question as a failure. That inverts the usual incentive, where a model is rewarded for
always producing output.

### Groundedness

Groundedness is checked structurally rather than by another model: the summariser receives only the
returned rows, and the harness asserts that numeric claims in the answer appear in the result set,
and that empty results produce an explicit "no matching data" rather than an invented figure. This
catches the failure that matters — inventing numbers — without importing a second model's judgement
into the scoring.

## Alternatives considered

**Exact SQL string or AST match.** Rejected: penalises correct queries for style. Measures the
wrong thing.

**LLM-as-judge.** Rejected as the *primary* metric, for three reasons: it is nondeterministic, so
the same run scores differently; it is circular, grading a model's output with a model; and it
would itself need validating against human labels before its scores meant anything — which is more
work than writing reference SQL. It has a legitimate place for fuzzy dimensions like answer
phrasing quality, which is not what is being claimed here.

**Manual review.** Rejected: does not survive contact with change. The entire value is being able to
re-run the suite after every prompt edit.

**A public benchmark (Spider, BIRD).** Rejected: they run against their own schemas, so they would
measure a general capability rather than this system on this data. The interesting question is
whether *this* agent answers *these* client questions correctly.

## Honest limitations

Stated here rather than discovered by a reviewer:

- **The gold set is small.** At roughly 20–25 items, one case is worth 4–5 percentage points, so the
  headline number has a wide confidence interval. It is a regression detector and a smoke test, not
  a precise measurement of general capability. Reporting "90%" from 20 items without saying this
  would be overclaiming.
- **Result-set comparison can pass a wrong query.** If the reference SQL is itself wrong, agreement
  is meaningless. Mitigated by hand-verifying every reference query against the data, but the
  reference set is a human artefact and inherits human error.
- **Degenerate agreement.** Two queries can both return zero rows and compare equal. Gold cases are
  chosen to return non-trivial results for this reason.
- **The suite tests the data it was written against.** Planted patterns
  ([ADR-001](ADR-001-domain-and-data-model.md)) make questions answerable; a different dataset would
  need a different gold set.

## Consequences

**Positive**

- "The SQL is correct" becomes a reproducible number rather than a claim.
- Failures are attributable to a specific stage, because the graph exposes intermediate state
  ([ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)).
- The harness calls the agent directly, with no UI in the loop, so it can run in CI.
- Safety behaviour is regression-tested, not assumed — a prompt change that quietly weakens refusal
  shows up as a failing case.

**Negative / accepted**

- Writing and verifying reference SQL is the most time-expensive part of the build. Accepted: it is
  also the highest-signal part.
- The suite requires a seeded database, so it is an integration test, not a unit test. This is the
  correct trade — mocked results would test the harness rather than the agent.
- Adding gold cases has ongoing cost as the schema evolves. Named as a real maintenance obligation
  rather than waved away.
