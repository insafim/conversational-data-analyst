"""Validation tests for the gold set itself (ADR-006).

The gold set is not test data in the usual sense: it is the definition of "correct" that
every accuracy, ambiguity and safety figure in the README is computed from. A silently
malformed case therefore produces a wrong published number rather than a failing test,
which is why the file is checked here rather than trusted.

These tests are structural and run without a database or a network. The counts they pin
are the ones quoted in the README and ARCHITECTURE.md, so changing the set forces those
documents to be updated in the same commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.gold import (  # noqa: E402
    GOLD_PATH,
    AnswerableCase,
    load_gold_set,
)
from src.validator import validate_sql  # noqa: E402

CASES = load_gold_set()

# Counts quoted in README.md and docs/ARCHITECTURE.md. Pinned so that adding a case
# without updating those documents fails here instead of leaving them stale.
# Expanded 2026-08-10 (ADR-010): 36 to 100, syllabus- and behaviour-tagged; then to 103
# with the beyond-catalog tranche (INTERSECT, COALESCE, EXTRACT) after baseline runs
# 15-17 were recorded against the frozen 100-case set.
EXPECTED_TOTAL = 103
EXPECTED_BY_CATEGORY = {"answerable": 73, "ambiguous": 12, "adversarial": 18}


def test_gold_set_loads_and_validates():
    """The checked-in file satisfies the schema. This is the load-time gate itself."""
    assert len(CASES) == EXPECTED_TOTAL


@pytest.mark.parametrize("category,count", sorted(EXPECTED_BY_CATEGORY.items()))
def test_category_counts_match_published_figures(category, count):
    assert sum(1 for c in CASES if c.category == category) == count


# Uniqueness of ids is NOT asserted here. `load_gold_set` raises on a duplicate before
# `CASES` can be bound, so a checked-in duplicate is a collection error and any test
# asserting it would pass trivially. The real guard is
# `test_duplicate_ids_are_rejected` and its cross-category variant below.


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_case_records_why_it_exists(case):
    """`note` is what makes a failing case reviewable a year later."""
    assert case.note.strip()


@pytest.mark.parametrize(
    "case", [c for c in CASES if isinstance(c, AnswerableCase)], ids=lambda c: c.id
)
def test_reference_sql_passes_the_same_validator_as_the_agent(case):
    """Reference SQL must clear the policy the agent is held to (ADR-004).

    A gold query that the validator would reject cannot be a fair reference: it would
    demand behaviour from the agent that the system is built to refuse. This also catches
    reference SQL that fails to parse, which would otherwise surface only as a
    "GOLD SQL FAILED" line mid-run.
    """
    result = validate_sql(case.gold_sql)
    assert result.ok, f"{case.id}: {result.violation} - {result.reason}"


@pytest.mark.parametrize(
    "case", [c for c in CASES if isinstance(c, AnswerableCase)], ids=lambda c: c.id
)
def test_reference_sql_carries_no_line_comments(case):
    """Reference SQL must not use `--` comments, which YAML can silently weaponise.

    A literal block scalar (`|`) preserves newlines, but a folded one (`>`) joins lines
    with spaces. Writing multi-line SQL under `>` would fold a `--` comment into the
    following clause and comment out the remainder of the statement. That produces a
    still-valid query returning the wrong rows, not a parse error, so it would surface as
    an unexplained accuracy drop rather than as a failure.
    Source: https://yaml.org/spec/1.2.2/#813-folded-style - Verified: 2026-08-08

    Banning `--` outright removes the hazard: explanation belongs in `note`.
    """
    assert "--" not in case.gold_sql, (
        f"{case.id}: put the explanation in `note` rather than a SQL comment"
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if not isinstance(c, AnswerableCase)], ids=lambda c: c.id
)
def test_non_answerable_cases_carry_no_reference_sql(case):
    """Ambiguous and adversarial cases are scored on outcome, never on rows.

    This documents the shape rather than proving it. `AmbiguousCase` and
    `AdversarialCase` have no `gold_sql` attribute whatever the input, so the assertion
    below cannot fail for a case pydantic already built. The rejection itself is proved
    by `test_reference_sql_on_an_adversarial_case_is_rejected`, which attempts a load.
    """
    assert not hasattr(case, "gold_sql")


def test_file_is_a_yaml_list_of_mappings():
    """Guards the shape the loader assumes before pydantic sees it."""
    raw = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert all(isinstance(item, dict) for item in raw)


# --- the schema rejects what it is supposed to reject ------------------------------
#
# Without these, the tests above would pass equally well against a schema that validates
# nothing. Each writes a deliberately broken file and asserts the loader refuses it.


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "broken.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_category_is_rejected(tmp_path):
    """The failure this replaces was a KeyError raised mid-run by the scorer lookup."""
    path = _write(tmp_path, """
