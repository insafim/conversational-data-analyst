"""The sidebar brand lockup. Pure unit tests, no database and no browser.

`brand_lockup` is a string builder, so pinning it is almost free. It is pinned because
the one thing it gets right is the thing a browser-less suite cannot see, and the thing a
later reader is most likely to undo while tidying.

The wordmark is joined with `<br>` rather than a newline. That looks like a stylistic
choice and is not one: `st.header("Conversational\\nData Analyst")` on streamlit 1.61.1
renders an `h2` whose `innerText` is "Conversational", the second line dropped rather
than wrapped, and the markdown hard-break form drops it too. Measured in a headless
browser on 2026-08-17, alongside the fact that makes it dangerous: `AppTest` reports that
element's `value` as the full string, newline intact. A suite asserting on `value` stays
green while the application shows half its own name.

So these are deliberately NOT `AppTest` tests. Driving `st.markdown` through `AppTest`
would assert on the source string handed to it, which is the same string this module
asserts on directly, while adding a coupling to sidebar element ordering and an
appearance of browser coverage that does not exist. `AppTest` also cannot see
`unsafe_allow_html` at all, so the last test here reads the call site out of
`views/brand.py`'s caller rather than pretending a rendering test could reach it.

What is left uncovered is stated rather than implied, and it is now a short list: whether
the flex row survives a resize, and whether the glyphs and strokes land where the
geometry says. Both are browser facts, confirmed in a browser once, with the measurements
recorded in `views/brand.py`. Everything else that can be checked without one is checked
here, including that the mark is well-formed markup, which substring counting cannot
establish.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from views.brand import BRAND_MARK, brand_lockup

SVG_NS = "http://www.w3.org/2000/svg"
PRIMARY = "#FF4B4B"
SIDEBAR_BACKGROUND = "#F0F2F6"
CHAT_PAGE = Path(__file__).resolve().parent.parent / "views" / "chat.py"


def test_the_wordmark_breaks_with_a_tag_and_never_a_newline() -> None:
    """The regression this file exists for.

    A newline here renders as "Conversational" alone in a browser while every assertion
    on `AppTest`'s element value continues to pass.
    """
    html = brand_lockup()

    assert "Conversational<br>Data Analyst" in html
    assert "\n" not in html, "a newline in the lockup truncates the wordmark in a browser"


def test_the_mark_is_embedded_exactly_once() -> None:
    """Neither dropped nor duplicated.

    Dropped leaves a wordmark with no mark and a flex row with one child, which still
    renders and still reads as deliberate. Duplicated is a second bubble at 64px. Both
    survive every other check in this suite.
    """
    assert brand_lockup().count(BRAND_MARK) == 1


def test_the_mark_is_well_formed_markup_and_every_path_carries_geometry() -> None:
    """The mark PARSES, which counting substrings cannot establish.

    The string is assembled from adjacent Python literals with comments between them, so
    the realistic corruption is a lost quote or a truncated attribute. An earlier version
    of this test counted `<path` and `#FF4B4B` occurrences and claimed to catch exactly
    that; it could not, because dropping the closing quote of a `d` attribute leaves both
    counts untouched. Parsing does catch it, and it does not care how the artwork is
    arranged, so a redraw that merges or adds a path is free.
    """
    root = ElementTree.fromstring(BRAND_MARK)  # noqa: S314 - a literal in this repo

    assert root.tag == f"{{{SVG_NS}}}svg"
    assert root.get("viewBox"), "without a viewBox the mark does not scale"
    paths = root.iter(f"{{{SVG_NS}}}path")
    geometry = [path.get("d") for path in paths]
    assert geometry, "a mark with no path draws nothing"
    assert all(d and d.strip() for d in geometry), "a path with an empty `d` paints nothing"


def test_the_mark_is_stroked_and_names_no_background_colour() -> None:
    """The property that keeps the mark theme-independent.

    The artwork is outline, so its counters are holes and show whatever is behind them.
    The moment somebody fills them to "tidy up", the mark carries a hardcoded page colour
    and turns into pale blocks on any theme but this one.

    Read from the parsed attributes rather than the source text, so that lowercasing the
    hex or changing the quoting does not fail a test about colour.
    """
    root = ElementTree.fromstring(BRAND_MARK)  # noqa: S314 - a literal in this repo
    group = root.find(f"{{{SVG_NS}}}g")

    assert group is not None, "the stroked artwork is grouped, so the group must exist"
    assert group.get("fill") == "none", "a filled mark needs its counters painted"
    assert (group.get("stroke") or "").upper() == PRIMARY

    painted = {
        element.get("fill") or element.get("stroke") or "" for element in root.iter()
    }
    assert not any(
        colour.upper() == SIDEBAR_BACKGROUND for colour in painted
    ), "the page background must not be painted into the mark"


def test_the_lockup_is_one_element_with_no_interpolation_point() -> None:
    """The security posture `unsafe_allow_html` depends on.

    The call site passes this string with `unsafe_allow_html=True`, which is only safe
    because nothing reaching it comes from the user, the database or the model. That
    holds today because the function takes no arguments, and this test fails the moment
    somebody gives it one.
    """
    from inspect import signature

    assert signature(brand_lockup).parameters == {}


def test_the_chat_page_renders_the_lockup_as_html_and_keeps_no_second_title() -> None:
    """The wiring, which no rendering test in this suite can reach.

    `AppTest` cannot see `unsafe_allow_html`: `streamlit.testing.v1.element_tree.Markdown`
    exposes `value` and `is_caption` and nothing else, so the flag is invisible to it and
    the element's value is the same string either way. Drop the flag and the sidebar
    displays the raw `<svg ...>` source as visible text, with the whole suite still green.
    Only a browser or the source itself can tell, and the source is cheaper and always
    available. `tests/test_repo_hygiene.py` sets the precedent for asserting on repository
    state rather than on behaviour when behaviour is out of reach.

    The second assertion pins the REMOVAL. A partial revert that restores the old
    `st.header` while leaving the lockup in place renders the name twice, and every other
    test here passes, because each one only ever looks at `views/brand.py`.

    Whitespace-insensitive, so reformatting the call does not fail the test.
    """
    source = CHAT_PAGE.read_text(encoding="utf-8")

    call = re.compile(
        r"st\.markdown\(\s*brand_lockup\(\)\s*,\s*unsafe_allow_html\s*=\s*True\s*,?\s*\)"
    )
    assert call.search(source), (
        "views/chat.py must render brand_lockup() with unsafe_allow_html=True, "
        "or the sidebar shows raw SVG source as text"
    )

    header = re.compile(r"st\.header\(\s*[\"']Conversational Data Analyst[\"']")
    assert not header.search(source), (
        "the sidebar header was replaced by the lockup; both together render the name twice"
    )
