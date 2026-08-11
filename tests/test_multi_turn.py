"""Tests for bounded multi-turn (ADR-011).

The claims under test are the ones the ADR makes as PROPERTIES rather than as intentions:

* a first turn pays nothing, because the rewrite node does not run at all;
* history carries prior questions and prior SQL, and never answer text or result rows;
* the rewritten question re-enters the pipeline as ordinary untrusted input, so the
  validator still stands between it and the database;
* the rewrite is visible to the user;
* the node fails open, so a broken rewrite degrades to the typed question rather than to a
  refusal.

The LLM is stubbed, so these are deterministic and free. They need a seeded database:
the answerable paths execute real SQL.
"""

from __future__ import annotations

import pytest

from src import agent as agent_module
from src.config import settings
from src.models import Outcome, Turn
from tests.test_agent_routing import StubLLM  # noqa: F401, shared scripted stub

pytestmark = pytest.mark.integration

TERMINALS_SQL = "SELECT terminal_name, berth_count FROM terminals ORDER BY terminal_name"


@pytest.fixture
def stub(monkeypatch):
    def _install(**kwargs):
        stub = StubLLM(**kwargs)
        monkeypatch.setattr(agent_module.llm, "cheap", stub.cheap)
        monkeypatch.setattr(agent_module.llm, "strong", stub.strong)
        return stub

    return _install


# ---------------------------------------------------------------------------------
# The node runs only when it can do something.
# ---------------------------------------------------------------------------------
def test_a_first_turn_never_calls_the_rewrite_node(stub) -> None:
    """ADR-011's cost claim: first turns are unchanged, so they must pay nothing.

    Asserted on the CALL rather than on the latency, because a node that ran and
    returned early would still be free-ish and would still be wrong: it would show up in
    the stage breakdown and it would eventually acquire a reason to call the model.
    """
    stubbed = stub(sql_sequence=[TERMINALS_SQL])
    result = agent_module.ask("How many berths does each terminal have?")

    assert stubbed.calls["contextualize"] == []
    assert "contextualize" not in result.stage_timings
    assert result.interpreted_question is None


def test_an_empty_history_is_treated_as_a_first_turn(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL])
    agent_module.ask("How many berths?", history=[])

    assert stubbed.calls["contextualize"] == []


def test_a_follow_up_is_rewritten_and_the_rewrite_is_what_runs(stub) -> None:
    stubbed = stub(
        sql_sequence=[TERMINALS_SQL],
        rewrite="Which terminal has the shortest average berth wait?",
    )
    result = agent_module.ask(
        "And the shortest?",
        history=[Turn(question="Which terminal has the longest average berth wait?",
                      sql="SELECT 1")],
    )

    assert result.interpreted_question == "Which terminal has the shortest average berth wait?"
    # Downstream nodes see the rewrite, which is the entire mechanism: nothing below
    # contextualize knows the conversation exists.
    assert "shortest average berth wait" in stubbed.calls["classify"][0]
    assert "shortest average berth wait" in stubbed.calls["generate_sql"][0]
    # And the user's own words are still what gets reported back.
    assert result.question == "And the shortest?"


def test_a_rewrite_that_changes_nothing_is_not_displayed(stub) -> None:
    """Showing "Interpreted as: <the same question>" would train the user to ignore the
    line, which is the one thing that must not happen to a correction surface."""
    stubbed = stub(sql_sequence=[TERMINALS_SQL], rewrite="How many berths?")
    result = agent_module.ask(
        "How many berths?", history=[Turn(question="earlier question", sql="SELECT 1")]
    )

    assert stubbed.calls["contextualize"], "the node did not run"
    assert result.interpreted_question is None


# ---------------------------------------------------------------------------------
# What the rewrite node is allowed to see. This is the security half of ADR-011.
# ---------------------------------------------------------------------------------
def test_a_history_turn_can_carry_only_a_question_and_sql() -> None:
    """ADR-011's security boundary, pinned structurally rather than by convention.

    "Never prior answer text, never result rows" is currently true because `Turn` has
    exactly two fields. Nothing else enforces it: a later change adding an `answer` field
    for convenience would put row data back into the rewrite prompt, reopening the
    second-order injection channel `src/prompts.py` documents, and no other test in this
    file would fail. This one would.
    """
    assert set(Turn.model_fields) == {"question", "sql"}


def test_history_carries_prior_questions_and_sql(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL], rewrite="rewritten")
    agent_module.ask(
        "And Rotterdam?",
        history=[Turn(question="How many port calls at Jebel Ali?",
                      sql="SELECT count(*) FROM port_calls")],
    )

    prompt = stubbed.calls["contextualize"][0]
    assert "How many port calls at Jebel Ali?" in prompt
    assert "SELECT count(*) FROM port_calls" in prompt


