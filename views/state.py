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
