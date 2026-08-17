"""What the observability page aggregates, asserted without a browser.

Two halves, matching the page: `Store.telemetry()` over turns this application served,
and `load_eval_runs()` over the committed artefacts in `eval/results/`.
"""

from __future__ import annotations

import json

import pytest

from src.models import AgentResult, Outcome, QueryResult, RetryReason
from src.telemetry import (
    HEADLINE_CRITERIA,
    HEADLINE_LABELS,
    OUTCOME_LABELS,
    count_of,
    format_unit_usd,
    format_usd,
    load_eval_runs,
    outcome_tiles,
)


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


def test_a_run_is_scored_per_category_because_the_denominators_differ(tmp_path) -> None:
    """Three questions, three denominators, one file.

    The overall score hides all three. An adversarial case passes by being REFUSED, so
    safety is a count of successful blocks and cannot be read off a pass rate that also
    contains answered questions.
    """
    _write_run(
        tmp_path,
        1,
        [
            _case(category="answerable"),
            _case(category="answerable", passed=False),
            _case(category="ambiguous", outcome="clarify"),
            _case(category="adversarial", outcome="refused"),
            _case(category="adversarial", outcome="rejected"),
        ],
    )

    run = load_eval_runs(tmp_path)[0]

    assert run.categories["answerable"] == (1, 2)
    assert run.categories["answerable"].rate == pytest.approx(0.5)
    assert run.categories["adversarial"] == (2, 2)
    assert run.pass_rate == pytest.approx(4 / 5), (
        "the overall score should not have moved; the categories are a different cut"
    )


def test_a_category_the_run_never_contained_is_absent_not_zero(tmp_path) -> None:
    """A run with no adversarial cases has not scored 0% on its guardrails, it has no
    guardrail figure at all. Showing zero would report an untested guardrail as a failed
    one, so the cell says `n/a` and the tile shows the same string."""
    _write_run(tmp_path, 1, [_case(category="answerable")])

    run = load_eval_runs(tmp_path)[0]

    assert "adversarial" not in run.categories
    assert run.rate_cell("adversarial") == "n/a"
    assert run.count_cell("adversarial") == "n/a"
    assert run.rate_cell("answerable") == "100.0%", "a category that IS present must score"


def test_the_headline_figures_are_labelled_in_a_fixed_order(tmp_path) -> None:
    """Same four positions for every run, so a reader comparing two runs is not re-reading
    the labels. The order comes from `_HEADLINE_TEXT`, not from whatever order the cases
    happened to appear in the file.

    The labels themselves are pinned because the run table renders its columns from the
    same mapping: a tile and a column showing one figure under two names is the confusion
    this module exists to prevent.
    """
    _write_run(
        tmp_path,
        1,
        [
            _case(category="adversarial", outcome="refused"),
            _case(category="ambiguous", outcome="clarify"),
            _case(category="answerable"),
        ],
    )

    tiles = load_eval_runs(tmp_path)[0].headline_tiles()

    assert [tile.label for tile in tiles] == [
        "SQL correctness",
        "Answer groundedness",
        "Ambiguity handling",
        "Guardrails",
    ]
    assert [tile.label for tile in tiles] == list(HEADLINE_LABELS.values()), (
        "the tiles and the table columns are no longer taking their names from one place"
    )
    # The criteria are a second mapping keyed the same way, and nothing in the code makes
    # that true: `headline_tiles` reads two of the four keys by literal and the page reads
    # all four, so a renamed key fails loudly in one place and silently drops a column
    # tooltip in the other.
    assert set(HEADLINE_CRITERIA) == set(HEADLINE_LABELS), (
        "the criteria and the labels are keyed differently, so a column will lose its tooltip"
    )
    # The two figures whose tooltip IS the criterion, asserted by content rather than by
    # presence. These are the strings a reviewer reads to find out what counts as a pass,
    # and a swap between them puts the guardrail rule under the ambiguity column.
    assert tiles[2].tooltip == HEADLINE_CRITERIA["ambiguous"]
    assert tiles[3].tooltip == HEADLINE_CRITERIA["adversarial"]
    assert "adversarial subset" in tiles[3].tooltip, "the guardrail criterion moved tiles"
    # Counts where the denominator is small, rates where it is not. The two formats are the
    # claim each tile is entitled to make, so a swap here is a real defect.
    assert tiles[0].value == "100.0%"
    assert tiles[3].value == "1 of 1"


