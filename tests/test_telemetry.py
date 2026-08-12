"""What the observability page aggregates, asserted without a browser.

Two halves, matching the page: `Store.telemetry()` over turns this application served,
and `load_eval_runs()` over the committed artefacts in `eval/results/`.
"""

from __future__ import annotations

import json

import pytest

from src.models import AgentResult, Outcome, QueryResult, RetryReason
from src.telemetry import load_eval_runs


def _answer(**overrides) -> AgentResult:
    base = dict(
        question="Which terminal has the longest average berth wait?",
        outcome=Outcome.ANSWERED,
        answer="Jebel Ali Terminal 2, at 17.46 hours.",
        sql="SELECT terminal_name FROM terminals",
        elapsed_s=6.0,
        llm_calls=3,
        cost_usd=0.01,
    )
    return AgentResult(**{**base, **overrides})


# --- the live half -------------------------------------------------------------------
@pytest.mark.integration
def test_an_empty_store_reports_zeroes_rather_than_failing(store) -> None:
    """The state a reviewer opens this page in. `percentile_cont` returns NULL over an
    empty table and `sum` returns NULL over no rows, so every scalar is coalesced."""
    live = store.telemetry()

    assert live.turns == 0
    assert live.conversations == 0
    assert live.total_cost_usd == 0.0
    assert live.mean_cost_usd == 0.0, "a mean over no turns divided by zero"
    assert live.median_latency_s == 0.0
    assert live.p95_latency_s == 0.0
    assert live.outcomes == {}
    assert live.retry_reasons == {}
    assert live.stage_means_s == {}


@pytest.mark.integration
def test_cost_and_calls_total_across_every_conversation(store) -> None:
    first = store.create_conversation("One")
    second = store.create_conversation("Two")
    store.append_turn(first, "q", _answer(cost_usd=0.01, llm_calls=3))
    store.append_turn(first, "q", _answer(cost_usd=0.02, llm_calls=4))
    store.append_turn(second, "q", _answer(cost_usd=0.03, llm_calls=5))

    live = store.telemetry()

    assert live.turns == 3
    assert live.conversations == 2
    assert live.total_cost_usd == pytest.approx(0.06)
    assert live.mean_cost_usd == pytest.approx(0.02)
    assert live.llm_calls == 12


@pytest.mark.integration
def test_latency_is_reported_as_median_and_p95_not_as_a_mean(store) -> None:
    """The repo quotes median deliberately: cold-start outliers drag the mean, and one
    control run hit a 17s provider outlier. A page that showed the mean would contradict
    the README's own reasoning about which figure to trust."""
    conversation = store.create_conversation("Latency")
    for seconds in [1.0, 2.0, 3.0, 4.0, 60.0]:
        store.append_turn(conversation, "q", _answer(elapsed_s=seconds))

    live = store.telemetry()

    assert live.median_latency_s == pytest.approx(3.0), "the outlier moved the median"
    # Pinned, not bounded. PostgreSQL's percentile_cont(0.95) over this set is 48.8 and
    # percentile_cont(0.9) is 37.6, so a loose "greater than 4" band would not notice the
    # constant changing. Both figures were read from the running instance.
    assert live.p95_latency_s == pytest.approx(48.8), "this is not the 95th percentile"


@pytest.mark.integration
def test_outcomes_are_counted_so_a_refusal_rate_is_visible(store) -> None:
    conversation = store.create_conversation("Outcomes")
    store.append_turn(conversation, "q", _answer(outcome=Outcome.ANSWERED))
    store.append_turn(conversation, "q", _answer(outcome=Outcome.ANSWERED))
    store.append_turn(conversation, "q", _answer(outcome=Outcome.REFUSED, sql=None))
    store.append_turn(conversation, "q", _answer(outcome=Outcome.CLARIFY, sql=None))

    live = store.telemetry()

    assert live.outcomes == {"answered": 2, "refused": 1, "clarify": 1}
    # Order matters and dict equality does not check it. The page renders these as a table
    # described as "widest first", so the SQL's ORDER BY is part of the contract.
    assert list(live.outcomes) == ["answered", "refused", "clarify"], "counts are not ordered"


@pytest.mark.integration
def test_a_turn_that_retried_twice_is_attributed_to_both_reasons(store) -> None:
    """Named rather than counted (ADR-012): a count of retries cannot tell a database
    error from the verifier changing the query, which is the distinction the whole
    reason code exists for."""
    conversation = store.create_conversation("Retries")
    store.append_turn(
        conversation,
        "q",
        _answer(retry_reasons=[RetryReason.DB_ERROR, RetryReason.VERIFIER_OBJECTION]),
    )
    store.append_turn(conversation, "q", _answer(retry_reasons=[RetryReason.DB_ERROR]))
    store.append_turn(conversation, "q", _answer())

    live = store.telemetry()

    assert live.retry_reasons == {"db_error": 2, "verifier_objection": 1}


