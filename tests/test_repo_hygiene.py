"""What must not be in `docs/`, asserted rather than remembered.

Scoped to `docs/`, plus `sample/` and `.claude/` in the last test. That is where every piece
of preparation material in this repository has ever lived, and a repository-wide version of
the first test would fail every time anyone created any file anywhere before deciding to
commit it, which is too noisy to survive. The cost of that scope is stated rather than
hidden: **a prep file written outside `docs/` is not covered by anything here.**

This exists because of a near miss. On 2026-08-13 two files were found sitting untracked
AND unmatched by `.gitignore`: `docs/important_things_to_add.md`, which is rehearsed answers
to interview questions plus session ids from the tooling that wrote it, and
`docs/my_narration.md`, a spoken cut of the demo script. Each had a sibling of its own
category already excluded, the Q&A scripts for the first and `docs/video.md` for the second,
which is what made the omission easy to miss: the categories were handled, these two files
were not. A single `git add -A` before submission would have shipped both to the reviewer.

**Why the obvious test is the wrong one.** Pinning the current list of ignored filenames
against `git check-ignore` guards only against someone deleting a line that already exists.
That is not what happened and not what will happen: the failure was a NEW file, in a
category the list could not have anticipated, written by a session that had no reason to
think about `.gitignore` at all. A test that enumerates known names cannot catch an unknown
one.

So the invariant is the general one: **nothing under `docs/` may be both untracked and
unignored.** Every file there is either a deliverable, in which case it belongs in git, or
it is preparation, in which case it belongs in `.gitignore`. The state in between is the
only dangerous one, and it is the state a forgotten file sits in.

This is deliberately a test that can fail while you are working, in the window between
creating a document and deciding what it is. That window IS the risk, so being told about
it is the feature. The message names both remedies.

Not marked `integration`: it shells out to `git` and touches no database, no model and no
network. It skips rather than fails outside a git work tree, because a source archive of
this repository is a legitimate thing to run the suite in and has no index to consult.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The only things under `docs/` that ship. Everything else there is preparation material.
# `docs/ADR/` is the reasoning record and is the most load-bearing part of the deliverable,
# which is why the ignore rules are written as exact paths rather than as a `docs/*` sweep
# with exceptions: a sweep that went wrong would silently drop the ADRs.
DELIVERABLE_DOCS = {"DATA.md", "ARCHITECTURE.md"}


def _git(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments], cwd=REPO, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def in_a_work_tree() -> None:
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git work tree, so there is no index to check against")


def test_no_document_is_both_untracked_and_unignored(in_a_work_tree) -> None:
    """The invariant that would have caught the near miss.

    `--others` lists untracked files and `--exclude-standard` applies the ignore rules, so
    together they report exactly the files git would add on `git add -A` and that nobody
    has decided about. Under `docs/` that set must be empty.
    """
    listed = _git("ls-files", "--others", "--exclude-standard", "docs/")
    assert listed.returncode == 0, listed.stderr

    exposed = [line for line in listed.stdout.splitlines() if line.strip()]

    assert not exposed, (
        "these files under docs/ are untracked AND unignored, so `git add -A` would ship "
        f"them to the reviewer: {exposed}. Decide which they are: commit them if they are "
        "part of the deliverable, or add them to the preparation block in .gitignore if "
        "they are notes, a script, a rehearsal or anything else written for the candidate "
        "rather than for the audience."
    )


def test_the_deliverable_docs_are_tracked_and_not_ignored(in_a_work_tree) -> None:
    """The other direction: that the deliverables are still in the index.

    The first test is satisfied by an empty `docs/`, so something has to assert that the
    documents which are supposed to ship are present. This is that assertion.

    It is deliberately NOT a check that no over-broad ignore rule was added, which is what
    an earlier version of this docstring claimed. `git ls-files` reports the index, and
    ignore rules never apply to a path already in the index, so adding `docs/*` to
    `.gitignore` would not move this test at all. What it detects is an accidental
    UNTRACKING, by `git rm --cached` or by a path being dropped in a history rewrite, which
    is a real thing that has happened to this repository once.
    """
    tracked = _git("ls-files", "docs/")
    assert tracked.returncode == 0, tracked.stderr
    tracked_names = {Path(line).name for line in tracked.stdout.splitlines() if line.strip()}

    missing = DELIVERABLE_DOCS - tracked_names
    assert not missing, f"{missing} are deliverables but are not tracked"

    adrs = [line for line in tracked.stdout.splitlines() if line.startswith("docs/ADR/")]
    assert len(adrs) >= 14, (
        f"only {len(adrs)} ADRs are tracked; the reasoning record is the deliverable, so an "
        "ignore rule that swept docs/ would show up here first"
    )


def test_the_preparation_material_is_actually_excluded(in_a_work_tree) -> None:
    """Named files, because these are the ones whose exposure has a specific cost.

    The general invariant above already covers them while they exist on disk. This pins
    them by name so that removing an ignore line fails immediately rather than at the
    moment someone runs `git add -A`, and so the list reads as a statement of what must
    never ship.

    **Two assertions, because `check-ignore` alone gives a false all-clear.**
    `--no-index` makes the question purely about the patterns, so it answers "would git
    exclude this" for a path that is not on this machine, which is what lets the list name
    files a given checkout may not have. The cost is that it is blind to whether the path
    is already tracked. Measured on 2026-08-13 in a scratch repository: for a file that
    matches an ignore pattern AND is in the index, `check-ignore --no-index` exits 0,
    reporting it ignored, while the index-aware form exits 1. So a prep file that had been
    committed would satisfy the pattern check and ship anyway.

    The second assertion closes that, the same way the unrelated-project test below does:
    it asks the index directly. An ignore rule is a statement about what git will add,
    never about what git has already added.
    """
    must_not_ship = [
        "docs/problem.md",
        "docs/repo.md",
        "docs/video.md",
        "docs/deck.md",
        "docs/presentation.md",
        "docs/drill_brief.md",
        "docs/sql_study_guide.md",
        "docs/interview_qa.md",
        "docs/prep_card.md",
        "docs/important_things_to_add.md",
        "docs/my_narration.md",
        # Everything under `docs/visuals/`, not a representative of it. One directory rule
        # covers all three, so naming a subset would leave the rest unpinned if that rule
        # were ever narrowed to a per-file list. Listing two of the three was the first
        # attempt and was caught for exactly the reason the sentence above gives.
        "docs/visuals/pipeline.html",
        "docs/visuals/data.html",
        "docs/visuals/README.md",
    ]

    exposed = [
        path
        for path in must_not_ship
        if _git("check-ignore", "--no-index", "--quiet", path).returncode != 0
    ]
    assert not exposed, f"{exposed} are no longer excluded by .gitignore"

    tracked = _git("ls-files", "--", *must_not_ship)
    assert tracked.returncode == 0, tracked.stderr
    assert not tracked.stdout.strip(), (
        f"these are in the index despite matching an ignore rule, so they ship: "
        f"{tracked.stdout.split()}. Removing them needs `git rm --cached`, and if they "
        "have been pushed, the ignore rule does nothing for the copies already out there."
    )


def test_nothing_from_the_unrelated_project_is_tracked(in_a_work_tree) -> None:
    """`sample/` is reference material describing a different client's system.

    It was tracked once and was removed from history with `git-filter-repo`, along with
    `.claude/` and the preparation documents. That is the expensive remedy this file exists
    to make unnecessary, and it is the reason a `.gitignore` entry alone is not evidence:
    an ignore rule does nothing about what is already committed.
    """
    tracked = _git("ls-files", "sample/", ".claude/")
    assert tracked.returncode == 0, tracked.stderr
    assert not tracked.stdout.strip(), (
        f"these are tracked again: {tracked.stdout.split()}. An ignore rule does not "
        "untrack anything already in the index."
    )