def test_a_run_without_categories_at_all_reports_none(tmp_path) -> None:
    """Older artefacts predate the field. The page must render them as a run with no
    category breakdown rather than raising, because every committed run is read on load."""
    _write_run(tmp_path, 1, [_case()])

    run = load_eval_runs(tmp_path)[0]

    assert run.categories == {}
    assert [tile.value for tile in run.headline_tiles()] == ["n/a", "100.0%", "n/a", "n/a"], (
        "an artefact with no categories must still render, with the unscored figures blank"
    )
    # The tooltip changes with the branch, not just the value. A tile reading `n/a` under a
    # tooltip that says "0 of 0 answerable cases" would be the arithmetic leaking through
    # the formatting the value was corrected for.
    assert run.headline_tiles()[0].tooltip == "This run scored no answerable cases."


def test_a_run_that_never_scored_groundedness_reports_it_as_absent(tmp_path) -> None:
    """`n/a`, not `0.0%`, and this is the branch the whole function exists for.

    `runs 1 to 3` in `eval/results/` predate the groundedness check: their records carry no
    `grounded` field, so `grounded_scored` is zero and `grounded_rate` divides by nothing
    and returns 0.0. Rendered as a percentage that reads "not one figure in any answer came
    from the data", which is the worst thing this table could say about a run and is not a
    measurement of one. Asserted here rather than left to the page test, which renders those
    rows and would not notice what they say.
    """
    _write_run(tmp_path, 1, [_case(grounded=None), _case(grounded=None)])

    run = load_eval_runs(tmp_path)[0]

    assert run.grounded_scored == 0, "the fixture is not exercising the branch under test"
    assert run.grounded_rate == 0.0, "the rate itself is still zero; the cell is what fixes it"
    assert run.grounded_cell() == "n/a"
    assert run.headline_tiles()[1].value == "n/a", "the tile did not take the corrected cell"
    assert run.headline_tiles()[1].tooltip == "No case in this run carries a groundedness result."


def test_groundedness_is_scored_over_the_cases_that_carry_a_result(tmp_path) -> None:
    """The other side of the branch above. Two of three cases scored, one of them grounded,
    reads 50.0% rather than 33.3%: the unscored case leaves the denominator rather than
    counting against it, which is the denominator ADR-012's figure rests on."""
    _write_run(
        tmp_path,
        1,
        [
            _case(grounded=True),
            _case(grounded=False),
            _case(grounded=None, outcome="refused"),
        ],
    )

    run = load_eval_runs(tmp_path)[0]

    assert run.grounded_cell() == "50.0%"
    assert run.headline_tiles()[1].tooltip == (
        "1 of 2 cases where groundedness was scored. A refusal or a clarification has no "
        "figures to ground, so it is not counted here."
    )


# --- how a measurement is written down -------------------------------------------------
def test_a_per_question_cost_keeps_four_decimals_at_every_size() -> None:
    """The bug this pair of functions exists to prevent.

    An answered question costs about a cent and a third, which the aggregate format rounds
    to `$0.01`, the same string a question costing half as much produces. The magnitude
    rule worked only while a single question stayed under a cent, and it does not.
    """
    assert format_unit_usd(0.0142) == "$0.0142"
    assert format_unit_usd(0.005) == "$0.0050"
    assert format_unit_usd(0.0135) != format_unit_usd(0.0071), (
        "two costs that differ by a factor of two render identically"
    )


def test_a_single_thing_is_counted_in_the_singular() -> None:
    """A refusal and a clarification each make exactly ONE model call, so the two outcomes
    a demo is most likely to show were the two that read "1 model calls". Found by
    replaying real run-26 records through the disclosure, not by any test, which is why
    there is now a test."""
    assert count_of(1, "model call") == "1 model call"
    assert count_of(4, "model call") == "4 model calls"
    assert count_of(0, "row") == "0 rows", "zero takes the plural"
    assert count_of(1500, "row") == "1,500 rows", "large counts keep their separator"


def test_an_aggregate_cost_is_rounded_to_money_but_never_to_nothing() -> None:
    """A run total of `$1.2567` is noise, so totals round. A total below a cent still
    keeps its decimals, because `$0.00` for money that was actually spent reads as free."""
    assert format_usd(1.2567) == "$1.26"
    assert format_usd(1234.5) == "$1,234.50"
    assert format_usd(0.004) == "$0.0040"


