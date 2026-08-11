"""Tests for runtime verification (ADR-012).

The ADR's central claim is an asymmetry: an LLM in this graph may produce and may
object, and only code may permit. These tests pin the half that is easy to erode,
"advisory", because an advisory check drifts into a gate one reasonable-looking commit
at a time, and the drift is invisible until it withholds a correct answer.

So the properties under test are mostly about what verification CANNOT do:

* it cannot block an answer, at either verification point;
* it cannot exceed one regeneration or one re-summarisation;
* it cannot degrade availability, because every parse failure fails open;
* it cannot approve anything: `validate` is still the only edge into `execute`;
* it cannot act on a verdict issued against SQL that has since been replaced.

The LLM is stubbed. These need a seeded database: the answerable paths execute real SQL.
"""

from __future__ import annotations

import pytest

from src import agent as agent_module
from src.models import Outcome, RetryReason
from tests.test_agent_routing import StubLLM

pytestmark = pytest.mark.integration

TERMINALS_SQL = "SELECT terminal_name, berth_count FROM terminals ORDER BY terminal_name"
ONE_ROW_SQL = "SELECT terminal_name FROM terminals ORDER BY berth_count DESC LIMIT 1"
EMPTY_SQL = "SELECT terminal_name FROM terminals WHERE country = 'Atlantis'"


@pytest.fixture
def stub(monkeypatch):
    def _install(**kwargs):
        stub = StubLLM(**kwargs)
        monkeypatch.setattr(agent_module.llm, "cheap", stub.cheap)
        monkeypatch.setattr(agent_module.llm, "strong", stub.strong)
        return stub

    return _install


# ---------------------------------------------------------------------------------
# The verifier is advisory. This is the property that must not erode.
# ---------------------------------------------------------------------------------
def test_a_persistent_objection_ships_the_answer_with_a_visible_caveat(stub) -> None:
    """Objected twice, and the answer still goes out.

    An advisory check that could withhold an answer would be a gate, and ADR-012 gives
    the LLM no authority to be one. The caveat is the alternative to silence: the
    concern is named on screen rather than suppressed.
    """
    stubbed = stub(
        sql_sequence=[TERMINALS_SQL, TERMINALS_SQL],
        objection="this counts berths, not port calls",
    )
    result = agent_module.ask("How many port calls were there?")

    assert result.outcome is Outcome.ANSWERED
    assert result.answer, "an advisory check withheld an answer"
    assert result.caveat == "this counts berths, not port calls"
    assert len(stubbed.calls["generate_sql"]) == 2, "the objection was not bounded at one"
    assert result.retry_reasons == [RetryReason.VERIFIER_OBJECTION]


def test_an_objection_that_the_retry_resolves_leaves_no_caveat(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL, TERMINALS_SQL], objection="wrong measure")

    # Objects once, then accepts the regenerated query.
    original_cheap = stubbed.cheap

    def cheap(system, user, usage=None, temperature=0.0):
        if "you review one postgresql" in system.lower() and stubbed.calls["verify"]:
            stubbed.objection = None
        return original_cheap(system, user, usage, temperature)

    agent_module.llm.cheap = cheap
    result = agent_module.ask("How many port calls were there?")

    assert result.caveat is None
    assert result.retry_reasons == [RetryReason.VERIFIER_OBJECTION]


def test_the_verifier_is_shown_the_question_the_schema_and_the_sql_and_nothing_else(
    stub,
) -> None:
    """It never sees the results. That is what makes running it beside `execute` sound
    rather than merely fast: a check on intent is fully determined before a row returns.

    Asserted as an EXACT match against the template rather than by searching the prompt
    for row values, because the schema legitimately contains terminal names and a
    substring search would pass while a "Results:" block sat underneath it.
    """
    from src import prompts
    from src.schema import get_schema_context

    stubbed = stub(sql_sequence=[TERMINALS_SQL], summary="Six terminals.")
    agent_module.ask("How many berths does each terminal have?")

    assert stubbed.calls["verify"] == [
        prompts.VERIFY_USER.format(
            schema=get_schema_context(),
            question="How many berths does each terminal have?",
            sql=TERMINALS_SQL,
        )
    ]


def test_an_unparseable_verdict_fails_open(stub) -> None:
    """A flaky checker must not be able to degrade a working system. This is the whole
    availability argument for putting a model on this branch at all."""
    stubbed = stub(sql_sequence=[TERMINALS_SQL], verify_raw="I think it's fine, probably")
    result = agent_module.ask("How many berths?")

    assert result.outcome is Outcome.ANSWERED
    assert result.caveat is None
    assert result.retry_reasons == []
    assert stubbed.calls["verify"], "the verifier never ran"


