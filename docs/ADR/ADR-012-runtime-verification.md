# ADR-012: Runtime Verification, Each Property Gets Its Instrument

- **Status:** Accepted
- **Date:** 2026-08-10
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-004](ADR-004-defence-in-depth-sql.md), [ADR-006](ADR-006-eval-execution-accuracy.md), [ADR-009](ADR-009-withheld-runtime-capabilities.md), [ADR-011](ADR-011-bounded-multi-turn.md)

## Context

[ADR-009](ADR-009-withheld-runtime-capabilities.md) rejected an LLM stage inside the
validator and located the intent-alignment residual in two places: offline result-set
comparison, and the SQL being displayed at runtime for the reader to check. The second
half of that argument does not survive contact with the product's own persona.
[ADR-008](ADR-008-ui-and-scope-boundary.md) defines the user as a non-technical
operations manager, and rejected a Jupyter-notebook interface on exactly those grounds
(its CLI rejection was about chart rendering; the persona reasoning sits in the notebook
paragraph). A user who cannot read
SQL cannot be the intent verifier. This ADR supersedes that clause of ADR-009 section 4;
the rest of ADR-009 stands.

The failure evidence sharpens the gap. Of the twelve semantic failures across runs 1 to
7 (ADR-009, evidence table), the three code-detectable signals cover exactly three: the
empty-result cases (run3 q15, run6 q19) and the row-count-versus-grammar case (run1
q05). The remaining nine, extra columns (q09, four runs), invented banding (q23), a
different count basis (q14), a missing column (q27), an order-and-values mismatch (run1
q21), and a LAG value mismatch (run7 q28), are invisible to code and, per the persona,
invisible to the user. Separately, the worst groundedness incident
([ADR-006](ADR-006-eval-execution-accuracy.md)) happened in the summarise stage: correct
SQL, correct rows, and a summary that summed twelve rows to 228,499 against a true
239,099. No question-versus-SQL check can see that stage at all.

## Decision

**Three verification points, each using the instrument its property demands. An LLM in
this graph may produce and may object; only code may permit.**

| Point | Property | Instrument | Authority |
| --- | --- | --- | --- |
| `validate` (exists) | single read-only SELECT-family statement | code, sqlglot tree walk | absolute: only edge into `execute` |
| `verify` (new) | SQL corresponds to the question asked | LLM, cheap tier | advisory: one bounded regeneration, or a visible caveat |
| `ground_check` (new) | every figure in the answer appears in the rows, question, or SQL | code, ported from `eval/run_eval.py:154` | advisory: one bounded re-summarise, or serve with flag |

### The semantic verifier

- Reads the question, the schema context, and the generated SQL. Never the results. It
  therefore runs **in parallel with `execute` and `summarize`**: execute averages
  0.025 s and summarise about 1.5 s across the run14 records that reach them, and a
  cheap-tier judgement fits inside that window, so happy-path wall-clock cost is near
  zero when the verdict returns within it and small when it does not. Dollar cost is roughly
  +$0.002 per question against a $0.0098 to $0.0100 mean.
- Structured output, parsed by code: aligned, or an objection string. **Fail-open**: an
  unparseable verdict counts as no objection, so a flaky checker cannot degrade
  availability.
- On objection, the graph re-enters `generate_sql` once, with the objection attached
  exactly as the database error is attached on the existing retry
  (`src/prompts.py:120-132`), the mechanism that recovered four of four historical
  retries. The graph owns the counter, as it always has. If the regenerated SQL draws an
  objection again, the answer ships with a visible caveat naming the concern. Nothing is
  silently suppressed.
- A by-product is kept: the verifier's plain-language reading of what the SQL measures
  ("counted all port calls in 2025, including cancelled ones") is surfaced with the
  answer, alongside ADR-011's "Interpreted as:" line. For the stated persona this line,
  not the SQL expander, is the real verification surface.
- It has no safety authority. It cannot approve anything, and `validate` remains the
  only gate before the database.

### The groundedness check

`_check_groundedness()` exists, is calibrated across ten runs, and has documented false
positives (the LAG-magnitude case and derived phrasings, ADR-006). It is ported into the
graph after `summarize` with floor semantics: on violation, one re-summarise with the
offending figure named; on a second violation, the answer is served with the flag logged
and shown, never blocked. Blocking is wrong precisely because the known false positives
are correct answers the checker misreads.

### Code-detected quality triggers

The three signals from ADR-009's deferred list join the same retry path: an empty result
on an `answerable` classification, a multi-row result for a singular-superlative
question (the rule at `src/prompts.py:89-94`), and a result that saturates `ROW_CAP`.
Each is free to detect and each has at least one historical instance.

### Accounting

The results schema stops recording `retried` as one boolean. Retries carry a reason:
`db_error`, `verifier_objection`, `ground_check`, or `quality_trigger`, so run
comparisons can attribute movement to the mechanism that caused it. This supersedes the
single-bit caveat noted in ADR-009.

