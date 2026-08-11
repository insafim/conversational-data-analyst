"""Thin two-tier wrapper over LiteLLM (ADR-007).

Provider choice lives in the model string (`anthropic/...`, `openai/...`, `gemini/...`),
so switching provider is an environment change rather than a code change. There is no
provider abstraction beyond this file, deliberately: LiteLLM already is that abstraction,
and wrapping it again would be building a framework nobody asked for.

Two tiers, because cost per task is a design input rather than an afterthought:

* ``cheap``  — classification and summarisation. High call volume, low difficulty.
* ``strong`` — SQL generation. The one step where capability actually changes the answer.

Roughly two thirds of the calls per question go to the cheap tier.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm import completion

from .config import settings

# LiteLLM logs provider chatter at INFO on import; it drowns out the app's own output.
litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a given provider does not support


class LLMError(RuntimeError):
    """A provider call failed after retries, or returned something unusable.

    `safe_detail` is the part of the failure a USER may be shown. It is opt-in: a message
    only becomes user-visible if the code raising it decided the text is both useful and
    free of provider internals. Everything else reaches the log through `str(self)` and
    stops there.

    The split exists because the two obvious policies are both wrong here. Showing every
    provider message puts request and response internals on a screen a client analyst is
    looking at. Showing none of them turns "your API key is the placeholder" into "an
    error occurred", which is the single most common first-run failure and the one the
    README's troubleshooting table sends people to. So the auth message is curated and
    shown; a malformed request or an unrecognised fault is summarised and logged.
    """

    def __init__(self, message: str, safe_detail: str | None = None) -> None:
        super().__init__(message)
        self.safe_detail = safe_detail


@dataclass
class Usage:
    """Running totals for one question, surfaced in the UI and the eval harness.

    Locked, because the semantic verifier (ADR-012) runs on a worker thread while
    `summarize` is calling the same tier on the main one. `calls += 1` is a
    read-modify-write, so two concurrent completions can both read the same value and
    the count silently loses one, an undercount in exactly the number the cost figures
    are built from.
    """

    calls: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, model: str, response: Any) -> None:
        try:
            cost = float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:  # noqa: BLE001 — an unpriced model must not break the request
            cost = 0.0
        with self._lock:
            self.calls += 1
            self.by_model[model] = self.by_model.get(model, 0) + 1
            self.cost_usd += cost


def _call(model: str, system: str, user: str, usage: Usage | None, temperature: float) -> str:
    """One completion call, with a bounded manual retry.

    Retries are implemented here rather than via LiteLLM's `num_retries` because that is
    not a parameter of `completion()` in the installed version — it is accepted through
    **kwargs and silently ignored if misspelled, which is exactly the kind of guarantee
    that should not be assumed. Only transient failures are retried; a bad request or an
    auth failure is permanent and retrying it just doubles the latency of the error.
    """
    last: Exception | None = None
    for _attempt in range(2):
        try:
            response = completion(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                timeout=settings.llm_timeout_s,
                max_tokens=1500,
            )
            if usage is not None:
                usage.record(model, response)
            content = response.choices[0].message.content
            if not content or not content.strip():
                raise LLMError(f"{model} returned an empty response.")
            return content.strip()

        except (litellm.RateLimitError, litellm.Timeout, litellm.APIConnectionError,
                litellm.InternalServerError) as exc:
            last = exc
            continue  # transient — one more attempt
        except litellm.AuthenticationError as exc:
            # Curated and therefore safe to show: it names the model and the action, and
            # carries no provider payload. This is the first-run failure, so hiding it
            # would cost more than it protects.
            message = (
                f"Authentication failed for {model}. Check the API key for this provider."
            )
            raise LLMError(message, safe_detail=message) from exc
        except litellm.BadRequestError as exc:
            # `exc` can quote the request, which means the schema, the prompt, and on a
            # summarise call the returned ROWS. It goes to the log, never to the screen.
            raise LLMError(f"{model} rejected the request: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Unexpected error calling {model}: {exc}") from exc

    raise LLMError(f"{model} failed after retries: {last}")


def cheap(system: str, user: str, usage: Usage | None = None, temperature: float = 0.0) -> str:
    """Fast, inexpensive tier: classification and summarisation."""
    return _call(settings.cheap_model, system, user, usage, temperature)


def strong(system: str, user: str, usage: Usage | None = None, temperature: float = 0.0) -> str:
    """Capable tier: SQL generation."""
    return _call(settings.strong_model, system, user, usage, temperature)


# --- output extraction -------------------------------------------------------------
# Model output is parsed by code rather than trusted to be well-formed. Both helpers
# below are total: they either return a usable value or raise, and never return
# something half-parsed that a caller might mistake for success.

_FENCE = re.compile(r"```(?:sql|json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Pull SQL out of a model response.

    Models wrap SQL in fences most of the time and prose around it some of the time.
    Extraction is code, not trust — and whatever comes out still has to pass the
    validator before it reaches the database, so a bad extraction fails safe.
    """
    match = _FENCE.search(text)
    candidate = match.group(1) if match else text
    return candidate.strip().rstrip(";").strip()


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Tries a fenced block first, then the outermost brace pair, then the raw string.
    Raises LLMError rather than returning a partial dict, so a caller cannot silently
    proceed on a field that was never actually parsed.
    """
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise LLMError(f"Expected a JSON object, got: {text[:200]}")


def _json_candidates(text: str):
    match = _FENCE.search(text)
    if match:
        yield match.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        yield text[start : end + 1]
    yield text.strip()
