"""Tests for the gold-set audit ledger (eval/audit.py).

The ledger exists to stop a claim outrunning its evidence, so these tests are mostly
about the ways that could still happen: an entry for a case that no longer exists, a
duplicate verdict inflating coverage, a defect recorded with no explanation, and a
document quoting a coverage figure the ledger cannot support.

`test_documented_coverage_matches_the_ledger` is the one that matters. Every other
guarantee here is internal to the ledger; that one binds prose to evidence, which is the
whole reason the ledger is committed rather than kept in a notebook.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eval.audit import (
    COVERAGE_PATTERN,
    AuditCoverage,
    AuditEntry,
    coverage,
    coverage_claims,
    coverage_figures,
    coverage_sentence,
    load_audit,
    mechanical_flags,
    record_entry,
    sentences,
)
from eval.gold import AnswerableCase, load_gold_set

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Documents allowed to state audit coverage. Anything quoting a figure outside this set
#: is not pinned by these tests, which is why the list is explicit rather than a glob.
TRACKED_DOCUMENTS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "ARCHITECTURE.md",
    REPO_ROOT / "docs" / "DATA.md",
    REPO_ROOT / "docs" / "EVAL.md",
    REPO_ROOT / "docs" / "GOLD_AUDIT.md",
    REPO_ROOT / "docs" / "ADR" / "ADR-006-eval-execution-accuracy.md",
]


def _deliverable_documents() -> list[Path]:
    """Every markdown file a reviewer receives.

    Discovered rather than listed, because the list above is the guards' scope and a
    hand-maintained scope is exactly what a new document escapes. `docs/GOLD_AUDIT.md`
    made the point: it was written to describe the audit, and until it was registered it
    could have stated any coverage figure at all without a single test noticing.

    Prep material is excluded by asking git, not by pattern. The ignore list is the
    repository's own statement of what ships, and duplicating it here would create a
    second answer that drifts from the first.
    """
    candidates = sorted(REPO_ROOT.glob("*.md")) + sorted(REPO_ROOT.glob("docs/**/*.md"))
    if not candidates:
        return []
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(str(p) for p in candidates),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    # 0 means some paths were ignored, 1 means none were, and both are ordinary. Anything
    # else is git failing, which produces empty stdout and would otherwise be read as
    # "nothing is ignored", quietly reclassifying every prep document as shipped.
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed ({result.returncode}): {result.stderr}")
    ignored = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return [p for p in candidates if str(p) not in ignored]


#: An explicit ISO date inside a sentence. A dated statement records what was true on that
#: date and is checkable against the artefacts from it; an undated one claims what is true
#: now, which is what the coverage guards police. The distinction is what keeps ADR-010's
#: "executed against the seeded database on 2026-08-10" out of scope: an ADR is a record
#: amended by addendum, so rewriting its figures to today's would falsify it.
DATED = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")

#: The checkable form of the adjudicated-count claim: a number, in words or digits,
#: immediately before "of the 77 answerable cases".
ADJUDICATED_CLAIM = re.compile(r"\b([A-Za-z]+|\d+)\s+of\s+the\s+77\s+answerable\s+cases")

#: What the claim is ABOUT, detected separately from its figure, mirroring the split
#: between `_COVERAGE_TOPIC` and `COVERAGE_PATTERN` in eval/audit.py. Matching only the
#: exact phrase above is escapable by writing a different one: "Twelve answerable cases
#: have been adjudicated" states the wrong number while omitting "of the 77", so a
#: pattern-only guard never sees it. Detecting the topic forces any such sentence to
#: state its figure in the one form that can be checked.
ADJUDICATED_TOPIC = re.compile(r"adjudicat\w*", re.IGNORECASE)
ADJUDICATED_SUBJECT = re.compile(r"answerable case", re.IGNORECASE)

#: An unqualified claim that a human checked the reference SQL, in any of its spellings.
#: "by hand" and "manually" are the two ways the agent is taken out of the loop in prose,
#: and the verb varies freely; keying on one verb-plus-hyphen spelling caught one of them.
MANUAL_VERIFICATION = re.compile(
    r"\b(?:hand|human)[-\s]+(?:verif|check|audit|review|confirm)\w*"
    r"|\bmanually\s+(?:verif|check|audit|review|confirm)\w*"
    r"|\b(?:verif|check|audit|review|confirm)\w*\s+by\s+(?:hand|a\s+human)\b",
    re.IGNORECASE,
)

NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
}


def _unguarded_claims(path: Path, root: Path = REPO_ROOT) -> list[str]:
    """Coverage claims in `path` that no other guard would read.

    The date exemption is scoped by full relative path rather than by the parent
    directory's name. `path.parent.name == "ADR"` was the first attempt and it was wrong:
    `docs/**/*.md` recurses, so a directory called `ADR` nested anywhere under `docs/`
    bought the exemption without being a decision record, which reopened the loophole one
    level down.

    `root` is a parameter so that a test can exercise this against real files it wrote,
    rather than restating the rule and asserting on its own restatement.
    """
    is_decision_record = path.relative_to(root).parts[:2] == ("docs", "ADR")
    return [
        claim
        for claim in coverage_claims(path.read_text(encoding="utf-8"))
        if not (is_decision_record and DATED.search(claim))
    ]


def _manual_verification_offenders(paths: Iterable[Path]) -> list[str]:
    """Documents claiming a human checked the reference SQL, by name.

    A function rather than a comprehension inside the guard, so the wiring test can drive
    the same code the guard runs instead of restating the rule and asserting on its own
    restatement. `_stated_coverage` below exists for the same reason.
    """
    return [
        path.name
        for path in paths
        if path.exists() and MANUAL_VERIFICATION.search(path.read_text(encoding="utf-8"))
    ]


def _stated_coverage(path: Path) -> list[tuple[int, int]]:
    """Every (audited, total) pair a document states, read sentence by sentence.

    Not read over the raw text, which is what this did first and which left a hole wide
    enough to walk a wrong figure through. `COVERAGE_PATTERN` spells "reference queries"
    with a literal space, so a claim wrapped across two source lines matched nothing, and a
    guard that looks for figures and finds none concludes that all is well. The sentence
    guard below did see such a claim, but it only asks that a figure be present, so a
    wrapped "71 of 77" satisfied both checks at once. `sentences` collapses the wrapping,
    which is why it is the unit both guards read.

    A function rather than a comprehension inside the test, so the regression test can
    exercise the same code the guard runs instead of restating the rule and asserting on
    its own restatement. The sentence walk itself now lives in `coverage_figures`, which
    also reads the population's bare names when the sentence talks about scrutiny.
    """
    return coverage_figures(path.read_text(encoding="utf-8"))


def _coverage_mismatches(paths: list[Path], cov: AuditCoverage) -> list[str]:
    """Documents in `paths` stating a coverage figure the ledger cannot support.

    Takes its paths as an argument rather than looping over `TRACKED_DOCUMENTS` inside the
    guard, so a test can run the guard's own logic over a file it wrote. Pinning
    `_stated_coverage` on its own was not enough, and the gap was measured rather than
    supposed: reverting only the guard's call site to raw-text scanning, and leaving the
    helper untouched, left every test in this file green on 2026-08-14, all 49 of them at the
    time. Nothing asserted
    that the guard reads what the helper returns.
    """
    return [
        f"{path.name} claims {audited} of {total} reference queries audited, but "
        f"eval/gold_audit.yaml supports {cov.audited} of {cov.total}. "
        f"Update the prose to: {coverage_sentence(cov)}"
        for path in paths
        if path.exists()
        for audited, total in _stated_coverage(path)
        if (audited, total) != (cov.audited, cov.total)
    ]


def _answerable() -> list[AnswerableCase]:
    return [c for c in load_gold_set() if isinstance(c, AnswerableCase)]


def _case(case_id: str) -> AnswerableCase:
    return next(c for c in _answerable() if c.id == case_id)


# --- The ledger itself ---------------------------------------------------------------


def test_the_committed_ledger_loads_and_every_id_is_a_real_answerable_case():
    """Guards the failure that would silently inflate coverage: an entry left behind
    after its case was renamed or removed still counts unless the ids are cross-checked."""
    entries = load_audit()
    answerable_ids = {c.id for c in _answerable()}
    assert {e.id for e in entries} <= answerable_ids


def test_coverage_totals_the_answerable_set_not_the_whole_gold_set():
    """108 cases, 77 of them answerable. Only answerable cases carry reference SQL, so
    quoting 108 as the denominator would understate the audit's completeness and imply
    the ambiguous and adversarial cases have SQL to check."""
    cov = coverage()
    assert cov.total == len(_answerable())
    assert cov.audited + len(cov.remaining) == cov.total


def test_an_unknown_id_is_rejected_rather_than_counted(tmp_path: Path):
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "q_does_not_exist",
                    "disposition": "confirmed",
                    "method": "rederived",
                    "auditor": "Tester",
                    "checked_on": date(2026, 8, 13),
                    "note": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not answerable gold cases"):
        load_audit(ledger)


def test_a_duplicate_verdict_is_rejected_rather_than_double_counted(tmp_path: Path):
    """Two entries for one case would make `audited` exceed the number of cases actually
    looked at, which is the arithmetic that would let coverage reach 77 without 77
    checks."""
    entry = {
        "id": "q01",
        "disposition": "confirmed",
        "method": "rederived",
        "auditor": "Tester",
        "checked_on": date(2026, 8, 13),
        "note": "",
    }
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(yaml.safe_dump([entry, dict(entry)]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate entries"):
        load_audit(ledger)


@pytest.mark.parametrize("disposition", ["defect", "unsure"])
def test_an_unresolved_verdict_must_carry_its_reasoning(disposition: str):
    """A recorded defect with no note cannot be acted on by whoever picks it up, and
    both of these dispositions exist precisely to be picked up later."""
    with pytest.raises(ValidationError, match="requires a note"):
        AuditEntry(
            id="q01",
            disposition=disposition,
            method="rederived",
            auditor="Tester",
            checked_on=date(2026, 8, 13),
            note="   ",
        )


def test_a_plain_confirmation_needs_no_note():
    entry = AuditEntry(
        id="q01",
        disposition="confirmed",
        method="rederived",
        auditor="Tester",
        checked_on=date(2026, 8, 13),
    )
    assert entry.note == ""


def test_recording_the_same_case_twice_is_refused(tmp_path: Path):
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text("# header\n\n[]\n", encoding="utf-8")
    entry = AuditEntry(
        id="q01",
        disposition="confirmed",
        method="rederived",
        auditor="Tester",
        checked_on=date(2026, 8, 13),
    )
    record_entry(entry, ledger)
    with pytest.raises(ValueError, match="already in the ledger"):
        record_entry(entry, ledger)


def test_recording_preserves_the_header_and_reloads(tmp_path: Path):
    """The header explains what `method` means and why agreement is not verification. A
    writer that dropped it would leave the next reader with bare YAML."""
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text("# explanatory header\n# second line\n\n[]\n", encoding="utf-8")
    record_entry(
        AuditEntry(
            id="q02",
            disposition="defect",
            method="counted",
            auditor="Tester",
            checked_on=date(2026, 8, 13),
            note="divisor is wrong",
        ),
        ledger,
    )
    text = ledger.read_text(encoding="utf-8")
    assert text.startswith("# explanatory header")
    reloaded = load_audit(ledger)
    assert [e.id for e in reloaded] == ["q02"]
    assert reloaded[0].note == "divisor is wrong"


# --- Mechanical triage ---------------------------------------------------------------


def test_ordered_case_without_order_by_is_flagged():
    case = _case("q01").model_copy(update={"gold_sql": "SELECT terminal_name FROM terminals"})
    assert "ORDERED_NO_ORDER_BY" in {f.code for f in mechanical_flags(case)}


def test_an_ordered_case_with_an_order_by_is_not_flagged():
    """The negative half of the check above. Without it, a pattern that fired on every
    case would still pass the positive test."""
    case = _case("q01").model_copy(
        update={"gold_sql": "SELECT terminal_name FROM terminals ORDER BY terminal_name"}
    )
    assert "ORDERED_NO_ORDER_BY" not in {f.code for f in mechanical_flags(case)}


def test_an_unordered_ranking_query_is_flagged():
    """A query that ranks and limits while `ordered` is false scores order-insensitively,
    so an agent answer with the ranking upside down would still pass."""
    case = _case("q01").model_copy(
        update={
            "question": "Which three terminals are busiest?",
            "ordered": False,
            "gold_sql": "SELECT terminal_name FROM terminals ORDER BY calls DESC LIMIT 3",
        }
    )
    assert "RANKING_NOT_ORDERED" in {f.code for f in mechanical_flags(case)}


def test_a_ranking_query_marked_ordered_is_not_flagged():
    case = _case("q01").model_copy(
        update={
            "question": "Which three terminals are busiest?",
            "ordered": True,
            "gold_sql": "SELECT terminal_name FROM terminals ORDER BY calls DESC LIMIT 3",
        }
    )
    assert "RANKING_NOT_ORDERED" not in {f.code for f in mechanical_flags(case)}


def test_a_genuine_per_group_question_with_a_limit_is_flagged():
    """The true-positive half of `PER_GROUP_WITH_LIMIT`.

    Narrowing the pattern to remove its false positives could equally have stopped it
    firing at all, and two negative tests would happily pass against a check that is
    silently dead. This is the test that would notice.
    """
    case = _case("q01").model_copy(
        update={
            "question": "What is the average berth wait for each terminal?",
            "ordered": False,
            "gold_sql": "SELECT terminal_name, AVG(w) FROM x GROUP BY 1 LIMIT 5",
        }
    )
    assert "PER_GROUP_WITH_LIMIT" in {f.code for f in mechanical_flags(case)}


def test_a_per_group_question_without_a_limit_is_not_flagged():
    """The other half of `PER_GROUP_WITH_LIMIT`. Without this, dropping the LIMIT
    requirement from the check would flag every per-group question and the suite would
    stay green, because both existing negatives vary the question rather than the SQL."""
    case = _case("q01").model_copy(
        update={
            "question": "What is the average berth wait for each terminal?",
            "ordered": False,
            "gold_sql": "SELECT terminal_name, AVG(w) FROM x GROUP BY 1",
        }
    )
    assert "PER_GROUP_WITH_LIMIT" not in {f.code for f in mechanical_flags(case)}


def test_a_singular_superlative_without_limit_is_flagged():
    """The q05 defect class: 'which operator waits longest' answered with the whole
    ranking. Recorded in ADR-006 as a real failure this triage should surface."""
    case = _case("q01").model_copy(
        update={
            "question": "Which terminal has the longest average berth wait?",
            "gold_sql": "SELECT t.terminal_name FROM terminals t ORDER BY 1",
        }
    )
    assert "SUPERLATIVE_NO_LIMIT" in {f.code for f in mechanical_flags(case)}


def test_a_per_group_question_is_not_treated_as_a_superlative():
    """'the longest wait for each terminal' is a per-group question, so LIMIT would be
    wrong. Flagging it would send attention to a case that is correct by construction."""
    case = _case("q01").model_copy(
        update={
            "question": "What is the longest average berth wait for each terminal?",
            "gold_sql": "SELECT t.terminal_name FROM terminals t ORDER BY 1",
        }
    )
    assert "SUPERLATIVE_NO_LIMIT" not in {f.code for f in mechanical_flags(case)}


def test_a_year_named_in_the_question_but_absent_from_the_sql_is_flagged():
    case = _case("q01").model_copy(
        update={
            "question": "How many containers moved in 2025?",
            "gold_sql": "SELECT SUM(container_count) FROM cargo_moves",
            "ordered": False,
        }
    )
    assert "YEAR_NOT_IN_SQL" in {f.code for f in mechanical_flags(case)}


def test_a_rate_is_not_mistaken_for_a_per_group_question():
    """'containers per hour' is a rate, so a LIMIT is correct and the case needs no
    attention. This fired on q06 before the pattern was narrowed."""
    case = _case("q01").model_copy(
        update={
            "question": "Which crane at Rotterdam moves the fewest containers per hour?",
            "gold_sql": "SELECT crane_id FROM cranes ORDER BY 1 LIMIT 1",
        }
    )
    assert "PER_GROUP_WITH_LIMIT" not in {f.code for f in mechanical_flags(case)}


def test_a_ranking_dimension_is_not_mistaken_for_a_per_group_question():
    """'top 5 vessels by container capacity' ranks by a dimension; LIMIT is the point.
    This fired on q10, q48 and q69 before the pattern was narrowed."""
    case = _case("q01").model_copy(
        update={
            "question": "What were the top 5 vessels by container capacity?",
            "gold_sql": "SELECT vessel_name FROM vessels ORDER BY teu DESC LIMIT 5",
        }
    )
    assert "PER_GROUP_WITH_LIMIT" not in {f.code for f in mechanical_flags(case)}


def test_a_quantity_that_looks_like_a_year_is_not_flagged():
    """q46 asks the agent to confirm 'around 2000 port calls'. The number is the claim
    under test, not a period, so demanding it appear in the SQL is noise."""
    case = _case("q01").model_copy(
        update={
            "question": "I heard there are around 2000 port calls in the system. Can you confirm?",
            "gold_sql": "SELECT COUNT(*) FROM port_calls",
            "ordered": False,
        }
    )
    assert "YEAR_NOT_IN_SQL" not in {f.code for f in mechanical_flags(case)}


def test_a_year_present_in_the_sql_is_not_flagged():
    case = _case("q01").model_copy(
        update={
            "question": "How many containers moved in 2025?",
            "gold_sql": "SELECT SUM(container_count) FROM cargo_moves WHERE year = 2025",
            "ordered": False,
        }
    )
    assert "YEAR_NOT_IN_SQL" not in {f.code for f in mechanical_flags(case)}


def test_an_average_question_computed_without_avg_is_flagged():
    """The q54 defect class: the question said average, the reference used a different
    divisor, and the wording had to change rather than the SQL."""
    case = _case("q01").model_copy(
        update={
            "question": "What is the average number of moves per call?",
            "gold_sql": "SELECT SUM(moves) / COUNT(*) FROM port_calls",
            "ordered": False,
        }
    )
    assert "AVERAGE_WITHOUT_AVG" in {f.code for f in mechanical_flags(case)}


def test_an_average_question_using_avg_is_not_flagged():
    case = _case("q01").model_copy(
        update={
            "question": "What is the average berth wait?",
            "gold_sql": "SELECT AVG(berth_wait_hours) FROM port_calls",
            "ordered": False,
        }
    )
    assert "AVERAGE_WITHOUT_AVG" not in {f.code for f in mechanical_flags(case)}


def test_static_triage_runs_without_a_database():
    """The worksheet must still be generatable with no connection, so every static check
    has to work from the YAML alone."""
    for case in _answerable():
        mechanical_flags(case, None)


class _Result:
    def __init__(self, row_count: int, columns: list[str], truncated: bool = False):
        self.row_count = row_count
        self.columns = columns
        self.rows = [[None] * len(columns) for _ in range(row_count)]
        self.truncated = truncated


def test_an_empty_result_is_flagged_as_degenerate_agreement():
    """ADR-006 names this: two queries both returning zero rows compare equal, so the
    case would pass without testing anything."""
    codes = {f.code for f in mechanical_flags(_case("q01"), _Result(0, ["a"]))}
    assert "EMPTY_RESULT" in codes


def test_a_single_scalar_result_is_flagged_as_the_weakest_comparison():
    codes = {f.code for f in mechanical_flags(_case("q01"), _Result(1, ["a"]))}
    assert "SINGLE_SCALAR" in codes


def test_a_truncated_reference_result_is_flagged():
    codes = {f.code for f in mechanical_flags(_case("q01"), _Result(5, ["a", "b"], truncated=True))}
    assert "TRUNCATED" in codes


def test_an_ordinary_result_carries_none_of_the_result_shape_flags():
    """The negative half of all three result-shape checks at once.

    `SINGLE_SCALAR` fires on one row AND one column. If that `and` ever became an `or`,
    every single-row and every single-column case would flag, the triage would drown, and
    the three positive tests above would all still pass.
    """
    codes = {f.code for f in mechanical_flags(_case("q01"), _Result(5, ["a", "b"]))}
    assert {"EMPTY_RESULT", "SINGLE_SCALAR", "TRUNCATED"} & codes == set()


@pytest.mark.parametrize(("rows", "columns"), [(1, 2), (3, 1)])
def test_single_scalar_needs_both_one_row_and_one_column(rows: int, columns: int):
    result = _Result(rows, [f"c{i}" for i in range(columns)])
    assert "SINGLE_SCALAR" not in {f.code for f in mechanical_flags(_case("q01"), result)}


# --- The claim guard -----------------------------------------------------------------


def test_documented_coverage_matches_the_ledger():
    """No tracked document may quote a coverage figure the ledger cannot support.

    This is the reason the ledger is committed. The failure being prevented is specific
    and has already happened once in this repo's history with other figures: prose states
    a number, the underlying evidence changes, and nobody remembers the prose. Here the
    number is generated by `coverage_sentence`, so a stale claim fails the suite instead
    of surviving into a submission.
    """
    mismatches = _coverage_mismatches(TRACKED_DOCUMENTS, coverage())
    assert not mismatches, "\n".join(mismatches)


def test_every_sentence_discussing_the_audit_states_a_checkable_figure():
    """Closes the hole that made the guard above passable by saying nothing.

    `COVERAGE_PATTERN` matches exactly one phrasing. A claim written any other way, such
    as "roughly half the reference queries have been checked", produces no match, and a
    guard that looks for figures and finds none concludes that all is well. So the topic
    is detected separately from the figure: any sentence that discusses auditing
    reference queries must also carry a figure the guard can parse.
    """
    for path in TRACKED_DOCUMENTS:
        if not path.exists():
            continue
        for sentence in coverage_claims(path.read_text(encoding="utf-8")):
            assert COVERAGE_PATTERN.search(sentence), (
                f"{path.name} discusses the reference-query audit without stating a "
                f"checkable figure, so the coverage guard cannot police it: "
                f"{sentence.strip()!r}. Phrase it as: {coverage_sentence()}."
            )


def test_a_figure_wrapped_across_two_source_lines_is_still_read(tmp_path: Path):
    """The escape a probe found on the day `docs/EVAL.md` was written.

    Markdown wraps prose at the column, not at the sentence, so "0 of 77 reference queries"
    lands with a newline inside it about as often as not. The figure guard missed those
    entirely, and the sentence guard that did see them only requires *a* figure, so an
    overstated one passed both. The document that provoked this stated the true figure; the
    point is that it could have stated any figure at all.

    The second assertion is the reason reading sentences is still required now that
    `COVERAGE_PATTERN` tolerates the line break itself. Tolerating a break also tolerates a
    blank line, so over raw text a figure ending one block and the words opening the next
    join into a claim neither block makes. That would fail a document for prose it does not
    contain, which is the mirror of the defect this test is named for and would be the more
    annoying of the two to live with.
    """
    doc = tmp_path / "WRAPPED.md"
    doc.write_text(
        "Coverage today is **71 of 77 reference\nqueries** independently audited.\n",
        encoding="utf-8",
    )
    assert _stated_coverage(doc) == [(71, 77)]

    unrelated = tmp_path / "TWO_BLOCKS.md"
    unrelated.write_text(
        "The ledger records 3 of 77\n\nreference queries execute on every run.\n",
        encoding="utf-8",
    )
    assert _stated_coverage(unrelated) == [], (
        "a figure in one block was read as the subject of the next, so the guard would fail "
        "a document over a sentence it does not contain"
    )
    assert COVERAGE_PATTERN.findall(unrelated.read_text(encoding="utf-8")) == [("3", "77")], (
        "if raw text stops matching here, the sentence unit is no longer what prevents this "
        "and this half of the test proves nothing"
    )


def test_the_guard_itself_rejects_a_wrapped_figure_the_ledger_cannot_support(tmp_path: Path):
    """The test above proves the helper reads a wrapped figure. This proves the guard uses it.

    Both are needed, and the reason is measured: with only the helper pinned, reverting the
    guard's call site to raw-text scanning left every test in this file passing. A guard and
    a helper that agree in source but are asserted on separately can be separated again.
    """
    doc = tmp_path / "OVERSTATED.md"
    doc.write_text(
        "Coverage today is **71 of 77 reference\nqueries** independently audited.\n",
        encoding="utf-8",
    )
    assert _coverage_mismatches([doc], coverage())


@pytest.mark.parametrize(
    "claim",
    [
        "In addition, 41 of the 77 answerable cases have already been checked by hand.",
        "So far 41 of 77 answerable cases are audited.",
        "That covers 41 of the 77 reference SQL statements, all audited.",
        "We have audited 41 of 77 gold cases.",
    ],
)
def test_a_coverage_claim_that_renames_the_population_is_still_read(tmp_path: Path, claim: str):
    """The population has several names, and the guard must not be fooled by any of them.

    Measured on 2026-08-14, and the first of these four is the exact sentence that escaped:
    placed in `README.md` one paragraph below the correct guarded figure, it passed all 55
    tests in this file while telling the reader the opposite of the ledger. It carried a
    number and an audit word and was invisible only because it said "answerable cases"
    rather than "reference queries". "of the" defeated the pattern a second time, independent
    of the noun. Detecting the topic apart from the figure buys nothing when both are keyed
    to one phrasing, so the subject is now an alternation and this test holds it open.
    """
    doc = tmp_path / "RENAMED.md"
    doc.write_text(f"Coverage is under way. {claim}\n", encoding="utf-8")
    assert _coverage_mismatches([doc], coverage())


@pytest.mark.parametrize(
    "claim",
    [
        "So far 41 of 77 questions are audited.",
        "So far 41 of the 77 cases have been audited.",
        "That covers 41 of 77 questions, all independently checked.",
    ],
)
def test_a_coverage_claim_using_the_bare_population_nouns_is_read(tmp_path: Path, claim: str):
    """"Question" and "case" are what this project calls a gold entry nearly everywhere.

    Measured on 2026-08-14, after the subject list above was widened and thought finished:
    `gold_questions.yaml` names the population "questions", so a claim reaches for that word
    long before it reaches for "answerable cases", and "41 of 77 questions are audited"
    escaped every guard in this file. It is caught here only because the sentence also talks
    about scrutiny, which is the condition `coverage_figures` applies to the bare nouns and
    the reason they are not simply appended to `COVERAGE_PATTERN`.
    """
    doc = tmp_path / "BARE.md"
    doc.write_text(f"Notes on progress. {claim}\n", encoding="utf-8")
    assert _coverage_mismatches([doc], coverage())


@pytest.mark.parametrize(
    "sentence",
    [
        "25 of the 77 cases return a single scalar.",
        "13 of the 77 questions require four-table joins.",
        "2 of the 77 cases return zero rows deliberately.",
    ],
)
def test_a_figure_about_the_cases_that_is_not_about_auditing_them_is_left_alone(
    tmp_path: Path, sentence: str
):
    """The other half of the bare-noun contract, and the reason it is gated on an audit word.

    This documentation counts the cases constantly, by result shape, by join depth and by
    what they deliberately return empty. Reading "25 of the 77 cases" as a coverage claim
    would fail the suite over a sentence claiming nothing about scrutiny, and a guard that
    rejects true prose gets weakened by whoever next trips it. These three are real shapes
    from `docs/GOLD_AUDIT.md`'s triage section.
    """
    doc = tmp_path / "SHAPES.md"
    doc.write_text(f"Triage notes. {sentence}\n", encoding="utf-8")
    assert not _coverage_mismatches([doc], coverage())


@pytest.mark.parametrize(
    "claim",
    [
        "The reference SQL is hand-verified.",
        "The reference SQL is hand-checked throughout.",
        "Every reference query was manually reviewed before release.",
        "The gold set was verified by hand.",
        "Each query has been confirmed by a human.",
    ],
)
def test_a_manual_verification_claim_is_caught_in_any_of_its_spellings(claim: str):
    """One claim, many spellings, and only the first was ever detected.

    `hand[- ]verif` matched the wording nine sites in this repo happened to use, not the
    claim they made. On 2026-08-14 the other four here were measured passing the suite
    untouched while saying the same unsupported thing to a reader. A guard on a phrase
    expires the moment someone paraphrases; a guard on the claim does not.
    """
    assert MANUAL_VERIFICATION.search(claim)


@pytest.mark.parametrize(
    "innocent",
    [
        "The validator is checked by the security boundary tests.",
        "Reference SQL executes against the seeded data on every run.",
        "Disagreements are adjudicated individually.",
        "The ledger records who checked a case and by what method.",
    ],
)
def test_the_manual_verification_guard_leaves_ordinary_prose_alone(innocent: str):
    """The other half of the pattern's contract, without which it would be untunable.

    A guard broad enough to catch paraphrase is broad enough to fire on description, and a
    guard that fires on description gets weakened by whoever next trips it. These four are
    the shapes this document genuinely needs to be able to write: a component being tested,
    a mechanical property, the adjudication vocabulary, and the ledger's own description of
    itself. None of them claims a human audited the reference SQL.
    """
    assert not MANUAL_VERIFICATION.search(innocent)


def test_the_guard_does_not_invent_a_claim_out_of_two_adjacent_blocks(tmp_path, monkeypatch):
    """Pins the guard's WIRING, which the two tests around it cannot reach.

    They call `_coverage_mismatches` directly, so reverting the guard to scan raw text
    leaves both passing, and the tracked documents cannot tell the difference either now
    that `COVERAGE_PATTERN` tolerates a line break by itself. Measured: with only those
    tests present, the revert kept the file green, all 54 tests as it then stood.

    The one fixture that separates the two readings is a document where raw text finds a
    claim and sentences find none. A figure ending one block and the words opening the next
    is such a document, and it is the shape a revert would newly fail on, so this test fails
    exactly when the guard stops reading sentences. It asserts the guard stays SILENT, which
    is the harder direction to get right: the failure it prevents is a document rejected over
    prose it does not contain.

    The figure is DERIVED from the ledger rather than written as a literal. A hardcoded "3 of
    77" would agree with the truth on the day the ledger reaches three audited entries, and a
    reverted guard would pass this test for that one commit, on a coincidence rather than on
    the behaviour being pinned. `audited + 1` cannot coincide with `audited`.
    """
    cov = coverage()
    doc = tmp_path / "TWO_BLOCKS.md"
    doc.write_text(
        f"The ledger records {cov.audited + 1} of {cov.total}\n\n"
        "reference queries execute on every run.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "TRACKED_DOCUMENTS", [doc])
    test_documented_coverage_matches_the_ledger()


def test_the_guard_accepts_the_figure_the_ledger_does_support(tmp_path: Path):
    """The negative half. A guard that reported every document would satisfy the test above
    while failing the whole suite, so the true sentence has to be shown passing."""
    doc = tmp_path / "TRUTHFUL.md"
    doc.write_text(f"Coverage today is {coverage_sentence()}.\n", encoding="utf-8")
    assert _coverage_mismatches([doc], coverage()) == []


@pytest.mark.parametrize(
    "claim",
    [
        "Coverage is 71 of 77 reference-queries independently audited.",
        "## 71 Of 77 Reference Queries Independently Audited",
        "Coverage is 71 of 77 reference\nqueries independently audited.",
    ],
)
def test_a_figure_is_read_whatever_joins_the_two_words(claim: str):
    """A hyphen, a capital letter and a line break are all ways of writing the same claim.

    Each was measured escaping the pattern entirely on 2026-08-14, which is the same defect
    as the line wrap and not a separate one: the pattern assumed one literal separator in one
    case. A figure guard that misses a phrasing does not fail, it says nothing, and saying
    nothing is what let an overstated figure through in the first place.

    The claim is matched RAW, with no whitespace normalisation. An earlier version collapsed
    it first, which quietly gutted the third case: the newline never reached the pattern, so
    narrowing the separator class back to a hyphen and a literal space would have passed the
    one case named for the line break.
    """
    assert COVERAGE_PATTERN.findall(claim) == [("71", "77")]


def test_a_coverage_claim_in_unparseable_prose_is_detected():
    """Proves the detector above would actually fire, rather than passing because the
    tracked documents happen to contain no prose of this shape today."""
    unparseable = "Roughly half the reference queries have been audited by hand."
    assert coverage_claims(unparseable) == [unparseable]
    assert COVERAGE_PATTERN.search(unparseable) is None


def test_prose_that_merely_mentions_reference_queries_is_not_treated_as_a_claim():
    """The detector must not force a coverage figure into every sentence that happens to
    say 'reference query', or the rule becomes unworkable and gets deleted."""
    neutral = "Every reference query executes against the seeded data on every run."
    assert coverage_claims(neutral) == []


def test_every_shipped_document_discussing_the_audit_is_inside_the_guards():
    """A document outside `TRACKED_DOCUMENTS` is a document the guards cannot see.

    The three coverage guards each iterate that list, so a new file stating "we audited
    most of them" would satisfy all of them by being invisible. This is the same escape
    that a topic-blind regex allows, one level up: the pattern is right and the scope is
    wrong. Discovering the candidates from the filesystem means adding a document either
    puts it inside the guards or fails the suite.

    It found `docs/GOLD_AUDIT.md` on the day that file was written, and `docs/ADR/ADR-010`,
    whose claims are dated and therefore exempt. See `DATED`.
    """
    guarded = {p.resolve() for p in TRACKED_DOCUMENTS}
    unguarded = [
        path.relative_to(REPO_ROOT)
        for path in _deliverable_documents()
        if path.resolve() not in guarded and _unguarded_claims(path)
    ]
    assert not unguarded, (
        f"{unguarded} discuss the gold-set audit but are not in TRACKED_DOCUMENTS, so no "
        "coverage guard reads them. Add them to that list."
    )


def test_the_discovery_helper_excludes_prep_material_and_finds_the_deliverables():
    """The guard above is only as good as what it discovers, and a helper that silently
    returned nothing would make it pass forever."""
    found = {p.relative_to(REPO_ROOT).as_posix() for p in _deliverable_documents()}
    assert {"README.md", "docs/GOLD_AUDIT.md", "docs/ARCHITECTURE.md", "docs/DATA.md"} <= found
    assert "docs/video.md" not in found, "gitignored prep material must not ship"


def test_a_dated_claim_is_exempt_but_an_undated_one_is_not():
    """The exemption has to be narrow enough to still catch a live overclaim.

    Both sentences say the same unsupportable thing; only the dated one records rather
    than asserts, so only that one is allowed to stand without a figure.
    """
    undated = "Every reference query has been checked by hand."
    dated = "Every reference query was checked by hand on 2026-08-10."
    assert coverage_claims(undated) and not DATED.search(undated)
    assert coverage_claims(dated) and DATED.search(dated)


def test_a_date_does_not_exempt_a_document_that_is_not_a_decision_record(tmp_path):
    """The date exemption must not become the loophole.

    Every other guard iterates `TRACKED_DOCUMENTS`, so a document dropped by the discovery
    test is read by nothing at all. Allowing any file to buy its way out by carrying a date
    would let a new note claim the whole set was checked by hand and never be seen again.

    This writes real files and calls `_unguarded_claims`, the same function the discovery
    test uses. An earlier version restated the rule in a local closure and asserted against
    that, which passed whatever the production code did.
    """
    claim = "All of the reference queries have been checked by hand on 2026-08-13."
    assert coverage_claims(claim), "the sentence must register as a claim in the first place"
    assert DATED.search(claim), "and it must carry a date, or this proves nothing"

    record = tmp_path / "docs" / "ADR" / "ADR-099-example.md"
    nested = tmp_path / "docs" / "scratch" / "ADR" / "decoy.md"
    ordinary = tmp_path / "docs" / "NOTES.md"
    for path in (record, nested, ordinary):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Heading\n\n{claim}\n", encoding="utf-8")

    assert _unguarded_claims(record, root=tmp_path) == [], (
        "a decision record is amended by addendum, so a dated statement may stand"
    )
    assert _unguarded_claims(ordinary, root=tmp_path), (
        "ordinary prose may not buy an exemption by carrying a date"
    )
    assert _unguarded_claims(nested, root=tmp_path), (
        "a directory merely named ADR is not a decision record, and docs/**/*.md recurses"
    )


def test_a_failing_git_check_ignore_raises_rather_than_widening_the_scope(monkeypatch):
    """An empty stdout from a crashed git reads as 'nothing is ignored', which would
    silently reclassify every prep document as shipped. Loud beats wrong."""

    def broken(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(RuntimeError, match="check-ignore"):
        _deliverable_documents()


@pytest.mark.parametrize(
    "claim",
    [
        "Most of the reference queries have been checked and confirmed correct by hand.",
        "The reference queries were reviewed individually.",
        "Nearly every reference query has now been examined.",
    ],
)
def test_ordinary_synonyms_for_auditing_are_detected(claim: str):
    """The word list decides what the guard can see, so its gaps are the guard's gaps.

    An earlier version matched only "audit" and "verify" forms. "Checked" and "confirmed"
    are the ordinary way to say this, and `confirmed` is this module's own disposition
    name, so a claim phrased that way escaped both guards completely.
    """
    assert coverage_claims(claim) == [claim]


def test_an_unparseable_claim_cannot_hide_behind_a_parseable_one_on_the_next_line():
    """Markdown bullets usually carry no full stop, so splitting on sentence terminators
    alone merges a bullet with the line beneath it. The merged text would inherit the
    figure from its neighbour and satisfy the guard while the misleading claim went
    unread. Blocks are therefore separated before sentences are."""
    markdown = (
        "- Most of the reference queries have been audited\n"
        "- 0 of 77 reference queries independently audited.\n"
    )
    claims = coverage_claims(markdown)
    assert any(COVERAGE_PATTERN.search(c) is None for c in claims), (
        f"the unparseable bullet was absorbed into its neighbour: {claims}"
    )


def test_a_paragraph_wrapped_across_source_lines_is_read_as_one_sentence():
    """The mirror risk. Splitting on every newline would tear a wrapped sentence in half
    and lose the figure from the half that names the topic."""
    wrapped = "Coverage stands at 0 of 77 reference queries\nindependently audited today."
    claims = coverage_claims(wrapped)
    assert len(claims) == 1
    assert COVERAGE_PATTERN.search(claims[0]) is not None


def test_the_adjudicated_count_quoted_in_prose_still_matches_the_run_artefacts():
    """Pins the other number this change introduced.

    The documentation states that eleven answerable cases have been adjudicated after
    disagreeing with the agent. Unlike the coverage figure, that one is hand-counted from
    `eval/results/`, which is exactly the shape of claim that went stale as
    "hand-verified" in the first place. Recomputing it here means a future run, or a
    changed gold set, fails the suite instead of quietly falsifying the prose.
    """
    answerable = {c.id for c in _answerable()}
    disagreed: set[str] = set()
    for number in range(20, 27):
        path = REPO_ROOT / "eval" / "results" / f"run{number}.json"
        # Not a skip. These artefacts are committed on purpose, as the evidence behind
        # the published figures, so an absent one is a defect rather than a reason to
        # stop checking. Skipping would silently retire this guard exactly when the
        # evidence it polices had gone missing.
        assert path.exists(), f"{path.name} is missing; it is the evidence for this claim"
        for record in json.loads(path.read_text(encoding="utf-8")):
            if record.get("id") in answerable and not record.get("passed"):
                disagreed.add(record["id"])

    count = len(disagreed)
    expected = NUMBER_WORDS.get(count, str(count))

    # Two earlier attempts at this assertion were both escapable, and each escape is
    # worth naming because the pattern recurs. The first checked only that SOME document
    # carried the phrase, which passed when README was changed to "twelve" while ADR-006
    # still said "eleven": the documents contradicted each other and the untouched one
    # satisfied the check. The second asserted on every occurrence of the exact phrase,
    # which passed when the phrase itself was rewritten, because a sentence the regex
    # cannot parse is a sentence it cannot police. So the topic is detected separately
    # from the figure: any sentence about adjudicating answerable cases must state its
    # number in the checkable form, and that number must be the recomputed one.
    found = 0
    for path in TRACKED_DOCUMENTS:
        if not path.exists():
            continue
        for sentence in sentences(path.read_text(encoding="utf-8")):
            if not (ADJUDICATED_TOPIC.search(sentence) and ADJUDICATED_SUBJECT.search(sentence)):
                continue
            found += 1
            stated = ADJUDICATED_CLAIM.search(sentence)
            assert stated, (
                f"{path.name} states how many answerable cases were adjudicated in prose "
                f"this guard cannot check: {sentence.strip()!r}. Phrase it as "
                f"'{expected} of the 77 answerable cases'."
            )
            assert stated.group(1).lower() in {expected, str(count)}, (
                f"{path.name} says '{stated.group(1)} of the 77 answerable cases' were "
                f"adjudicated, but runs 20 to 26 show {count} ({sorted(disagreed)}). "
                f"Every site must say {expected}."
            )

    assert found, (
        "no tracked document states how many answerable cases were adjudicated, so this "
        "test pins a figure that appears nowhere. Restore the claim or delete this test."
    )


def test_the_coverage_sentence_is_matched_by_the_pattern_that_polices_it():
    """A sentence the guard cannot parse would pass every document check by accident,
    which is the quiet way this whole mechanism could stop working."""
    match = COVERAGE_PATTERN.search(coverage_sentence())
    assert match is not None
    cov = coverage()
    assert (int(match.group(1)), int(match.group(2))) == (cov.audited, cov.total)


def test_unqualified_hand_verification_claims_are_absent_while_coverage_is_incomplete():
    """The claim this whole exercise exists to correct.

    While the ledger supports fewer than every case, no tracked document may say the
    reference SQL is hand-verified without qualification. The phrase is checked rather
    than the sentiment because the phrase is what a reader takes away, and it is what
    nine sites in this repo said before the ledger existed.

    Matching only the literal `hand-verif` was measured as too narrow on 2026-08-14: it is
    one spelling of a claim with many, and "hand-checked", "manually reviewed" and
    "verified by hand" all say the same unsupported thing to a reader while leaving the
    guard silent. The pattern below covers the claim rather than the wording, which is the
    same correction `COVERAGE_PATTERN`'s subject list carries for the figure.
    """
    cov = coverage()
    if cov.audited >= cov.total:
        pytest.skip("coverage is complete, so an unqualified claim is supportable")
    offenders = _manual_verification_offenders(TRACKED_DOCUMENTS)
    assert not offenders, (
        f"{offenders} claim hand-verified reference SQL, but the ledger supports only "
        f"{coverage_sentence(cov)}. Either audit the remaining cases or state the "
        f"measured figure."
    )


@pytest.mark.parametrize(
    "claim",
    [
        "The reference SQL is hand-verified.",
        "The reference SQL is hand-checked throughout.",
        "Every reference query was manually reviewed before release.",
    ],
)
def test_the_guard_itself_reads_a_paraphrased_manual_claim(tmp_path: Path, claim: str):
    """Pins the guard's WIRING, not just the pattern it is supposed to use.

    The two tests on `MANUAL_VERIFICATION` assert on the pattern in isolation, and that is
    not enough on its own. Measured on 2026-08-14: reverting the guard's call site to the
    old `hand[- ]verif` literal, while leaving the pattern and its tests untouched, kept
    every test in this file passing. A guard and a helper that agree in source but are
    asserted on separately can be separated again, which is the same lesson
    `test_the_guard_itself_rejects_a_wrapped_figure_the_ledger_cannot_support` records for
    the coverage figure.
    """
    doc = tmp_path / "CLAIMED.md"
    doc.write_text(f"# Notes\n\n{claim}\n", encoding="utf-8")
    assert _manual_verification_offenders([doc]) == [doc.name]


def test_the_manual_claim_guard_reads_the_documents_through_the_helper(tmp_path, monkeypatch):
    """Pins the guard's own body, which the helper tests above cannot reach.

    They call `_manual_verification_offenders` directly, so reverting the guard itself to an
    inline `hand[- ]verif` check leaves every one of them passing: the pattern still exists
    and is still asserted on, it is simply no longer what the guard consults. That is the
    same separation `test_the_guard_does_not_invent_a_claim_out_of_two_adjacent_blocks`
    exists to prevent for the coverage figure, and this file learned it there first.

    Driving the real guard against a document it must reject is what fails on the revert,
    because the phrasing chosen here is one the old literal cannot see.
    """
    cov = coverage()
    if cov.audited >= cov.total:
        pytest.skip("coverage is complete, so an unqualified claim is supportable")
    doc = tmp_path / "PARAPHRASED.md"
    doc.write_text("Every reference query was manually reviewed.\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "TRACKED_DOCUMENTS", [doc])
    with pytest.raises(AssertionError):
        test_unqualified_hand_verification_claims_are_absent_while_coverage_is_incomplete()