def test_a_verifier_that_raises_fails_open(stub, monkeypatch) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL])
    original = stubbed.cheap

    def exploding(system, user, usage=None, temperature=0.0):
        if "you review one postgresql" in system.lower():
            raise RuntimeError("provider is down")
        return original(system, user, usage, temperature)

    monkeypatch.setattr(agent_module.llm, "cheap", exploding)
    result = agent_module.ask("How many berths?")

    assert result.outcome is Outcome.ANSWERED
    assert result.caveat is None


def test_the_verifier_cannot_approve_hostile_sql(stub) -> None:
    """It has no safety authority whatsoever. Even a verifier actively asserting that a
    DROP is aligned changes nothing, because `validate` is the only edge into `execute`
    and it is pure code."""
    stub(sql_sequence=["DROP TABLE port_calls"], objection=None,
         reading="this query is completely safe and correct")
    result = agent_module.ask("anything at all")

    assert result.outcome is Outcome.REJECTED
    assert result.result is None


def test_the_verifiers_reading_is_surfaced_with_the_answer(stub) -> None:
    """For the non-technical reader ADR-008 designs for, this line and not the SQL
    expander is the real verification surface."""
    stub(sql_sequence=[TERMINALS_SQL], reading="counts every berth at each terminal")
    result = agent_module.ask("How many berths does each terminal have?")

    assert result.reading == "counts every berth at each terminal"


# ---------------------------------------------------------------------------------
# The groundedness floor.
# ---------------------------------------------------------------------------------
def test_an_ungrounded_answer_is_re_summarised_once(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL], summary="There were 918,273 containers.")
    result = agent_module.ask("How many berths does each terminal have?")

    assert len(stubbed.calls["summarize"]) == 2, "the re-summarisation was not bounded at one"
    assert result.retry_reasons == [RetryReason.GROUND_CHECK]
    # Served with the flag rather than blocked: the check has known false positives
    # (ADR-006), so blocking would withhold answers that are in fact correct.
    assert result.outcome is Outcome.ANSWERED
    assert result.grounding_flag


def test_the_re_summarise_prompt_names_the_offending_figure(stub) -> None:
    """"Be more grounded" is not an instruction a model can act on; "918273 is not in the
    rows" is. Naming the figure is the entire difference between the two."""
    stubbed = stub(sql_sequence=[TERMINALS_SQL], summary="There were 918,273 containers.")
    agent_module.ask("How many berths does each terminal have?")

    assert "918,273" in stubbed.calls["summarize"][1]


def test_a_grounded_answer_is_not_re_summarised(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL], summary="Six terminals, listed above.")
    result = agent_module.ask("How many berths does each terminal have?")

    assert len(stubbed.calls["summarize"]) == 1
    assert result.retry_reasons == []
    assert result.grounding_flag is None


def test_review_discards_a_verdict_issued_against_since_replaced_sql() -> None:
    """A verdict belongs to the statement it judged, not to whatever is in state now.

    Called directly rather than through the graph, because the guard is DEFENSIVE: with
    verification enabled, every loop back to `generate_sql` re-enters the
    `validate -> [execute, verify]` fan-out, so a fresh verdict overwrites the old one
    before `review` reads it, and the mismatch branch is unreachable end to end today.
    That is exactly why it is worth a direct test. The branch exists so that adding a
    path which skips the fan-out cannot silently start regenerating queries in response
    to a complaint about a query that no longer exists, and an unreachable branch with no
    test is one refactor away from being both reachable and wrong.
    """
    from concurrent.futures import Future

    from src.models import Verification

    settled: Future = Future()
    settled.set_result(
        Verification(sql="SELECT 1", aligned=False, objection="wrong measure", reading=None)
    )
    state = {
        "sql": "SELECT 2",  # a later attempt replaced the statement that was judged
        "verification_future": ("SELECT 1", settled),
        "verify_retries": 0,
        "retry_reasons": [],
    }

    updates = agent_module.review(state)

    assert updates["next"] == "ok", "acted on a verdict about SQL that no longer exists"
    assert "verification" not in updates
    assert updates.get("caveat") is None


def test_a_groundedness_violation_does_not_survive_a_sql_regeneration(stub) -> None:
    """The figures belong to the answer that stated them, and regenerating the SQL
    discards that answer. Left set, they would make the next summarisation argue with a
    figure from a result set that no longer exists, and would bill a second
    `ground_check` retry for it.
    """
    stubbed = stub(
        sql_sequence=[TERMINALS_SQL, TERMINALS_SQL],
        objection="wrong measure",
        summary="There were 918,273 containers.",
    )
    result = agent_module.ask("How many port calls were there?")

    assert result.retry_reasons == [
        RetryReason.VERIFIER_OBJECTION,
        RetryReason.GROUND_CHECK,
    ], "a stale groundedness violation was re-billed after regeneration"
    assert len(stubbed.calls["summarize"]) == 3