## Why an LLM is right here and wrong in the validator

The validator's property is decidable by parsing; a probabilistic layer cannot
strengthen a decidable check, and a disagreement between the two has no good resolution.
Semantic correspondence is not decidable, verification of it is easier than generation
of it, and an advisory objection has a cheap, bounded, fail-open resolution: regenerate
once with the objection in context. The asymmetry, decidable-code versus
semantic-advisory-LLM, is the entire design.

## Evaluation

The expanded gold set ([ADR-010](ADR-010-syllabus-mapped-eval-expansion.md)) is the
measuring instrument: the suite runs with the verifier off (runs 15 onward, baseline)
and on, and the README reports semantic-failure rate, cost, and latency as ranges across
runs for both configurations. Cases q46 (false-premise fact-check), q56 (deliberately
empty result), q59 (bands defined in the question), and q65 (partial-month recency trap)
were designed to exercise exactly the classes this ADR targets.

---

## Addendum, 2026-08-11: measured, and defaulted off

Built as specified: `verify` on a parallel branch, `ground_check` ported from
`eval/run_eval.py` into `src/grounding.py` and shared with the harness, the three
code-detected quality triggers, and retry reason codes replacing the single `retried` bit.
Every mechanism is advisory, bounded at one retry, and fail-open, and
`tests/test_runtime_verification.py` pins each of those properties.

**The parallelism argument needed correcting.** This ADR reasons that the verifier is
nearly free because it runs in parallel with `execute` and `summarize`. Measured against
the installed langgraph, a plain parallel node does not deliver that: parallel branches
overlap only within one superstep and the runtime synchronises between them, so a
blocking `verify` node would have overlapped `execute` (0.025 s) and nothing else. The
node therefore submits its call to a thread pool and `review` collects the verdict after
`summarize`, which does deliver the overlap. The library behaviour is pinned by
`tests/test_agent_routing.py::test_langgraph_barriers_between_supersteps_so_verify_must_not_block`
so the reasoning fails loudly if a future version changes it.

**The result. Six runs on the 108-case set, alternating ON and OFF so that provider drift
could not be mistaken for an effect of the feature. Zero infrastructure errors.**

| | ON (runs 20, 22, 24) | OFF (runs 21, 23, 25) |
| --- | --- | --- |
| Overall | 100 to 101 / 108 | 102 to 103 / 108 |
| Execution accuracy | 89.6% to 92.2% | 93.5% to 94.8% |
| Answer groundedness | 98.7% to 100% | 96.0% to 97.4% |
| Median latency | 6.74 to 7.11 s | 6.03 to 6.65 s |
| Cost per run | $1.33 to $1.36 | $1.01 to $1.04 |

It improved the property it was aimed at and cost more elsewhere. Groundedness is now
near-perfect, and the only figure still flagged is q28, the negative-magnitude false
positive [ADR-006](ADR-006-eval-execution-accuracy.md) documents and declines to fix. But
execution accuracy fell further than groundedness rose, and neither range overlaps.

**The reason codes located it, which is what they were added for.** Across the three ON
runs: 13 verifier objections, 8 re-summarisations, 6 quality triggers. Every accuracy
regression carries a `verifier_objection` (q66 in all three runs; q14, q18, q65 in one
each). The groundedness check and the code triggers cost nothing measurable. The LLM
verifier is the part that does not pay for itself.

q66 ("How many port calls were there in 2020?", correct answer: none, the data begins in
2025) fails identically in all three ON runs. The objection is *correct* and precise: "the
question asks for port calls in 2020, but the query filters for arrivals in 2025". The
single bounded regeneration does not fix it, and the answer ships with that caveat beside
the wrong number. Two things follow, and the second is unresolved:

1. A correct objection is not sufficient. Regenerating with the objection attached
   recovered four of four *database* errors historically, and it does not transfer to
   semantic ones: the model that wrote the query is being asked to disagree with itself.
2. Why the first attempt differs by configuration at all is **not explained by these
   artifacts**, since the first `generate_sql` prompt is identical either way. A probe to
   settle it was cut short when the API credit ran out. Recorded as open rather than
   given a plausible-sounding cause.

One concrete defect is identified and not yet fixed: `VERIFY_SYSTEM` instructs the
verifier to object to "extra columns beyond the label and the measure(s) asked for", and
that is what stripped the month column from q65's correct two-column answer.

**Decision: the feature ships behind `RUNTIME_VERIFICATION`, defaulting to false.** The
machinery, the switch, the reason codes and all six runs stay in the repo. Turning it on
is defensible when an invented figure costs more than a wrong row, which is a judgement
about a deployment rather than about this code. This supersedes the implicit expectation
in the Decision section above that these checks would be on in normal operation; nothing
else in the ADR changes.
