"""Conversations and per-turn telemetry, in their own PostgreSQL database (ADR-014).

Two features share one store because they need the same record. Saved chats need a turn
to render again; the observability page needs that same turn's latency, cost and outcome.
Two stores would be more code and would let the two views disagree about what happened.

**Why a separate database, not a table in the analytics one.** The shape of a real
deployment decides this. The data an analyst agent queries is usually someone else's, with
its own owner, its own release cadence, its own workload and its own retention rules;
chat history is the application's own state and shares none of that. They are separate
systems in production, so the step from here to a separate server is a connection string
rather than a migration.

It is also the strongest isolation available in one instance. `analyst_ro` is granted no
CONNECT on `ports_app`, so the agent's role cannot open a session there, and PostgreSQL
has no cross-database queries without FDW. The alternative matters: `db/02_roles.sql`
grants `analyst_ro` SELECT on every table in `public` and, via ALTER DEFAULT PRIVILEGES,
on every table added later, so history kept in the analytics database would be readable by
the agent. That costs twice over. "What have other people asked?" becomes a question it
answers, and the database it queries starts holding user-supplied text, which is the
second-order injection channel `src/prompts.py` documents and
`tests/test_second_order_injection.py` polices.

`tests/test_store_isolation.py` proves the denial by trying to connect as the agent's role.

**Why not SQLite.** Built that way first, then rejected. This application cannot answer
anything without PostgreSQL, so a second store buys no availability and no deployment
simplicity; it buys faster tests, and costs a second storage technology plus a security
boundary with nothing to demonstrate. It is also not what production app-state storage
looks like: SQLite cannot serve two application instances, which is the first thing that
changes when this scales.

**Records are span-shaped and stored as `jsonb`.** `stage_timings` is a name-to-duration
map per turn, which is what an OpenTelemetry exporter needs, so emitting traces later is
an adapter rather than a rewrite. `jsonb` rather than text because the observability page
aggregates over these fields, and aggregating in SQL beats loading every row into Python
to average one number.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .models import AgentResult

# Titles come from the first question rather than from a model. A title is not worth a
# call, and a generated one is a second thing that can be wrong.
_TITLE_CHARS = 60


class StoreError(RuntimeError):
    """The store could not be reached or written.

    Distinct from `ExecutionError`, which is about the agent's own queries. A caller may
    reasonably answer a question while failing to save it; it must not fail to answer
    because saving failed.
    """


class _NoSuchConversation(Exception):
    """Internal signal, raised inside a unit of work and translated at the boundary.

    It exists because `_run` treats `psycopg.Error` as a database failure to roll back and
    report. A missing conversation is not that: it is a caller mistake, and raising
    `StoreError` directly from inside the transaction would be caught by the same handler
    and re-wrapped with the rollback's message instead of the real cause.
    """

    def __init__(self, conversation_id: str) -> None:
        super().__init__(conversation_id)
        self.conversation_id = conversation_id


def title_for(question: str) -> str:
    """The conversation title, taken from the question that started it.

    Truncated on a word boundary where one is available, because a title cut mid-word
    reads like a bug. Falls back to a hard cut for a single long token, and to a fixed
    label for an empty question, which `ask()` refuses anyway but which the store must not
    depend on.
    """
    text = " ".join(question.split())
    if not text:
        return "Untitled chat"
    if len(text) <= _TITLE_CHARS:
        return text
    clipped = text[:_TITLE_CHARS]
    spaced = clipped.rsplit(" ", 1)[0]
    return (spaced if len(spaced) >= _TITLE_CHARS // 2 else clipped) + "..."


@dataclass(frozen=True)
class ConversationSummary:
    """One row of the sidebar list."""

    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int


@dataclass(frozen=True)
class LiveTelemetry:
    """What the observability page shows for traffic this application actually served.

    Separate from the eval figures, which `src/telemetry.py` reads from
    `eval/results/`. The two must never be added together: the eval set is a fixed
    108-case benchmark and this is whatever a user happened to ask, so a combined
    average would describe neither.
    """

    turns: int
    conversations: int
    total_cost_usd: float
    mean_cost_usd: float
    llm_calls: int
    median_latency_s: float
    p95_latency_s: float
    outcomes: dict[str, int]
    retry_reasons: dict[str, int]
    stage_means_s: dict[str, float]


@dataclass(frozen=True)
class StoredTurn:
    """One exchange, rehydrated. `result` is the full `AgentResult`, rows included, so a
    reopened conversation renders identically rather than losing its tables and charts."""

    id: str
    conversation_id: str
    seq: int
    asked_at: str
    question: str
    result: AgentResult


class Store:
    """Conversations and turns in the application's own database.

    The DSN is a constructor argument rather than read from `settings` at every call site,
    so a test can point at a scratch database and drop it afterwards. `views/state.py` passes the
    configured default.

    ONE connection is held and reused, guarded by a lock. The first version opened a
    connection per operation, copying `src/executor.py`, and that was wrong here: the
    `app_rw` role carries `CONNECTION LIMIT 10`, so concurrent operations exhausted it and
    failed with "too many connections for role". A probe at 24 concurrent appends lost 8
    of them. Serialising on one connection removes the failure mode entirely, and at one
    user's typing speed there is nothing to gain from parallel writes.

    The connection is reopened if it has died, and an operation that fails on a broken
    connection is retried exactly once. That is not defensive padding: `docker compose
    restart` during a demo would otherwise turn every subsequent save into an error until
    the app itself was restarted.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn if dsn is not None else settings.store_dsn
        self._lock = threading.Lock()
        self._connection: psycopg.Connection | None = None
        self._ensure_tables()

    def close(self) -> None:
        with self._lock:
            self._discard()

    # --- connection ------------------------------------------------------------------
    def _live(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            try:
                self._connection = psycopg.connect(
                    self.dsn, connect_timeout=10, row_factory=dict_row
                )
            except psycopg.Error as exc:
                raise StoreError(
                    f"{str(exc).strip()}\n\nThe conversation store lives in its own "
                    "database, created by db/03_app_store.sql, which the container runs "
                    "only on first boot. If this is an older data volume, recreate it "
                    "with `docker compose down -v && docker compose up -d --wait` and "
                    "re-seed."
                ) from exc
        return self._connection

    def _discard(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001  # Already broken; closing is best effort.
                pass
            self._connection = None

    def _run(self, operation: Callable[[psycopg.Connection], Any]) -> Any:
        """Run one unit of work on the shared connection, committing or rolling back.

        A transport failure is retried once against a fresh connection. A SQL failure is
        not retried, because re-running a statement the database rejected produces the
        same rejection and hides the cause behind a second one.

        A `psycopg.Error` that is not operational now propagates as itself rather than as
        `StoreError`; callers that need the friendlier type wrap it at their own boundary.

        One honest gap: if the operation body succeeds and `commit()` itself fails on a
        dropped connection, the whole callable is retried, so anything it generated is
        generated again. That is safe here because the first transaction never committed,
        so the retry writes the only surviving copy. It is recorded rather than defended,
        because an in-doubt commit is the one case where "retry" and "it already ran" are
        not mutually exclusive.
        ANY exception rolls the transaction back, not just a database one. An earlier
        version rolled back on `psycopg.Error` alone, so `_NoSuchConversation` escaped with
        the transaction still open. The connection then sat idle-in-transaction holding
        the locks its SELECT had taken, and the next `DROP DATABASE` blocked on them
        forever. The symptom was a test suite that hung rather than failed, which is the
        expensive kind of bug: nothing points at the cause.
        """
        with self._lock:
            for attempt in (1, 2):
                connection = self._live()
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except psycopg.OperationalError as exc:
                    self._discard()
                    if attempt == 2:
                        raise StoreError(str(exc).strip()) from exc
                except BaseException:
                    # Includes the internal signals raised by callers, and KeyboardInterrupt:
                    # a transaction left open by Ctrl-C would block the next writer too.
                    try:
                        connection.rollback()
                    except psycopg.Error:
                        self._discard()
                    raise
            raise StoreError("unreachable")  # pragma: no cover

    def _ensure_tables(self) -> None:
        """Idempotent DDL, applied on construction.

        The DATABASE is not created here, and that is a privilege decision rather than an
        oversight: `app_rw` holds no CREATEDB, because a role that can create databases is
        a much larger thing than a role that owns one. `db/03_app_store.sql` creates it as
        the bootstrap superuser on first boot.

        The TABLES are created here because `app_rw` owns the database and because that
        init script runs only on the container's FIRST boot: anyone with an existing data
        volume would otherwise have neither. `IF NOT EXISTS` makes repeating it free.

        A missing database cannot be handled here at all, because the connection fails
        before this runs; `_live` turns that into a StoreError naming the remedy.
        """
        statements = [
            """
            CREATE TABLE IF NOT EXISTS conversation (
                id          text PRIMARY KEY,
                title       text NOT NULL,
                created_at  timestamptz NOT NULL DEFAULT now(),
                updated_at  timestamptz NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS turn (
                id              text PRIMARY KEY,
                conversation_id text NOT NULL
                                REFERENCES conversation(id) ON DELETE CASCADE,
                seq             integer NOT NULL,
                asked_at        timestamptz NOT NULL DEFAULT now(),
                question        text NOT NULL,
                result          jsonb NOT NULL,
                UNIQUE (conversation_id, seq)
            )
            """,
            # The panel scans turns by recency across conversations; the sidebar scans
            # conversations by recency. Both are ordered scans that would otherwise walk
            # the table once the store holds a few thousand rows.
            "CREATE INDEX IF NOT EXISTS turn_by_time ON turn (asked_at DESC)",
            "CREATE INDEX IF NOT EXISTS conversation_by_update"
            " ON conversation (updated_at DESC)",
        ]

        def apply(connection: psycopg.Connection) -> None:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

        self._run(apply)


    # --- conversations ---------------------------------------------------------------
    def create_conversation(self, title: str) -> str:
        conversation_id = uuid.uuid4().hex
        self._run(
            lambda c: c.execute(
                ("INSERT INTO conversation (id, title) VALUES (%s, %s)"),
                (conversation_id, title),
            )
        )
        return conversation_id

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        """Renaming does NOT touch `updated_at`.

        That field orders the sidebar by recent activity, and renaming is not activity in
        the conversation. Bumping it would shuffle the list under the user's cursor as a
        side effect of tidying a label, which is the sort of small dishonesty that makes
        an interface feel unpredictable.
        """
        self._run(
            lambda c: c.execute(
                ("UPDATE conversation SET title = %s WHERE id = %s"),
                (title, conversation_id),
            )
        )

    def delete_conversation(self, conversation_id: str) -> None:
        self._run(
            lambda c: c.execute(
                ("DELETE FROM conversation WHERE id = %s"),
                (conversation_id,),
            )
        )

    def list_conversations(self, limit: int = 50) -> list[ConversationSummary]:
        """Newest activity first. A conversation with no turns is included, because a chat
        opened and not yet used still exists to the user looking at the sidebar."""
        rows = self._run(
            lambda c: c.execute(
                (
                    """
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           count(t.id) AS turn_count
                    FROM conversation c
                    LEFT JOIN turn t ON t.conversation_id = c.id
                    GROUP BY c.id, c.title, c.created_at, c.updated_at
                    ORDER BY c.updated_at DESC, c.created_at DESC
                    LIMIT %s
                    """
                ),
                (limit,),
            ).fetchall()
        )
        return [_to_summary(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> ConversationSummary | None:
        row = self._run(
            lambda c: c.execute(
                (
                    """
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           count(t.id) AS turn_count
                    FROM conversation c
                    LEFT JOIN turn t ON t.conversation_id = c.id
                    WHERE c.id = %s
                    GROUP BY c.id, c.title, c.created_at, c.updated_at
                    """
                ),
                (conversation_id,),
            ).fetchone()
        )
        return _to_summary(row) if row else None

    # --- turns -----------------------------------------------------------------------
    def append_turn(self, conversation_id: str, question: str, result: AgentResult) -> str:
        """Persist one exchange and return its id.

        The conversation row is locked FOR UPDATE first, which serialises appends to the
        same conversation. `_run` already serialises this process on one connection, so
        within a single app the lock is redundant; it is kept because it is the only thing
        that holds if a second process ever writes the same store, and because relying on
        a client-side lock for a database invariant is the kind of assumption that is true
        until the day it is not. Without it, two transactions under READ COMMITTED both
        read the same `max(seq)` and one loses to the UNIQUE constraint.
        """
        turn_id = uuid.uuid4().hex

        def write(connection: psycopg.Connection) -> None:
            with connection.cursor() as cursor:
                cursor.execute(
                    ("SELECT id FROM conversation WHERE id = %s FOR UPDATE"),
                    (conversation_id,),
                )
                if cursor.fetchone() is None:
                    raise _NoSuchConversation(conversation_id)

                cursor.execute(
                    (
                        "SELECT coalesce(max(seq), 0) + 1 AS next"
                        " FROM turn WHERE conversation_id = %s"
                    ),
                    (conversation_id,),
                )
                seq = cursor.fetchone()["next"]

                cursor.execute(
                    (
                        "INSERT INTO turn"
                        " (id, conversation_id, seq, question, result)"
                        " VALUES (%s, %s, %s, %s, %s::jsonb)"
                    ),
                    (turn_id, conversation_id, seq, question, result.model_dump_json()),
                )
                cursor.execute(
                    (
                        "UPDATE conversation SET updated_at = now() WHERE id = %s"
                    ),
                    (conversation_id,),
                )

        try:
            self._run(write)
        except _NoSuchConversation as exc:
            raise StoreError(f"no such conversation: {exc.conversation_id}") from None
        return turn_id

    def load_conversation(self, conversation_id: str) -> list[StoredTurn]:
        """Every turn of one conversation, oldest first, ready to render."""
        rows = self._run(
            lambda c: c.execute(
                (
                    "SELECT * FROM turn WHERE conversation_id = %s ORDER BY seq"
                ),
                (conversation_id,),
            ).fetchall()
        )
        return [_to_turn(row) for row in rows]

    def recent_turns(self, limit: int = 100) -> list[StoredTurn]:
        """Newest turns across every conversation, for the observability page."""
        rows = self._run(
            lambda c: c.execute(
                (
                    "SELECT * FROM turn ORDER BY asked_at DESC, seq DESC LIMIT %s"
                ),
                (limit,),
            ).fetchall()
        )
        return [_to_turn(row) for row in rows]

    def turn_count(self) -> int:
        row = self._run(
            lambda c: c.execute("SELECT count(*) AS n FROM turn").fetchone()
        )
        return int(row["n"])

    def telemetry(self) -> LiveTelemetry:
        """Everything the observability page aggregates, in one transaction.

        Computed in SQL rather than in Python, which is the reason the record is stored as
        `jsonb` rather than as text. Averaging one number should not require loading every
        turn, its answer and its result rows into the application to do it.

        Five statements share one `_run`, so the page reads a single consistent snapshot
        instead of five that can disagree if a turn lands between them.

        `percentile_cont` returns NULL over an empty table and `sum` returns NULL over no
        rows, so every scalar is coalesced. An empty store is the state a reviewer opens
        this page in, and it has to render as zeroes rather than as a crash.
        """

        def gather(connection: psycopg.Connection) -> LiveTelemetry:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) AS turns,
                           coalesce(sum((result->>'cost_usd')::float), 0) AS cost,
                           coalesce(sum((result->>'llm_calls')::int), 0) AS calls,
                           coalesce(
                               percentile_cont(0.5) WITHIN GROUP (
                                   ORDER BY (result->>'elapsed_s')::float
                               ), 0) AS median_s,
                           coalesce(
                               percentile_cont(0.95) WITHIN GROUP (
                                   ORDER BY (result->>'elapsed_s')::float
                               ), 0) AS p95_s
                    FROM turn
                    """
                )
                totals = cursor.fetchone()

                cursor.execute("SELECT count(*) AS n FROM conversation")
                conversations = int(cursor.fetchone()["n"])

                cursor.execute(
                    "SELECT result->>'outcome' AS key, count(*) AS n"
                    " FROM turn GROUP BY 1 ORDER BY n DESC"
                )
                outcomes = {r["key"]: int(r["n"]) for r in cursor.fetchall() if r["key"]}

                # A turn carries zero or more reasons, so the array is unnested and the
                # rows counted. A turn that retried twice contributes to both reasons,
                # which is what makes this attributable rather than a count of retries.
                cursor.execute(
                    "SELECT reason AS key, count(*) AS n FROM turn,"
                    " jsonb_array_elements_text(result->'retry_reasons') AS reason"
                    " GROUP BY 1 ORDER BY n DESC"
                )
                retries = {r["key"]: int(r["n"]) for r in cursor.fetchall()}

                # Mean rather than total: totals just re-rank the stages by how many turns
                # ran, and the question this answers is which stage to optimise.
                cursor.execute(
                    "SELECT stage AS key, avg(seconds::float) AS mean FROM turn,"
                    " jsonb_each_text(result->'stage_timings') AS t(stage, seconds)"
                    " GROUP BY 1 ORDER BY mean DESC"
                )
                stages = {r["key"]: float(r["mean"]) for r in cursor.fetchall()}

            turns = int(totals["turns"])
            return LiveTelemetry(
                turns=turns,
                conversations=conversations,
                total_cost_usd=float(totals["cost"]),
                mean_cost_usd=float(totals["cost"]) / turns if turns else 0.0,
                llm_calls=int(totals["calls"]),
                median_latency_s=float(totals["median_s"]),
                p95_latency_s=float(totals["p95_s"]),
                outcomes=outcomes,
                retry_reasons=retries,
                stage_means_s=stages,
            )

        return self._run(gather)


def _to_summary(row: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        turn_count=int(row["turn_count"]),
    )


def _to_turn(row: dict[str, Any]) -> StoredTurn:
    # `result` arrives as a dict, not a string: psycopg decodes jsonb for us, which is why
    # this is `model_validate` rather than `model_validate_json`.
    return StoredTurn(
        id=row["id"],
        conversation_id=row["conversation_id"],
        seq=row["seq"],
        asked_at=row["asked_at"].isoformat(),
        question=row["question"],
        result=AgentResult.model_validate(row["result"]),
    )