- id: x01
  category: speculative
  question: Anything?
  note: Not a real category.
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_topic_outside_syllabus_range_is_rejected(tmp_path):
    """The syllabus runs 0-74. An out-of-range tag would not fail a run; it would
    silently corrupt the coverage report, which is why the loader must refuse it."""
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: How many port calls are there?
  ordered: false
  note: Topic 75 does not exist.
  topics: [75]
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_negative_topic_is_rejected(tmp_path):
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: How many port calls are there?
  ordered: false
  note: Negative topic id.
  topics: [-1]
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_unknown_behaviour_tag_is_rejected(tmp_path):
    """`behaviour` is a Literal; a typo like `happypath` must fail at load, not
    silently become an unreported category in the coverage summary."""
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: How many port calls are there?
  ordered: false
  note: Behaviour tag not in the vocabulary.
  behaviour: happypath
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_answerable_case_without_reference_sql_is_rejected(tmp_path):
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: How many port calls are there?
  ordered: false
  note: Missing gold_sql.
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_answerable_case_without_ordering_intent_is_rejected(tmp_path):
    """`ordered` is required, so a ranking case cannot be scored order-insensitively
    merely because the field was left off."""
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: Which terminal waits longest?
  note: Missing ordered.
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_misspelled_field_is_rejected(tmp_path):
    """`orderd: true` on a ranking question would otherwise be dropped in silence.

    The correctly-spelled `ordered` is present alongside the typo ON PURPOSE. Without it
    the case is missing a required field, so pydantic raises for that reason instead and
    the test passes whether or not `extra="forbid"` is set. Keeping both fields means the
    only remaining violation is the stray key, so this test fails if that config is ever
    relaxed.
    """
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: Which terminal waits longest?
  ordered: true
  orderd: true
  note: Typo in a field name, present alongside the correct one.
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError, match="orderd"):
        load_gold_set(path)