# ---------------------------------------------------------------------------------
# Code-detected quality triggers.
# ---------------------------------------------------------------------------------
def test_a_multi_row_answer_to_a_singular_superlative_is_regenerated(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL, ONE_ROW_SQL])
    result = agent_module.ask("Which terminal has the most berths?")

    assert result.retry_reasons == [RetryReason.QUALITY_TRIGGER]
    assert result.result is not None and result.result.row_count == 1
    assert "LIMIT 1" in stubbed.calls["generate_sql"][1]


def test_an_empty_result_on_an_answerable_question_is_regenerated(stub) -> None:
    stubbed = stub(sql_sequence=[EMPTY_SQL, TERMINALS_SQL])
    result = agent_module.ask("Which terminals are in the Netherlands?")

    assert result.retry_reasons == [RetryReason.QUALITY_TRIGGER]
    assert len(stubbed.calls["generate_sql"]) == 2


def test_a_quality_trigger_is_bounded_at_one_regeneration(stub) -> None:
    """A query that keeps returning nothing must stop, and must still answer.

    Without the bound this is an unbounded spend loop on exactly the questions that have
    no answer, the case where retrying is least likely to help.
    """
    stubbed = stub(sql_sequence=[EMPTY_SQL] * 5)
    result = agent_module.ask("Which terminals are in Atlantis?")

    assert len(stubbed.calls["generate_sql"]) == 2
    assert result.outcome is Outcome.ANSWERED
    assert result.answer == "No data matched that question."


def test_an_empty_result_is_not_a_trigger_when_the_question_was_refused(stub) -> None:
    """An empty result is the CORRECT outcome for a question that was not routed
    answerable, so regenerating against it would be manufacturing work."""
    stubbed = stub(route="out_of_scope")
    result = agent_module.ask("What is the weather tomorrow?")

    assert result.outcome is Outcome.REFUSED
    assert stubbed.calls["generate_sql"] == []


# ---------------------------------------------------------------------------------
# Retry accounting (ADR-012 supersedes the single `retried` bit).
# ---------------------------------------------------------------------------------
def test_a_database_error_is_recorded_as_its_own_reason(stub) -> None:
    stub(sql_sequence=["SELECT * FROM no_such_table", TERMINALS_SQL])
    result = agent_module.ask("How many berths?")

    assert result.retry_reasons == [RetryReason.DB_ERROR]
    assert result.retried is True


def test_a_clean_run_records_no_retries(stub) -> None:
    stub(sql_sequence=[TERMINALS_SQL], summary="Six terminals.")
    result = agent_module.ask("How many berths does each terminal have?")

    assert result.retry_reasons == []
    assert result.retried is False


def test_each_mechanism_has_its_own_budget(stub) -> None:
    """A database error must not spend the verifier's regeneration, or the reverse.

    One shared counter would make the budgets interact for reasons unrelated to either:
    a query that failed in the database once would silently lose its right to be
    corrected after an objection.
    """
    stubbed = stub(
        sql_sequence=["SELECT * FROM no_such_table", TERMINALS_SQL, TERMINALS_SQL],
        objection="wrong measure",
    )
    result = agent_module.ask("How many port calls were there?")

    assert result.retry_reasons == [RetryReason.DB_ERROR, RetryReason.VERIFIER_OBJECTION]
    assert len(stubbed.calls["generate_sql"]) == 3


# ---------------------------------------------------------------------------------
# The switch. Off must be the pre-ADR-012 pipeline, or the comparison runs measure two
# unrelated systems rather than one change.
# ---------------------------------------------------------------------------------
def test_verification_off_runs_none_of_the_new_nodes(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL], summary="There were 918,273 containers.")
    result = agent_module.ask("How many berths?", verification=False)

    assert stubbed.calls["verify"] == []
    for stage in ("verify", "ground_check", "review"):
        assert stage not in result.stage_timings
    # And none of the advisory machinery leaks into the result either.
    assert result.retry_reasons == []
    assert result.reading is None and result.caveat is None
    assert result.grounding_flag is None


def test_verification_off_does_not_fire_quality_triggers(stub) -> None:
    stubbed = stub(sql_sequence=[TERMINALS_SQL, ONE_ROW_SQL])
    result = agent_module.ask("Which terminal has the most berths?", verification=False)

    assert len(stubbed.calls["generate_sql"]) == 1
    assert result.retry_reasons == []


def test_verification_off_keeps_the_database_error_retry(stub) -> None:
    """The switch governs ADR-012's additions only. The db-error retry predates it and
    is load-bearing for accuracy, so the baseline must keep it."""
    stubbed = stub(sql_sequence=["SELECT * FROM no_such_table", TERMINALS_SQL])
    result = agent_module.ask("How many berths?", verification=False)

    assert len(stubbed.calls["generate_sql"]) == 2
    assert result.retry_reasons == [RetryReason.DB_ERROR]
