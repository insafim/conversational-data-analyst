# ADR-008 — A Thin Streamlit UI, and the Scope Boundary

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-005](ADR-005-deterministic-chart-selection.md), [ADR-011](ADR-011-bounded-multi-turn.md), [ADR-013](ADR-013-the-reading-without-the-verdict.md), [ADR-014](ADR-014-conversation-store.md)

## Context

The requirements ask for a conversational interface and allow a few hours. Interface work expands to fill
whatever time it is given, and none of that expansion is scored here. The interesting question is
therefore not which UI framework, but **how little UI can be built while still demonstrating every
assessed behaviour** — and, having drawn that line, whether the things left outside it were left out
on purpose or by accident.

Scope control is itself a deliverable. A prototype that quietly omits things reads as incomplete; a
prototype that names its omissions and justifies each one reads as judgement.

## Decision

**Streamlit, deliberately thin, plus an explicit and defended out-of-scope list.**

The UI's entire job is to make a small set of things visible, because each maps to an assessed
line. The set is listed rather than counted: this sentence stated a total once, addenda added
rows underneath it, and the total was wrong for long enough to be worth not repeating.

| Element | What it demonstrates |
| --- | --- |
| Chat input and message history | The conversational requirement |
| Natural-language answer | Groundedness — phrased only from returned rows |
| Chart, rendered from a typed `ChartSpec` | Chart-type selection ([ADR-005](ADR-005-deterministic-chart-selection.md)) |
| Collapsed "View SQL" expander | Auditability |
| Collapsed per-answer telemetry: seconds, cost, model calls, stage breakdown | Latency AND cost, for the turn just taken (see the 2026-08-12 addenda) |
| Sidebar table list, each opening onto its columns, their types and their units | Schema handling, without a column list a non-technical reader must scroll past |
| Sidebar list of saved chats | Conversations that survive a reload ([ADR-014](ADR-014-conversation-store.md)) |
| An Observability page: latency, cost, guardrail counts, stage means, per-category eval scores, eval runs | Latency and cost as a distribution, and the safety record as evidence |

Plus one sidebar block with a schema summary and four example questions as buttons, which exists so
a reviewer can drive the demo without inventing questions.

No auth, no theming beyond defaults, no custom components. Charts use Streamlit built-ins
(`st.metric`, `st.line_chart`, `st.bar_chart`, `st.scatter_chart`, `st.dataframe`), which adds zero
charting dependencies beyond pandas.

Streamlit specifically because the alternative — a React front end with an API behind it — is
several hours of work that demonstrates nothing on the rubric. The UI is the least interesting
component of this system and its implementation should say so.

**The visible SQL expander is the load-bearing UI decision.** It is what converts the system from
something a user must trust into something a user can check, and it is the human-in-the-loop story
in this build: an analyst can read the SQL that produced a number before that number reaches a deck.

## Deliberately out of scope

One line of reasoning each, because "not built" and "not considered" are different claims.

**Authentication.** The prototype has one implicit user. Real deployment needs identity, and
identity is a precondition for row-level security ([ADR-004](ADR-004-defence-in-depth-sql.md)) —
which is why it is the first item on the path to production rather than a UI feature.

**Caching.** Repeated questions re-run the pipeline. A semantic cache on question → SQL would cut
both cost and latency, and it is genuinely useful, but it optimises a system whose correctness has
not yet been established. Correctness first, then make it fast.

**Multi-turn memory.** *Superseded on 2026-08-11 by [ADR-011](ADR-011-bounded-multi-turn.md), which
built it. The paragraph below is the original deferral, kept as the record; see the addendum.*
Every question is answered independently; "and what about last year?" is not
resolved against the previous turn. This is the most defensible omission to *want* and the most
expensive to do properly, because it turns SQL generation into a coreference problem and every
ambiguity compounds across turns. LangGraph's checkpointing is the intended mechanism when this is
built, which is part of why the framework was chosen despite being oversized for six nodes
([ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)).

**Streaming.** Answers appear when complete rather than token by token. Streaming is perceived
latency, not latency; with a three-call pipeline the honest fix is fewer or faster calls
([ADR-007](ADR-007-llm-provider-and-tiering.md)), not a progressive reveal that makes the same wait
feel shorter.

**Deployment.** Runs locally via Docker Compose and `streamlit run`. Containerising and deploying
demonstrates infrastructure competence that this project does not set out to show.

