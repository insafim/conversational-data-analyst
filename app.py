"""Entrypoint. Sets the page config and hands off to the two pages (ADR-008).

The UI is deliberately thin and stayed that way as it grew. It exists to serve what the
brief actually assesses:

* **auditability**: the SQL is one click away on every answer, because a client analyst
  must be able to check the agent's work rather than trust it;
* **chart-type selection**: the rule-based choice is rendered, and the rule that fired
  is shown, so the behaviour is inspectable rather than magic;
* **conversations that survive a reload**, kept in a database the agent cannot reach
  (ADR-014);
* **latency and cost**, measured and shown, now aggregated on the Observability page
  rather than as a caption under one answer.

Two pages rather than one long scroll, because the audiences differ: the chat is for
someone asking a question, and the panel is for someone deciding whether to trust the
answers or what to make faster.

Both pages are FILES rather than callables. `st.Page` accepts either, but
`AppTest.switch_page` only reaches file-based pages, so a callable page cannot be driven
by a test.

    Source: streamlit/testing/v1/app_test.py, `AppTest.switch_page`
    Verified: 2026-08-12 on the pinned Streamlit 1.61.1. Its docstring opens "Switch to a
    file-based page relative to the app's main script" and it takes a `page_path: str`,
    raising ValueError if that path does not point to a file. There is no callable form.

`views/state.py` holds what the two pages share, because this module calls
`st.navigation(...).run()` at import and so cannot be imported by a page.

No decisions live in `views/`. What is said beside an answer is `src/notices.py`, which
conversation is open and when a turn is saved is `src/conversations.py`, and what the
panel aggregates is `Store.telemetry()` and `src/telemetry.py`. All four are asserted
without a browser.
"""

from __future__ import annotations

import streamlit as st

# Must precede every other Streamlit call, including any made while a page is imported.
st.set_page_config(page_title="Conversational Data Analyst", layout="wide")

st.navigation(
    [
        st.Page("views/chat.py", title="Chat", default=True),
        st.Page("views/observability.py", title="Observability"),
    ]
).run()
