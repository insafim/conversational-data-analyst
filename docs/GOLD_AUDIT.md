# Auditing the gold set by hand

The accuracy figures this project publishes are produced by comparing the agent's rows against the
reference SQL in [`eval/gold_questions.yaml`](../eval/gold_questions.yaml). That makes the reference
SQL the measuring instrument, and an instrument nobody has checked is an assumption wearing a
number's clothes.

This document is the working procedure for checking it. [ADR-006](ADR/ADR-006-eval-execution-accuracy.md)
holds the reasoning behind the decision; this holds the method, so that a second person could repeat
the audit and get a comparable result.

## What is being audited, and what is not

Only the **77 answerable cases** carry reference SQL. The 12 ambiguous and 19 adversarial cases
assert a behaviour, that the agent asks back or refuses, and there is no query to re-derive.

Within those 77 there are two populations, and they carry different amounts of evidence:

| | Cases | Evidence today |
| --- | --- | --- |
| Disagreed with the agent at least once across runs 20 to 26 | 11 | Adjudicated individually. Two of this population's ancestors, `q54` and `q61`, were found to be defective and rewritten |
| Agreed with the agent in all seven runs | 66 | None that is independent |

The second row is the gap. Agreement is weak evidence, because the agent and the reference are not
independent sources: both encode the same reading of the same question against the same schema, so a
question misread the same way twice produces two matching wrong answers and a passing score.

The first row is not the answer to it. Adjudication is not an audit: it is triggered by the agent
disagreeing, so it reaches only the cases where the two happened to differ and never the ones where
a shared misreading held. That is why the eleven contribute nothing to the figure below. What the
ledger records is scrutiny that does not depend on the agent agreeing, which is the one form of
evidence this harness cannot produce for itself.

Coverage today is **0 of 77 reference queries** independently audited. That figure is generated from
[`eval/gold_audit.yaml`](../eval/gold_audit.yaml) and pinned by `tests/test_gold_audit.py`, so no
document here can come to claim more scrutiny than has been performed.

## What counts as evidence

The `method` field on each ledger entry records how the check was done, and it matters more than the
verdict. Re-reading your own SQL and agreeing with it establishes nothing, because the misreading
that produced the query produces the same reading on review.

| Method | What it means | Independent? |
| --- | --- | --- |
| `rederived` | The answer was recomputed by a differently shaped query and the two compared | Yes |
| `seed_constant` | The result was checked against a value [`db/seed.py`](../db/seed.py) plants deliberately | Yes, and the strongest available |
| `counted` | A small result was counted by hand from the underlying rows | Yes |
| `intent_only` | The question was read against the SQL, nothing was recomputed | **No** |

`intent_only` is recorded rather than forbidden. A case checked that way is still better than one
never looked at, and the ledger states the strength of its own evidence instead of flattening every
check into the word "verified".

## The workflow

```bash
# Reading material for everything not yet in the ledger. Regenerate whenever you like;
# it is gitignored, because it holds nothing the repo does not already contain.
python eval/audit.py worksheet --out gold_audit_worksheet.md

# One line per verdict.
python eval/audit.py record q01 confirmed --method rederived --note "counted 1,500 port calls"

# Where you are.
python eval/audit.py status
```

The worksheet puts everything needed for one case on a single screen: the question, the note saying
why the case exists, the reference SQL, the rows it returns against the seeded database right now,
and any mechanical flags.

For each case, ask two questions **in this order**:

1. **Does the SQL answer the question that was asked?** Read the question and say what the answer
   should be before looking at the query. Both defects found so far failed here rather than being
   SQL errors: in both the query was valid and the question meant something else.
2. **Is the returned value right?** Check it against a planted value in `db/seed.py`, against a
   magnitude you already know, or by writing the query a different way.

A case whose reference SQL is correct but whose *question* is ambiguous is a `defect`. The gold set
is a specification, and an ambiguous specification is a defective one.

## Suggested order

Defect risk is not spread evenly, so neither should attention be. In order:

1. **The six high-signal flags**: `q25`, `q54`, `q56`, `q65`, `q74`, `q77`. These carry a flag other
   than `SINGLE_SCALAR` and are the most likely places for a real defect.
2. **The 13 unchallenged cases with two or more joins.** A wrong join key or a misplaced filter hides
   far more easily in a multi-table aggregate than in a `COUNT(*)`.
3. **Everything else.** Mostly single-table counts and aggregates, and quick to confirm.

Stopping after any of these three is a legitimate outcome, because coverage is reported rather than
assumed. What is not legitimate is stopping and describing the set as verified.

## What the flags mean

Flags are prompts for attention, never findings. Nine can be raised. Five of them fire on the set
as it stands, and 29 of the 77 cases raise at least one: 25 `SINGLE_SCALAR`, plus the six
high-signal cases above, of which `q54` and `q77` also return a single scalar and so are counted
once.

Raised today:

| Flag | What it observes | Why it is worth a look |
| --- | --- | --- |
| `SINGLE_SCALAR` | Returns one row and one column | The weakest possible comparison. Agreement on a single number is easy to reach by luck. Raised by 25 cases, so it marks a population rather than a suspicion |
| `EMPTY_RESULT` | Returns zero rows | Any agent query returning nothing compares equal. ADR-006 calls this degenerate agreement. `q25` and `q56` return nothing **deliberately**, because a question about a period outside data coverage should, and groundedness scores the answer separately |
| `RANKING_NOT_ORDERED` | Ranks and limits, but `ordered` is false | A wrongly ordered answer would still pass |
| `AVERAGE_WITHOUT_AVG` | Asks for an average, computed without `AVG` | Check the divisor is the one the question implies. This is the `q54` defect class |
| `SUPERLATIVE_NO_LIMIT` | Asks for a single extreme, no `LIMIT` | The whole ranking is returned where one row was asked for. This is the `q05` defect class |

Silent on the current set, and listed because a worksheet can still show them after a case is added
or edited:

| Flag | What it observes | Why it is worth a look |
| --- | --- | --- |
| `ORDERED_NO_ORDER_BY` | `ordered` is true, no `ORDER BY` | The comparison enforces an order the query does not produce |
| `PER_GROUP_WITH_LIMIT` | Asks per group, SQL limits rows | Most groups are missing from the expected answer |
| `YEAR_NOT_IN_SQL` | Question names a year the SQL does not | The period may be unfiltered |
| `TRUNCATED` | The row cap trimmed the result | The reference itself is incomplete, so the comparison depends on the cap |

A flagged case is very often correct and deliberately so. Treating a flag as a finding would be the
same error as treating agreement as verification, pointed the other way.

## When you find a defect

Record it first, with `--method` and a note describing what is wrong, then decide separately whether
to fix it. The two acts are separate on purpose: a recorded defect is evidence the audit worked, and
it stays true whether or not there was time to act on it.

Fixing one has a cost worth knowing before you start. A corrected reference query changes what that
case scores, so every published figure computed from it becomes stale, including run 26. Re-running
the full set costs about 13 minutes and about $1.26. If a defect is found and not re-measured, say so
in the ledger note and leave the published figures alone rather than adjusting them by hand.

## What may be claimed

The only sanctioned sentence about coverage is the one `eval/audit.py` generates from the ledger,
which today reads:

> 0 of 77 reference queries independently audited

Four tests enforce this. One checks that every coverage figure in the shipped documents matches the
ledger. One checks that any sentence discussing the audit states a figure in that checkable form, so
a claim phrased as "most of them have been checked" fails rather than passing unread. One checks that
no shipped document claims the reference SQL was verified by a person without qualification while
coverage is short. The fourth checks the other three are actually reading every shipped document,
because a new file is the easiest way to write a claim nothing is watching.

That last one has already earned its place: this document tripped all three guards on the day it was
written, once for a placeholder figure and once for language the ledger could not support.

The phrasing those guards forbid was previously used in eleven places, including two in code, to
describe reference SQL for which no per-case record existed. It was replaced with what the harness
actually establishes. The ledger exists so that the claim and the work cannot drift apart again.