@pytest.mark.integration
def test_stage_timings_are_averaged_so_the_slowest_stage_is_identifiable(store) -> None:
    """The question this answers is which of the three model calls to optimise, so the
    mean per stage is the figure. A total would just rank stages by how many turns ran."""
    conversation = store.create_conversation("Stages")
    store.append_turn(
        conversation, "q", _answer(stage_timings={"generate_sql": 4.0, "classify": 1.0})
    )
    store.append_turn(
        conversation, "q", _answer(stage_timings={"generate_sql": 2.0, "classify": 3.0})
    )

    live = store.telemetry()

    assert live.stage_means_s["generate_sql"] == pytest.approx(3.0)
    assert live.stage_means_s["classify"] == pytest.approx(2.0)


@pytest.mark.integration
def test_a_refusal_contributes_to_counts_without_breaking_the_cost_arithmetic(store) -> None:
    """A refusal has no SQL and no rows, and it still costs a classify call. It has to
    appear in the totals or the cost per question reads low."""
    conversation = store.create_conversation("Mixed")
    store.append_turn(
        conversation,
        "q",
        _answer(outcome=Outcome.REFUSED, sql=None, cost_usd=0.002, llm_calls=1),
    )
    store.append_turn(
        conversation,
        "q",
        _answer(result=QueryResult(
            columns=["a"], column_types=["text"], rows=[["x"]], row_count=1, elapsed_s=0.01
        )),
    )

    live = store.telemetry()

    assert live.turns == 2
    assert live.total_cost_usd == pytest.approx(0.012)
    assert live.outcomes == {"refused": 1, "answered": 1}


# --- the eval half -------------------------------------------------------------------
def _write_run(directory, number: int, records: list[dict]) -> None:
    (directory / f"run{number}.json").write_text(json.dumps(records))


def _case(**overrides) -> dict:
    base = dict(passed=True, grounded=True, outcome="answered", elapsed_s=6.0, cost_usd=0.01)
    return {**base, **overrides}


def test_runs_are_ordered_by_their_number_not_their_name(tmp_path) -> None:
    """`run9` sorts after `run25` lexically, which would put a nine-month-old run at the
    top of the page."""
    _write_run(tmp_path, 9, [_case()])
    _write_run(tmp_path, 25, [_case()])
    _write_run(tmp_path, 10, [_case()])

    runs = load_eval_runs(tmp_path)

    assert [run.number for run in runs] == [25, 10, 9]
    assert runs[0].name == "run25"


def test_a_run_summarises_its_scores_and_cost(tmp_path) -> None:
    _write_run(
        tmp_path,
        1,
        [
            _case(elapsed_s=5.0, cost_usd=0.01),
            _case(elapsed_s=6.0, cost_usd=0.02),
            _case(passed=False, elapsed_s=7.0, cost_usd=0.03, outcome="error"),
        ],
    )

    run = load_eval_runs(tmp_path)[0]

    assert run.cases == 3
    assert run.passed == 2
    assert run.pass_rate == pytest.approx(2 / 3)
    assert run.total_cost_usd == pytest.approx(0.06)
    assert run.median_latency_s == pytest.approx(6.0)
    assert run.outcomes == {"answered": 2, "error": 1}


def test_passed_and_grounded_are_counted_separately(tmp_path) -> None:
    """They moved in opposite directions when runtime verification was switched on
    (ADR-012), which is the measurement that overruled an accepted ADR. One combined
    score would hide it."""
    _write_run(
        tmp_path,
        1,
        [_case(passed=True, grounded=False), _case(passed=False, grounded=True)],
    )

    run = load_eval_runs(tmp_path)[0]

    assert run.passed == 1
    assert run.grounded == 1
    assert run.pass_rate == pytest.approx(0.5)
    assert run.grounded_rate == pytest.approx(0.5)


