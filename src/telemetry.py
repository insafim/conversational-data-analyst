"""The eval half of the observability page: what the committed runs in `eval/results/` say.

No Streamlit here, for the reason `src/notices.py` and `src/conversations.py` are also
free of it. The page renders what these functions return and decides nothing, so the
arithmetic can be asserted without a browser.

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

# `run25.json`, not `run25.meta.json` or `run25.log`. Anchored at both ends so the sibling
# metadata files that HANDOFF item 5 will add do not get read as runs.
_RUN_FILE = re.compile(r"^run(\d+)\.json$")


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


def _summarise(name: str, number: int, records: list[dict]) -> EvalRun:
    latencies = [float(r.get("elapsed_s") or 0.0) for r in records]
    outcomes: dict[str, int] = {}
    for record in records:
        outcome = record.get("outcome")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

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