**Semantic layer.** No governed metric definitions. This is the most consequential omission and the
one to raise before a reviewer does: without agreed definitions, "utilisation" resolves to whatever
the model infers that day, and the same question yields three different SQLs and three different
numbers across three sessions. At prototype scale the column comments carried by schema
introspection ([ADR-003](ADR-003-schema-introspection.md)) are a partial substitute. At client
scale they are not, and this — not model capability — is the usual reason agent-analytics
deployments stall.

## Alternatives considered

**React front end with a FastAPI backend.** The right architecture for a product and the wrong one
for a project this size. It would consume a large share of the time budget to demonstrate a skill this
project does not set out to show, at the direct expense of the eval harness — which is the component that actually
differentiates.

**Command-line interface only.** Cheapest, and it would satisfy "conversational" on a technicality.
Rejected because chart rendering is an assessed behaviour and a terminal cannot show a chart. The UI
exists mainly to make the chart and the SQL visible.

**Jupyter notebook.** Rejected: it demonstrates the pipeline to someone who already reads Python,
which is the opposite of the stated user — a non-technical operations manager.

## Consequences

**Positive**

- UI time stayed within its budget, leaving the eval harness fully built.
- Visible SQL and a latency caption make two assessed properties directly observable rather than
  claimed. *Both 2026-08-12 addenda apply. The caption became a page, then came back as a
  collapsed expander beside the SQL once the page existed to give it a baseline. It is
  observable in both places now, per turn and as a distribution.*
- Streamlit's built-in charts mean chart rendering added no dependency and no custom code.

**Negative / accepted**

- Streamlit re-runs the script on interaction, which constrains how state is handled and would not
  scale to concurrent users. Correct for a single-reviewer demo; not a production serving model.
- Single-turn only, which is the omission a live demo is most likely to expose: a reviewer's
  natural second question is usually a follow-up. Better to state the boundary in advance than to
  demonstrate it accidentally. *This prediction is the reason the omission was later closed;
  [ADR-011](ADR-011-bounded-multi-turn.md) quotes this line as its own starting point. Superseded
  2026-08-11, see the addendum.*
- No accessibility, mobile, or internationalisation consideration whatsoever.

## Addendum, 2026-08-11: the multi-turn omission was closed

Two clauses above are no longer true of the shipped app, and both are marked in place rather
than deleted, because the deferral was a correct decision at the time and the record of it is
worth more than a tidy document.

[ADR-011](ADR-011-bounded-multi-turn.md) added one rewrite node at the edge of the graph. A
follow-up such as "and Rotterdam?" is now resolved against the previous turns, bounded to the
last `HISTORY_TURNS` exchanges (default 3), carrying the earlier question and SQL and never the
answer text or the returned rows. The chat page prints the resolved form above the answer as
"Interpreted as: ...", so a misreading is visible to the only person who can correct it. Five
conversational cases sit in the gold set and are scored on every run.

What did not change is the reason the deferral was defensible. The coreference problem is real,
and ADR-011's answer to it is to bound the window and to keep everything downstream of the
rewrite single-turn, rather than to give the graph a memory. The core is still stateless: the
caller owns the conversation and the agent holds nothing between calls.

Why it was built at all, given this ADR argued for leaving it out: the consequence clause above
predicted that a live demo would expose the omission, and the remaining deliverable is a demo
video. ADR-011 quotes that prediction as its starting point. A stated risk that then materialises
in the one scenario it was stated about is a reason to act, not a reason to cite the original
decision.

## Addendum, 2026-08-12: saved chats, and one ordering defect the thin-UI rule was hiding

The element table above gains a row: a sidebar list of saved chats, with New chat, reopen,
rename and delete. The store behind it, and the reasoning for putting it in its own
database, are [ADR-014](ADR-014-conversation-store.md); this addendum records only what
reached the interface and what it cost.

**Why this is not a breach of the scope boundary this ADR exists to defend.** The requirements
ask for a chat, and a chat that forgets everything when the tab is closed demonstrates a
chat rather than being one. The same argument [ADR-011](ADR-011-bounded-multi-turn.md) made
about multi-turn applies here, and the work is wiring a store that was already built and
already justified, not a new capability. The boundary that still holds is the one in the
alternatives above: this is a sidebar list in Streamlit, not a React front end.

**The defect worth recording.** Before this change `app.py` rendered an answer and appended
it to session history on the line after. Streamlit reruns the script on any widget
interaction, so a click landing while the answer was still rendering would preempt the
rerun before the append executed, and the turn would be gone from a pane the user had
already paid a model call for. Nothing about reading the file made that visible, and the
ordering was correct in every reading that did not model the rerun.

