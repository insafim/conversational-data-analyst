# ADR-011: Bounded Multi-Turn, Single-Turn Core

- **Status:** Accepted
- **Date:** 2026-08-10
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-008](ADR-008-ui-and-scope-boundary.md), [ADR-010](ADR-010-syllabus-mapped-eval-expansion.md), [ADR-012](ADR-012-runtime-verification.md)

## Context

[ADR-008](ADR-008-ui-and-scope-boundary.md) deferred multi-turn memory and, in its
consequences section, named its own exposure: single-turn is the omission a live demo is
most likely to reveal, because a reviewer's natural second question is a follow-up. That
deferral was scope control against a few-hours budget. Both premises have since changed: the remaining
deliverable is a demo video, which is the predicted exposure scenario, and the build
window was deliberately extended (direction of 2026-08-10) with the instruction that
logically explained design evolution is wanted, not scope freeze.

The external reference taxonomy adopted for the eval expansion
([ADR-010](ADR-010-syllabus-mapped-eval-expansion.md)) has a Follow-Up and Contextual
category that was excluded solely because the graph was single-turn. Adopting multi-turn
returns it to scope.

## Decision

**One rewrite node at the edge of the graph. Everything downstream of it stays
byte-identical to the single-turn pipeline.**

- A `contextualize` node runs first, on the cheap tier
  ([ADR-007](ADR-007-llm-provider-and-tiering.md)). Input: the new question plus a
  bounded window of prior turns. Output: one standalone question. When the history is
  empty, the node is skipped entirely; first turns pay nothing.
- **History carries prior (question, SQL) pairs only. Never prior answer text, never
  result rows.** Answer text quotes row data, which is the second-order injection
  channel `src/prompts.py:16-21` documents; result rows are the same risk plus token
  cost. Prior questions are user-authored, which is already the threat model, and prior
  SQL passed the validator when it was produced. "And Rotterdam?" resolves from the
  prior question; it never needs the prior numbers.
- **The rewritten question is displayed to the user** as an "Interpreted as:" line. This
  extends the pattern ADR-008 calls the load-bearing UI decision, the visible SQL
  expander, to interpretation itself, and it is the honest mitigation for the
  compounding-ambiguity concern in ADR-008: the resolution is visible, and the
  classifier still gets its chance to route the rewritten question as ambiguous.
- The standalone question then enters the unchanged pipeline as fully untrusted input:
  classify, generate, validate, execute. `validate` remains the only edge into `execute`
  (`src/agent.py`, graph wiring). The rewrite node has no path to the database and no
  vote on safety.
- The window is the last 3 exchanges. The `max_question_chars` bound applies to the
  rewritten output exactly as it applies to a typed question.

## What this is not

- **Not LangGraph checkpointing.** The UI session already holds the history for display;
  the graph input gains a `history` field and the Streamlit layer passes it. ADR-002's
  honest assessment named checkpointing as a reason the framework was chosen; that
  remains the mechanism for cross-session persistence if ever required, and nothing
  here consumes it.
- **Not long-term memory.** The taxonomy, recorded so the terms stay precise: short-term
  memory is the conversation window above, and it is the entire feature. The long-term
  memory an analytics system actually needs is the semantic layer, governed metric
  definitions, which ADR-008 already names the most consequential omission; it stays on
  the production path. Per-user preference memory requires identity, and authentication
  is scoped out as the precondition for row-level security. Result-row memory, carrying
  data across turns, is rejected outright: it breaks the grounding contract and widens
  the injection surface.

## Cost

Projected from the run14 stage timings, where the cheap-tier `classify` node averages
2.0 s across the 36 records: a follow-up turn adds roughly 2 s and one cheap call, about
$0.002 against a measured mean of $0.0098 to $0.0100 per question (runs 12 to 14). First turns are
unchanged. The call ceiling becomes 4, or 5 with the single retry, on follow-up turns
only. Measured figures replace these projections in the README once the conversational
tranche runs.

