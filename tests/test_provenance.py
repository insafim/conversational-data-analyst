"""What an eval run records about what produced it.

The failure these guard against is not a crash. It is a run artefact that looks complete
and cannot answer "which prompts was this?", which is the state runs 1 to 25 are in and
the reason ADR-006's variance finding rests on memory rather than on evidence.

Two of these tests are doing real work rather than restating the implementation:

* `TestNoSecrets`. `Settings` holds three DSNs and every one contains a password. The
  metadata file is committed. An allowlist that silently became `asdict()` would leak
  them, and nothing else in the suite would notice.
* `TestDiscovery`. The registry finds prompt constants by introspection, so a prompt
  added later is covered without anyone remembering to add it. That property is the whole
  reason discovery was chosen over a list, so it is asserted directly.

No database, no network.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from dataclasses import fields
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import prompts  # noqa: E402
from src.config import Settings  # noqa: E402
from src.provenance import (  # noqa: E402
    capture,
    git_state,
    prompt_digest,
    prompt_hashes,
    sibling_meta_path,
    write_metadata,
)

# Every field of `Settings`, with a recognisable secret in each of the three DSNs. Kept at
# module level so `TestNoSecrets` can compare its keys against the dataclass's own fields
# as well as construct from it.
_SETTINGS_WITH_SECRETS = {
    "analyst_dsn": "host=localhost dbname=ports user=analyst_ro password=SECRET_ANALYST",
    "admin_dsn": "host=localhost dbname=ports user=postgres password=SECRET_ADMIN",
    "store_dsn": "host=localhost dbname=ports_app user=app_rw password=SECRET_STORE",
    "cheap_model": "anthropic/claude-haiku-4-5",
    "strong_model": "anthropic/claude-sonnet-5",
    "statement_timeout_ms": 5000,
    "row_cap": 500,
    "llm_timeout_s": 45,
    "max_sql_retries": 1,
    "max_question_chars": 2000,
    "history_turns": 3,
    "runtime_verification": False,
    "sql_reading": True,
}


def _capture(**overrides) -> dict:
    """A metadata capture with the arguments the harness supplies."""
    kwargs = {
        "verification": False,
        "reading": True,
        "argv": ["eval/run_eval.py", "--json", "eval/results/run26.json"],
        "case_ids": ["q1", "q2"],
    }
    kwargs.update(overrides)
    return capture(**kwargs)


class TestDiscovery:
    """The registry finds prompt constants itself rather than being told about them."""

    def test_every_prompt_constant_in_the_module_is_hashed(self) -> None:
        """The property that makes discovery worth having: a prompt added to
        `src/prompts.py` tomorrow is covered without anyone updating a list.

        The pattern is written out again here rather than imported from
        `src.provenance._CONSTANT_NAME`, and that duplication is the point. Importing the
        production regex would make this test agree with the implementation by
        construction, whatever the implementation became. Stating the rule independently
        is what lets the two disagree, which is the only way the test can fail.
        """
        expected = {
            name
            for name, value in vars(prompts).items()
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", name) and isinstance(value, str)
        }

        assert expected, "sanity: src/prompts.py should define upper-case string constants"
        assert set(prompt_hashes()) == expected

    def test_the_known_prompts_are_present(self) -> None:
        """Named explicitly so that a refactor deleting one is a failing test rather than
        a quietly smaller registry."""
        registry = prompt_hashes()

        for name in (
            "CONTEXTUALIZE_SYSTEM",
            "CLASSIFY_SYSTEM",
            "GENERATE_SQL_SYSTEM",
            "VERIFY_SYSTEM",
            "SUMMARIZE_SYSTEM",
            "RETRY_SUFFIX",
            "VERIFIER_SUFFIX",
        ):
            assert name in registry

    def test_non_prompts_are_excluded(self) -> None:
        """Imports, dunders and lower-case names are module furniture, not prompts."""
        module = SimpleNamespace(
            REAL_PROMPT="you are a helpful assistant",
            lowercase_helper="not a prompt",
            _PRIVATE="not a prompt",
            NOT_A_STRING=42,
            __doc__="module docstring",
        )

        assert set(prompt_hashes(module)) == {"REAL_PROMPT"}

    def test_hashes_are_full_sha256_of_the_exact_bytes(self) -> None:
        module = SimpleNamespace(A_PROMPT="answer the question")
        expected = hashlib.sha256(b"answer the question").hexdigest()

        assert prompt_hashes(module)["A_PROMPT"] == expected


class TestSensitivity:
    """What must and must not move the digest."""

    def test_a_changed_prompt_changes_its_hash_and_the_digest(self) -> None:
        before = SimpleNamespace(P="original text")
        after = SimpleNamespace(P="edited text")

        assert prompt_hashes(before)["P"] != prompt_hashes(after)["P"]
        assert prompt_digest(prompt_hashes(before)) != prompt_digest(prompt_hashes(after))

    def test_whitespace_is_not_normalised_away(self) -> None:
        """A trailing newline is part of what the model reads, so it is part of the
        identity of the prompt."""
        assert (
            prompt_hashes(SimpleNamespace(P="text"))["P"]
            != prompt_hashes(SimpleNamespace(P="text\n"))["P"]
        )

    def test_adding_a_prompt_moves_the_digest(self) -> None:
        one = SimpleNamespace(P="text")
        two = SimpleNamespace(P="text", Q="more text")

        assert prompt_digest(prompt_hashes(one)) != prompt_digest(prompt_hashes(two))

    def test_renaming_a_prompt_moves_the_digest_though_no_text_changed(self) -> None:
        """Deliberate over-sensitivity. Every individual hash is unchanged by a rename,
        but a rename can point a call site at a different constant, so the digest has to
        cover names as well as values."""
        before = SimpleNamespace(OLD_NAME="identical text")
        after = SimpleNamespace(NEW_NAME="identical text")

        assert prompt_digest(prompt_hashes(before)) != prompt_digest(prompt_hashes(after))

    def test_the_digest_is_stable_across_calls(self) -> None:
        """Two runs of unchanged prompts must produce the same digest, or comparing runs
        is impossible, which is the entire purpose."""
        assert prompt_digest(prompt_hashes()) == prompt_digest(prompt_hashes())

    def test_the_digest_does_not_depend_on_attribute_order(self) -> None:
        assert prompt_digest({"A": "1", "B": "2"}) == prompt_digest({"B": "2", "A": "1"})


class TestNoSecrets:
    """`Settings` carries three DSNs with passwords in them and this file is committed."""

    def test_every_settings_field_is_covered_by_the_test_below(self) -> None:
        """Guards the guard. `Settings` has no field defaults today, so adding one would
        break the constructor call below with a `TypeError` and be noticed. That safety is
        incidental: a field added later WITH a default would construct fine and quietly
        escape the leak test, which is the one place a new secret-bearing field would have
        been caught. Comparing against `fields()` fails either way."""
        assert {f.name for f in fields(Settings)} == set(_SETTINGS_WITH_SECRETS)

    def test_no_password_reaches_the_metadata(self) -> None:
        settings_with_secrets = Settings(**_SETTINGS_WITH_SECRETS)

        serialised = json.dumps(_capture(settings_obj=settings_with_secrets))

        for secret in ("SECRET_ANALYST", "SECRET_ADMIN", "SECRET_STORE"):
            assert secret not in serialised
        assert "password" not in serialised.lower()
        assert "dsn" not in serialised.lower()

    def test_the_recorded_config_keys_are_a_frozen_set(self) -> None:
        """The actual control, and the reason the two substring tests above are not it.

        Those tests look for `password`, `dsn` and the planted `SECRET_*` tokens. That
        catches the three DSNs, whose own field NAMES betray them. It would not catch a
        future field such as `webhook_token` carrying a value that resembles nothing in
        particular: it would serialise, match no substring, and ship.

        Freezing the key set is what makes `src/provenance.py`'s claim that adding a field
        to the allowlist "is therefore a deliberate act" true rather than aspirational.
        Widening the allowlist now requires editing this line, which is a reviewable diff.
        """
        assert set(_capture()["config"]) == {
            "cheap_model",
            "strong_model",
            "history_turns",
            "max_sql_retries",
            "row_cap",
            "statement_timeout_ms",
            "llm_timeout_s",
            "max_question_chars",
        }

    def test_the_real_settings_object_is_also_clean(self) -> None:
        """The test above proves the allowlist holds for a constructed object. This one
        proves it for the object the harness actually passes."""
        serialised = json.dumps(_capture()).lower()

        assert "password" not in serialised
        assert "dsn" not in serialised


class TestCapturedContent:
    def test_the_metadata_is_json_serialisable_without_a_default_encoder(self) -> None:
        """`eval/run_eval.py` writes the records with `default=str`, which turns an
        unserialisable value into a string instead of failing. The metadata is written
        without that fallback, so a non-serialisable value must raise here first."""
        json.dumps(_capture())

    def test_the_effective_switches_are_recorded_not_the_configured_ones(self) -> None:
        """`--verification` / `--no-reading` override settings (`eval/run_eval.py:244-259`).
        Recording the settings default would describe a pipeline the run did not use."""
        meta = _capture(verification=True, reading=False)

        assert meta["run"]["verification"] is True
        assert meta["run"]["reading"] is False

    def test_both_model_tiers_are_recorded(self) -> None:
        config = _capture()["config"]

        assert config["cheap_model"]
        assert config["strong_model"]

    def test_the_cases_actually_run_are_recorded(self) -> None:
        """With `--limit`, `--category` or `--id` the run covers a subset, and which
        subset is provenance rather than a result."""
        meta = _capture(case_ids=["q1", "q7", "q9"])

        assert meta["run"]["case_ids"] == ["q1", "q7", "q9"]
        assert meta["run"]["case_count"] == 3

    def test_the_invocation_is_recorded(self) -> None:
        meta = _capture(argv=["eval/run_eval.py", "--category", "adversarial"])

        assert meta["run"]["argv"] == ["eval/run_eval.py", "--category", "adversarial"]

    def test_the_gold_set_is_hashed(self) -> None:
        """The accuracy number means nothing without knowing which gold set produced it.
        ADR-010 took the set from 36 cases to 108."""
        from eval.gold import GOLD_PATH

        gold = _capture()["gold_set"]
        expected = hashlib.sha256(GOLD_PATH.read_bytes()).hexdigest()

        assert gold["sha256"] == expected

    def test_the_prompt_section_carries_both_the_registry_and_the_digest(self) -> None:
        section = _capture()["prompts"]

        assert section["digest"] == prompt_digest(prompt_hashes())
        assert section["hashes"] == prompt_hashes()

    def test_the_environment_records_what_talks_to_the_provider(self) -> None:
        """A provider behaviour change between litellm versions is otherwise invisible.

        Compared against the real installed versions rather than asserted truthy.
        `_package_version` returns None only when the package is absent, and litellm is a
        hard dependency, so a truthiness check here passes in every environment the suite
        can run in and would not notice the package name being misspelled.
        """
        environment = _capture()["environment"]

        assert environment["litellm"] == package_version("litellm")
        assert environment["python"] == platform.python_version()

    def test_capture_is_timestamped(self) -> None:
        assert _capture()["captured_at"].endswith("Z")


class TestGitState:
    def test_the_sha_and_the_dirty_flag_are_both_reported(self) -> None:
        """A dirty tree means the SHA does not identify the code that ran. Recording the
        SHA alone would overstate what the artefact pins down."""
        state = git_state(Path(__file__).resolve().parent.parent)

        assert re.fullmatch(r"[0-9a-f]{40}", state["sha"])
        assert isinstance(state["dirty"], bool)

    def test_dirty_tracks_the_tree_rather_than_being_a_constant(self, tmp_path) -> None:
        """The test above accepts any bool, so a hardcoded `dirty = False` satisfies it,
        and satisfies every other assertion in this file too. This one drives a real
        repository from clean to modified and back, which is what makes the flag's honesty
        checkable rather than assumed, and its honesty is the entire reason it is recorded.

        Committer identity is passed per-command so the test does not depend on, or
        inherit, whatever global git config the machine happens to have.
        """
        identity = [
            "-c", "user.email=t@example.com",
            "-c", "user.name=t",
            # Neutralised because it is a common global default. With signing on and no
            # usable key in the test environment, `git commit` exits non-zero and this
            # test fails for a reason that has nothing to do with what it asserts.
            "-c", "commit.gpgsign=false",
        ]

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
            )

        git("init", "-q")
        (tmp_path / "tracked.txt").write_text("original")
        git("add", "tracked.txt")
        git(*identity, "commit", "-q", "-m", "initial")

        clean = git_state(tmp_path)
        assert clean["dirty"] is False
        assert re.fullmatch(r"[0-9a-f]{40}", clean["sha"])

        (tmp_path / "tracked.txt").write_text("modified")
        assert git_state(tmp_path)["dirty"] is True

        # An UNTRACKED file counts too. `git status --porcelain` lists it, and it should:
        # a new module sitting in the tree is code that could have run.
        (tmp_path / "tracked.txt").write_text("original")
        assert git_state(tmp_path)["dirty"] is False
        (tmp_path / "untracked.txt").write_text("new file")
        assert git_state(tmp_path)["dirty"] is True

    def test_the_sha_is_the_commit_git_itself_reports(self, tmp_path) -> None:
        """Guards against a plausible-looking value that is not actually HEAD."""
        identity = [
            "-c", "user.email=t@example.com",
            "-c", "user.name=t",
            # Neutralised because it is a common global default. With signing on and no
            # usable key in the test environment, `git commit` exits non-zero and this
            # test fails for a reason that has nothing to do with what it asserts.
            "-c", "commit.gpgsign=false",
        ]
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", *identity, "commit", "-q", "-m", "c"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()

        assert git_state(tmp_path)["sha"] == expected

    def test_a_directory_outside_a_repository_reports_no_sha_rather_than_raising(
        self, tmp_path
    ) -> None:
        """A run from an exported tarball still has to produce its metadata."""
        state = git_state(tmp_path)

        assert state["sha"] is None
        assert state["dirty"] is None


class TestSiblingPath:
    def test_the_meta_sits_beside_the_run_it_describes(self) -> None:
        assert sibling_meta_path(Path("eval/results/run26.json")) == Path(
            "eval/results/run26.meta.json"
        )

    def test_the_sibling_name_is_not_read_as_a_run_by_the_observability_page(self) -> None:
        """The cross-module contract. `src/telemetry.py` anchors its pattern at both ends
        so these files are skipped; asserting it against the name this module actually
        produces is what makes that arrangement hold if either side changes."""
        from src.telemetry import _RUN_FILE

        produced = sibling_meta_path(Path("eval/results/run26.json")).name

        assert _RUN_FILE.match(produced) is None
        assert _RUN_FILE.match("run26.json") is not None

    def test_writing_produces_a_readable_sibling(self, tmp_path) -> None:
        results = tmp_path / "run26.json"
        results.write_text("[]")

        written = write_metadata(results, _capture())

        assert written == tmp_path / "run26.meta.json"
        assert json.loads(written.read_text())["prompts"]["digest"]

    def test_an_unwritable_destination_raises_rather_than_being_swallowed(
        self, tmp_path
    ) -> None:
        """The harness writes the records first and the sibling second, so a failure here
        happens after a completed run. Raising is still right: a silent miss produces a
        `runNN.json` with no provenance, which is indistinguishable from runs 1 to 25 and
        is exactly the state this module exists to end. Better a traceback beside a
        finished result file than a quiet return to the old failure.

        The destination is unwritable because its parent is a FILE, not because of
        permission bits. `chmod(0o500)` was tried first and rejected: root ignores the
        bits, so under any container running the suite as root the write would succeed and
        the test would fail with DID NOT RAISE, for a reason unrelated to what it asserts.
        A file where a directory is required fails for every user on every platform.
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, so nothing can be created inside it")
        results = blocker / "run26.json"

        with pytest.raises(OSError):
            write_metadata(results, _capture())

    def test_writing_does_not_touch_the_run_file(self, tmp_path) -> None:
        """The reason the design is a sibling at all: the 25 committed artefacts are the
        evidence behind published figures and must not be rewritten."""
        results = tmp_path / "run26.json"
        results.write_text('[{"id": "q1"}]')

        write_metadata(results, _capture())

        assert results.read_text() == '[{"id": "q1"}]'


