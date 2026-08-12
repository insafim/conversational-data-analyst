"""What produced an eval run: prompt state, models, configuration, code version.

## Why this exists

`eval/results/runNN.json` is a list of per-case records and nothing else. Runs 1 to 25
carry no run-level object at all, and two claims in the repository already depend on
information those files do not hold:

* [ADR-006](../docs/ADR/ADR-006-eval-execution-accuracy.md) states that runs 2 and 3
  "share identical code and prompts" and scored 95.5% against 86.4%. That sentence is the
  basis of the run-to-run variance finding, and nothing on disk can confirm or refute it.
* The same ADR calls the harness's output "a reproducible number". Reproducing a number
  requires knowing what produced it.

So this module answers one question about an artefact: which prompts, which models, which
configuration, which commit.

## Sibling files, not a rewrite

The metadata goes to `runNN.meta.json` beside `runNN.json`. The committed run files are
the evidence behind the figures in README, ARCHITECTURE and ADR-012; adding a field to
them would edit committed evidence so that it appears to have recorded something it never
did. `src/telemetry.py` anchors its `_RUN_FILE` pattern at both ends so these siblings are
skipped by the observability page.

Runs 1 to 25 are NOT backfilled. Their prompt state and commit are not recoverable, and a
metadata file containing guesses is worse than none, because it looks like a record.

## Provenance only, never results

The records say what happened; this says what produced it. No scores, no totals, no
latencies are copied in. A duplicated figure is a second source for one number and the two
can drift, which has already cost this repository once (the groundedness denominator).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from types import ModuleType
from typing import Any

from . import prompts
from .config import Settings, settings

# A prompt constant: module-level, upper-case, and a string. Imports, dunders, private
# helpers and the module docstring are furniture and are excluded.
_CONSTANT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

# `git rev-parse` on a healthy repository is instant. The bound is here so that a run
# cannot hang on a git process wedged behind a lock or a network-mounted worktree.
_GIT_TIMEOUT_S = 5


def prompt_hashes(module: ModuleType | Any = prompts) -> dict[str, str]:
    """sha256 of every prompt constant in `module`, keyed by name.

    Constants are DISCOVERED rather than listed. A hand-maintained list omits a prompt
    added later, and it omits it silently, which is precisely the failure this module
    exists to prevent.

    The bytes are hashed exactly, with no normalisation of whitespace or line endings. A
    trailing newline is part of what the model reads, so it is part of the identity of the
    prompt: the alternative is a registry that reports "unchanged" for an edit that
    changed the request sent to the provider.
    """
    return {
        name: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for name, value in sorted(vars(module).items())
        if _CONSTANT_NAME.fullmatch(name) and isinstance(value, str)
    }


def prompt_digest(hashes: dict[str, str]) -> str:
    """One string identifying a whole prompt state, for comparing two runs at a glance.

    Covers NAMES as well as values, so adding, removing or renaming a constant moves the
    digest even when no prompt text changed. That is deliberate over-sensitivity: a rename
    leaves every individual hash identical while being able to point a call site at a
    different constant, and a digest that missed it would report two genuinely different
    pipelines as the same.

    Sorted before hashing, so the digest is a property of the registry rather than of the
    order `vars()` happened to return.
    """
    lines = "\n".join(f"{name}={digest}" for name, digest in sorted(hashes.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def git_state(repo_root: Path) -> dict[str, Any]:
    """The commit the run was made from, and whether the tree was clean.

    `dirty` is reported rather than dropped. On a modified tree the SHA does not identify
    the code that ran, and an artefact recording only the SHA would overstate what it
    pins down.

    Returns nulls rather than raising when git is unavailable or the directory is not a
    repository. A run made from an exported tarball still has to produce its metadata; a
    missing commit is a fact about that run, not a reason to lose the rest of the record.
    """

    def run(*args: str) -> str | None:
        try:
            # Fixed argv and no shell: nothing here interpolates caller-supplied text.
            completed = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout

    sha = run("rev-parse", "HEAD")
    if sha is None:
        return {"sha": None, "dirty": None}

    status = run("status", "--porcelain")
    return {
        "sha": sha.strip(),
        # `status` failing where `rev-parse` succeeded leaves cleanliness unknown, which
        # is not the same as clean and must not be recorded as it.
        "dirty": bool(status.strip()) if status is not None else None,
    }


def _package_version(name: str) -> str | None:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


def _config_snapshot(settings_obj: Settings) -> dict[str, Any]:
    """The configuration that changes what a run measures.

    An EXPLICIT allowlist, never `dataclasses.asdict(settings_obj)`. `Settings` holds
    three DSNs and every one carries a password in the connection string, and this
    metadata is committed to a repository that goes to a reviewer. Enumerating the fields
    is the control that keeps credentials out of the artefact; `asdict` would put all
    three in the first time anyone used it, with no error and no test failure.

    Adding a field here is therefore a deliberate act. Anything secret-bearing must not be
    added at all.
    """
    return {
        "cheap_model": settings_obj.cheap_model,
        "strong_model": settings_obj.strong_model,
        "history_turns": settings_obj.history_turns,
        "max_sql_retries": settings_obj.max_sql_retries,
        "row_cap": settings_obj.row_cap,
        "statement_timeout_ms": settings_obj.statement_timeout_ms,
        "llm_timeout_s": settings_obj.llm_timeout_s,
        "max_question_chars": settings_obj.max_question_chars,
    }


def capture(
    *,
    verification: bool,
    reading: bool,
    argv: list[str],
    case_ids: list[str],
    settings_obj: Settings = settings,
) -> dict[str, Any]:
    """Assemble the provenance record. Called at the START of a run.

    Timing matters. The prompt constants were read into memory when the process imported
    them, so a snapshot taken after a fifteen-minute run would describe the working tree
    as it stands then rather than as it stood when the run loaded it. Capture at the
    beginning, write at the end.

    `verification` and `reading` are the EFFECTIVE values the harness resolved, not the
    configured ones: `eval/run_eval.py` lets `--verification` and `--no-reading` override
    `settings`, and recording the setting would describe a pipeline the run did not use.
    They repeat what each record already carries, because the metadata has to stand alone;
    a reader should not have to parse 108 records to learn how the run was configured.
    """
    # Function-local, and NOT because there is a cycle to break: `eval/gold.py` imports
    # nothing from `src/`, so a module-level import would work today. It is scoped here to
    # keep the layering one-way, so that importing `src.provenance` does not require
    # `eval/` to be present. This is the only Python-level reference to `eval/` anywhere
    # in `src/`; `src/telemetry.py` reaches the same directory by path, never by import.
    from eval.gold import GOLD_PATH

    repo_root = Path(__file__).resolve().parent.parent
    hashes = prompt_hashes()

    return {
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompts": {"digest": prompt_digest(hashes), "hashes": hashes},
        "gold_set": {
            "path": GOLD_PATH.name,
            "sha256": hashlib.sha256(GOLD_PATH.read_bytes()).hexdigest(),
        },
        "config": _config_snapshot(settings_obj),
        "run": {
            "verification": verification,
            "reading": reading,
            "case_count": len(case_ids),
            "case_ids": list(case_ids),
            "argv": list(argv),
        },
        "code": git_state(repo_root),
        "environment": {
            "python": sys.version.split()[0],
            "litellm": _package_version("litellm"),
        },
    }


def sibling_meta_path(results_path: Path) -> Path:
    """`eval/results/run26.json` -> `eval/results/run26.meta.json`."""
    return results_path.with_suffix(".meta.json")


def write_metadata(results_path: Path, metadata: dict[str, Any]) -> Path:
    """Write the sibling beside the run file it describes, and return where it went.

    Serialised WITHOUT `default=str`, unlike the records. The records contain model output
    of unpredictable type and a stringifying fallback is the right trade there; this
    structure is built entirely from values this module chose, so an unserialisable one is
    a bug in the allowlist and should raise rather than be quietly coerced into a string.
    """
    path = sibling_meta_path(results_path)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return path