## Evaluation

The Follow-Up and Contextual behaviour category enters the gold set as a fourth tranche
of scripted two-turn cases, scored on the final turn's result set, including at least
one follow-up whose correct outcome is refusal ("why did it drop?" asks for causation
the data does not contain). The harness gains a turns-list case shape. Sequencing is
deliberate: the single-turn baseline on the expanded set (runs 15 onward) is measured
first, so the effect of this change is attributable rather than confounded with the
eval expansion.

---

## Addendum, 2026-08-11: implemented, and the projections replaced with measurements

Built and merged. The `contextualize` node hangs off a conditional edge from START, so a
first turn does not execute it at all rather than executing it and returning early.

**Measured cost of the rewrite.** Across the 30 conversational records in runs 20 to 25,
the `contextualize` stage takes a median of 1.14 s (mean 1.32 s, range 0.92 s to 3.42 s)
and one cheap-tier call. The projection above was "roughly 2 s and one cheap call", so
the call count was right and the latency was pessimistic by about a second. The dollar
increment is *not* isolable from these artifacts: a conversational case records the cost
of both its turns together (median $0.0256 against a median $0.0129 for a single-turn
answerable case), and separating the rewrite from the second turn's own three calls would
need an instrument the harness does not have. Stated rather than estimated.

**One deviation, recorded.** This ADR says the `max_question_chars` bound applies to the
rewritten output "exactly as it applies to a typed question". The bound is enforced, but
the action on breaching it differs: an over-long rewrite is discarded and the typed
question runs, rather than the turn being refused. A typed question over the limit is a
user pasting a document; an over-long rewrite is this node malfunctioning, and refusing a
legitimate follow-up on that basis would turn an internal fault into a user-visible
failure. Pinned by
`tests/test_multi_turn.py::test_an_oversized_rewrite_is_discarded_rather_than_refused`.

**What the evaluation showed.** All five conversational cases pass. The one worth naming
is q77: "How many containers has it moved in total?" resolves a pronoun pointing at a
value the *previous turn returned*, and the rewrite node never sees result rows. It
resolved it by description instead, producing "How many containers has the vessel that
made the most port calls moved in total?", which the next query re-derives. That is the
history-carries-questions-not-answers rule working rather than being worked around.

## Addendum, 2026-08-12: what "three turns" bounds, and what it does not

`HISTORY_TURNS=3` reads as though it caps the conversation. It does not, and the
distinction matters enough to state plainly.

**The conversation is unlimited.** A user may ask fifty questions; every turn is displayed,
and with [ADR-014](ADR-014-conversation-store.md) every turn is persisted and can be
reopened later.

**The rewrite window is three.** `contextualize` reads `history[-settings.history_turns:]`
(`src/agent.py`). So three bounds how far back a pronoun may reach, not how long the
conversation may be.

Three properties follow, and together they are the reason this is a bounded feature rather
than a memory system:

1. **One node sees history at all.** `contextualize` sits at the edge of the graph.
   Everything after it — classify, generate_sql, validate, execute, summarize — is
   byte-identical to the single-turn pipeline and cannot tell that a conversation exists.
2. **Only questions and SQL cross that boundary.** Never answer text, never result rows.
   Answer text quotes row data, which is the second-order injection channel.
3. **The resolution is shown, not hidden.** The rewritten question is printed above the
   answer as "Interpreted as: ...", so a misreading is visible to the only person who can
   correct it.

**On whether multi-turn was in scope at all.** The requirements say a simple chat
interface is fine. They do not say single-turn and they do not say multi-turn: they leave
the interface open and ask for a chat. A chat that cannot take a follow-up is a question
box, so implementing the interface properly is not scope creep. What would have been scope
creep is an unbounded memory feature, which is precisely what the window, the
questions-and-SQL rule and the visible rewrite exist to prevent.
