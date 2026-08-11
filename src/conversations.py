"""The chat session: which conversation is open, and what happens to it. No Streamlit here.

Same reasoning as `src/notices.py`, applied to state rather than to ordering. `app.py`
held this inline, and the cost was concrete: it rendered the answer and appended it to
history afterwards, so a click landing during rendering preempted the rerun and the turn
was lost after the user had already paid for it. An ordering bug that only appears when a
human clicks at the wrong moment is not something a reviewer can catch by reading, and
`app.py` has no test file to catch it either.

So the sequence lives in `answer()` and is asserted in `tests/test_conversations.py`. The
caller passes a question and gets back a turn that is already persisted and already in the
pane. There is no order for the view layer to get wrong, because there is no second call.

**A save failure never costs the user an answer.** `StoreError`'s own contract says a
caller may reasonably answer while failing to save, and the turn carries `saved` so the
interface can say so rather than pretending. The chat is the deliverable; history is not
worth failing the whole app for, which is also why `store` may be None.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from .models import AgentResult, Outcome, Turn
from .store import ConversationSummary, Store, StoreError, title_for

# What the user is shown when something failed that `ask()` did not anticipate. The cause
# is kept on the result for the log and deliberately not put in front of the reader.
_UNEXPECTED = (
    "Something failed while answering that. The database or the model provider is the "
    "usual cause. The error is in the server log."
)

# Public because `app.py` catches the same set when it constructs the Store, and two copies
# of this tuple would drift the first time one of them is widened.
#
# Everything a store call can fail with. `psycopg.Error` is in here on `src/store.py`'s own
# instruction: `Store._run` turns a dropped connection into `StoreError` but re-raises any
# other database error as itself, and its docstring says callers needing the friendlier
# type wrap it at their own boundary. This module is that boundary, so a privilege error or
# a constraint violation degrades the same way a connection loss does instead of reaching
# the page as a traceback.
STORE_FAILURES = (StoreError, psycopg.Error)

# Shown when a store call failed. Deliberately fixed text: a raw database error can quote
# the row that caused it, and `src/notices.py` and the error-disclosure split already
# established that database detail does not go in front of a chat user. The operator-facing
# message, with the remedy, is the one the sidebar shows when the store is missing at start.
_STORE_UNREACHABLE = (
    "The conversation store could not be reached, so that did not happen. "
    "Your current chat is unaffected."
)


@dataclass
class ChatTurn:
    """One exchange as the pane needs it.

    Deliberately not `StoredTurn`: `seq` and `asked_at` are assigned by the database, and a
    turn that failed to save has neither. Inventing them client-side would put two
    different meanings behind the same field.
    """

    question: str
    result: AgentResult
    saved: bool = False


@dataclass
class ChatSession:
    """The open conversation and its turns, backed by a `Store` when one is reachable."""

    store: Store | None
    conversation_id: str | None = None
    turns: list[ChatTurn] = field(default_factory=list)

    # Set when a navigation action failed, cleared when one succeeds, so the sidebar can
    # say that a click did nothing. Saving a turn does NOT set it: `ChatTurn.saved` already
    # reports that beside the answer it belongs to, and reporting it twice would put the
    # same failure in two places.
    last_error: str | None = None

    # --- reading -----------------------------------------------------------------------
    def history(self) -> list[Turn]:
        """The prior turns the rewrite node is allowed to see (ADR-011).

        Question and SQL only, never the answer text or the rows. The interpreted form of
        an earlier follow-up is preferred over what was typed, so that a chain resolves:
        once "and Rotterdam?" has become "how many port calls at Rotterdam in 2025?", the
        turn after it can refer back to something that stands on its own.

        Trimming to `HISTORY_TURNS` happens in the agent; this passes what the session
        holds.
        """
        return [
            Turn(
                question=turn.result.interpreted_question or turn.question,
                sql=turn.result.sql or "",
            )
            for turn in self.turns
        ]

    def list_conversations(self, limit: int = 50) -> list[ConversationSummary]:
        """The sidebar list, or an empty one if the store cannot be read.

        An unreachable store makes the list unavailable, not the app. The caller reports
        the failure once, beside the list, rather than raising through the render.
        """
        if self.store is None:
            return []
        try:
            return self.store.list_conversations(limit=limit)
        except STORE_FAILURES:
            return []

    # --- navigation --------------------------------------------------------------------
    def start_new(self) -> None:
        """Clear the pane. No row is written until a question arrives.

        Two reasons it is lazy. The title comes from the first question, so there is
        nothing to name an empty conversation with; and clicking New chat repeatedly would
        otherwise leave a row in the sidebar each time.
        """
        self.conversation_id = None
        self.turns = []

    def open(self, conversation_id: str) -> bool:
        """Load a stored conversation into the pane and continue it. True if it opened.

        Turns are rehydrated whole, rows included, so a reopened chat renders its tables
        and charts again rather than coming back as text.

        A failure leaves the pane exactly as it was rather than half-swapping it. The
        conversation being read is still valid, and replacing it with an empty screen
        because a different one could not be loaded would lose the user's place for a
        reason that has nothing to do with it.
        """
        if self.store is None:
            return False
        try:
            stored = self.store.load_conversation(conversation_id)
        except STORE_FAILURES:
            self.last_error = _STORE_UNREACHABLE
            return False
        self.conversation_id = conversation_id
        self.turns = [
            ChatTurn(question=turn.question, result=turn.result, saved=True)
            for turn in stored
        ]
        self.last_error = None
        return True

    def rename(self, conversation_id: str, title: str) -> bool:
        """Rename a conversation, refusing a blank title. True if it was applied.

        A blank title renders as an empty row in the sidebar, which is unclickable, so
        accepting one would lose the conversation behind it.
        """
        # Cleared before the blank-title check, not only on the store path. `app.py` tells
        # the two failures apart by whether `last_error` is set, so an error left over from
        # an earlier click would make a blank title report itself as a store outage.
        self.last_error = None
        cleaned = " ".join(title.split())
        if not cleaned or self.store is None:
            return False
        try:
            self.store.rename_conversation(conversation_id, cleaned)
        except STORE_FAILURES:
            self.last_error = _STORE_UNREACHABLE
            return False
        self.last_error = None
        return True

    def delete(self, conversation_id: str) -> bool:
        """Delete a conversation, clearing the pane if it is the one being read.

        Leaving the turns on screen would offer a conversation that no longer exists, and
        the next question would append to a deleted row.

        The pane is cleared only after the delete has actually committed. Clearing first
        would show the user a conversation as gone while it is still in the store, which is
        the one outcome worse than the delete failing.
        """
        if self.store is None:
            return False
        try:
            self.store.delete_conversation(conversation_id)
        except STORE_FAILURES:
            self.last_error = _STORE_UNREACHABLE
            return False
        if conversation_id == self.conversation_id:
            self.start_new()
        self.last_error = None
        return True

    # --- asking ------------------------------------------------------------------------
    def answer(
        self,
        question: str,
        ask: Callable[..., AgentResult],
    ) -> ChatTurn:
        """Ask, persist, then hand back the turn to render. That order is the point.

        `ask` is injected rather than imported so the graph is not built to run a test, and
        so the eval harness and the UI keep calling the same function.
        """
        try:
            result = ask(question, history=self.history())
        except Exception as exc:  # noqa: BLE001
            # `ask()` converts the failures it anticipates into an ERROR outcome, so
            # reaching here means an unanticipated one: a dropped database connection, an
            # import-time fault in a lazily loaded dependency, a bug. Streamlit renders an
            # uncaught exception as a traceback in the page, which shows a non-technical
            # user a stack trace and shows anyone else the internal structure of the app.
            result = AgentResult(
                question=question,
                outcome=Outcome.ERROR,
                answer=_UNEXPECTED,
                error=f"{type(exc).__name__}: {exc}",
            )

        turn = ChatTurn(question=question, result=result, saved=self._persist(question, result))
        self.turns.append(turn)
        return turn

    def _persist(self, question: str, result: AgentResult) -> bool:
        """Write the turn, creating the conversation if this is the first one.

        Every store failure is swallowed into a False. The turn is already answered and
        the user is about to read it; raising here would replace an answer they paid for
        with an error about bookkeeping.

        If creation succeeds and the append then fails, the empty conversation is kept and
        reused by the next turn rather than cleaned up, because a delete on a store that
        just failed to write is not a delete worth trusting.
        """
        if self.store is None:
            return False
        try:
            if self.conversation_id is None:
                self.conversation_id = self.store.create_conversation(title_for(question))
            self.store.append_turn(self.conversation_id, question, result)
        except STORE_FAILURES:
            return False
        return True
