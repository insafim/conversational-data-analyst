"""Streamlit state shared by both pages: the store handle and the chat session.

Both pages need the store, and `app.py` cannot hold it for them: it calls
`st.navigation(...).run()` at import time, so importing it from a page would re-enter
navigation. This module is the shared piece instead.

It is the only place outside the pages themselves that imports Streamlit. `src/` stays
free of it, which is what keeps `src/conversations.py`, `src/notices.py` and
`src/telemetry.py` assertable without a browser.
"""

from __future__ import annotations

import streamlit as st

from src.conversations import STORE_FAILURES, ChatSession
from src.store import Store


@st.cache_resource
def _build_store() -> Store:
    """Construct the store, letting a failure escape.

    cache_resource because `Store` holds one live connection guarded by a lock, and is
    built to be shared: opening a connection per browser session would spend the
    `CONNECTION LIMIT 10` that `app_rw` carries. Both pages calling this get the same
    object, which is the point.

    The failure is caught by `store_handle` OUTSIDE this function rather than inside it,
    and that placement is the whole reason the two are separate. Streamlit memoises a
    return value but not a raised exception, verified on 2026-08-12 against the pinned
    1.61.1 by calling a failing cached function three times and watching the body run all
    three, then a succeeding one three times and watching it run once. Catching in here
    would turn a failure into an ordinary cached value, so a store that was down when the
    first browser session opened would stay down for the life of the process even after
    the database came back. Letting it raise means every rerun retries construction, and
    the app recovers on its own.
    """
    return Store()


def store_handle() -> tuple[Store | None, str | None]:
    """The conversation store, or the reason there is none (ADR-014).

    A failure is returned rather than raised. `db/03_app_store.sql` runs only on the
    container's first boot, so anyone with an older data volume has no store database, and
    the chat itself does not depend on it. The message already names the remedy.

    `STORE_FAILURES` rather than `StoreError` alone because construction applies the table
    DDL: `Store._live` turns an unreachable server into a `StoreError`, but a privilege or
    schema failure inside `_ensure_tables` arrives as itself.
    """
    try:
        return _build_store(), None
    except STORE_FAILURES as exc:
        return None, str(exc)


# The date range is queried rather than written here, because a hard-coded window is
# wrong the moment the data is reloaded, and wrong quietly.
#
# The column is named in `views/` and not in `src/schema.py` on purpose. ADR-003's
# portability argument rests on the schema layer holding no domain literals, and `views/`
# is already the domain-aware layer: the chat page's example questions name terminals and
# operators. Keeping the domain knowledge together is what keeps the claim about
# `src/schema.py` true.
#
# It sits in THIS module rather than in `views/chat.py`, where it was written, because
# `views/chat.py` is a page: importing it runs its module-level script body. A test that
# imported the page to reach this function poisoned Streamlit's delta-generator context
# for the rest of the process, and every later `AppTest` run that rendered the chat page
# then read its `New chat` button as sitting inside the rename `st.form`. Measured on
# 2026-08-12: `tests/test_app_smoke.py` passed 10 of 10 alone, and lost its six chat-page
# cases when collected alongside `tests/test_data_coverage.py`, which imported the page at
# module scope. The four observability-page cases passed throughout, which is what placed
# the fault in the chat sidebar rather than in the harness. This module renders nothing on
# import, so reaching the function no longer costs a page run.
_COVERAGE_SQL = (
    "SELECT min(arrival_ts)::date AS first_day, max(arrival_ts)::date AS last_day "
    "FROM port_calls"
)


@st.cache_data(show_spinner=False)
def data_coverage() -> str | None:
    """One sentence naming the period the data covers, or None if it cannot be read.

    This exists because of a real failure rather than for polish. Asked to project July
    2026 port calls, the agent returned a historical average and presented it as a
    forecast. The summariser now refuses that (SUMMARIZE_SYSTEM rule 6), but the catch is
    downstream of the real problem: nothing on screen told the user the data stops in June
    2026, so asking about July was a reasonable thing to do. A guard that rejects a
    question the interface invited is a worse design than an interface that does not
    invite it.

    cache_data rather than cache_resource because the return is a plain string, and the
    window only changes when the database is reseeded.
    """
    from src.executor import ExecutionError, run_query

    try:
        result = run_query(_COVERAGE_SQL, row_cap=1)
    except ExecutionError:
        # The chat page's sidebar already reports an unreachable database, in the table
        # listing below this line. Returning None omits the sentence rather than saying
        # the same thing twice.
        return None

    if not result.rows or result.rows[0][0] is None:
        return None

    first, last = result.rows[0][0], result.rows[0][1]
    return f"{first:%B %Y} to {last:%B %Y}"


def chat_session() -> ChatSession:
    """The open conversation for this browser session.

    The store is shared across browser sessions; which conversation is open is not, so the
    session lives in `st.session_state` and the handle does not.

    The handle is re-pointed on every call rather than only at construction. It is a
    process-wide cached resource and the session outlives any single rerun, so a session
    built while the store was unreachable would otherwise hold `None` for as long as the
    browser tab stayed open, and would render as "no saved chats" rather than as a store
    that is down. Paired with `_build_store` not caching its failures, that makes recovery
    actually reachable: the next rerun rebuilds the store and this line hands it to a
    session that started without one.
    """
    handle, _ = store_handle()
    if "session" not in st.session_state:
        st.session_state.session = ChatSession(store=handle)
    session: ChatSession = st.session_state.session
    session.store = handle
    return session
