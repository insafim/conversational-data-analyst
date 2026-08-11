# ADR-010: Syllabus-Mapped Eval Expansion, 36 to 100 Cases

- **Status:** Accepted
- **Date:** 2026-08-10
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-006](ADR-006-eval-execution-accuracy.md), [ADR-009](ADR-009-withheld-runtime-capabilities.md)

## Context

The gold set stood at 36 cases and had scored 36/36 on seven consecutive runs (runs 8
through 14). A saturated suite confirms; it no longer measures. Separately, the set had
grown organically (30, then 33, then 36) rather than against any stated coverage model,
so "what does this eval actually exercise" had no checkable answer.

Two structured taxonomies existed and were unused: a 75-topic SQL syllabus
(`docs/sql_study_guide.md`, section 1) and a 19-category behavioural question taxonomy
from an external multi-agent project (`Questions/`, treated as reference only; different
domain, different system).

## Decision

**Expand to 100 cases, every case tagged on two orthogonal dimensions, with coverage
printed by the harness on every run.**

- `topics`: which syllabus topic ids (0 to 74) the reference answer exercises.
- `behaviour`: how the question is asked, from a fixed vocabulary in `eval/gold.py`
  (happy_path, edge_data, fact_check, phrasing, vague, manipulation, destructive, and
  nine others).

Category split: 70 answerable, 12 ambiguous, 18 adversarial
(`tests/test_gold_set.py:36-38` pins these).

### The mapping rule

1. Every syllabus topic expressible as a read-only analytics question against this
   schema has at least one answerable case.
2. Every write topic appears as an adversarial case that must be refused: the INSERT
   block (20-23) at `s06`, UPDATE (37-40) at `s07`/`s08`, DELETE and TRUNCATE (41-45) at
   `s09`/`s10`/`s14`, CREATE at `s11`, ALTER at `s12`, DROP at the pre-existing `s01`.
   The SKIP topics of the syllabus are therefore covered, as refusals, which is the same
   posture the role grants take: the write path exists in the eval precisely to be
   denied.
3. Topics that cannot be exercised are excluded with recorded reasons: 59/60 (a question
   cannot compel RIGHT or FULL JOIN syntax; any such answer is expressible as LEFT JOIN
   and result-set scoring cannot distinguish them), 54 (execution order is a concept,
   not a question), 63 (no one-to-one pair exists in this schema), 65 (many-to-many:
   no junction table exists; `cargo_moves` carries its own measures, so the shape is a
   fact table and no question can exercise a link-table traversal), 69 (ALL reduces to
   an aggregate comparison), 72-74 (index topics are not analyst questions), 12/16/17/19
   (type-system topics with no schema instance; 3 and 10 likewise).

### Beyond the syllabus

Constructs the course catalog lacks but the system meets in practice: six window-function
cases (`q26`-`q28` pre-existing; `q60` RANK over PARTITION, `q61` explicit ROWS BETWEEN
frame, `q62` LAG added), plus a considered-and-excluded list: GROUPING SETS and ROLLUP
(no question shape at five tables), recursive CTEs (no hierarchy exists in this schema),
LATERAL (used internally at `src/schema.py:69` but contrived as a question). A follow-up
tranche adds set operations (INTERSECT), an explicit-override COALESCE case, and
EXTRACT day-of-week, verified 2026-08-10 and queued behind the baseline runs so the
baseline is measured against a frozen set.

### Authoring protocol (enforced, not aspirational)

Every reference query was executed against the seeded database on 2026-08-10 before its
case was added; LIMIT queries were checked for ties at the cut boundary (`q52`: Germany
62825 against UK 61242, no tie; `q58`: three distinct values). Reference SQL passes the
same validator the agent is held to and carries no comments, both test-enforced
(`tests/test_gold_set.py`). The set was NOT tuned to what the agent currently passes:
several cases were designed at known failure classes, deliberately.

Cases worth naming because they encode past failures or known traps:

- `q56`: NOT EXISTS with a deliberately empty answer. The empty-result-on-answerable
  class failed twice historically (run3 q15, run6 q19).
- `q59`: CASE with bands defined in the question, the legitimate version of run4 q23's
  invented-bands failure.
- `q46`: fact-check with a false premise (2000 asserted, 1500 true), aimed at the
  summariser's grounding contract.
- `q65`: "most recent month" resolves to July 2026, which holds two days of data. The
  recency trap is in the data, not the phrasing.
- `q69` against `a01`: the same stem ("busiest terminal") appears on both sides of the
  ambiguity boundary, disambiguated on one side only. `q70` against `a12`: terse
  phrasing on both sides, determined on one ("port calls 2025") and undetermined on the
  other ("Rotterdam?"). The sparing-clarify rule is tested in both directions.
- `s14`: a user-pasted CTE-wrapped DELETE, the statement-type-check evasion from
  `tests/test_validator.py:55`, now exercised end to end.

### Why two dimensions rather than more cases on one

A topic-only expansion measures SQL breadth and nothing else; the twelve historical
failures (ADR-009, evidence table) were dominated by intent and phrasing, not SQL
capability. A behaviour-only expansion measures robustness against a fixed, narrow SQL
surface. The two tags together let a failure be located in both planes ("phrasing
failures cluster on join topics") at the cost of two yaml keys per case.

## Consequences

- The suite measures again: baseline runs against the expanded set start at run 15, and
  their results are recorded in the README alongside the earlier series with the set
  size stated per run, as was already the practice when the set grew from 30 to 36.
- 546 tests pass (`pytest`, 2026-08-10), including per-case schema validation, validator
  compliance of every reference query, and topic-range checks on tags.
- The study guide's absence claims for topics 53 and 68 flipped to GROUNDED and its
  tally moved from 48/14/13 to 50/14/11; the guide records the flips inline.
- Failures surfaced by the expanded set feed the runtime-verification design (deferred
  candidates in [ADR-009](ADR-009-withheld-runtime-capabilities.md); adoption decided
  2026-08-10, recorded in ADR-012 when implemented).
- Cost per full run rises with the set size: projected from the measured per-question
  mean of runs 12 to 14 ($0.0098 to $0.0100), roughly $1.00 and 10 to 12 minutes per
  run. Actual figures land in the README with runs 15 onward.

## Addendum, same day: baseline results and the 103-case final set

Baseline runs 15 to 17 executed against the frozen 100-case set: 94/100, 93/100, 94/100,
at $0.8805 to $0.8816 per run. Six cases failed identically in all three runs (q49, q61,
q62, q64, q70, a10) and q54 failed once. The review attributed q54 and q61 to gold
wording that admitted two defensible readings; both questions were rewritten to pin one
reading (q54 now names the all-calls divisor and its reference SQL returns 4.38; q61 now
names both output columns, mirroring q27's phrasing). The other five failures are
retained as measured headroom: two clarify-boundary misplacements in each direction
(q49/q70 answered-side, a10 clarify-side) and two column-shape mismatches (q62, q64),
the class [ADR-012](ADR-012-runtime-verification.md) targets.

After the baseline was recorded, the beyond-catalog tranche landed: q71 (INTERSECT,
exercising the validator's SetOperation allowlist), q72 (COALESCE under explicit user
instruction to override the NULL-ignoring default, 7.41 against 7.64), q73 (EXTRACT
day-of-week with the numeric representation pinned in the question). Final set: 103
cases, 73/12/18, pinned at `tests/test_gold_set.py`. Test suite at this state: 558
passing, of which 302 in `tests/test_gold_set.py`; both figures grow with the set, so
recount before quoting.
