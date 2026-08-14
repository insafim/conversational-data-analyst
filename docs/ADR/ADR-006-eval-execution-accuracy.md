# ADR-006 — Evaluation by Execution Accuracy on a Gold Set

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-001](ADR-001-domain-and-data-model.md), [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)

## Context

The requirements name three things to be evaluated: **SQL correctness, answer groundedness, and ambiguous
query handling**. Every text-to-SQL demo asserts the first. Almost none measure it.

The distinction matters commercially, not just academically. A client cannot deploy a system whose
correctness is a vibe, and the first question any serious buyer asks is "how do you know it works,
and how will you know it still works after you change the prompt?" A number that can be regenerated
on every change is a different kind of artefact from a demo that worked once on camera.

## Decision

**A gold question set plus an execution-accuracy harness: run the agent's SQL and the reference SQL
against the same database, and compare the result sets.**

The gold set (`eval/gold_questions.yaml`) contains three categories of case, because the requirements
name three behaviours:

| Category | What the item asserts | How it is scored |
| --- | --- | --- |
| **Answerable** | Reference SQL, hand-written and executed against the seeded data | Result-set equivalence with the agent's SQL |
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

Fourteen full runs have now been executed. Runs 2 and 3 share identical code and prompts and scored **95.5%**
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

Safety, by contrast, was 5/5 in every run: 70/70 attempts across fourteen runs. That stability is not a property of the
model; it is a property of enforcing the guarantee at a layer the model cannot reach
([ADR-004](ADR-004-defence-in-depth-sql.md)).
**That last sentence is wrong, and is corrected by the 2026-08-14 addendum at the end of this
record.** It is left standing because this is a record of what was decided and believed, but a
reader who stops here would otherwise carry away the claim that propagated into four documents.

## Honest limitations

Stated here rather than discovered by a reviewer:

- **The gold set is small.** At 28 answerable items one case is worth 3.6 percentage points, so the
  headline number has a wide confidence interval. It is a regression detector and a smoke test, not
  a precise measurement of general capability. Reporting "90%" from 20 items without saying this
  would be overclaiming. This was subsequently confirmed empirically — see the variance section
  above.
- **Result-set comparison can pass a wrong query, and agreement is not the fix.** If the reference
  SQL is itself wrong, agreement is meaningless. The tempting mitigation is that the agent agrees
  with the reference on 93% of cases, so the reference is probably right. That reasoning does not
  hold, because the two are not independent sources: both encode the same reading of the same
  question against the same schema, so a question misread the same way twice produces two matching
  wrong answers and a passing score. Both gold defects found so far, `q54` and `q61`, were exactly
  this. Neither was a SQL error; in both the query was valid and the *question* meant something
  else, and both surfaced only because the agent happened to disagree.

  What the harness does establish is narrower and worth stating precisely: every reference query
  executes against deterministically seeded data on every run, passes the same validator as the
  agent's SQL, and eleven of the 77 answerable cases have been adjudicated individually after
  disagreeing with the agent across runs 20 to 26. The 66 that have never disagreed carry no
  independent evidence at all, and that is the gap.

  Scrutiny that does not depend on the agent agreeing is recorded separately, per case, in
  `eval/gold_audit.yaml`: who checked it, by what method, and what they concluded. Any figure a
  document quotes from that ledger is generated by `eval/audit.py` and pinned by
  `tests/test_gold_audit.py`, so no document here can come to claim more scrutiny than has
  actually been performed.
- **Degenerate agreement.** Two queries can both return zero rows and compare equal. Most gold
  cases return non-trivial results for this reason, but two do not and should not: `q25` asks for
  terminals in Japan and `q56` for cranes that have never moved cargo, and the correct answer to
  both is nothing. Emptiness is the assertion in those cases, so the residual risk is accepted
  rather than designed away, and it is bounded by scoring groundedness separately, which requires
  an explicit "no matching data" rather than an invented figure. `eval/audit.py` raises
  `EMPTY_RESULT` on exactly these two cases so that a third one cannot appear unnoticed.
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

## Addendum, 2026-08-12: the artefacts now record what produced them

Runs 1 to 25 are lists of per-case records and nothing else. There is no run-level object
in any of them: no model ids, no configuration, no prompt state, no commit.

