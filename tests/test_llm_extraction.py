"""Tests for parsing model output (src/llm.py).

Extraction is code rather than trust: models wrap SQL in fences most of the time, add
prose around it some of the time, and occasionally do neither. These functions must be
total — return something usable, or raise. Silently returning a half-parsed value would
let a caller act on a field that was never actually read.

No network and no database: these are pure string functions.
"""

from __future__ import annotations

import pytest

from src.llm import LLMError, extract_json, extract_sql


class TestExtractSQL:
    def test_fenced_sql_block(self) -> None:
        assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_fenced_block_without_a_language_tag(self) -> None:
        assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"

    def test_prose_around_the_fence_is_discarded(self) -> None:
        text = "Here is the query:\n```sql\nSELECT COUNT(*) FROM terminals\n```\nHope that helps!"
        assert extract_sql(text) == "SELECT COUNT(*) FROM terminals"

    def test_bare_sql_with_no_fence(self) -> None:
        assert extract_sql("SELECT 1") == "SELECT 1"

    def test_trailing_semicolon_is_stripped(self) -> None:
        """The executor wraps the query in a subquery, where a semicolon is a syntax error."""
        assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"

    def test_multiline_sql_is_preserved(self) -> None:
        sql = extract_sql(
            "```sql\nSELECT t.terminal_name,\n       AVG(pc.berth_wait_hours)\n"
            "FROM port_calls pc\nJOIN terminals t ON t.terminal_id = pc.terminal_id\n"
            "GROUP BY 1\n```"
        )
        assert sql.startswith("SELECT t.terminal_name,")
        assert sql.endswith("GROUP BY 1")

    def test_extraction_failure_still_produces_something_the_validator_can_reject(self) -> None:
        """Extraction never needs to be perfect, because its output is validated next.
        Garbage in, rejection out — not garbage in, execution out."""
        from src.validator import validate_sql

        assert not validate_sql(extract_sql("I'm sorry, I cannot help with that.")).ok


class TestExtractJSON:
    def test_bare_json_object(self) -> None:
        assert extract_json('{"route": "answerable"}') == {"route": "answerable"}

    def test_fenced_json_block(self) -> None:
        assert extract_json('```json\n{"route": "ambiguous"}\n```')["route"] == "ambiguous"

    def test_prose_around_the_object(self) -> None:
        text = 'Sure! Here is my classification:\n{"route": "out_of_scope", "reason": "no"}'
        assert extract_json(text)["route"] == "out_of_scope"

    def test_nested_object_is_preserved(self) -> None:
        parsed = extract_json('{"route": "ambiguous", "meta": {"confidence": 0.4}}')
        assert parsed["meta"]["confidence"] == 0.4

    @pytest.mark.parametrize("text", ["not json at all", "", "[1, 2, 3]", "{broken"])
    def test_unparseable_input_raises_rather_than_returning_a_partial(self, text: str) -> None:
        """A caller must never receive a dict that quietly lost fields."""
        with pytest.raises(LLMError):
            extract_json(text)
