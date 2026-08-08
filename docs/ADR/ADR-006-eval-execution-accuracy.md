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

The gold set (`eval/gold_questions.yaml`) contains three categories of case, because the brief
names three behaviours:

| Category | What the item asserts | How it is scored |
| --- | --- | --- |
| **Answerable** | Reference SQL, hand-written and hand-verified | Result-set equivalence with the agent's SQL |
| **Ambiguous** | The question is genuinely under-specified | Agent must return a clarifying question, not SQL |
| **Adversarial** | Injection / destructive / out-of-scope requests | Agent must refuse, and nothing may execute |

Reported metrics: execution accuracy, ambiguity handling rate, refusal rate, mean and p95 latency,
and per-item failure diffs.

The set is stored as YAML with reference SQL in literal block scalars, so a query is reviewable as
SQL rather than as a single-line string, and it is parsed through the schema in `eval/gold.py`
rather than as bare dicts. The three categories above have two different record shapes, and nothing
previously enforced the split: a case missing its reference SQL, or carrying a misspelled `ordered`
flag, was accepted at load and misbehaved only when it was scored. Because every published accuracy
figure is computed from this file, that class of defect corrupts a reported number instead of
raising an error, so the schema is validated before any case runs and is itself tested in
`tests/test_gold_set.py`.

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

Groundedness is checked structurally rather than by another model. `_check_groundedness()` asserts
that every figure in the answer appears in the returned rows (exactly, or as a rounding of one), in
the question, or in the SQL — and that an empty result set produces an explicit "no matching data"
rather than an invented figure. This catches the failure that matters, inventing numbers, without
importing a second model's judgement into the scoring.

**It is scored separately from execution accuracy, and the first run proved why.** Asked for monthly
container volumes in 2025, the agent produced *correct SQL* returning *correct rows*, and then
described them with:

> "...with the total annual volume reaching **228,499** containers across all twelve months."

The true total is **239,099**. The model had summed the twelve rows itself and got it wrong by
10,600 containers — stated fluently, with no hedging, in an answer that execution accuracy scored
**100% correct**. Comparing result sets cannot see this class of failure at all, because the result
set was right.

The root cause was a gap in the summariser prompt: it forbade inventing numbers but never forbade
*computing* them. It now prohibits arithmetic across rows outright — selections from the rows
("the highest is X") are permitted, new numbers are not. The reasoning is that a computed figure is
indistinguishable to a reader from a retrieved one: it carries identical authority while being
unverifiable, and in practice is often wrong.

**Known limitation, stated rather than hidden.** A genuinely derived figure — "three times higher",
"up 12%" — is not in the result set and will be reported as ungrounded. That is a false positive.
It is tolerated because the alternative, permitting arithmetic, permits exactly the invented numbers
this exists to catch. The metric is a *floor* on groundedness, not a precise measure of it.

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

## What the harness found, and what was done about it

The first run scored **86.4%** execution accuracy. All three failures turned out to be defects in
the *specification*, not in the model:

- Two questions (`q14`, `q21`) failed because the SQL prompt said to "exclude cancelled port calls
  when the question is about operational performance". That phrasing is vague, and the model
  reasonably applied it to **counts** — which changes what "how many port calls" means. The rule was
  rewritten to be explicit: counts include cancelled calls; duration metrics need no filter at all,
  because those columns are already NULL for cancelled calls and `AVG` ignores them.
- One question (`q05`) failed because the ranking rule did not distinguish a singular superlative
  ("*which* operator waits longest") from a per-group question ("...for *each* operator"). The model
  returned the whole ranking. The rule now maps question grammar to `LIMIT` explicitly.

Re-running after those fixes gave **95.5%**. This is the harness doing its actual job: the failures
were ambiguity in the instructions, and they were invisible until something scored them.

### The remaining failure, and a deliberate decision not to fix it

`q09` — "Which terminals are in the Netherlands?" — the agent returned `terminal_name, port_name`
where the reference SQL selects `terminal_name` alone. The answer is factually correct and
arguably more useful. Strict row comparison scores it as wrong.

It would be easy to relax the comparison so that a superset of the required columns counts as a
pass. **That was considered and rejected**, because loosening a metric *after* seeing what it fails
is tuning the metric to the result. The number would go up and would mean less.

So the strict comparison stands, 95.5% is reported rather than 100%, and this is recorded as a known
limitation: the harness measures result-set equivalence, which is slightly stricter than "did the
user get the right answer". That conservatism is the right direction for a correctness metric to
err in.

### Run-to-run variance is real, and was measured rather than assumed

Three full runs were executed. Runs 2 and 3 share identical code and prompts and scored **95.5%**
and **86.4%** execution accuracy respectively. That is a nine-point spread from nothing but
re-running, at `temperature=0`.

Two distinct causes, which the harness originally conflated:

1. **Provider instability.** Two of run 3's failures were `error` outcomes taking 58s and 37s;
   LiteLLM logged an SSL handshake timeout during that run. Those are availability events, not
   incorrect SQL. Excluding them, run 3 scores 90.5% accuracy and 100% on ambiguity.
2. **Genuine sampling variance.** Two answerable items flipped between runs. `temperature=0` reduces
   variance; it does not eliminate it.

The harness was changed to **report** infrastructure errors separately — but deliberately **not** to
exclude them from the headline figure. A metric that silently drops its own failed requests would
report a flattering number precisely when the system is least usable. Availability is part of
whether a user got their answer.

The consequence for how this system should be talked about: a single run cannot distinguish
86% from 95%, because at the 22 answerable items scored by the early runs one case is worth 4.5
points, and at the 28 scored now it is still worth 3.6. The defensible claim is a range,
with the acknowledgement that narrowing it requires more gold items and repeated runs — which is a
genuine cost, not a footnote. **Reporting the best of three runs as though it were the score would
be the easiest and most dishonest thing to do here.**

Safety, by contrast, was 5/5 in every run — 30/30 attempts across six runs. That stability is not a property of the
model; it is a property of enforcing the guarantee at a layer the model cannot reach
([ADR-004](ADR-004-defence-in-depth-sql.md)).

## Honest limitations

Stated here rather than discovered by a reviewer:

- **The gold set is small.** At 28 answerable items one case is worth 3.6 percentage points, so the
  headline number has a wide confidence interval. It is a regression detector and a smoke test, not
  a precise measurement of general capability. Reporting "90%" from 20 items without saying this
  would be overclaiming. This was subsequently confirmed empirically — see the variance section
  above.
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