That gap undercuts two claims made above in this document.

The variance section states that runs 2 and 3 "share identical code and prompts" and
scored 95.5% against 86.4%. The nine-point spread is the evidence for treating a single
run as unable to distinguish 86% from 95%, and it is load-bearing: it is why every figure
in the README is quoted as a range. But the premise is unfalsifiable from the artefacts.
Nothing on disk records what run 2's prompts were, so the claim rests on the author's
recollection of a working tree. It is probably true and it is not checkable, and this
document is not entitled to the difference.

The consequences section then says the harness turns correctness into "a reproducible
number". Reproducing a number requires knowing what produced it. Until now the harness
did not write that down.

**From run 26 onward, `eval/run_eval.py` writes `runNN.meta.json` beside `runNN.json`**,
carrying a sha256 per prompt constant in `src/prompts.py` and one aggregate digest over
the registry, a hash of the gold set, both model ids, the configuration that changes what
a run measures, the effective `--verification` and `--reading` values, the case ids
actually run, the invocation, the commit with a dirty flag and the paths that were dirty,
and the Python and litellm versions. The comparison the variance section wanted to make is a
diff of two digests.

The dirty PATHS were added on the same day, after run 26 recorded `dirty: true` for an honest
but harmless reason: redirecting the harness output to `runNN.log` creates that file seconds
before provenance is captured. The boolean was correct and useless, because a reader could not
tell the run's own log from a modified `src/`. Listing the paths separates them without
special-casing the harness's output, which would have made the flag lie in the one situation it
exists for. Run 26's own metadata predates the field and carries only the boolean.

Three choices inside that are worth stating, because each rejects an easier option:

- **A sibling file rather than a field in the run file.** The 25 committed artefacts are
  the evidence behind the figures in the README and in
  [ADR-012](ADR-012-runtime-verification.md). Adding a key to them would edit committed
  evidence so that it appeared to have recorded something it never did.
  `src/telemetry.py` already anchors its filename pattern at both ends so the observability
  page skips these siblings.
- **No backfill.** Runs 1 to 25 get nothing. Their prompt state and commit are not
  recoverable, and a metadata file containing reconstructions would look like a record
  while being a guess. The absence is left visible, and the paragraph above stands as the
  statement of what that costs.
- **Provenance only, never results.** No score, total or latency is copied into the
  metadata. The records say what happened; the sibling says what produced it. A figure
  written in two places is two sources for one number, and they drift.

This does not make a run reproducible. The models are external and non-deterministic at
`temperature=0`, as this document has already measured. It makes a run **attributable**,
which is the part that was missing: a future spread between two runs can now be assigned
to sampling variance or to a changed prompt, instead of argued about.

## Addendum, 2026-08-14: which layer the safety figure actually measures

The variance section above says the safety score "is a property of enforcing the guarantee at a
layer the model cannot reach". That sentence is left standing because it is what was believed
when it was written, and this record is amended rather than rewritten. It is wrong, and the
correction matters more than the original claim did.

Counted across all 26 committed runs, 1,745 case results: **no adversarial case has ever ended in
`rejected`.** That is the outcome the validator produces. Every adversarial case ended in
`refused`, apart from 18 provider errors, and `refused` is produced by `classify` before any SQL
exists. Not one attack in the gold set has ever reached the validator, let alone the database.

So the perfect safety score measures the **first** layer, and that layer is a prompt, which is
precisely the layer an attacker is able to argue with. A metric that never exercises the layers
behind it cannot be evidence about them. Reported honestly, this figure says the classifier has
turned away nineteen known attack shapes on every run, which is worth having and is not the same
claim as "enforced where the model cannot reach".

The write guarantee is real, and it is established somewhere else entirely:
`tests/test_security_boundary.py` disables the bypassable read-only transaction guard, attempts
each write anyway, and requires the failure to come back as `permission denied` from PostgreSQL
rather than from application code. That test is what ADR-004's claim rests on, and it holds
whether or not a single gold case passes.

The eval and the security boundary therefore prove different things, and the honest description
of this harness is that it measures behaviour, not enforcement. [docs/EVAL.md](../EVAL.md) §6 and
§8 carry the same correction.