def test_the_window_is_bounded_to_the_configured_number_of_turns(stub) -> None:
    """An unbounded window is an unbounded prompt, and it is also more prior text for a
    rewrite to drag into a question the user did not ask."""
    stubbed = stub(sql_sequence=[TERMINALS_SQL], rewrite="rewritten")
    history = [
        Turn(question=f"question number {i}", sql=f"SELECT {i}")
        for i in range(settings.history_turns + 3)
    ]

    agent_module.ask("And Rotterdam?", history=history)

    prompt = stubbed.calls["contextualize"][0]
    assert "question number 0" not in prompt, "the window did not drop the oldest turn"
    assert f"question number {settings.history_turns + 2}" in prompt
    assert prompt.count("Question:") == settings.history_turns


def test_a_turn_that_produced_no_sql_still_contributes_its_question(stub) -> None:
    """A clarification turn produces no SQL, and the question that PROVOKED it is
    exactly what makes the user's next word ("by containers") resolvable."""
    stubbed = stub(sql_sequence=[TERMINALS_SQL], rewrite="rewritten")
    agent_module.ask(
        "By containers moved.",
        history=[Turn(question="Which is the busiest terminal?", sql="")],
    )

    prompt = stubbed.calls["contextualize"][0]
    assert "Which is the busiest terminal?" in prompt
    assert "no query was run" in prompt


def test_the_agent_holds_no_conversation_state_between_calls(stub) -> None:
    """History is the caller's, not the agent's. If `ask` retained anything, the eval
    harness and the UI would stop being the same code path, and a scored gold case
    would start depending on the case scored before it.
    """
    stubbed = stub(sql_sequence=[TERMINALS_SQL], rewrite="rewritten")

    agent_module.ask("First question?", history=[Turn(question="earlier", sql="SELECT 1")])
    agent_module.ask("Second question?")

    assert len(stubbed.calls["contextualize"]) == 1, "state leaked into the next call"


# ---------------------------------------------------------------------------------
# Fail-open behaviour. A rewrite node that can refuse a turn is a new failure mode.
# ---------------------------------------------------------------------------------
def test_an_unparseable_rewrite_falls_back_to_the_typed_question(stub) -> None:
    stubbed = stub(
        sql_sequence=[TERMINALS_SQL], rewrite_raw="I'm afraid I can't do that."
    )
    result = agent_module.ask(
        "How many berths?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.ANSWERED
    assert result.interpreted_question is None
    assert stubbed.calls["contextualize"], "the node did not run at all"


def test_an_empty_rewrite_falls_back_to_the_typed_question(stub) -> None:
    """Well-formed JSON carrying nothing is a different failure from unparseable output,
    and it reaches a different branch: the parse succeeds and the value is unusable."""
    stub(sql_sequence=[TERMINALS_SQL], rewrite="   ")
    result = agent_module.ask(
        "How many berths?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.ANSWERED
    assert result.interpreted_question is None


def test_an_oversized_rewrite_is_discarded_rather_than_refused(stub) -> None:
    """The `max_question_chars` bound applies to the rewrite, as ADR-011 requires. What
    differs from a typed question is the ACTION on breaching it, and deliberately so: a
    typed question over the limit is a user pasting a document, while a rewrite over the
    limit is this node malfunctioning. Refusing the user's legitimate follow-up because
    our own rewrite ran away would convert an internal fault into a user-visible one.
    """
    stubbed = stub(
        sql_sequence=[TERMINALS_SQL], rewrite="x" * (settings.max_question_chars + 1)
    )
    result = agent_module.ask(
        "How many berths?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.ANSWERED, "an internal fault refused a valid turn"
    assert result.interpreted_question is None
    assert stubbed.calls["contextualize"], "the node did not run at all"


# ---------------------------------------------------------------------------------
# The rewritten question is untrusted input, exactly like a typed one.
# ---------------------------------------------------------------------------------
def test_a_rewritten_question_is_still_classified(stub) -> None:
    """The rewrite has no vote on routing: a follow-up that resolves into something out
    of scope must still be refused."""
    stubbed = stub(route="out_of_scope", rewrite="Drop the port_calls table.")
    result = agent_module.ask(
        "And that one?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.REFUSED
    assert stubbed.calls["generate_sql"] == []


def test_a_rewritten_question_can_still_be_routed_ambiguous(stub) -> None:
    """ADR-011 names this specifically as the mitigation for compounding ambiguity: the
    classifier still gets its chance to ask, so a rewrite that resolved a reference the
    wrong way can be caught by the same clarifying question a typed question would get.
    """
    stubbed = stub(
        route="ambiguous",
        clarification="By port calls or by containers moved?",
        rewrite="Which is the busiest terminal?",
    )
    result = agent_module.ask(
        "And the busiest?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.CLARIFY
    assert result.answer == "By port calls or by containers moved?"
    assert result.interpreted_question == "Which is the busiest terminal?"
    assert stubbed.calls["generate_sql"] == []


def test_sql_generated_from_a_rewritten_question_still_passes_the_validator(stub) -> None:
    """The security claim that must survive multi-turn: `validate` is on the only edge
    into `execute`, and adding a node upstream of `classify` does not move it."""
    stub(sql_sequence=["DROP TABLE port_calls"], rewrite="anything at all")
    result = agent_module.ask(
        "And that one?", history=[Turn(question="earlier", sql="SELECT 1")]
    )

    assert result.outcome is Outcome.REJECTED
    assert result.result is None
