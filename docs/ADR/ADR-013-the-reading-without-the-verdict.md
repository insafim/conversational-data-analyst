# ADR-013: The Reading Without the Verdict

- **Status:** Accepted
- **Date:** 2026-08-11
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-006](ADR-006-eval-execution-accuracy.md), [ADR-008](ADR-008-ui-and-scope-boundary.md), [ADR-012](ADR-012-runtime-verification.md)

## Context

[ADR-012](ADR-012-runtime-verification.md) added a semantic verifier and, after measuring it,
defaulted it off. That decision was correct on its own terms: execution accuracy fell further
than groundedness rose, and every regression carried a `verifier_objection` reason code.

It also had a consequence nobody costed at the time. The verifier node produces two things from
one call:

1. a **reading**, a plain-language sentence saying what the SQL measures;
2. an **objection**, which can force one regeneration.

Both live in the same node, so turning the node off took the reading off the screen along with
the objection. The evidence is in the committed run artifacts:

| Runs | Configuration | Cases carrying a reading |
| --- | --- | --- |
| 20, 22, 24 | verification on | 75, 76, 76 of 108 |
| 21, 23, 25 | verification off | **0, 0, 0 of 108** |

In run 20, every one of the 75 cases that reached an `answered` outcome carried a reading, and
the 33 that did not were the 19 refusals and 14 clarifications, which have no SQL to describe.
So the reading was not partially present. It was present for every answer, and then absent for
every answer.

That matters because of a claim this repository makes elsewhere. `app.py` states, in a comment
above the line that renders it, that for the non-technical reader
[ADR-008](ADR-008-ui-and-scope-boundary.md) designs for, this sentence and not the SQL expander
is the verification surface. The shipped configuration had no such surface. A reader who cannot
audit SQL was left with the answer, the chart, and an expander containing the artefact they came
here to avoid.

Nothing else fills the gap by accident: `SUMMARIZE_SYSTEM` instructs the summariser "Do not
describe the SQL; the user can see it", so the answer text deliberately does not say what was
measured.

## Decision

**Run the verifier for its reading. Discard its objection.**

A switch, `SQL_READING`, independent of `RUNTIME_VERIFICATION` rather than a weaker setting of
it. Three configurations, and the graph topology differs in each:

| `RUNTIME_VERIFICATION` | `SQL_READING` | Nodes whose branch is active | What the user gets |
| --- | --- | --- | --- |
| off | off | none | the pre-ADR-012 pipeline exactly |
| off | **on (shipped)** | `verify`, `review` | a reading |
| on | either | `verify`, `ground_check`, `review` | ADR-012 in full |

`verify` already runs on a worker thread alongside `execute`, so the branch is unchanged. What
changes is that `review` now also runs in the middle configuration, because `review` is the only
place the verifier's future is collected, and an early return there skips the objection and
groundedness logic entirely.

### Why the objection is discarded rather than shown as a caveat

The obvious middle position is to print the objection beside the answer without acting on it.
Rejected, on ADR-012's own evidence. Its `q66` probe found the verifier objecting to a **correct**
zero-row query in 4 of 4 trials, because nothing in `VERIFY_SYSTEM` tells it that an empty result
can be right. A warning printed next to a correct answer is worse than no warning: it teaches the
reader to distrust the cases where the system is working. The two known one-line defects in
`VERIFY_SYSTEM` remain unapplied, so its judgement is known to be unreliable in a way its
description is not.

The asymmetry is the point. Describing what a query does is a task the model is reliably good at.
Deciding whether that query is right is the task it measurably is not.

## Consequences

**Positive**

- Every answered question carries a "What was measured" line in the shipped configuration.
  Refusals and clarifications carry none, correctly, because there is no SQL to describe.
- The SQL, the answer and the chart are unchanged by construction, not by measurement: no path
  in this configuration reaches `generate_sql` or `summarize` a second time. Execution accuracy
  and groundedness therefore cannot move.
- ADR-012's comparison stays reproducible. Setting both switches off reproduces runs 20 to 25
  exactly, including the call count and the cost.

**Negative / accepted**

- One extra cheap-tier call per **answered** question. Measured at 18% to 27% more cost per
  answered question across five questions in both orderings. Refusals and clarifications are
  unaffected.
- Latency was **not** resolved at that sample size. The verifier overlaps `execute` and
  `summarize`, so the additional wall-clock is whatever the overlap does not absorb; the `review`
  collection point measured 0.00s to 2.83s, and single-run deltas were swamped by provider
  variance of up to 17s on one sample. This is quoted as unresolved rather than estimated,
  and the harness is what will settle it.
- A third configuration is a third thing to keep true. The three-way table above is asserted in
  `tests/test_runtime_verification.py`, including that reading-only performs no regeneration, no
  re-summarisation and raises no caveat, that a raising verifier still returns an answer, and
  that reading-only produces the same SQL, answer and chart as the all-off configuration from
  the same stub. That last test asserts the claim in this ADR directly rather than leaving a
  reader to reconcile two separate test bodies.
- The suppression of the groundedness flag rests on routing, where the objection suppression
  also has a guard inside `review`. A probe showed the asymmetry: sending the reading path
  through `ground_check` leaks a flag to the user with that guard untouched. `review` now
  clears `grounding` on its reading-only exit, so both properties have two mechanisms rather
  than one.

## Alternatives considered

**Turn `RUNTIME_VERIFICATION` back on.** Rejected. It would trade measured execution accuracy for
a UI affordance, and it would overturn a decision the evidence made rather than a decision taste
made.

**Have the summariser state what was measured.** Zero extra calls, and genuinely attractive. It
requires editing `SUMMARIZE_SYSTEM`, which is the prompt that produces the answer text every
groundedness score is computed from, so it cannot be adopted without re-measuring. It is the
better long-term answer and is deferred rather than rejected.

**Fix the two `VERIFY_SYSTEM` defects and re-enable verification.** The honest full solution, and
out of scope here: applying either defect invalidates runs 20 to 25, so the comparison would have
to be re-measured before ADR-012's conclusion could be restated. That work is scoped in ADR-012's
addendum and is deliberately left undone.