def test_every_outcome_gets_a_tile_including_the_ones_at_zero() -> None:
    """Zero is the interesting case for a guardrail. A store in which nothing was ever
    blocked has a real figure, and omitting it would make an untested guardrail look
    identical to an absent one."""
    tiles = outcome_tiles({"answered": 5, "rejected": 1})

    assert tiles == [
        ("Answered", 5),
        ("Clarified", 0),
        ("Refused", 0),
        ("Blocked by validator", 1),
        ("Errors", 0),
    ]


def test_every_outcome_the_system_can_produce_has_a_tile() -> None:
    """The guardrail panel must not be able to lose a turn.

    `outcome_tiles` renders the labels it knows and silently drops anything else, so a
    sixth `Outcome` added to the enum without a label here would vanish from the panel:
    the tiles would still sum to less than the question count and nothing would say so.
    Pinned against the enum rather than against a hand-written list, so adding a terminal
    state fails this test instead of quietly shrinking the panel.
    """
    assert set(OUTCOME_LABELS) == set(Outcome), (
        f"{set(Outcome) - set(OUTCOME_LABELS)} would be counted but never shown"
    )


def test_the_tiles_account_for_every_turn_in_the_store() -> None:
    """The arithmetic that makes the panel trustworthy: the tiles partition the traffic,
    so a reader adding them up gets the question count back."""
    outcomes = {"answered": 7, "clarify": 2, "refused": 1, "rejected": 1, "error": 1}
    assert sum(count for _, count in outcome_tiles(outcomes)) == sum(outcomes.values())


def test_a_block_is_attributed_to_the_validator_not_to_the_model() -> None:
    """"Rejected" alone invites the reading that the model declined. It is `validator.py`,
    pure code, refusing to run a statement the model had already written (ADR-004), and
    that distinction is the whole of the read-only argument."""
    labels = dict(outcome_tiles({}))
    assert "Blocked by validator" in labels
    assert "Rejected" not in labels


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

    # The claim `grounded_cell` was written for, checked against the artefacts it is about
    # rather than against a fixture that imitates them. Runs 1 to 3 were made before the
    # groundedness check existed, so their records carry no `grounded` field, and the table
    # renders those three rows every time the page loads.
    for number in (1, 2, 3):
        early = by_number[number]
        assert early.grounded_scored == 0, (
            f"run {number} carries groundedness results, so it is no longer the case that "
            "the early runs predate the check"
        )
        assert early.grounded_cell() == "n/a", "an unscored run is being reported as 0.0%"


@pytest.mark.integration
def test_the_shipped_run_scores_the_four_figures_the_page_puts_on_screen() -> None:
    """Run 26, pinned against the committed artefact.

    Run 26 rather than 25, and the distinction is the easiest one in this repository to
    get wrong: runs 21, 23 and 25 are the both-switches-off baseline, runs 20, 22 and 24
    have runtime verification on, and **run 26 is the configuration that actually ships**
    (verification off, ADR-013's reading on). These four tiles are the first thing on the
    eval half of the page, so they are the figures most likely to be read aloud.

    The same four appear on the eval board in `docs/visuals/eval.html`, which quotes this
    run. A reviewer who has seen the board and then opens the page is checking one set of
    numbers against the other, so a drift between them is a defect in both.
    """
    from pathlib import Path

    runs = load_eval_runs(Path(__file__).resolve().parent.parent / "eval" / "results")
    by_number = {run.number: run for run in runs}
    assert 26 in by_number, "run26.json is missing, so the shipped figures cannot be checked"
    shipped = by_number[26]

    assert shipped.categories["answerable"] == (72, 77), "execution accuracy is not 72/77"
    assert shipped.categories["ambiguous"] == (11, 12), "ambiguity handling is not 11/12"
    assert shipped.categories["adversarial"] == (19, 19), "guardrails are not 19/19"

    # The rendered strings, because a rate that is right to four places can still round to
    # the wrong tile. These are the four figures on the eval board, in its order.
    assert [tile.value for tile in shipped.headline_tiles()] == [
        "93.5%",
        "96.1%",
        "11 of 12",
        "19 of 19",
    ]

    # The categories partition the run: every case is in exactly one, so the three
    # denominators sum to the whole and none of the 108 is being quietly dropped.
    assert sum(score.cases for score in shipped.categories.values()) == shipped.cases
    assert sum(score.passed for score in shipped.categories.values()) == shipped.passed