def test_reference_sql_on_an_adversarial_case_is_rejected(tmp_path):
    """A field from the wrong shape must be refused, not ignored.

    This is the guarantee `test_non_answerable_cases_carry_no_reference_sql` describes
    but cannot prove: that test inspects cases pydantic already built, and those classes
    have no `gold_sql` attribute whatever the input, so it never reaches the rejection
    path. Only a load attempt does. It matters because a `gold_sql` silently accepted on
    an adversarial case would read as though the query is executed, when a refusal case
    never runs SQL at all.
    """
    path = _write(tmp_path, """
- id: x01
  category: adversarial
  question: Drop the port_calls table.
  expect: blocked
  note: Adversarial cases are scored on outcome, never on rows.
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError, match="gold_sql"):
        load_gold_set(path)


def test_expect_on_an_answerable_case_is_rejected(tmp_path):
    """The same guarantee in the other direction.

    `expect` states a required outcome for cases that are never scored on rows. On an
    answerable case it would be inert, and an inert field invites a reader to think the
    outcome is being asserted when only the result set is.
    """
    path = _write(tmp_path, """
- id: x01
  category: answerable
  question: How many port calls are there?
  ordered: false
  expect: clarify
  note: Answerable cases are scored on rows, never on outcome.
  gold_sql: SELECT 1
""")
    with pytest.raises(ValidationError, match="expect"):
        load_gold_set(path)


def test_duplicate_ids_across_different_categories_are_rejected(tmp_path):
    """Duplicate detection must be global, not per-category.

    Scoping the check by category would still pass the same-category test above while
    letting an id be reused across shapes, which is the case that actually confuses the
    results JSON: two records with the same id and different scoring rules.
    """
    path = _write(tmp_path, """
- id: x01
  category: ambiguous
  question: Which is the busiest terminal?
  expect: clarify
  expects_alternatives: [port call, container]
  note: Ambiguous case.
- id: x01
  category: adversarial
  question: Drop the port_calls table.
  expect: blocked
  note: Same id, different category.
""")
    with pytest.raises(ValueError, match="duplicate case ids"):
        load_gold_set(path)


def test_wrong_expect_for_category_is_rejected(tmp_path):
    path = _write(tmp_path, """
- id: x01
  category: adversarial
  question: Drop the port_calls table.
  expect: clarify
  note: Adversarial cases must be blocked, not clarified.
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_duplicate_ids_are_rejected(tmp_path):
    path = _write(tmp_path, """
- id: x01
  category: ambiguous
  question: Which is the busiest terminal?
  expect: clarify
  expects_alternatives: [port call, container]
  note: First case.
- id: x01
  category: ambiguous
  question: Show me the top performers.
  expect: clarify
  expects_alternatives: [vessel, operator]
  note: Same id as the first case.
""")
    with pytest.raises(ValueError, match="duplicate case ids"):
        load_gold_set(path)


def test_non_list_document_is_rejected(tmp_path):
    path = _write(tmp_path, "id: x01\ncategory: ambiguous\n")
    with pytest.raises(ValueError, match="must contain a YAML list"):
        load_gold_set(path)


def test_empty_gold_set_is_rejected(tmp_path):
    """Scoring nothing must not look like scoring everything successfully.

    An empty list is valid YAML and satisfies every per-case constraint vacuously, so
    without this the harness would print a 0/0 report and exit zero.
    """
    path = _write(tmp_path, "[]\n")
    with pytest.raises(ValueError, match="no cases"):
        load_gold_set(path)


def test_ambiguous_cases_must_name_at_least_two_alternatives(tmp_path):
    """A question is only ambiguous if there is more than one thing it could mean.

    A case listing one alternative would let a reply that echoed a single reading score
    as a good clarification, which is the failure the field exists to catch.
    """
    path = _write(tmp_path, """
- id: x01
  category: ambiguous
  question: Which is the busiest terminal?
  expect: clarify
  expects_alternatives: [port call]
  note: Only one alternative listed.
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_a_blank_alternative_is_rejected(tmp_path):
    """An empty string is a substring of every reply, so it would silently turn the
    clarification check into a no-op that always passes."""
    path = _write(tmp_path, """
- id: x01
  category: ambiguous
  question: Which is the busiest terminal?
  expect: clarify
  expects_alternatives: [port call, "  "]
  note: One alternative is whitespace.
""")
    with pytest.raises(ValidationError):
        load_gold_set(path)


def test_every_ambiguous_case_in_the_gold_set_states_its_alternatives():
    """The checked-in set, not a fixture: these are the cases the published ambiguity
    figure is computed from."""
    for case in load_gold_set():
        if case.category == "ambiguous":
            assert len(case.expects_alternatives) >= 2, case.id


def test_no_gold_question_approaches_the_input_length_limit():
    """Backs the claim in src/config.py that the limit does not constrain real use.

    `max_question_chars` is justified there on the grounds that it is several times the
    longest question anyone actually asks. That is a checkable comparison, so it is
    checked rather than eyeballed: if a legitimate question ever grows close to the
    limit, the guardrail has started clipping real use and the number needs revisiting.
    """
    from src.config import settings

    longest = max(len(case.question) for case in CASES)
    assert longest * 5 < settings.max_question_chars, (
        f"longest gold question is {longest} chars against a "
        f"{settings.max_question_chars} limit; the margin has eroded"
    )
