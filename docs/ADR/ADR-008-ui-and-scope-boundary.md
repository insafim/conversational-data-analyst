# ADR-008 — A Thin Streamlit UI, and the Scope Boundary

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-005](ADR-005-deterministic-chart-selection.md)

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

**Multi-turn memory.** Every question is answered independently; "and what about last year?" is not
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
- Single-turn only, which is the omission a live demo is most likely to expose — a reviewer's
  natural second question is usually a follow-up. Better to state the boundary in advance than to
  demonstrate it accidentally.
- No accessibility, mobile, or internationalisation consideration whatsoever.
