"""Audit ledger for the gold set itself (ADR-006).

Every accuracy figure this project publishes is computed by comparing the agent's rows
against `gold_sql` in `gold_questions.yaml`. That makes the reference SQL the measuring
instrument, and until now the instrument was the one thing in the repo that was never
measured. `ADR-006` already names the failure: "if the reference SQL is itself wrong,
agreement is meaningless."

Agreement between the agent and the reference is **weak evidence** that the reference is
right, because the two are not independent. Both encode the same reading of the same
question against the same schema, so a question misread the same way twice produces two
matching wrong answers and a passing score. Both gold defects found so far, `q54` and
`q61`, were exactly this: the SQL was valid and the *question* meant something else.

So this module supports an audit whose evidence is independent of the harness:

* **The ledger** (`gold_audit.yaml`) records one entry per reference query, naming who
  checked it, by what method, and what they concluded. It is committed evidence, so a
  reader can see the audit's coverage rather than take the word "verified" on trust.
* **The triage** (`mechanical_flags`) does the part a human should not spend attention
  on. It cannot tell whether a query answers its question, but it can spot the shapes
  that historically preceded a defect, so scarce reading time goes to the suspicious
  cases first.
* **The coverage report** (`coverage`) is the *only* sanctioned source for any number
  the documentation quotes about this audit. Documentation states what the ledger can
  support and nothing more, which is why `coverage_sentence` exists and why
  `tests/test_gold_audit.py` pins any published figure to it.

**The flags are prompts for attention, not defects.** A flagged case is very often
correct and deliberately so: `q25` and `q56` return zero rows on purpose, because a
question about a period outside data coverage *should* return nothing. `EMPTY_RESULT`
fires on both, and the right disposition for them is `confirmed`. Treating a flag as a
finding would be the same error as treating agreement as verification, in the opposite
direction.

    python eval/audit.py status
    python eval/audit.py worksheet --out /tmp/gold_audit_worksheet.md
    python eval/audit.py record q01 confirmed --method rederived --note "..."
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.gold import GOLD_PATH, AnswerableCase, load_gold_set  # noqa: E402

__all__ = [
    "AUDIT_PATH",
    "AuditEntry",
    "AuditCoverage",
    "Flag",
    "coverage",
    "coverage_sentence",
    "load_audit",
    "mechanical_flags",
    "record_entry",
    "sentences",
]

AUDIT_PATH = Path(__file__).parent / "gold_audit.yaml"

#: How the reference query was checked. The distinction is the whole point of the
#: ledger: re-reading one's own SQL is not evidence, because the misreading that
#: produced the query produces the same reading on review. Only the first three
#: methods establish anything independent of the query text.
Method = Literal[
    "rederived",      # answer recomputed by a differently shaped query, then compared
    "seed_constant",  # result checked against the value db/seed.py plants
    "counted",        # small result counted by hand from the underlying rows
    "intent_only",    # question-to-SQL reading only; NOT independent, recorded honestly
]

Disposition = Literal[
    "confirmed",  # the reference query answers its question correctly
    "defect",     # the reference query is wrong and the gold set needs a fix
    "unsure",     # examined, not resolved; needs a second look
]


class AuditEntry(BaseModel):
    """One human's verdict on one reference query.

    `frozen` and `extra="forbid"` for the same reason `eval/gold.py` sets them: a
    misspelled key here would silently drop a verdict and inflate coverage, and coverage
    is quoted in the documentation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="Gold case id, must exist and be answerable.")
    disposition: Disposition
    method: Method
    auditor: str = Field(min_length=1, description="Who checked it. Named, not anonymous.")
    checked_on: date = Field(description="Date of the check, for staleness against gold edits.")
    note: str = Field(
        default="",
        description="What was done and what was concluded. Required for anything that is "
        "not a plain confirmation, because a recorded defect with no explanation cannot "
        "be acted on later.",
    )

    @model_validator(mode="after")
    def _unresolved_verdicts_carry_their_reasoning(self) -> AuditEntry:
        """A `defect` or `unsure` with no note is a dead end for whoever reads it next,
        and both dispositions exist precisely to be picked up later."""
        if self.disposition in ("defect", "unsure") and not self.note.strip():
            raise ValueError(f"{self.id}: disposition '{self.disposition}' requires a note")
        return self


