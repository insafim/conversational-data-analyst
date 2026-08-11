# ADR-008 — A Thin Streamlit UI, and the Scope Boundary

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-005](ADR-005-deterministic-chart-selection.md), [ADR-011](ADR-011-bounded-multi-turn.md), [ADR-013](ADR-013-the-reading-without-the-verdict.md), [ADR-014](ADR-014-conversation-store.md)

## Context

The brief asks for a conversational interface and allows a few hours. Interface work expands to fill
whatever time it is given, and none of that expansion is scored here. The interesting question is
therefore not which UI framework, but **how little UI can be built while still demonstrating every
assessed behaviour** — and, having drawn that line, whether the things left outside it were left out
on purpose or by accident.

Scope control is itself a deliverable. A prototype that quietly omits things reads as incomplete; a
prototype that names its omissions and justifies each one reads as judgement.

## Decision

**Streamlit, deliberately thin, plus an explicit and defended out-of-scope list.**

The UI's entire job is to make four things visible, because each maps to an assessed line:

| Element | What it demonstrates |
| --- | --- |
| Chat input and message history | The conversational requirement |
| Natural-language answer | Groundedness — phrased only from returned rows |
| Chart, rendered from a typed `ChartSpec` | Chart-type selection ([ADR-005](ADR-005-deterministic-chart-selection.md)) |
| Collapsed "View SQL" expander + `6.24s · 3 LLM calls · 6 rows · $0.0093` caption | Auditability, latency and cost visibility |

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
demonstrates infrastructure competence that this brief does not assess.

**Semantic layer.** No governed metric definitions. This is the most consequential omission and the
one to raise before a reviewer does: without agreed definitions, "utilisation" resolves to whatever
the model infers that day, and the same question yields three different SQLs and three different
numbers across three sessions. At prototype scale the column comments carried by schema
introspection ([ADR-003](ADR-003-schema-introspection.md)) are a partial substitute. At client
scale they are not, and this — not model capability — is the usual reason agent-analytics
deployments stall.

## Alternatives considered

**React front end with a FastAPI backend.** The right architecture for a product and the wrong one
for a take-home. It would consume a large share of the time budget to demonstrate a skill the brief
does not assess, at the direct expense of the eval harness — which is the component that actually
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
  claimed.
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
answer text or the returned rows. `app.py` prints the resolved form above the answer as
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

**Why this is not a breach of the scope boundary this ADR exists to defend.** The brief
asks for a chat, and a chat that forgets everything when the tab is closed demonstrates a
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
same reason. `app.py` now makes one call, `session.answer(question, ask)`, which asks,
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
