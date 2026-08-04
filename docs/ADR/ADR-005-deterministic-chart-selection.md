# ADR-005 — Chart Type Chosen by Rules, Not by the Model

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)

## Context

The brief requires the agent to "visualise it with a chart where appropriate", and names
"chart-type selection" as an assessed capability. Two words in that requirement carry weight:
*appropriate* implies a judgement, and *where* implies that sometimes the answer is no chart at all.

The obvious implementation is to ask the model. It is already in the loop, it understands the
question, and one more field in a JSON response is nearly free to write.

## Decision

**Select the chart type in pure code, from the shape and column types of the result set. No LLM
call.**

The rules, applied in order, first match wins:

| # | Condition on the result set | Output |
| --- | --- | --- |
| 1 | Zero rows | No chart — the answer states that nothing matched |
| 2 | Exactly one row, exactly one numeric column | **Metric card** — a single number needs no axes |
| 3 | A temporal column present, plus ≥1 numeric column | **Line chart** — time on x, ordered chronologically |
| 4 | One categorical column with ≤ 12 distinct values, plus ≥1 numeric | **Bar chart** |
| 5 | Exactly two numeric columns, nothing temporal or categorical | **Scatter chart** |
| 6 | Anything else (wide, many-category, or multi-dimensional results) | **Table** |

Column classification comes from the PostgreSQL type codes returned with the cursor description —
that is, from the database's own declared types, not from inspecting values or guessing from
column names.

The cardinality bound in rule 4 encodes a real charting principle: a bar chart of 300 categories is
not a chart, it is an unreadable table with extra steps. Falling back to a table is the honest
output, and "sometimes the right visualisation is no visualisation" is the *where appropriate* half
of the requirement.

## Rationale

**Chart selection is a function of data shape, not of language.** Once the SQL has run, the result
set fully determines which visual encodings are valid. One temporal axis and a measure is a line
chart regardless of whether the user asked about berth waits or container moves. There is no
linguistic judgement left to make, so there is nothing for a language model to contribute.

Given that, the code version wins on every axis that matters:

- **Deterministic.** The same result set always produces the same chart. A demo cannot embarrass
  itself by rendering a pie chart on the second take.
- **Testable.** Each rule is a unit test over a synthetic DataFrame. This is the difference between
  a claim and a verified property, and it is cheap to verify.
- **Free.** No fourth LLM call, no added latency on a system where latency is assessed, no tokens.
- **One fewer failure mode.** A model asked for a chart type can return a chart type that does not
  exist, or one that is invalid for the data — a bar chart keyed on a column that is not in the
  result. Every one of those needs validating in code anyway, at which point the code already
  encodes the rules and the model call is redundant.

The general principle: **do not spend a language model on a decision that is not about language.**
Reserving the model for the genuinely hard task — turning English into correct SQL — is what keeps
the trusted surface small.

## Alternatives considered

**Ask the LLM for the chart type** (extra field in the summarise call). Rejected. Nondeterministic,
untestable, and — since the output must be validated against the actual result columns regardless —
it does not even remove the code path it was supposed to replace.

**Always render a table.** Rejected: the brief explicitly asks for charts, and a table answers a
"how did this trend over time" question far less well than a line.

**A charting library that auto-selects** (e.g. Vega-Lite style automatic encoding). Rejected as
disproportionate: a heavier dependency and a new specification language to learn, to replace six
rules that fit in a table.

## Consequences

**Positive**

- Fully deterministic and unit-tested chart behaviour.
- No latency or cost contribution.
- The rules are legible and arguable — a reviewer can disagree with rule 4's threshold of 12, which
  is exactly the kind of disagreement that is productive.

**Negative / accepted**

- **Explicit user intent is ignored.** If a user says "show me that as a pie chart", the rules do
  not hear them. Accepted for this scope; the fix is a small intent-extraction step feeding an
  override, which is a genuine improvement rather than a rewrite.
- **Semantic nuance is missed.** Part-of-whole data is arguably better as a stacked bar, and the
  rules cannot know that "share of total" is what the numbers mean. A semantic layer would supply
  that; see [ADR-003](ADR-003-schema-introspection.md).
- The 12-category threshold is a judgement call, not a derived constant. Documented as such rather
  than presented as principled.