So the sequence moved into `src/conversations.py`, alongside `src/notices.py` and for the
same reason. The view makes one call, `session.answer(question, ask)`, which asks,
persists, and then returns the turn to be rendered. There is no second call for the view to
put in the wrong order. `tests/test_conversations.py` asserts that the turn is durable at
the moment it is returned, which is the last instant before the caller could render it.

**A save failure does not cost an answer.** A store failure becomes a flag on the turn and
a caption saying it was not saved, because the answer is real and the failure is
bookkeeping. The same reasoning makes the store optional: `db/03_app_store.sql` runs only
on a container's first boot, so a reviewer with an older data volume has no store database,
and that degrades to a caption rather than a page that will not load.

Two corrections to that rule came out of the coherence review, and both were real. The
first is that reopening, renaming and deleting did not degrade at all: they called the
store directly, so a connection lost between drawing the sidebar and clicking it would
reach the page as a traceback. They now report the failure beside the list and leave the
open conversation exactly as it was, since losing your place because a different
conversation could not be loaded is a worse outcome than the click doing nothing.

The second is narrower and easier to miss. `Store._run` converts a dropped connection into
`StoreError` but re-raises every other database error as itself, and says so: callers
needing the friendlier type wrap it at their own boundary. Catching only `StoreError`
therefore left a privilege error or a constraint violation propagating through a handler
written to stop exactly that. `src/conversations.py` is that boundary and now catches both.
The distinction is invisible when the store is simply absent, which is the failure that
gets tested by hand, and it is the reason the audit was worth running.

**`app.py` acquired its first test.** This file had none, which is part of why the ordering
defect survived: no test executed the page, so the only way to reach it was to click at the
wrong moment. `tests/test_app_smoke.py` runs the page under Streamlit's `AppTest` harness
against a throwaway store and covers what only breaks when the page executes, including
that a reopened chat comes back with its table and chart rather than as text. It submits no
question, so it calls no model and spends nothing.

## Addendum, 2026-08-12: the telemetry caption becomes a page

The element table above listed a caption reading `6.24s · 3 LLM calls · 6 rows · $0.0093`
under every answer. It is gone, and the same numbers are now aggregated on a second page.
The table has been corrected rather than annotated, because a list of elements that
describes an element the app no longer renders is worse than no list.

**Why a caption was the wrong home for these numbers.** What a caption beside one answer
can report is what that one turn cost, which is the least useful form of the figure: a
single reading has no baseline, so a reader cannot tell whether six seconds is fast. The
same measurements over the store become a median, a p95, a cost per question and a
per-stage mean, which is what the numbers were being collected for. Nothing new is
measured; the same `AgentResult` fields are read from a different place.

The split is by audience, which is the same test this ADR used to justify the sidebar
ordering. Someone asking a question wants the answer and the SQL that produced it.
Someone deciding whether to trust the system, or what to make faster, wants the
distribution. Those are different readers, so they are different pages rather than one
scroll. What stays beside an answer is still owned by `src/notices.py`, and it is exactly
the set that qualifies that answer rather than describing the system.

**Two pages, both as files.** `st.Page` accepts a callable or a path, and these are paths
for a testability reason rather than a stylistic one: `AppTest.switch_page` only reaches
file-based pages, so a callable page cannot be driven by a test at all. That decided the
layout. `app.py` is now the entrypoint and holds no rendering, `views/chat.py` and
`views/observability.py` are the pages, and `views/state.py` holds what they share,
because the entrypoint calls `st.navigation(...).run()` at import and so cannot be
imported by a page.

**What is aggregated in SQL, and why.** `Store.telemetry()` computes the totals,
percentiles, outcome counts, retry counts and stage means in one transaction over the
`jsonb` record. That is the reason ADR-014 stored the turn as `jsonb` rather than as text:
averaging one number should not require loading every turn, its answer and its result rows
into the application. The eval half is read from the committed artefacts in
`eval/results/` instead, because those are the evidence behind the README's figures and
are under version control; copying them into the store would create a second copy that can
disagree with the file.

The two halves are never added together. The live half is whatever a user happened to ask
and the eval half is a fixed 108-case benchmark, so a combined average would describe
neither.