@pytest.mark.integration
class TestHarnessWiring:
    """The harness really writes the sibling. Costs nothing: `ask` is replaced, and an
    adversarial case is scored on its outcome alone, so no gold SQL is executed either."""

    def test_a_run_with_json_output_also_writes_its_provenance(
        self, tmp_path, monkeypatch
    ) -> None:
        import eval.run_eval as harness
        from src.models import AgentResult, Outcome

        monkeypatch.setattr(
            harness,
            "ask",
            lambda question, **kwargs: AgentResult(
                question=question,
                outcome=Outcome.REFUSED,
                answer="I can only answer questions about the shipping data.",
            ),
        )
        results = tmp_path / "run99.json"
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_eval.py", "--category", "adversarial", "--limit", "2", "--json", str(results)],
        )

        harness.main()

        meta = json.loads((tmp_path / "run99.meta.json").read_text())
        assert meta["prompts"]["digest"] == prompt_digest(prompt_hashes())
        assert meta["run"]["case_count"] == 2
        assert meta["completed_at"]

    def test_no_json_output_means_no_orphan_metadata(self, tmp_path, monkeypatch) -> None:
        """Without `--json` there is no run for a sibling to sit beside."""
        import eval.run_eval as harness
        from src.models import AgentResult, Outcome

        monkeypatch.setattr(
            harness,
            "ask",
            lambda question, **kwargs: AgentResult(
                question=question, outcome=Outcome.REFUSED, answer="No."
            ),
        )
        monkeypatch.setattr(
            sys, "argv", ["run_eval.py", "--category", "adversarial", "--limit", "1"]
        )
        monkeypatch.chdir(tmp_path)

        harness.main()

        assert list(tmp_path.glob("*.meta.json")) == []
