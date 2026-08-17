"""The sidebar brand lockup: mark, wordmark, and the measurements behind both.

This lives in its own module and not in `views/chat.py`, where it was written, for the
reason `views/state.py` already records: `views/chat.py` is a page, so importing it runs
its module-level script body and poisons Streamlit's delta-generator context for the rest
of the process. Every later `AppTest` render of the chat page then reads its `New chat`
button as sitting inside the rename `st.form`, and nine of the page's cases fail.

Measured on 2026-08-17, reproducing the 2026-08-12 finding in `views/state.py`: a test
module doing `from views.chat import brand_lockup` at module scope passed on its own and
took nine `tests/test_app_smoke.py` cases down with it when the suite ran together. The
suite is the only thing that catches this, because the failure is an interaction and
every file passes alone.

This module renders nothing on import, so a test can reach the lockup without paying for
a page run. It is also the module the Observability page would import if the branding is
ever extended there, which is a live question: that page still opens on a plain
`st.header("Observability")` as of this writing.
"""

from __future__ import annotations

# The sidebar mark: a database in a speech bubble, traced rather than loaded.
#
# The supplied logo is `docs/logo.png`, a 1536x1024 neon render. Two versions of it were
# supplied and neither can be scaled down, because in both the bubble and its glow are the
# same red and there is no edge anywhere for a threshold to find.
#
#     Source: `docs/logo.png`, sampled with Pillow - Verified: 2026-08-17. Body pixel
#     (y=500, x=900) is (249, 58, 60) against glow pixel (y=150, x=500) at (232, 55, 58).
#     Taking luminance as the unweighted mean of R, G and B on the 0-255 scale, the
#     largest absolute difference between horizontally adjacent pixels anywhere on row
#     y=500 is 7.3, where a drawn edge on this scale exceeds 60. Reproduce with
#     `numpy.abs(numpy.diff(numpy.asarray(img.convert("RGB")).mean(axis=2)[500])).max()`.
#     Scaled to the ~64px a sidebar lockup affords, it resolves to a blob.
#
# So the artwork is traced instead. The geometry below is measured off that file rather
# than invented, which is why the numbers are not round:
#
#     Source: `docs/logo.png` - Verified: 2026-08-17. The interior line work sits a few
#     levels below the body and is invisible until stretched, so the green channel was
#     rescaled to its own 2nd and 98th percentiles over the bubble, which resolves the
#     cylinder and the lettering; the shapes were then read off the stretched image and
#     divided by the bubble's own width to make them scale-free. Normalised to a bubble
#     60 units wide: body 60x43.4 with a 10.2 corner, stroke 3, tail tip at (12.9, 58.6).
#     Database cylinder centred at x=20.2, radius 10.4, running y=15.5 to y=36.9 in three
#     sections. Right-hand block from x=35.4 to x=56.4, with a rule at y=16.2, "SQL"
#     seated on y=29.6, a rule at y=34.3 and a short rule with a full stop at y=39.8.
#
# Both citations name a file that does NOT ship, and no rewording fixes that: the source
# artwork is excluded by the `docs/` allowlist in .gitignore. They are written to be
# reproducible by whoever holds the file, which is the most that is available here. What
# a reader without it CAN check is that the figures above match the path data below.
#
# Drawn as strokes on transparent rather than as a filled silhouette, which is both what
# the source does and the reason no background colour is named anywhere here. A filled
# mark would need its counters painted in the page colour, hardcoding the light theme; an
# outline leaves them as holes that are whatever is behind them.
#
# The stroke is Streamlit's default primary, which is also the `New chat` button and the
# avatar on every question the user has asked, so the mark belongs to the page rather
# than sitting on it:
#
#     Source: computed styles read from the running app - Verified: 2026-08-17. The
#     `New chat` button's `background-color` and the user `stChatMessageAvatar`'s
#     `background-color` both resolve to `rgb(255, 75, 75)`. The message BODY is not
#     primary-coloured, it is `rgba(240, 242, 246, 0.5)`, and the assistant's avatar is
#     `rgb(255, 164, 33)`; only the two surfaces named above match the mark.
#
# It is written out rather than read from the theme because the theme does not answer:
#
#     Source: `st.get_option('theme.primaryColor')` on streamlit 1.61.1 returns None,
#     and `st.get_option('theme.base')` returns None too - Verified: 2026-08-17. There is
#     no `.streamlit/config.toml` in this repository, and the option is unset until one
#     exists, so reading the theme would yield None rather than the red actually rendered.
#     The rendered value was read back from the running app: the `New chat` button
#     computes to `rgb(255, 75, 75)`.
#
# "SQL" is live text rather than outlined letterforms, so it is one attribute to edit
# rather than a glyph to redraw. It names a concrete family with a generic fallback: the
# word is four characters of geometry at 11px and a substituted face changes its width,
# not its legibility.
BRAND_MARK: str = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 62" width="64" height="62" '
    'aria-hidden="true" style="flex:0 0 auto;">'
    '<g fill="none" stroke="#FF4B4B" stroke-width="3" stroke-linecap="round" '
    'stroke-linejoin="round">'
    # The bubble, drawn clockwise from the top-left corner, with the tail hanging off the
    # bottom edge and closing back into the bottom-left corner.
    '<path d="M12.2 2H51.8A10.2 10.2 0 0 1 62 12.2V35.2A10.2 10.2 0 0 1 51.8 45.4H24.2'
    "L12.9 58.6A0.9 0.9 0 0 1 11.4 58V45.4A10.2 10.2 0 0 1 2 35.2V12.2"
    'A10.2 10.2 0 0 1 12.2 2Z"/>'
    # The cylinder: a full ellipse for the top face, then the two walls closed by the
    # bottom face, then the two bands that divide it into three sections.
    '<path d="M9.8 15.5a10.4 3.6 0 1 0 20.8 0a10.4 3.6 0 1 0 -20.8 0Z'
    "M9.8 15.5V36.9A10.4 3.6 0 0 0 30.6 36.9V15.5"
    "M9.8 22.5A10.4 3.6 0 0 0 30.6 22.5"
    'M9.8 29.2A10.4 3.6 0 0 0 30.6 29.2"/>'
    '<path d="M35.4 16.2H56.4M35.4 34.3H56.4M35.4 39.8H48.4"/>'
    "</g>"
    '<circle cx="54.9" cy="39.8" r="1.7" fill="#FF4B4B"/>'
    '<text x="35.4" y="29.6" font-family="Helvetica,Arial,sans-serif" font-size="11.4" '
    'font-weight="700" letter-spacing="-0.6" fill="#FF4B4B">SQL</text>'
    "</svg>"
)


