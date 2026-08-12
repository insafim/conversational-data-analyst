"""What the committed runs in `eval/results/` say, and how a measurement is written down.

No Streamlit here, for the reason `src/notices.py` and `src/conversations.py` are also
free of it. The page renders what these functions return and decides nothing, so the
arithmetic can be asserted without a browser.

Two jobs rather than one, and the second is why `src/notices.py` imports this module.
Reading the artefacts is the eval half of the observability page. Formatting a measurement
is shared by all three surfaces that state one: the eval table, the live panel, and the
disclosure under a single answer. A cost written three ways by three call sites is three
chances to disagree about whether half a cent is `$0.01` or `$0.0050`, so the rule is
written once here.

**Why read the artefacts rather than a database.** The eval runs are committed evidence
(ADR-010): they are the record that produced the figures in the README, they predate the
conversation store, and they are what a reviewer can check out and re-read. Copying them
into `ports_app` would create a second copy that can disagree with the file, and the file
is the one under version control.

**Why the two halves stay apart.** `Store.telemetry()` covers traffic this application
served; this covers a fixed 108-case benchmark. Averaging across both would describe
neither, so the page shows them as two sections and never as one total.

A run's own log file is not parsed. `runNN.log` is human-readable output whose format has
changed across runs, and re-deriving numbers from it would be a parser to maintain for
figures the JSON already carries exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import NamedTuple

from .models import Outcome

# `run25.json`, not `run25.meta.json` or `run25.log`. Anchored at both ends so the sibling
# metadata files that HANDOFF item 5 will add do not get read as runs.
_RUN_FILE = re.compile(r"^run(\d+)\.json$")

# What each gold-set category is called when it is shown to a reader, in the words the
# harness, the README and ADR-006 already use.
#
# The mapping lives here rather than in the page for one reason: "execution accuracy" is
# not a loose synonym for "the answerable score". It is the specific figure ADR-006 defines
# and the README quotes, scored on the answerable subset alone, and it is routinely
# confused with the overall score and with groundedness, which have different denominators.
# A page that invented its own label would put a fourth name for the same number into the
# repository, which is how a deck ends up quoting the wrong one.
CATEGORY_LABELS = {
    "answerable": "Execution accuracy",
    "ambiguous": "Ambiguity handling",
    "adversarial": "Safety",
}

# Every terminal state, in the order the guardrail panel shows them: what the system did
# on purpose first, then what was stopped, then what broke.
#
# `Outcome.REJECTED` is labelled by the thing that did the rejecting. "Rejected" alone
# invites the reading that the model declined, when it is `src/validator.py`, pure code,
# refusing to run a statement the model had already written (ADR-004). That distinction is
# the whole of the read-only argument, so the panel should not blur it.
OUTCOME_LABELS = {
    Outcome.ANSWERED: "Answered",
    Outcome.CLARIFY: "Clarified",
    Outcome.REFUSED: "Refused",
    Outcome.REJECTED: "Blocked by validator",
    Outcome.ERROR: "Errors",
}


def outcome_tiles(outcomes: dict[str, int]) -> list[tuple[str, int]]:
    """Every outcome with its count, in a fixed order, including the ones at zero.

    Zeros are kept, unlike the omitted categories in `labelled_categories`, and the
    difference is not an inconsistency. A run that contains no adversarial cases has no
    safety figure; a store in which nothing was ever blocked has a real and interesting
    figure, which is zero. Dropping it would make an untested guardrail look identical to
    an absent one, which is the exact confusion this panel exists to remove.
    """
    return [(label, outcomes.get(str(key), 0)) for key, label in OUTCOME_LABELS.items()]


def format_usd(amount: float) -> str:
    """An aggregate cost: what a store or an eval run spent in total.

    Two decimals with thousands separators, because `$1.2567` for a run total is noise.
    Below a cent it keeps four, since a total rendered as `$0.00` reads as free.
    """
    return f"${amount:.4f}" if amount < 0.01 else f"${amount:,.2f}"


def count_of(number: int, noun: str) -> str:
    """`4 model calls`, and `1 model call`.

    A trivial function for a defect that is not trivial to notice. Every counted noun in
    this application was written as `f"{n} {noun}s"`, which is right for all but one value
    and wrong for the most common single value there is. It was caught by replaying real
    run-26 records rather than by any test: a refusal and a clarification each make exactly
    one model call, so the two outcomes a demo is most likely to show were the two that read
    "1 model calls".

    Regular plurals only. Every noun this is used with takes an `s`, and a mapping of
    irregular forms would be scaffolding for a case that does not exist here.
    """
    return f"{number:,} {noun}" if abs(number) == 1 else f"{number:,} {noun}s"


def format_unit_usd(amount: float) -> str:
    """The cost of ONE question: this answer, or the mean across answers.

    Always four decimals, never rounded to the cent, and that is the whole difference
    between this and `format_usd`. The two were one function using magnitude as the rule,
    which worked only for as long as a single question stayed under a cent. It does not:
    an answered question on the shipped configuration costs about `$0.0135`, so the
    magnitude rule rendered the one figure a reviewer is most likely to check as `$0.01`,
    throwing away the precision that makes it worth showing at all.

    Found by writing the per-answer disclosure, not by reading: the smoke test asserting
    `$0.0142` was absent from the chat pane kept passing after the caption came back,
    because the number had quietly become `$0.01`.

    So the rule is the QUANTITY, not its size. A per-unit cost is a rate and keeps its
    resolution; a total is a sum and is rounded to money.
    """
    return f"${amount:.4f}"


class CategoryScore(NamedTuple):
    """How one gold-set category scored in one run.

    Both halves are carried rather than just the rate, because "19/19" and "100.0%" are
    different claims to a reader: the first says how much evidence there is and the second
    does not. A safety score of 100% over two cases would be the same number as over
    nineteen.
    """

    passed: int
    cases: int

    @property
    def rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0


@dataclass(frozen=True)
class EvalRun:
    """One committed eval run, summarised.

    `passed` and `grounded` are counted separately because they measure different things
    and moved in opposite directions when runtime verification was switched on (ADR-012).
    Collapsing them into one score would hide exactly the result that decision rests on.
    """

    name: str
    number: int
    cases: int
    passed: int
    grounded: int
    grounded_scored: int
    total_cost_usd: float
    median_latency_s: float
    outcomes: dict[str, int]
    categories: dict[str, CategoryScore]

    @property
    def pass_rate(self) -> float:
        """Over every case, which is what the harness prints as the overall score."""
        return self.passed / self.cases if self.cases else 0.0

    @property
    def grounded_rate(self) -> float:
        """Over the cases where groundedness was SCORED, not over every case.

        `eval/run_eval.py` only checks groundedness on an ANSWERED outcome and records
        `null` otherwise, then divides by the records that carry a real value. A refusal
        has no figures to ground, so counting it as ungrounded would punish the system for
        correctly declining. Dividing by every case instead reports run 25 as 68.5% where
        the harness, the README and ADR-012 all say 97.4%, which is the kind of number that
        destroys trust in a panel faster than having no panel at all.
        """
        return self.grounded / self.grounded_scored if self.grounded_scored else 0.0

    def labelled_categories(self) -> list[tuple[str, CategoryScore]]:
        """The category scores a reader is shown, in the order they are shown.

        Ordered by `CATEGORY_LABELS` rather than by the run's own key order, so the three
        figures sit in the same positions for every run and a reader comparing two runs is
        not re-reading the labels. A category the run does not contain is omitted rather
        than shown as zero, because a run that never included adversarial cases has not
        scored 0% on safety, it has no safety figure at all.
        """
        return [
            (label, self.categories[key])
            for key, label in CATEGORY_LABELS.items()
            if key in self.categories
        ]


def _summarise(name: str, number: int, records: list[dict]) -> EvalRun:
    latencies = [float(r.get("elapsed_s") or 0.0) for r in records]
    outcomes: dict[str, int] = {}
    for record in records:
        outcome = record.get("outcome")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

    # Scored per category, because the three figures the brief asks about are three
    # different denominators over the same file and the overall score hides all of them.
    # An adversarial case "passing" means the system refused or blocked it, which is why
    # safety can read 100% in a run whose overall score is 94%.
    passed_by_category: dict[str, int] = {}
    cases_by_category: dict[str, int] = {}
    for record in records:
        category = record.get("category")
        if not category:
            continue
        cases_by_category[category] = cases_by_category.get(category, 0) + 1
        if record.get("passed") is True:
            passed_by_category[category] = passed_by_category.get(category, 0) + 1
    categories = {
        category: CategoryScore(passed_by_category.get(category, 0), cases)
        for category, cases in cases_by_category.items()
    }

    return EvalRun(
        name=name,
        number=number,
        cases=len(records),
        # `passed` and `grounded` are read with `is True` rather than for truthiness. A
        # record that predates a field has it absent, and `None` must count as "not
        # measured" rather than silently as a pass.
        passed=sum(1 for r in records if r.get("passed") is True),
        grounded=sum(1 for r in records if r.get("grounded") is True),
        grounded_scored=sum(1 for r in records if r.get("grounded") is not None),
        total_cost_usd=sum(float(r.get("cost_usd") or 0.0) for r in records),
        median_latency_s=median(latencies) if latencies else 0.0,
        outcomes=outcomes,
        categories=categories,
    )


def load_eval_runs(directory: Path) -> list[EvalRun]:
    """Every `runNN.json` in `directory`, newest run first.

    Sorted by the number in the filename, not by name or mtime: `run9` sorts after `run25`
    lexically, and mtime reorders the set whenever the repository is cloned.

    A file that cannot be read is skipped rather than raised. Run 18 is committed and
    deliberately invalid, kept as the record of a local DNS outage (ADR-010), so a
    malformed artefact is an anticipated state of this directory and not an error the page
    should fail on.
    """
    if not directory.is_dir():
        return []

    runs: list[EvalRun] = []
    for path in directory.iterdir():
        match = _RUN_FILE.match(path.name)
        if not match:
            continue
        try:
            records = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Every element must be a mapping, not just the top level a list. Checking only
        # the outer type let `[1, 2, 3]` through to `_summarise`, where `.get` on an int
        # raised and took the whole page down: valid JSON of the wrong shape defeated a
        # guard written for invalid JSON, which is the narrower failure of the two.
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            continue
        runs.append(_summarise(path.stem, int(match.group(1)), records))

    return sorted(runs, key=lambda run: run.number, reverse=True)
