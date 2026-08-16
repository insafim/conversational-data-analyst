# Charts: how a result set becomes a visual

> A single-purpose reference for the one output surface the model does not choose. It states
> what can be drawn, how the choice is made, which questions produce which chart, and where the
> rules are wrong.
>
> Companion documents: [ADR-005](ADR/ADR-005-deterministic-chart-selection.md) is the decision
> record and the argument; [ARCHITECTURE.md §10](ARCHITECTURE.md#10-chart-selection) places
> chart selection in the pipeline; [README](../README.md#how-it-works) is the summary a
> first-time reader gets; [DATA.md](DATA.md#questions-that-stress-the-charting-logic) lists
> questions to try in the running app. This document is the detailed one.
>
> Every figure below was produced on **2026-08-16** by running the rules over the seeded
> database in `docker-compose.yml`, not written from intent.
> [§11](#11-verify-it-yourself) gives the commands that reproduce each one.

---

## Table of contents

1. [The decision in one sentence](#1-the-decision-in-one-sentence)
2. [The six kinds, and what renders them](#2-the-six-kinds-and-what-renders-them)
3. [How a column gets a role](#3-how-a-column-gets-a-role)
4. [The six rules, in order](#4-the-six-rules-in-order)
5. [Rules 3 and 4 in detail: when a chart refuses](#5-rules-3-and-4-in-detail-when-a-chart-refuses)
6. [The metric card](#6-the-metric-card)
7. [What the gold set actually renders](#7-what-the-gold-set-actually-renders)
8. [Four guards, and how each was found](#8-four-guards-and-how-each-was-found)
9. [What the rules cannot do](#9-what-the-rules-cannot-do)
10. [What is tested, and what is not](#10-what-is-tested-and-what-is-not)
11. [Verify it yourself](#11-verify-it-yourself)

---

## 1. The decision in one sentence

**Chart choice is a function of the shape of the result set, not of the language in the
question.**

Once the SQL has run, the rows fully determine which visual encodings are valid. A temporal
axis with a measure is a line chart whether the user asked about berth waits or container
moves. No linguistic judgement remains, so there is nothing for a language model to contribute,
and `pick_chart` is a plain function in `src/charts.py` with no model call in it
([ADR-005](ADR/ADR-005-deterministic-chart-selection.md)).

It is wired as the last node of the graph, `pick_chart` in `src/agent.py`, edged straight to
`END`. Nothing downstream can revise its choice.

Three consequences follow, and they are the reason the decision is worth stating:

- **Deterministic.** The same rows always draw the same chart, so a demo cannot embarrass
  itself by rendering something different on the second take.
- **Testable.** Each rule is a unit test over a synthetic result set. See
  [§10](#10-what-is-tested-and-what-is-not).
- **Free.** Passing all 77 answerable gold results through `pick_chart` takes 0.21 to 0.22 ms in
  total across five repeats, a median of 0.003 ms each, so chart selection contributes nothing to
  the 6.91s median turn.

## 2. The six kinds, and what renders them

`ChartKind` in `src/models.py` has six members. `NONE` leaves the answer text to stand alone, so
five put something on the page. Of those five, a table is a rendering of the rows rather than a
chart in the ordinary sense, which leaves four.

| Kind      | What the user sees                     | Rendered by                            |
| --------- | -------------------------------------- | -------------------------------------- |
| `NONE`    | nothing; the answer text stands alone  | early return in `views/chat.py`        |
| `METRIC`  | one large figure with a label          | `st.metric`, via `metric_fields()`     |
| `LINE`    | one line per measure, or one per category, over a time axis | `st.line_chart`          |
| `BAR`     | one bar group per category             | `st.bar_chart`                         |
| `SCATTER` | one point per row, two measures        | `st.scatter_chart`                     |
| `TABLE`   | the rows as they came back             | `st.dataframe`                         |

The spec carried alongside is `ChartSpec`: the `kind`, an `x` column, a list of `y` columns, an
optional `series` column, and a mandatory `reason` string. `series` is set by the line rule alone
and names the column whose values become one line each; the view hands it to
`st.line_chart(color=...)`. It is named for the role it plays in the data rather than for the
channel it reaches, like `x` and `y`, and it defaults to `None`, so a chart saved before the field
existed reopens as the single-series line it was.

The reason is not decoration. It is printed under every chart in
the app as `Chart chosen by rule: ...`, so a reviewer can see which rule fired without reading
the source. Every spec carries one by construction, because the field is required rather than
optional. What the tests check about its *content* is narrower; see
[§10](#10-what-is-tested-and-what-is-not).

`views/chat.py` renders and decides nothing: `render_chart()` is a dispatch on `chart.kind`
whose `else` branch draws a table, so an unrecognised kind degrades to the rows rather than to a
blank space.

**One conversion makes any of this work.** PostgreSQL returns `numeric` as Python `Decimal`, and
pandas stores `Decimal` in an opaque object column that the chart libraries silently refuse to
plot. Aggregates are `numeric` far more often than not, so without the coercion in
`to_dataframe()` almost every chart in the app would be empty rather than wrong, which is the
harder failure to notice.

## 3. How a column gets a role

Before any rule runs, `classify_columns()` gives every column one of three roles, from the type
**PostgreSQL declares** rather than from inspecting the values. The executor resolves type names
from psycopg's OID registry and returns them on every `QueryResult` for exactly this purpose.

| Role          | Declared types                                                | Plays the part of |
| ------------- | ------------------------------------------------------------- | ----------------- |
| `temporal`    | `date`, `timestamp`, `timestamptz`, `time`, `timetz`          | an axis           |
| `numeric`     | `int2`, `int4`, `int8`, `float4`, `float8`, `numeric`, `money` | a measure         |
| `categorical` | everything else                                               | a label           |

Categorical is the default, and that direction matters: an unfamiliar type becomes a label
rather than a measure, so a column the classifier does not understand can never be silently
plotted as a quantity.

### The one exception, and why it is not a cheat

Text is promoted to temporal when **every** non-null value matches the anchored pattern
`^\d{4}(-\d{2}(-\d{2})?)?$`, that is a bare year, a month, or a full date.

The reason is concrete. The most common way to group by month in PostgreSQL is
`to_char(ts, 'YYYY-MM')`, which returns `text`. A purely type-driven rule therefore classifies
the single most common time-series result as categorical and draws bars where a line is right.
This was found by running the real query against the real database.

Two fixes are applied together, and the order is deliberate. The **primary** fix is in the SQL
prompt, which asks for `date_trunc(...)::date` so the column arrives genuinely typed. The regex
is the **safety net** for when the model uses `to_char` anyway, which it sometimes will.

The pattern is over the column's *values*, not its *name*. A rule keyed on a column being called
`month` would be the fragile version of the same idea, because it would fire on a text column
named `month` holding `January`.

## 4. The six rules, in order

First match wins. This table is the same rule set as
[ARCHITECTURE.md §10](ARCHITECTURE.md#10-chart-selection), with the guards spelled out.

| # | Condition                                                              | Result    | `x`            | `y`               |
| - | ---------------------------------------------------------------------- | --------- | -------------- | ----------------- |
| 1 | zero rows                                                              | `NONE`    |                |                   |
| 2 | one row, exactly one numeric column, at most two columns total         | `METRIC`  | the other column, if any | the measure |
| 3 | at least one temporal and one numeric, **more than one row**           | see [§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses) | | |
| 4 | at least one categorical and one numeric                               | see [§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses) | | |
| 5 | exactly two numerics, no categorical, no temporal, **more than one row** | `SCATTER` | first numeric  | second numeric    |
| 6 | anything else                                                          | `TABLE`   |                |                   |

Rules 3, 4 and 5 each refuse a single row, and rule 6 catches what they drop. A line through one
point shows no trend, a bar chart of one bar is a number drawn very wide, and a scatter of one
point shows no relationship. Rule 2 has already claimed the readable single-row shapes, so
anything single-row arriving at the later rules carries more than a label and a measure, and the
honest render is the row itself.

## 5. Rules 3 and 4 in detail: when a chart refuses

Both rules face the same question: the rows carry a dimension the chart has no room for, so is
there an encoding that shows it, and if not, is the honest answer a table? Rule 4 has asked it
since the first live queries. Rule 3 did not ask it at all until 2026-08-16, which is
[the defect below](#rule-3-one-line-several-lines-or-a-table).

### Rule 3: one line, several lines, or a table

A time axis and a measure is a line. When a category column comes with them, the rows are either
one period per row with the category describing it, or a breakdown of each period by that
category. The two look identical in the column types and need opposite charts.

The discriminator is **whether the time column repeats**, and that is the defect itself rather
than a proxy for it: a single line drawn through rows with two values at one x is a line that
travels backwards and forwards inside one period. `_line_or_table` therefore asks, in order:

1. **No category column.** One line per measure, exactly as before.
2. **The time column is unique per row.** The categories describe the row rather than divide it,
   so this stays a plain line with `series` unset. `q26` is this shape: the winning terminal
   printed beside each of four quarters.
3. Otherwise the rows are a breakdown, the series is the **first** category column, and four
   refusals follow.
   - **More than one measure.** A table. Colour is one channel and two measures would both need
     it. Tested before anything about the rows, because it is a fact about the columns.
   - **Time and series together do not identify a row.** A table, and the direct mirror of Rule
     4's second refusal. A third dimension is present and one line per series would overplot it.
   - **More than 10 distinct series.** A table, with the count in the reason.
   - **No series holds two rows.** A table. `q60` is this shape, six terminals each with one
     worst quarter, and every line would be a single point. Streamlit draws a line chart as three
     layers whose two point layers are both hover-only, one at `opacity: 0` and one filtered to
     the hover parameter, so those points are invisible until the cursor is over them. That was
     read off the spec `st.line_chart` emits under `AppTest` on streamlit 1.61.1 on 2026-08-16,
     not assumed and not taken from documentation, which does not describe the layering.

Otherwise: one line per category, in a single chart, with a legend beneath it.

**The 10 is derived, and it is the one threshold in this document that is not a judgement call.**
Streamlit's default categorical palette holds exactly ten colours, and its own configuration
description states that colours "repeat cyclically if there are more categories than colors"
(`theme.chartCategoricalColors` in `streamlit/config.py`, read from the installed 1.61.1 package
on 2026-08-16). At eleven series two lines are drawn in the same colour and the legend stops being
a key, which is a failure of the encoding rather than a matter of taste. Do not reconcile it with
the 12 below: that one is about how many labels an axis can carry, this one is about how many
colours exist. A user-supplied theme with a longer palette would move the true limit, and
`charts.py` deliberately does not read UI config, because a rule that did would stop being a
function of the result shape alone.

**The series is `categorical[0]`, a convention rather than a search**, matching Rule 4. The cost
is in [§9](#9-what-the-rules-cannot-do).

### Rule 4: bar, or table

A label column plus a measure is usually bars, but three shapes are refused. In order:

1. **One row.** A table, for the reason above.
2. **Several categorical columns whose first one repeats.** A table. If the first categorical
   column does not identify each row uniquely, the rows are a genuine multi-dimensional
   breakdown, and a single-axis bar chart would silently collapse a dimension. `q39`, port calls
   per terminal split by status, lands here: 12 rows, `terminal_name` repeated across statuses.
3. **More than 12 distinct labels.** A table, with the count stated in the reason. Beyond that a
   bar chart is an unreadable table with extra steps. `q55`, vessels above average capacity,
   lands here with 20.

Otherwise: bars, with every numeric column as a series. `q41` and `q63` return three columns each
and draw grouped bars.

The second refusal is narrower than it first appears, and the relaxation was needed. Asked for
average wait by terminal, models routinely return a descriptive companion column,
`terminal_name, port_name, avg_wait`. Under a strict "exactly one categorical column" test that
falls through to a table where bars are plainly right. Extra categorical columns are therefore
tolerated **only** when the first one is already a unique label and the rest are attributes of
it. This was observed on the first live query rather than anticipated.

**The 12 is a judgement call, not a derived constant**, and it is documented as one. A reviewer
who disagrees with it is having the productive argument.

## 6. The metric card

The metric card is the only chart kind whose content needs resolving, and that resolution lives
in `charts.py` rather than in the view, so a test can reach it.

**The measure is looked up by name**, never by position. Reverting to `frame.iloc[0, 0]` would
pass the entire suite and crash on the first superlative question, because column 0 of
`terminal_name, avg_wait` is a string. When a label column is present the entity takes the label
slot and the measure name moves to the tooltip, because `17.46` with the unit nowhere on screen
is not an answer.

`format_metric()` then renders the number without lying about its precision. Four branches, in
execution order. The three type checks among them were each wrong in a first draft, which is why
the order is load-bearing rather than stylistic:

| Value                    | Renders as        | Why this branch exists                                                |
| ------------------------ | ----------------- | --------------------------------------------------------------------- |
| `None`                   | `n/a`             | a null aggregate is a real result; `None` on a headline reads as a crash |
| `bool`                   | `True` / `False`  | `isinstance(True, numbers.Integral)` is `True`, so a boolean would otherwise print as `1` |
| `numbers.Integral`       | `239,099`         | thousands separators, no decimal point                                |
| `numbers.Real`, `Decimal` | `17.46`          | an unformatted average prints as `17.459999999999999`                 |

Two of those types are less obvious than they look. `numbers.Integral` is used rather than `int`
because a value that has passed through pandas is a `numpy.int64`, and
`isinstance(numpy.int64(1), int)` is **False** while the ABC check is `True`; numpy registers its
scalar types with the ABCs rather than subclassing the builtins. A plain `int` check would drop
the separators from exactly the `COUNT` and `SUM` aggregates the function exists to format.
`Decimal` is named explicitly because the standard library deliberately does **not** register it
as a `numbers.Real`, to prevent silent float and Decimal mixing.
Source: <https://docs.python.org/3/library/numbers.html>, verified 2026-08-08. The `numpy.int64`
behaviour above was confirmed by running both checks rather than from that page.

## 7. What the gold set actually renders

Chart choice is not scored by the evaluation harness ([§10](#10-what-is-tested-and-what-is-not)),
so the distribution below is a measurement rather than a result. Executing the 77 answerable
`gold_sql` queries and passing each result to `pick_chart` gives:

| Kind      | Count | The question shape that produces it                                     | Example |
| --------- | ----- | ----------------------------------------------------------------------- | ------- |
| `METRIC`  | 32    | counting questions, and superlatives that answer with a label and a figure | "How many port calls are in the database?" (`q03`) |
| `BAR`     | 22    | "for each X" grouping, and small top-N rankings                          | "What is the average berth wait for each terminal?" (`q02`) |
| `TABLE`   | 11    | list questions, wide profiles, and breakdowns a chart would flatten      | "Which vessels had a cancelled port call at Jebel Ali Terminal 2?" (`q44`) |
| `LINE`    | 8     | "each month" and "each quarter" trends                                   | "Show the total containers moved each month during 2025." (`q04`) |
| `SCATTER` | 2     | two measures with nothing to group by                                    | "For each distinct vessel capacity, what is the average berth wait?" (`q23`) |
| `NONE`    | 2     | questions whose correct answer is no rows                                | "Which terminals are in Japan?" (`q25`) |

Every rule is therefore exercised by real questions and not only by its unit tests.

Two readings of that table are worth having ready. **Metrics dominate because the question set
does**, not because the rules favour them: 38 gold cases are tagged `happy_path` and 14
`comparative`, and counting questions and superlatives both land on rule 2. And **tables arrive
four different ways**, which is why the count is higher than a fallback should be: a shape no
rule claims (`q44`, one text column), the 12-category limit (`q55`), Rule 4's multi-dimensional
refusal (`q39`), and Rule 3's single-point-series refusal (`q60`).

**A third reading is worth stating because it is what this table cannot show.** Only two of the
77 produce a time axis alongside a category at all, and neither is a breakdown: `q26` has four
quarters and one distinct terminal, `q60` has six terminals with one row each. So the shape that
[§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses) now charts as several lines never appears in
the gold set at full strength, which is why the defect it fixes survived every sweep of this
table and had to be found in the running app.

The other 31 gold cases never reach a chart. The 12 ambiguous ones answer with a clarifying
question and the 19 adversarial ones are refused, and neither path produces rows.

## 8. Four guards, and how each was found

Each arrived by a different route, and the routes are the useful part: running the gold set,
running one query against the real database, auditing the rule table as a set, and a user asking
a follow-up question in the running app.

**Rule 2 accepts two columns, not one.** A superlative question such as "which terminal has the
longest berth wait?" answers with a label *and* a measure. It therefore missed a one-column
metric rule, fell through to rule 4, and drew a bar chart containing exactly one bar stretched
across the full width of the container. Nine answerable gold questions produce that shape today,
`q01 q05 q06 q14 q37 q52 q65 q69 q74`, and the first of them is the first example button in the
sidebar. Four produced it when the defect was found, which is the figure
[ADR-005](ADR/ADR-005-deterministic-chart-selection.md) records and deliberately keeps. Found by
rendering the gold set; [§11](#11-verify-it-yourself) reproduces the list of nine.

**The ISO-8601 text check.** Described in [§3](#3-how-a-column-gets-a-role). Found by running the
query against the real database, not by predicting what the model would write.

**The row guard on rule 5.** Unlike the two above, this one was never seen in the wild. It was
found by reading the rule table as a set, after the same single-row defect had been fixed in
rules 2, 3 and 4: a scatter of one point is the shape a superlative takes whenever its label
column is itself numeric, because a numeric ID classifies as a measure. The other three rules
refused single rows and this one did not.

**The series split on rule 3**, added 2026-08-16, is the only one found by using the app rather
than by testing it. A follow-up question, "show its monthly container volume for 2025", returned
`month, operator, total_containers`: 33 rows of twelve months by three operators. Rule 3 kept the
time axis and the measure, dropped the operator, and `st.line_chart` joined all 33 rows in row
order, so inside every month the line jumped between the three operators' values and the whole
chart read as a sawtooth. The rules had been swept over the gold set twice before, and both
sweeps passed, because [§7](#7-what-the-gold-set-actually-renders) contains no question of this
shape. It was a known limitation with a written-down rationale, [§9](#9-what-the-rules-cannot-do)
in an earlier revision of this document, and the rationale said the chart "understates rather than
misstates". That was true of the two gold cases it was measured against and false of the shape a
user asked for.

## 9. What the rules cannot do

Stated rather than discovered by a reviewer. The first two are accepted in
[ADR-005](ADR/ADR-005-deterministic-chart-selection.md); the rest are measured behaviour of the
shipped rules.

**Explicit chart intent is ignored.** "Show me that as a pie chart" is not heard, because the
rules read the rows and never the question. Accepted for this scope. The fix is a small
intent-extraction step feeding an override, which is an addition rather than a rewrite.

**Part-of-whole is invisible.** Data that would suit a stacked bar draws grouped bars, because
nothing in the result set says that the numbers are shares of a total. A semantic layer would
supply that; see [ADR-003](ADR/ADR-003-schema-introspection.md).

**The 12-category threshold is a judgement call.** It is not derived from anything. The
10-series threshold in [§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses) is the exception
that proves it: that one is the length of the palette.

**A series with a missing period is drawn as though it had none.** A line breaks only where a row
is absent from the frame, and a breakdown has no row for a month in which an operator moved
nothing, so Vega joins the months on either side with a straight segment. Halcyon Freight moved
nothing at Jebel Ali in January, March or July 2025, and its line therefore runs straight from
February to April and from June to August at a level it never held. Filling the gaps means
generating rows the SQL did not return, which is a semantic-layer decision, so this is stated
rather than fixed.

**A single-point series inside an otherwise good chart is invisible.** The refusal in
[§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses) fires only when *no* series holds two rows,
because a ragged breakdown is normal and refusing it would table almost everything. So an
operator present in exactly one month contributes a legend entry and no visible mark until the
cursor is over it.

**A series column holding colour names is taken literally.** If the first value of the series
column looks like a colour, Streamlit uses the column's values as the chart's colours and drops
the legend, on the reasoning that a legend reading `#f00` helps nobody
(`built_in_chart_utils.py`, the `is_color_like` branch, read from the installed 1.61.1 package on
2026-08-16). Nothing in this schema produces such a column, and guarding it would mean inspecting
values, which [§3](#3-how-a-column-gets-a-role) restricts to the one anchored date pattern.

**The series column is the first category, not the best one.** `month, region, operator, value`
takes `region`, fails the third-dimension refusal, and renders as a table even though splitting
by `operator` would have drawn a legible chart. Rule 4 takes the first category too; making
either search for the column that works is an addition, and no gold question needs it.

**An encoded ordinal reads as a measure.** `q73` asks for arrivals per day of the week, numbered
0 to 6, and returns `int4, int8`. Both columns classify as numeric, so it renders as a scatter
where bars are the natural reading. This is the mirror image of the `to_char` problem in
[§3](#3-how-a-column-gets-a-role): there, a date arrived typed as text; here, a category arrives
typed as a number. Type-based classification cannot tell an encoded label from a quantity, and
the safe default described in [§3](#3-how-a-column-gets-a-role) protects the opposite direction
only.

## 10. What is tested, and what is not

**Tested.** `tests/test_charts.py` holds 43 tests: one or more per rule, the ISO-shaped text
case, both category limits at their boundaries, the companion-column relaxation on each of rules
3 and 4, all four single-row guards, and each of Rule 3's four series refusals.

**The `reason` string is unevenly covered, and the count is worth stating exactly.** Nine tests
assert a substring of one, covering six distinct reasons: the table a label over the category
limit produces, the multi-series line, and all four tables Rule 3 can return. Three of those six
are asserted from two directions, the multi-series line, the series limit and the hidden
dimension. A tenth test,
`test_every_spec_explains_which_rule_fired`, asserts only that a reason is longer than fifteen
characters. Nine reasons therefore have no assertion on what they actually say: the no-rows
reason, both metric reasons, the single-series line, the bar, the scatter, the table a single
row produces, Rule 4's multi-dimensional table, and the catch-all.

This paragraph was the one figure in this document nothing reproduced, and it drifted twice in a
single day: it claimed four assertions while eight were true and named two reasons as covered
that only the length check touches, and then a tenth assertion added an hour later made the
corrected figure of eight stale in turn. [§11](#11-verify-it-yourself) now carries a command that
recounts it, which is the actual fix. A number a reader cannot recompute is a number that will be
wrong again.

`test_every_spec_explains_which_rule_fired` was named for every spec but sampled four shapes that
all carried zero or one row, and rules 3, 4 and 5 each require more than one row, so three of its
four cases collapsed into rule 2. It now carries a multi-row line case and a multi-row bar case
as well. Every spec still *carries* a reason regardless, because the field is required.

**Tested through the running app.** `tests/test_app_smoke.py` drives the real page headlessly.
`st.bar_chart` has no accessor of its own in Streamlit's test harness, but it emits a
`vega_lite_chart` element whose spec is JSON, so axis titles and sort order are assertable there.
A saved chat is asserted to reopen with its table and its chart rather than with its text, and a
saved breakdown is asserted to reach the page as a colour encoding on the rendered chart. That
last one is the assertion the sawtooth defect needed: a `ChartSpec` naming a series column proves
nothing about the picture, because the column was lost in `render_chart`, the one step no unit
test can see.

**Not measured.** The evaluation harness scores execution accuracy, groundedness, and
clarification and safety. It has no concept of a chart, so no run reports whether the chart
chosen for a question was the right one. Chart correctness rests on the unit tests plus the
distribution in [§7](#7-what-the-gold-set-actually-renders), which shows that every rule fires on
real questions but not that every question got the best rule. The `q73` case in
[§9](#9-what-the-rules-cannot-do) is what that gap looks like once the rules are run over the
gold set and the specs they returned are read, which is how it was found. The sawtooth in
[§8](#8-four-guards-and-how-each-was-found) is what the gap looks like when the gold set has no
question of the shape at all, and that one needed a user.

Charts are also absent from the gold set's schema by construction: `eval/gold.py` sets
`extra="forbid"`, so an `expected_chart` field would fail to load rather than be ignored. Chart
expectations exist only as prose, in the `note` of five cases and in the table in
[DATA.md](DATA.md#questions-that-stress-the-charting-logic).

## 11. Verify it yourself

Start the database first, per the README's setup: `docker compose up -d --wait`, then
`python db/seed.py` if it has not been seeded. None of the commands below needs a model or a
network.

**The rule tests, which need neither a database nor a model:**

```bash
python -m pytest tests/test_charts.py -q
```

**The distribution in [§7](#7-what-the-gold-set-actually-renders)**, executed against the seeded
database:

```bash
python - <<'PY'
import collections, yaml
from src.charts import pick_chart
from src.executor import run_query

cases = [c for c in yaml.safe_load(open("eval/gold_questions.yaml"))
         if c["category"] == "answerable"]
counts = collections.Counter()
for case in cases:
    counts[str(pick_chart(run_query(case["gold_sql"])).kind)] += 1
print(len(cases), "answerable cases:", dict(counts))
PY
```

**The nine labelled metrics in [§8](#8-four-guards-and-how-each-was-found)**, which are the
questions that drew a single stretched bar before rule 2 was widened:

```bash
python - <<'PY'
import yaml
from src.charts import pick_chart
from src.executor import run_query
from src.models import ChartKind

for case in yaml.safe_load(open("eval/gold_questions.yaml")):
    if case["category"] != "answerable":
        continue
    spec = pick_chart(run_query(case["gold_sql"]))
    if spec.kind == ChartKind.METRIC and spec.x:
        print(case["id"], spec.x, spec.y)
PY
```

**The two worked examples in [§5](#5-rules-3-and-4-in-detail-when-a-chart-refuses)**, which are
the gold set's only results carrying a time axis beside a category. `q26` must stay a plain line
with no series, and `q60` must refuse:

```bash
python - <<'PY'
import yaml
from src.charts import pick_chart
from src.executor import run_query

cases = {c["id"]: c for c in yaml.safe_load(open("eval/gold_questions.yaml"))}
for qid in ("q26", "q60"):
    spec = pick_chart(run_query(cases[qid]["gold_sql"]))
    print(qid, spec.kind, "series=%r" % spec.series, "|", spec.reason)
PY
```

**A breakdown that does draw several lines**, since no gold question produces one. This is the
shape from [§8](#8-four-guards-and-how-each-was-found), and it must report three series:

```bash
python -c "
from src.charts import pick_chart
from src.executor import run_query
print(pick_chart(run_query('''
  SELECT date_trunc('month', cm.move_ts)::date AS month, v.operator,
         SUM(cm.container_count) AS total_containers
  FROM cargo_moves cm
  JOIN port_calls pc ON pc.port_call_id = cm.port_call_id
  JOIN vessels v ON v.vessel_id = pc.vessel_id
  WHERE v.operator IN ('Meridian Lines', 'Halcyon Freight', 'Cardinal Container Line')
  GROUP BY 1, 2 ORDER BY 1, 2''')).reason)"
```

**The reason-coverage count in [§10](#10-what-is-tested-and-what-is-not)**, which needs neither a
database nor a model. It is the figure in this document that drifted twice, so it is the one most
worth recomputing:

```bash
python - <<'PY'
import collections, pathlib, re

blocks = re.split(r"\ndef ", pathlib.Path("tests/test_charts.py").read_text())
asserting = {b.split("(")[0]: len(re.findall(r"in spec\.reason", b))
             for b in blocks if "in spec.reason" in b}
print(len(asserting), "tests assert a substring of a reason:")
for name, count in asserting.items():
    print(f"  {name}")
PY
```

**Why any single question drew what it drew.** Every `ChartSpec` carries its own explanation, and
the app prints it under the chart:

```bash
python -c "
from src.charts import pick_chart
from src.executor import run_query
spec = pick_chart(run_query('SELECT status, COUNT(*) FROM port_calls GROUP BY 1'))
print(spec.kind, spec.reason)"
```

**In the running app**, `streamlit run app.py`, ask any question from
[§7](#7-what-the-gold-set-actually-renders) and read the caption under the chart. It names the
rule that fired and the numbers it fired on.