def brand_lockup() -> str:
    """The sidebar's mark and wordmark as one HTML block.

    Built as a single element rather than `st.columns` around an image, because Streamlit
    columns divide the sidebar PROPORTIONALLY and the sidebar is resizable, so a column
    split cannot hold a fixed-size mark. A flex row with a fixed mark width can.

        Source: measured in a headless browser against this app on streamlit 1.61.1 -
        Verified: 2026-08-17. With `st.columns([1, 2])` and `st.image`, the rendered
        `img` box was 90x60 at the default 300px sidebar and 215x168 at 710px. The
        shipped flex version measures 64x62 at both widths.

    The name is broken after "Conversational" explicitly, for the same reason: left to
    wrap on its own the break lands wherever the operator dragged the edge, and at 710px
    there is no break at all.

    The block is HTML because Streamlit offers no way to sit an image beside a heading,
    and because a heading cannot carry the second line at all:

        Source: `st.header("Conversational\\nData Analyst")` on streamlit 1.61.1 -
        Verified: 2026-08-17. The rendered `h2` has `innerText` of "Conversational".
        Everything after the newline is DROPPED, not wrapped, and the markdown hard-break
        form ("  \\n") drops it too. `AppTest` disguises this: the element's `value` still
        holds the full string including the newline, so only the browser shows the loss.

    The row aligns on `flex-start` and the heading is pulled up 3px, so that the cap of
    the "C" meets the top of the bubble rather than the top of the heading's line box.
    Centring the row instead, which was the first arrangement, dropped the name 5px and
    read as a caption hung off the mark.

        Source: measured against this app in a headless browser -
        Verified: 2026-08-17. Procedure, so a re-measurement repeats it rather than
        inventing one: screenshot the sidebar at `device_scale_factor=4`, take the
        background as the corner pixel, mark a pixel as ink where it differs from that
        background by more than 40 summed across RGB, then compare the first inked row in
        the mark's column band against the first inked row in the wordmark's. Computed
        styles cannot answer this, because the cap sits below the top of the heading's
        box by an amount only the glyph outline knows.

        Centred, the cap sat 5px low. On `flex-start` alone it sat 1.5px low, because a
        20px font on a 23px line box carries half-leading above the cap that the mark
        does not. -3px puts both first inked rows on css y 76.50, an exact match at both
        the 300px and 710px sidebar widths.

    That figure is tied to THIS mark and THIS heading, and it moved once already: the
    earlier filled mark was 56 units tall and wanted -2px, while the traced one is 62 and
    its stroke starts half a pixel from the viewBox edge. Change the artwork, the font
    size or the line height and it must be measured again, not adjusted by eye. Note also
    that Streamlit's file watcher does not pick up a NEW module, so the server has to be
    restarted before any such measurement is taken; measuring through a stale process
    reports the previous value and looks like the change had no effect.

    The caller passes this with `unsafe_allow_html=True`. That is the price and it buys
    no exposure here: every byte of this string is a literal in this file, with nothing
    interpolated and nothing read from the user, the database or the model.
    """
    return (
        '<div style="display:flex;align-items:flex-start;gap:0.75rem;'
        'margin-bottom:0.5rem;">'
        f"{BRAND_MARK}"
        '<h2 style="line-height:1.15;margin:-3px 0 0;padding:0;">'
        "Conversational<br>Data Analyst</h2>"
        "</div>"
    )
