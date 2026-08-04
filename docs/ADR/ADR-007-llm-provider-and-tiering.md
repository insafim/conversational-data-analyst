# ADR-007 — Provider-Agnostic LLM Access and Two-Tier Model Routing

- **Status:** Accepted
- **Date:** 2026-08-04
- **Decision owner:** Insaf Ismath
- **Related:** [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md), [ADR-004](ADR-004-defence-in-depth-sql.md)

## Context

The pipeline makes three LLM calls per question — `classify`, `generate_sql`, `summarize` — and a
fourth when the single retry fires ([ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md)). Two
decisions follow from that and are worth recording separately from the graph itself:

1. How the process talks to a model provider.
2. Whether all three calls should go to the same model.

The second question is where cost per task is actually decided. The brief's evaluation criteria name
cost, and cost in an agent system is not a bill you receive — it is a consequence of routing choices
made at design time.

## Decision

**One thin LiteLLM wrapper for provider access, and two model tiers selected per node by
environment variable.**

Two model strings live in `.env`:

| Variable | Used by | Why |
| --- | --- | --- |
| `MODEL_CHEAP` | `classify`, `summarize` | Both are short, bounded, low-difficulty language tasks |
| `MODEL_STRONG` | `generate_sql` | The one node where capability determines whether the answer is correct |

Default provider is the Anthropic API. The wrapper is a single module; there is no provider
abstraction layer beyond it, no strategy pattern, and no runtime provider negotiation.

### Why the tiers split where they do

The split follows the failure consequence of each node, not its token count.

`generate_sql` is the node where model capability converts directly into measured accuracy. A weaker
model writes SQL that parses, passes the validator, executes without error, and returns the wrong
numbers — the worst failure mode in the system, because it is silent. This is the node that earns a
strong model, and it is the one the eval harness scores most directly
([ADR-006](ADR-006-eval-execution-accuracy.md)).

`classify` chooses between three labels. `summarize` phrases rows that have already been fetched,
and is constrained to ground itself in those rows. Both are tasks where a cheap, fast model is not a
compromise — it is the correct tool, and it also lowers the latency floor that three sequential
calls impose.

The point worth making out loud: **cost per task is a design input here, not an afterthought.**
The routing is visible in configuration rather than buried, so the cost profile of the system can be
reasoned about before it is deployed rather than discovered on an invoice.

### Why a wrapper at all

Provider-agnostic access is one dependency and roughly one module, and it buys two things that
matter commercially. A client with an existing Azure OpenAI or Bedrock commitment is a
configuration change rather than a rewrite. And model substitution becomes a variable to sweep
against the eval harness rather than a refactor — which is precisely how the cheap/strong split
should be re-tuned as model prices and capabilities move.

## Alternatives considered

**A single model for all three calls.** Simpler, and defensible at this scale. Rejected because it
forces a bad choice in both directions: route everything to the strong model and pay strong-model
prices to pick one of three labels, or route everything to the cheap model and accept degraded SQL
on the one node where correctness is measured. The split costs one environment variable.

**Direct provider SDK, no wrapper.** One fewer dependency, and honest for a prototype. Rejected on
the portability argument above — the wrapper is small enough that its cost is near zero, and it
removes a rewrite from the path to a client deployment that specifies a different provider.

**A full provider-abstraction layer** with pluggable backends, retry policies, and fallback chains.
Rejected as over-engineering. Nobody asked for multi-provider failover, and building it would be
scope invented rather than scope delivered.

**Model routing decided at runtime by a router model.** Rejected: it adds an LLM call to save LLM
calls, and reintroduces model-controlled flow into a graph deliberately built to avoid it. The node
identity already tells us the difficulty; asking a model to rediscover that per request is strictly
worse.

## Consequences

**Positive**

- Cost per question is bounded and legible: three calls, two of them on the cheap tier.
- Changing provider or model is an environment change, and the eval harness can measure whether the
  change mattered.
- Two of three calls being fast reduces the latency floor imposed by sequential execution.

**Negative / accepted**

- Two models mean two behaviour profiles to reason about, and a prompt tuned against one tier may
  not transfer to the other. The eval suite is what makes this manageable rather than theoretical.
- A wrapper adds a dependency that a single-provider prototype does not strictly need. Accepted for
  the portability argument; the trade-off is real and is the same shape as the LangGraph one in
  [ADR-002](ADR-002-fixed-path-graph-over-agent-loop.md).
- Per-question token accounting *is* built: `llm.Usage` totals calls and cost via
  `litellm.completion_cost()`, surfaced in the UI caption and aggregated by the eval harness, so a
  model-routing change can be priced rather than guessed at. What is **not** built is persistence —
  spend is visible per request and per eval run, but nothing is stored, so there is no spend trend,
  no per-user attribution, and no budget alerting. Those need a metrics backend and are named on the
  path to production.
- Model identifiers move faster than code. The defaults were verified against LiteLLM's registry on
  2026-08-04; identifiers that were current in 2025 are already retired. This is why the model
  strings are configuration rather than constants, and why a stale default fails loudly with an
  authentication or bad-request error rather than silently degrading.
- No fallback if the configured provider is unavailable; the request fails. Correct for a prototype,
  unacceptable for production, and stated rather than hidden.