**One number was nearly published wrong, and the shape of the mistake is worth keeping.**
The first version divided groundedness by every case in a run, which reports run 25 as
68.5%. The figure the harness prints, the README quotes and ADR-012 rests on is 97.4%. Both
are arithmetically correct; they differ in denominator. `eval/run_eval.py` scores
groundedness only on an ANSWERED outcome and records `null` otherwise, then divides by the
records carrying a real value, because a refusal has no figures to ground and counting it
as ungrounded would mark the system down for correctly declining. A panel whose headline
contradicts the README by 29 points is worse than no panel, and nothing about the code
looked wrong: it took reading the harness to see that the two were measuring different
populations. The column is now named "Overall" rather than "Accuracy" for the same reason,
since execution accuracy is a third number scored on the answerable subset alone.

**One measured caveat about the live half.** Its refresh is a `st.fragment(run_every=5)`,
and three facts were read from the installed Streamlit 1.61.1 rather than assumed:
`run_every` sends an `AutoRerun` interval to the browser, so the browser drives the timer;
a queued rerun is only serviced at a yield point and Streamlit's own comment says `st.*`
calls are those yield points, so a six-second `ask()` that touches no Streamlit API cannot
be interrupted to service one; and each websocket connection gets its own session, so a
second tab is a second session. Navigation makes the first two mostly moot, since a tab
showing the panel is not running `ask()` at all. The third is what makes the page's own
advice work: keep it open in a second tab and it keeps refreshing while the first waits.

This also retires the "Tracing persistence" row from `docs/ARCHITECTURE.md`'s omissions
table. Cost and latency were measured per request and discarded when the answer rendered;
they are now stored and aggregated, so the omission had become false.

## Addendum, 2026-08-12: the caption comes back, collapsed

The previous addendum removed the per-answer telemetry caption and gave the reason: what a
caption beside one answer can report is what that one turn cost, and a single reading has
no baseline, so a reader cannot tell whether six seconds is fast. It is back. The argument
was not wrong; it has been answered. **The Observability page is the baseline.** Once a
median, a p95 and a per-stage mean exist one click away, a per-turn figure has something to
be read against, and "6.94s" stops being a number with no scale.

What is still refused is the earlier *shape*. It was an always-visible caption under every
answer, competing for attention with the answer itself. It is now a collapsed expander
labelled `Answered in 6.94s`, sitting immediately after `View SQL`, and the two are placed
together because they are the same kind of object: a reader who wants to inspect the
machinery opens them, and a reader who wants the answer never sees either. The label states
the outcome as well as the duration, so it agrees with the badge above the answer rather
than duplicating only half of it.

The wording, the verb per outcome and the arithmetic are decided in `src/notices.py`
alongside `answer_notices`, not in the view. That is the same rule the previous addendum
applied and the reason this file can claim the UI layer is thin: the page opens an expander
around a value it did not compute.

**A rounding bug found by building it.** The cost was formatted by the function the panel
used, which switches to two decimals above a cent. An answered question costs about
`$0.0142`, so the figure a reviewer is most likely to check rendered as `$0.01`, which is
the same string a question costing half as much would produce. The magnitude rule was a
proxy for a distinction it could not actually express, so there are now two functions: a
per-unit cost keeps four decimals at every size, and an aggregate rounds to money. The
smoke test asserting the cost was *absent* from the chat pane had kept passing after the
caption returned, because the string it was looking for no longer existed.

## Addendum, 2026-08-12: two more things the sidebar owes a reviewer

**The page switcher moved to the top bar.** Streamlit renders it in the sidebar by default,
where it sat above the chat's own header and made the first thing in the sidebar a control
belonging to the application rather than to the page. `st.navigation(position="top")`
separates them: the top bar is where you are, the sidebar is what this page gives you. The
supported values on the pinned 1.61.1 are `sidebar`, `hidden` and `top`, so there is no
option to put it at the foot of the sidebar without hiding it and hand-rolling
`st.page_link` on every page, which is navigation in two places and one forgotten link away
from an unreachable page.

**The column count became the control that lists the columns.** The sidebar named each
table in the reader's language and stated how many columns it had, which is the useful half
for a non-technical user and not enough for a reviewer judging schema handling. The count is
now the label of an expander that opens onto the column names, their types, and their
`COMMENT ON COLUMN` text. The comments are the reason it is worth opening: `berth_wait_hours`
being *hours* is not recoverable from `numeric`, and that same text is what
[ADR-003](ADR-003-schema-introspection.md) injects into the SQL prompt, so the sidebar and
the model read one source and cannot drift.

Collapsed, for the reason the whole element table above is collapsed by default: a column
listing is the artefact a non-technical user came here to avoid. The count is the label
rather than a separate line so the two cannot disagree, and `column_count` is derived from
the column list rather than stored beside it so that stays true in code as well as on
screen.
