"""What must not be in `docs/`, asserted rather than remembered.

Scoped to `docs/`, plus `sample/` and `.claude/` in the last test. That is where every piece
of preparation material in this repository has ever lived, and a repository-wide version of
the first test would fail every time anyone created any file anywhere before deciding to
commit it, which is too noisy to survive. The cost of that scope is stated rather than
hidden: **a prep file written outside `docs/` is not covered by anything here.**

This exists because of a near miss. On 2026-08-13 two working documents were found sitting
untracked AND unmatched by `.gitignore`, one of them carrying session ids from the tooling
that wrote it. Each had a sibling of its own category already excluded, which is what made
the omission easy to miss: the categories were handled, these two files were not. A single
`git add -A` would have published both.

**Why the obvious test is the wrong one.** Pinning the current list of ignored filenames
against `git check-ignore` guards only against someone deleting a line that already exists.
That is not what happened and not what will happen: the failure was a NEW file, in a
category the list could not have anticipated, written by a session that had no reason to
think about `.gitignore` at all. A test that enumerates known names cannot catch an unknown
one.

So the invariant is the general one: **nothing under `docs/` may be both untracked and
unignored.** Every file there is either a deliverable, in which case it belongs in git, or
it is working material, in which case it belongs in `.gitignore`. The state in between is
the only dangerous one, and it is the state a forgotten file sits in. `.gitignore` now
resolves it by default: `docs/` is an allowlist, so a new name is excluded until someone
decides otherwise.

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

# The only things under `docs/` that ship. Everything else there is working material.
# `.gitignore` is now an allowlist (`docs/*` then named exceptions), so this set is the
# other half of that statement and the two are asserted against each other below. The
# earlier arrangement was a denylist, which failed twice in the way denylists do: a file
# arrived under a name no rule anticipated and sat untracked AND unignored.
DELIVERABLE_DOCS = {
    "docs/ARCHITECTURE.md",
    "docs/DATA.md",
    "docs/EVAL.md",
    "docs/GUARDRAILS.md",
    "docs/CHARTS.md",
    "docs/visuals/data.html",
    "docs/visuals/eval.html",
    "docs/visuals/guardrails.html",
    "docs/visuals/pipeline.html",
}


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
    tracked_names = {line.strip() for line in tracked.stdout.splitlines() if line.strip()}

    missing = DELIVERABLE_DOCS - tracked_names
    assert not missing, f"{missing} are deliverables but are not tracked"

    adrs = [line for line in tracked.stdout.splitlines() if line.startswith("docs/ADR/")]
    assert len(adrs) >= 14, (
        f"only {len(adrs)} ADRs are tracked; the reasoning record is the deliverable, so an "
        "ignore rule that swept docs/ would show up here first"
    )


def test_docs_defaults_to_excluded_for_a_name_no_rule_anticipated(in_a_work_tree) -> None:
    """The property an allowlist has and a denylist cannot have.

    The failure this replaces was never a deleted ignore line. Twice it was a NEW working
    document, in a category the list could not have anticipated, sitting untracked AND
    unignored, which is the one state where `git add -A` publishes it. A denylist cannot
    be tested against that, because the test would have to name the file nobody has
    written yet.

    An allowlist can: the question becomes whether an arbitrary new name under `docs/` is
    excluded by default, and that is answerable today. `--no-index` keeps the question
    purely about the patterns, so these paths need not exist on this machine.
    """
    invented = [
        "docs/some-note-nobody-has-written-yet.md",
        "docs/scratch/notes.md",
        "docs/visuals/an-extra-frame.html",
        "docs/ADR-draft.md",
    ]
    exposed = [
        path
        for path in invented
        if _git("check-ignore", "--no-index", "--quiet", path).returncode != 0
    ]
    assert not exposed, (
        f"{exposed} would not be excluded, so `docs/` is no longer default-deny. A new "
        "working document under any of these names would ship on the next `git add -A`."
    )


def test_nothing_under_docs_is_tracked_outside_the_allowlist(in_a_work_tree) -> None:
    """The other direction, and the one that catches an accident that already happened.

    `check-ignore` answers what git WILL add. It says nothing about what git has already
    added, and an ignore rule does not untrack a file: that needs `git rm --cached`. So a
    document committed before the rule existed stays in the index, ships, and satisfies
    every pattern assertion in this file. Asking the index directly is what closes it.
    """
    tracked = _git("ls-files", "docs/")
    assert tracked.returncode == 0, tracked.stderr
    paths = {line.strip() for line in tracked.stdout.splitlines() if line.strip()}

    unexpected = {
        path
        for path in paths
        if path not in DELIVERABLE_DOCS and not path.startswith("docs/ADR/")
    }
    assert not unexpected, (
        f"{sorted(unexpected)} are tracked under docs/ but are not deliverables. Either add "
        "them to DELIVERABLE_DOCS and to the allowlist in .gitignore, or remove them from "
        "the index with `git rm --cached`, which an ignore rule alone will not do."
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