class Flag(BaseModel):
    """A mechanical observation about a case, raised for human attention.

    `extra="forbid"` is omitted here and on `AuditCoverage`, unlike `AuditEntry` and the
    models in `eval/gold.py`. The asymmetry is deliberate rather than an oversight: those
    models are parsed from a file a human edits, where a misspelled key is the failure
    that matters, while these two are only ever constructed in this process.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class AuditCoverage(BaseModel):
    """What the ledger can currently support. The single source for published figures."""

    model_config = ConfigDict(frozen=True)

    total: int
    audited: int
    confirmed: int
    defects: int
    unsure: int
    remaining: list[str]


def load_audit(path: Path | None = None, *, gold_path: Path | None = None) -> list[AuditEntry]:
    """Parse and validate the ledger.

    Cross-checks every id against the gold set rather than trusting the ledger alone: an
    entry for a renamed or deleted case would otherwise keep counting toward coverage
    forever, which is the one way this file could come to overstate the audit.
    """
    target = path or AUDIT_PATH
    raw: Any = yaml.safe_load(target.read_text(encoding="utf-8")) if target.exists() else []
    entries = TypeAdapter(list[AuditEntry]).validate_python(raw or [])

    gold = load_gold_set(gold_path or GOLD_PATH)
    answerable = {c.id for c in gold if isinstance(c, AnswerableCase)}
    unknown = sorted({e.id for e in entries} - answerable)
    if unknown:
        raise ValueError(f"ledger references ids that are not answerable gold cases: {unknown}")

    seen: set[str] = set()
    duplicates = sorted({e.id for e in entries if e.id in seen or seen.add(e.id)})  # type: ignore[func-returns-value]
    if duplicates:
        raise ValueError(f"ledger has duplicate entries for: {duplicates}")
    return entries


def coverage(
    entries: list[AuditEntry] | None = None, *, gold_path: Path | None = None
) -> AuditCoverage:
    """Summarise the ledger against the full answerable set."""
    ledger = entries if entries is not None else load_audit(gold_path=gold_path)
    cases = load_gold_set(gold_path or GOLD_PATH)
    answerable = [c for c in cases if isinstance(c, AnswerableCase)]
    by_id = {e.id: e for e in ledger}
    return AuditCoverage(
        total=len(answerable),
        audited=len(by_id),
        confirmed=sum(1 for e in ledger if e.disposition == "confirmed"),
        defects=sum(1 for e in ledger if e.disposition == "defect"),
        unsure=sum(1 for e in ledger if e.disposition == "unsure"),
        remaining=[c.id for c in answerable if c.id not in by_id],
    )


#: The exact phrasing any document must use to state audit coverage. Generated rather
#: than written, so the figure in prose cannot drift from the evidence behind it;
#: `tests/test_gold_audit.py` fails the suite if a tracked document disagrees with this.
#:
#: The separator between the two words is a CLASS, not a space, and the case is ignored.
#: Both were literal until 2026-08-14, when `docs/EVAL.md` stated its figure and markdown
#: wrapped the line between "reference" and "queries". The pattern matched nothing, and a
#: guard that looks for figures and finds none reads that as nothing being wrong, so a
#: mutation to "71 of 77" left the suite green.
#:
#: Which repair closes which escape is worth being exact about, because the wrap is the one
#: that was found first and it is NOT the one this class fixes. Reading sentences collapses a
#: line break before any pattern runs, so the wrap would be caught by the old literal-space
#: pattern too. What the class fixes is a hyphen, and `re.IGNORECASE` a title-cased heading;
#: both were measured escaping every guard in silence, and neither is touched by collapsing
#: whitespace. The pattern's own tolerance of a newline is therefore redundant in the guard
#: pipeline, which always reads through `sentences()` first. It is not unguarded, though, and
#: two tests hold it: the raw-text half of
#: `test_a_figure_wrapped_across_two_source_lines_is_still_read`, which uses it to prove the
#: sentence unit is what prevents the paragraph-spanning match described below, and the third
#: case of `test_a_figure_is_read_whatever_joins_the_two_words`, which matches a raw embedded
#: newline directly. An earlier version of this comment named only the first, which was wrong
#: on the day it was written: the second test acquired that property in the same change, when
#: whitespace normalisation was removed from it.
#:
#: Widening the separator is why the guard in `tests/test_gold_audit.py` must read
#: SENTENCES rather than the raw file. Tolerating a line break between the two words also
#: tolerates a blank line, so over raw text "the ledger records 3 of 77" ending one
#: paragraph and "reference queries execute on every run" opening the next now match as one
#: claim that neither paragraph makes. Measured, not supposed. The two repairs pull in
#: opposite directions on purpose: the separator class stops a real claim being missed, and
#: the sentence unit stops an imagined one being found.
#:
#: The subject is every name this population goes by, not the one the sanctioned sentence
#: uses, and "of the" is tolerated as well as "of". Both were measured as escapes on
#: 2026-08-14: the sentence "In addition, 41 of the 77 answerable cases have already been
#: checked by hand", placed in README.md one paragraph below the correct guarded figure,
#: passed all 55 tests in `tests/test_gold_audit.py`. It states a number, contradicts the
#: ledger outright, and was invisible for no reason other than calling the cases something
#: else. Detecting the topic separately from the figure closes nothing if both descriptions
#: are keyed to the same single phrasing, which is the mistake the separator class above
#: records in a different form. Verified not to over-fire: across the six tracked documents
#: this alternation matches the five sanctioned sentences and nothing besides.
COVERAGE_PATTERN = re.compile(
    r"(\d+)\s+of\s+(?:the\s+)?(\d+)\s+"
    r"(?:reference[-\s]+(?:quer(?:y|ies)|sql)|answerable[-\s]+cases?|gold[-\s]+cases?)",
    re.IGNORECASE,
)


def coverage_sentence(cov: AuditCoverage | None = None) -> str:
    """The one sentence documentation is allowed to make about audit coverage."""
    c = cov or coverage()
    return f"{c.audited} of {c.total} reference queries independently audited"


#: Sentences that TALK about auditing reference queries, whether or not they carry a
#: parseable figure. `COVERAGE_PATTERN` alone leaves a hole big enough to walk through:
#: it matches one exact phrasing, so a claim written any other way ("roughly half the
#: reference queries have been checked") is invisible to it, and a guard that looks for
#: a figure and finds none passes by saying nothing is wrong. Detecting the *topic*
#: separately from the *figure* is what lets a test insist that any document discussing
#: this audit states its coverage in the one form that can be checked.
#: The separator is the same class as in `COVERAGE_PATTERN`, and for the same measured
#: reason: a topic detector that misses "reference-queries" hands the figure guard a
#: sentence it will never be asked about.
#:
#: Deliberately still narrow, and narrower than `COVERAGE_PATTERN`'s subject list. Widening
#: this to "reference SQL" was tried on 2026-08-14 and reverted: it fires on navigational
#: prose such as README's "GOLD_AUDIT.md is the audit of the reference SQL that method
#: measures against", which discusses the audit's existence rather than its extent and has
#: no figure to state. A topic detector that demands a figure from a sentence making no
#: quantitative claim trains the reader to add meaningless numbers. Quantified claims are
#: caught by subject breadth in `COVERAGE_PATTERN` instead, where a number is already
#: present and can simply be checked.
_COVERAGE_TOPIC = re.compile(r"reference[-\s]+quer(?:y|ies)", re.IGNORECASE)

#: Words marking a sentence as talking about scrutiny rather than mechanics. Broad on
#: purpose. An earlier version listed only "audit" and "verify" forms, which let
#: "Most of the reference queries have been checked and confirmed correct by hand"
#: through untouched, and `confirmed` is this module's own disposition name. "validate"
#: is deliberately NOT here: this repo calls a component the validator, and every
#: sentence describing it would otherwise be read as a coverage claim.
_AUDIT_WORD = re.compile(
    r"\b(audit\w*|verif\w*|scrutin\w*|check\w*|confirm\w*|review\w*|examin\w*)\b",
    re.IGNORECASE,
)

#: Sentence terminators, applied within a block rather than across the whole document.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: Markdown block starts: a blank line, a list item, a heading, or a table row. Splitting
#: on these BEFORE splitting into sentences matters, because a bullet usually carries no
#: terminal punctuation and would otherwise merge with the line after it. That merge is
#: exploitable: an unparseable claim on one bullet would inherit the figure from a
#: parseable claim on the next and pass the guard while misleading the reader.
_BLOCK_SPLIT = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+]\s|\d+\.\s|#{1,6}\s|\|))")


def sentences(text: str) -> list[str]:
    """Split markdown into sentences, block by block.

    Public because more than one guard needs it, and because the two guards must agree
    about what a sentence is. Blocks are separated first and each block's line wrapping
    collapsed, so a paragraph split across source lines reads as one sentence while a
    bullet reads as its own.
    """
    out: list[str] = []
    for block in _BLOCK_SPLIT.split(text):
        collapsed = " ".join(block.split())
        out.extend(s for s in _SENTENCE_SPLIT.split(collapsed) if s)
    return out


#: The same figure, over the population's BARE names. "question" and "case" are what this
#: project calls a gold entry nearly everywhere, starting with `gold_questions.yaml`, so a
#: claim will reach for them sooner than for "answerable case". They cannot go in
#: `COVERAGE_PATTERN` itself: "25 of the 77 cases return a single scalar" and "13 of the 77
#: questions require four-table joins" are true sentences this documentation needs to write,
#: and reading either as an audit claim would fail the suite over prose that claims nothing.
#: The audit word is what separates the two, which is why this pattern is only ever applied
#: to a sentence that carries one.
_LOOSE_COVERAGE_PATTERN = re.compile(
    r"(\d+)\s+of\s+(?:the\s+)?(\d+)\s+(?:cases?|questions?)", re.IGNORECASE
)


def coverage_figures(text: str) -> list[tuple[int, int]]:
    """Every (audited, total) pair the text claims, read sentence by sentence.

    Two patterns, deliberately. A subject specific enough to mean the gold set on its own
    (`COVERAGE_PATTERN`) is read wherever it appears, because no other reading is available.
    A bare "cases" or "questions" is read only in a sentence that also talks about scrutiny,
    because those words carry claims about coverage and claims about result shapes in the
    same document. Measured on 2026-08-14: "41 of 77 questions are audited" escaped every
    guard, while "25 of the 77 cases return a single scalar" must continue to pass.
    """
    figures: list[tuple[int, int]] = []
    for sentence in sentences(text):
        found = COVERAGE_PATTERN.findall(sentence)
        if _AUDIT_WORD.search(sentence):
            found += _LOOSE_COVERAGE_PATTERN.findall(sentence)
        figures.extend((int(audited), int(total)) for audited, total in found)
    return figures


def coverage_claims(text: str) -> list[str]:
    """Return sentences that make a claim about how much of the gold set was audited.

    Used by the guard test to find prose discussing this audit, so that a claim phrased
    outside `COVERAGE_PATTERN` fails loudly instead of slipping past unread.
    """
    return [
        sentence
        for sentence in sentences(text)
        if _COVERAGE_TOPIC.search(sentence) and _AUDIT_WORD.search(sentence)
    ]


# --- Mechanical triage --------------------------------------------------------------
# Each check encodes a failure this project has actually seen or that ADR-006 names as a
# risk. None of them can decide correctness; they decide reading order.

_SUPERLATIVE = re.compile(
    r"\b(longest|shortest|highest|lowest|most|least|busiest|quietest|fastest|slowest"
    r"|largest|smallest|best|worst|top)\b",
    re.IGNORECASE,
)
#: Deliberately narrow. An earlier version also matched "per" and "by <word>", which
#: fired on "fewest containers per hour" (a rate) and "top 5 vessels by capacity" (a
#: ranking dimension), neither of which is a per-group question. On the first run over
#: the real set, 4 of the 11 high-signal flags raised were false in this way. (That
#: count is unrelated to the 11 cases adjudicated after disagreeing with the agent,
#: which the documentation also quotes.) A triage that cries wolf gets skimmed, so the
#: loose alternatives were removed rather than tuned.
_PER_GROUP = re.compile(r"\b(each|every)\b", re.IGNORECASE)

#: Restricted to 2020-2029 because the seeded window is 2025-01-01 to 2026-06-30 and
#: questions about uncovered periods name adjacent years. A bare `20\d{2}` also matched
#: "around 2000 port calls" (q46), where the number is a quantity the user wants
#: confirmed rather than a period to filter on.
_YEAR = re.compile(r"\b(202\d)\b")


def mechanical_flags(case: AnswerableCase, result: Any | None = None) -> list[Flag]:
    """Flag shapes that warrant a closer human look at `case`.

    Args:
        case: the answerable gold case.
        result: an optional executed `QueryResult` for `case.gold_sql`. Static checks run
            without it, so the triage is usable with no database.
    """
    sql = case.gold_sql
    upper = sql.upper()
    question = case.question
    flags: list[Flag] = []

    if case.ordered and "ORDER BY" not in upper:
        flags.append(
            Flag(
                code="ORDERED_NO_ORDER_BY",
                message="ordered=true but the reference SQL has no ORDER BY, so the "
                "comparison enforces an order the query does not produce.",
            )
        )

    if not case.ordered and "ORDER BY" in upper and "LIMIT" in upper:
        flags.append(
            Flag(
                code="RANKING_NOT_ORDERED",
                message="SQL ranks and limits but ordered=false, so a wrongly ordered "
                "agent answer would still score as a pass.",
            )
        )

    singular_superlative = bool(_SUPERLATIVE.search(question)) and not _PER_GROUP.search(question)
    if singular_superlative and "LIMIT" not in upper:
        flags.append(
            Flag(
                code="SUPERLATIVE_NO_LIMIT",
                message="question asks for a single extreme but the SQL has no LIMIT. "
                "This is the q05 defect class.",
            )
        )

    if _PER_GROUP.search(question) and "LIMIT" in upper:
        flags.append(
            Flag(
                code="PER_GROUP_WITH_LIMIT",
                message="question asks per group but the SQL limits rows, so most groups "
                "are missing from the expected answer.",
            )
        )

    years = set(_YEAR.findall(question))
    if years and not (years & set(_YEAR.findall(sql))):
        flags.append(
            Flag(
                code="YEAR_NOT_IN_SQL",
                message=f"question names {sorted(years)} but the SQL contains no matching "
                "year literal, so the period may be unfiltered.",
            )
        )

    asks_average = re.search(r"\b(average|avg|mean)\b", question, re.IGNORECASE)
    if asks_average and "AVG(" not in upper.replace(" ", ""):
        flags.append(
            Flag(
                code="AVERAGE_WITHOUT_AVG",
                message="question asks for an average but the SQL does not use AVG, so "
                "check the divisor is the one the question implies. This is the q54 "
                "defect class.",
            )
        )

    if result is not None:
        row_count = getattr(result, "row_count", None)
        columns = getattr(result, "columns", []) or []
        if row_count == 0:
            flags.append(
                Flag(
                    code="EMPTY_RESULT",
                    message="returns zero rows, so any agent query returning zero rows "
                    "compares equal. ADR-006 names this degenerate agreement. Confirm the "
                    "emptiness is the intended answer.",
                )
            )
        if row_count == 1 and len(columns) == 1:
            flags.append(
                Flag(
                    code="SINGLE_SCALAR",
                    message="returns one scalar, the weakest possible comparison. Confirm "
                    "the value independently rather than by re-reading the SQL.",
                )
            )
        if getattr(result, "truncated", False):
            flags.append(
                Flag(
                    code="TRUNCATED",
                    message="the row cap trimmed this result, so the reference itself is "
                    "incomplete and the comparison depends on the cap.",
                )
            )

    return flags


def record_entry(entry: AuditEntry, path: Path | None = None) -> None:
    """Append one verdict to the ledger, preserving its header comment.

    Rewrites through the schema rather than appending text, so an entry that would not
    load is rejected at write time instead of breaking the next reader.
    """
    target = path or AUDIT_PATH
    existing = load_audit(target) if target.exists() else []
    if any(e.id == entry.id for e in existing):
        raise ValueError(f"{entry.id} is already in the ledger; edit the file to change it")

    header: list[str] = []
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                header.append(line)
            else:
                break

    payload = [
        {
            "id": e.id,
            "disposition": e.disposition,
            "method": e.method,
            "auditor": e.auditor,
            "checked_on": e.checked_on,
            "note": e.note,
        }
        for e in [*existing, entry]
    ]
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=96)
    target.write_text("\n".join(header).rstrip("\n") + "\n\n" + body, encoding="utf-8")


# --- Worksheet ----------------------------------------------------------------------


def _render_rows(result: Any, limit: int = 8) -> str:
    """Render a result as a markdown table, capped so the worksheet stays readable."""
    columns = list(getattr(result, "columns", []) or [])
    rows = list(getattr(result, "rows", []) or [])
    if not columns:
        return "_no columns returned_"
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join("NULL" if v is None else str(v) for v in row) + " |"
        for row in rows[:limit]
    ]
    more = f"\n\n_{len(rows) - limit} further rows not shown._" if len(rows) > limit else ""
    return "\n".join([head, rule, *body]) + more


def build_worksheet(cases: list[AnswerableCase], executed: dict[str, Any]) -> str:
    """Assemble the reading material for a hand audit.

    Everything needed to judge one case is put on one screen: the question, the reference
    SQL, what it returns against the seeded data right now, and the mechanical flags.
    The rows are included because the fastest independent check available is comparing a
    returned figure against a value planted by `db/seed.py` or against a magnitude the
    reader already knows, and neither is possible while looking only at SQL.
    """
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# Gold set hand-audit worksheet",
        "",
        f"Generated {generated}. {len(cases)} case(s) not yet in `eval/gold_audit.yaml`.",
        "",
        "For each case ask two questions, in this order:",
        "",
        "1. **Does the SQL answer the question that was asked?** Both defects found so far "
        "(`q54`, `q61`) were failures here, not SQL errors. Read the question first and "
        "say what the answer should be before looking at the query.",
        "2. **Is the returned value right?** Check it against a planted value in "
        "`db/seed.py`, a magnitude you already know, or a differently shaped query. "
        "Re-reading the reference SQL is not a check.",
        "",
        "Record each verdict with:",
        "",
        "```bash",
        'python eval/audit.py record <id> confirmed --method rederived --note "..."',
        "```",
        "",
        "Flags are prompts for attention, never findings. `EMPTY_RESULT` on a question "
        "about a period outside data coverage is the correct behaviour, not a defect.",
        "",
        "---",
        "",
    ]

    for case in cases:
        result = executed.get(case.id)
        flags = mechanical_flags(case, result)
        marker = f" ({len(flags)} flag{'s' if len(flags) != 1 else ''})" if flags else ""
        out += [
            f"## {case.id}{marker}",
            "",
            f"**Question:** {case.question}",
            "",
            f"**Note:** {case.note}",
            "",
            f"**behaviour:** `{case.behaviour}` | **ordered:** `{str(case.ordered).lower()}`"
            f" | **topics:** {', '.join(str(t) for t in case.topics) or 'none'}",
            "",
        ]
        if case.prior_turns:
            out += ["**Prior turns:** " + " / ".join(f'"{t}"' for t in case.prior_turns), ""]
        if flags:
            out.append("**Flags:**")
            out += [f"- `{f.code}` {f.message}" for f in flags]
            out.append("")
        out += ["```sql", case.gold_sql.strip(), "```", ""]
        if result is None:
            out += ["**Returns:** _not executed (no database connection)_", ""]
        else:
            count = getattr(result, "row_count", "?")
            cols = len(getattr(result, "columns", []) or [])
            out += [f"**Returns {count} row(s), {cols} column(s):**", "", _render_rows(result), ""]
        out += ["**Verdict:** &nbsp; **Method:** &nbsp; **Note:**", "", "---", ""]

    return "\n".join(out)


# --- CLI ----------------------------------------------------------------------------


def _execute_all(cases: list[AnswerableCase]) -> dict[str, Any]:
    """Run every reference query once, tolerating a missing database.

    The worksheet is still worth generating without rows, so a connection failure
    degrades the output rather than aborting the audit.
    """
    from src.executor import ExecutionError, run_query

    executed: dict[str, Any] = {}
    for case in cases:
        try:
            executed[case.id] = run_query(case.gold_sql)
        except ExecutionError as exc:
            # Narrow deliberately. `run_query` wraps every psycopg failure, including a
            # refused connection, into ExecutionError, so this catches the whole
            # database-unavailable case. Catching bare Exception as well would also
            # swallow defects in this module and report them as "could not execute",
            # which would hide a real bug behind a message about the database.
            print(f"  {case.id}: could not execute ({exc})", file=sys.stderr)
    return executed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="print audit coverage (default)")

    ws = sub.add_parser("worksheet", help="write the reading material for unaudited cases")
    ws.add_argument("--out", type=Path, required=True, help="path to write the markdown to")
    ws.add_argument("--all", action="store_true", help="include cases already in the ledger")
    ws.add_argument("--no-execute", action="store_true", help="skip running the reference SQL")

    rec = sub.add_parser("record", help="append one verdict to the ledger")
    rec.add_argument("id")
    rec.add_argument("disposition", choices=["confirmed", "defect", "unsure"])
    rec.add_argument(
        "--method",
        required=True,
        choices=["rederived", "seed_constant", "counted", "intent_only"],
    )
    rec.add_argument("--note", default="")
    rec.add_argument("--auditor", default="Insaf Ismath")

    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "record":
        entry = AuditEntry(
            id=args.id,
            disposition=args.disposition,
            method=args.method,
            auditor=args.auditor,
            checked_on=datetime.now(UTC).date(),
            note=args.note,
        )
        record_entry(entry)
        cov = coverage()
        print(f"recorded {entry.id} as {entry.disposition} ({entry.method})")
        print(f"{coverage_sentence(cov)}; {len(cov.remaining)} remaining")
        return 0

    cases = [c for c in load_gold_set() if isinstance(c, AnswerableCase)]

    if command == "worksheet":
        done = {e.id for e in load_audit()}
        selected = cases if args.all else [c for c in cases if c.id not in done]
        executed = {} if args.no_execute else _execute_all(selected)
        args.out.write_text(build_worksheet(selected, executed), encoding="utf-8")
        print(f"wrote {args.out} covering {len(selected)} case(s)")
        return 0

    cov = coverage()
    print(coverage_sentence(cov))
    print(f"  confirmed {cov.confirmed}, defects {cov.defects}, unsure {cov.unsure}")
    if cov.remaining:
        print(f"  remaining ({len(cov.remaining)}): {' '.join(cov.remaining)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