def test_groundedness_is_scored_over_answers_not_over_refusals(tmp_path) -> None:
    """`eval/run_eval.py` records `grounded: null` for anything that did not answer, then
    divides by the records carrying a real value. A refusal has no figures to ground, so
    counting it as ungrounded would punish the system for correctly declining."""
    _write_run(
        tmp_path,
        1,
        [
            _case(grounded=True),
            _case(grounded=True),
            _case(grounded=False, passed=False),
            _case(grounded=None, outcome="refused"),
            _case(grounded=None, outcome="clarify"),
        ],
    )

    run = load_eval_runs(tmp_path)[0]

    assert run.cases == 5
    assert run.grounded == 2
    assert run.grounded_scored == 3, "the refusals were counted as scored"
    assert run.grounded_rate == pytest.approx(2 / 3), (
        "groundedness was divided by every case, which reports refusals as ungrounded"
    )
    # The two rates have different denominators on purpose: a correct refusal counts
    # towards the overall score and is excluded from groundedness.
    assert run.pass_rate == pytest.approx(4 / 5), "the overall score is not over every case"


def test_a_run_with_no_cases_reports_zero_rather_than_dividing_by_zero(tmp_path) -> None:
    _write_run(tmp_path, 1, [])

    run = load_eval_runs(tmp_path)[0]

    assert run.cases == 0
    assert run.pass_rate == 0.0
    assert run.grounded_rate == 0.0
    assert run.median_latency_s == 0.0, "median of an empty set raised instead of defaulting"


def test_a_run_of_the_right_type_but_the_wrong_shape_is_skipped(tmp_path) -> None:
    """Valid JSON of the wrong shape defeated a guard written for invalid JSON: a list of
    scalars passed the outer isinstance check and raised inside the summariser, taking the
    whole page with it."""
    _write_run(tmp_path, 1, [_case()])
    (tmp_path / "run2.json").write_text(json.dumps([1, 2, 3]))
    (tmp_path / "run3.json").write_text(json.dumps({"not": "a list"}))
    (tmp_path / "run4.json").write_text(json.dumps(["a string"]))

    runs = load_eval_runs(tmp_path)

    assert [run.number for run in runs] == [1], "a malformed run crashed the page"


def test_a_missing_field_is_not_counted_as_a_pass(tmp_path) -> None:
    """Older artefacts predate fields that were added later, and an absent value means
    not measured. Truthiness would read it as a failure and `is not False` as a pass;
    both are claims the record does not make."""
    _write_run(tmp_path, 1, [{"outcome": "answered", "elapsed_s": 5.0}])

    run = load_eval_runs(tmp_path)[0]

    assert run.cases == 1
    assert run.passed == 0
    assert run.grounded == 0
    assert run.total_cost_usd == 0.0


def test_an_unreadable_run_is_skipped_rather_than_failing_the_page(tmp_path) -> None:
    """Run 18 is committed and deliberately invalid, kept as the record of a local DNS
    outage (ADR-010), so a malformed artefact is an expected state of this directory."""
    _write_run(tmp_path, 1, [_case()])
    (tmp_path / "run18.json").write_text("{ this is not json")

    runs = load_eval_runs(tmp_path)

    assert [run.number for run in runs] == [1]


def test_files_that_are_not_runs_are_ignored(tmp_path) -> None:
    """`runNN.meta.json` is the sibling HANDOFF item 5 will add, and the logs sit in the
    same directory."""
    _write_run(tmp_path, 1, [_case()])
    (tmp_path / "run1.log").write_text("not json at all")
    (tmp_path / "run1.meta.json").write_text(json.dumps({"git_sha": "abc"}))
    (tmp_path / "gold.json").write_text(json.dumps([_case()]))

    runs = load_eval_runs(tmp_path)

    assert [run.name for run in runs] == ["run1"]


def test_a_directory_that_does_not_exist_is_empty_rather_than_an_error(tmp_path) -> None:
    assert load_eval_runs(tmp_path / "nothing here") == []


@pytest.mark.integration
def test_the_committed_runs_in_this_repository_actually_parse() -> None:
    """The artefacts are the evidence for the README's figures, so the page reading them
    is worth asserting against the real directory rather than only against fixtures."""
    from pathlib import Path

    runs = load_eval_runs(Path(__file__).resolve().parent.parent / "eval" / "results")

    assert len(runs) >= 20, "the committed run artefacts were not found"
    assert runs[0].number > runs[-1].number, "newest run is not first"
    by_number = {run.number: run for run in runs}
    assert 25 in by_number, "run25.json is missing, so the published figures cannot be checked"
    latest = by_number[25]

    # Pinned to the committed artefact, which is frozen. These are the figures ADR-012 and
    # the README quote, and the panel showing anything else would contradict the repo.
    assert latest.cases == 108, "run 25 is the 108-case gold set"
    assert latest.pass_rate == pytest.approx(0.944, abs=0.001), "overall is not 94.4%"
    assert latest.grounded_rate == pytest.approx(0.974, abs=0.001), (
        "groundedness is not 97.4%, which is what the README and ADR-012 report"
    )
