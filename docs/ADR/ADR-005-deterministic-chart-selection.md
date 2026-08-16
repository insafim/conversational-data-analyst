# ADR-005 — Chart Type Chosen by Rules, Not by the Model

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)

## Context

The requirements call for the agent to "visualise it with a chart where appropriate", and name
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
| 2 | Exactly one row, exactly one numeric column, at most two columns | **Metric card** — a single number needs no axes; the second column, if present, labels it |
| 3 | A temporal column present, plus ≥1 numeric column, **and more than one row** | **Line chart** — time on x, ordered chronologically, one line per measure |
| 3b | ...and a categorical column, where the temporal column **repeats**, with ≤ 10 distinct categories | **Line chart** — one line per category, carried as colour |
| 3c | ...but with more than one measure, a hidden third dimension, more than 10 categories, or no category holding two rows | **Table** — no honest line can be drawn |
| 4 | A leading categorical label with ≤ 12 distinct values, plus ≥1 numeric, **and more than one row** | **Bar chart** |
| 4b | Several categorical columns where the first does **not** uniquely label each row | **Table** — a single-axis bar chart would silently collapse a dimension |
| 5 | Exactly two numeric columns, nothing temporal or categorical, **and more than one row** | **Scatter chart** |
| 6 | Anything else (wide, many-category, or multi-dimensional) | **Table** |

A single row that carries more than a label and a measure also ends as a table, but it is
refused inside rules 3, 4 and 5 rather than falling through to row 6.

**A second exception, also found by rendering rather than by reasoning.** Rule 2 originally
required *exactly one column*. A superlative question — "which terminal has the longest berth
wait?" — answers with a label **and** a measure, so it returns two columns, missed Rule 2, and
fell to Rule 4, which drew a bar chart containing a single bar stretched across the full width of
the container. Four gold questions produced it when the defect was found, including the first
example in the UI sidebar, so it was the chart most likely to be seen first. The gold set has
grown since, and nine answerable cases produce the shape today. **Four is left standing in this
paragraph deliberately**, because an ADR records what was true when the decision was taken. The
current figure is in [CHARTS.md §8](../CHARTS.md#8-four-guards-and-how-each-was-found), and the
command that reproduces it in
[CHARTS.md §11](../CHARTS.md#11-verify-it-yourself). Rule 2 now admits the label-plus-measure shape and
carries the label through as the metric's caption; Rules 3, 4 and 5 refuse single-row results
outright, because a line through one point shows no trend, one bar is a number drawn wide, and a
scatter of one point draws no relationship.

Rule 5's guard was the last of the four and came from an audit rather than from rendering, because
no gold question produces its shape. It arises whenever a superlative groups by a numeric column —
`year`, or `crane_id` — since column classification reads a numeric identifier as a measure rather
than as a label. That the same defect survived in one rule after being fixed in three is the
argument for auditing rule tables as a set rather than fixing the case that was reported.

**Row 4b is a third exception, and it runs the other way.** A strict "exactly one categorical
column" test would send a great many chartable results to a table, because models routinely add a
descriptive companion column: asked for average wait by terminal, they return `terminal_name,
port_name, avg_wait`. Rule 4 therefore tolerates extra categorical columns, but only when the
first one already identifies each row uniquely — that is, when it is a label and the rest are
attributes of it. When the first column repeats, the rows are a genuine multi-dimensional
breakdown and a single-axis bar chart would hide a dimension, so those fall to a table. Observed
on the first live query, not anticipated.

**Rows 3b and 3c are a fourth exception, added 2026-08-16, and they are the same idea as 4b
applied one rule earlier.** Rule 3 had no multi-dimensional refusal at all. A follow-up question
asked in the running app returned `month, operator, total_containers`, 33 rows of twelve months by
three operators, and the rule kept the time axis and the measure and dropped the operator, so
`st.line_chart` joined all 33 rows in row order and drew a sawtooth: inside each month the line
jumped between the three operators' values. It was a known limitation with a written rationale,
and the rationale was measured against the only two gold cases of the shape, neither of which is a
breakdown. Where 4b refuses, 3b encodes: the category becomes colour and the chart draws one line
per value. 3c is the refusal for the shapes where that cannot be done honestly. Found by a user
using the app, which is a fourth discovery route alongside rendering the gold set, running one
query against the real database, and auditing the rule table as a set.

Column classification comes from the PostgreSQL type codes returned with the cursor description —
that is, from the database's own declared types, not from guessing from column names.

**One documented exception, found by testing rather than predicted.** The most common way to group
by month in PostgreSQL is `to_char(ts, 'YYYY-MM')`, which returns **text**. Running that query
against the real database showed a purely type-driven rule classifying the single most common
time-series result as categorical, and drawing bars where a line is correct. Two fixes are applied
together: the SQL prompt asks for `date_trunc(...)::date` so the column arrives properly typed
(primary), and the rules additionally treat anchored ISO-8601-shaped text — `2025`, `2025-03`,
`2025-03-01` — as temporal (safety net). The fallback inspects *values* against a strict anchored
pattern, never column names, which would be the fragile version of the same idea.

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

**Always render a table.** Rejected: the requirements explicitly ask for charts, and a table answers a
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
  than presented as principled. The 10-series threshold in row 3b is the opposite case and the two
  must not be reconciled: it is the length of Streamlit's default categorical palette, past which
  colours repeat and two lines become the same colour, so it is a property of the encoding rather
  than a preference.
- **Part-of-whole is still invisible on the bar path.** Row 3b gives the line rule a colour
  channel; row 4b still refuses rather than stacking, because a stacked bar asserts that the
  numbers are shares of a total and nothing in the result set says so.
